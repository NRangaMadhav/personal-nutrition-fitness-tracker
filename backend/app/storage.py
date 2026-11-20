import io
import warnings
from functools import lru_cache
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import AIInput, Meal, StoredImage, User
from .security import get_current_user


router = APIRouter(tags=["images"])
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
IMAGE_TYPES = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}


class StorageUnavailable(RuntimeError):
    pass


@lru_cache
def storage_client():
    settings = get_settings()
    if not settings.object_storage_access_key or not settings.object_storage_secret_key:
        raise StorageUnavailable("Object storage credentials are not configured.")
    return boto3.client(
        "s3",
        endpoint_url=settings.object_storage_endpoint,
        aws_access_key_id=settings.object_storage_access_key,
        aws_secret_access_key=settings.object_storage_secret_key,
        region_name="us-east-1",
        config=Config(
            connect_timeout=3,
            read_timeout=10,
            retries={"mode": "standard", "total_max_attempts": 2},
            s3={"addressing_style": "path"},
        ),
    )


def init_storage():
    """Explicit initialization: creating an S3 bucket never grants public access."""
    client = storage_client()
    bucket = get_settings().object_storage_bucket
    try:
        try:
            client.head_bucket(Bucket=bucket)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) not in ("404", "NoSuchBucket", "NotFound"):
                raise
            try:
                client.create_bucket(Bucket=bucket)
            except ClientError as create_error:
                if create_error.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
                    raise
        # A dedicated application bucket must remain private, including on restart.
        client.delete_bucket_policy(Bucket=bucket)
    except (BotoCoreError, ClientError):
        raise StorageUnavailable("Private image storage is unavailable. Check the S3 service and bucket permissions.") from None


def validate_image(data: bytes) -> tuple[str, str]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Upload a nonempty JPEG, PNG, or WebP image of at most 8 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = image.format
                if image_format not in IMAGE_TYPES:
                    raise HTTPException(415, "Only JPEG, PNG, and WebP images are supported.")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(413, "Images must contain at most 20 megapixels.")
                if getattr(image, "is_animated", False):
                    raise HTTPException(415, "Upload a still image, not an animation.")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(413, "Images must contain at most 20 megapixels.") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise HTTPException(415, "The upload is not a valid JPEG, PNG, or WebP image.") from None
    return IMAGE_TYPES[image_format]


def put_image(key: str, data: bytes, content_type: str):
    try:
        storage_client().put_object(
            Bucket=get_settings().object_storage_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
    except (BotoCoreError, ClientError):
        raise StorageUnavailable("Image upload failed. Check that object storage is available.") from None


def delete_object(key: str):
    try:
        storage_client().delete_object(Bucket=get_settings().object_storage_bucket, Key=key)
    except (BotoCoreError, ClientError):
        raise StorageUnavailable("Image deletion failed. Please retry when object storage is available.") from None


def open_image(image: StoredImage):
    try:
        return storage_client().get_object(
            Bucket=get_settings().object_storage_bucket, Key=image.object_key
        )["Body"]
    except (BotoCoreError, ClientError):
        raise StorageUnavailable("The original image is currently unavailable in object storage.") from None


def get_image_bytes(image: StoredImage) -> bytes:
    body = open_image(image)
    try:
        data = body.read(MAX_IMAGE_BYTES + 1)
    except (BotoCoreError, OSError):
        raise StorageUnavailable("The original image could not be read from object storage.") from None
    finally:
        body.close()
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise StorageUnavailable("The stored image is empty or exceeds the supported size.")
    return data


def owned_image(db: Session, user_id: int, image_id: int, *, lock: bool = False) -> StoredImage:
    query = select(StoredImage).where(StoredImage.id == image_id, StoredImage.user_id == user_id)
    image = db.scalar(query.with_for_update() if lock else query)
    if image is None:
        raise HTTPException(404, "Image not found.")
    return image


@router.post("/images", status_code=201)
def upload_image(file: UploadFile, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        data = file.file.read(MAX_IMAGE_BYTES + 1)
    finally:
        file.file.close()
    content_type, extension = validate_image(data)
    key = f"users/{user.id}/{uuid4().hex}.{extension}"
    try:
        init_storage()
        put_image(key, data, content_type)
    except StorageUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    image = StoredImage(user_id=user.id, object_key=key, content_type=content_type)
    try:
        db.add(image)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        try:
            delete_object(key)
        except StorageUnavailable:
            raise HTTPException(503, "Image metadata could not be saved; object cleanup also failed.") from None
        raise HTTPException(503, "Image metadata could not be saved. Please retry.") from None
    return {"id": image.id}


@router.get("/images/{image_id}")
def download_image(image_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    image = owned_image(db, user.id, image_id)
    try:
        body = open_image(image)
    except StorageUnavailable as exc:
        raise HTTPException(503, str(exc)) from None

    def chunks():
        try:
            while chunk := body.read(64 * 1024):
                yield chunk
        finally:
            body.close()

    return StreamingResponse(
        chunks(),
        media_type=image.content_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/images/{image_id}")
def remove_image(image_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    image = owned_image(db, user.id, image_id, lock=True)
    attached_meal = db.scalar(select(Meal.id).where(Meal.image_id == image.id).limit(1))
    attached_draft = db.scalar(
        select(AIInput.id).where(AIInput.image_id == image.id, AIInput.status == "draft").limit(1)
    )
    if attached_meal is not None or attached_draft is not None:
        raise HTTPException(409, "This image is attached to a meal or an active AI draft.")
    try:
        delete_object(image.object_key)
    except StorageUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    for previous in db.scalars(
        select(AIInput).where(AIInput.image_id == image.id, AIInput.user_id == user.id)
    ):
        previous.image_id = None
    db.delete(image)
    db.commit()
    return {"status": "deleted", "id": image_id}
