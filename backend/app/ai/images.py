import io

from PIL import Image, ImageOps, UnidentifiedImageError

from .providers import ProviderUnavailable


MAX_VISION_SIDE = 1024
MAX_VISION_BYTES = 128 * 1024


def prepare_vision_image(data: bytes) -> bytes:
    """Create a bounded inference copy; never replace the stored original."""
    try:
        with Image.open(io.BytesIO(data)) as original:
            image = ImageOps.exif_transpose(original).convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        image = Image.alpha_composite(background, image).convert("RGB")
        image.thumbnail((MAX_VISION_SIDE, MAX_VISION_SIDE), Image.Resampling.LANCZOS)
        while True:
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
            if output.tell() <= MAX_VISION_BYTES:
                return output.getvalue()
            if max(image.size) <= 256:
                break
            side = max(256, int(max(image.size) * 0.75))
            image.thumbnail((side, side), Image.Resampling.LANCZOS)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ProviderUnavailable(
            "The image could not be prepared for AI. Upload a valid still image and retry.",
            code="ai_image_invalid",
        ) from None
    raise ProviderUnavailable(
        "The image exceeds the AI request limit. Upload a smaller image and retry.",
        code="ai_image_invalid",
    )
