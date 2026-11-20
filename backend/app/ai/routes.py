from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AIInput, Meal, MealItem, Supplement, SupplementLog, User, WaterLog, WeightRecord
from ..security import get_current_user
from ..storage import StorageUnavailable, get_image_bytes, owned_image
from ..tracking import user_zone
from .providers import ProviderUnavailable
from .schemas import (
    CoachRequest,
    ConfirmRequest,
    LogRequest,
    MealEvent,
    SupplementEvent,
    WaterEvent,
    WeightEvent,
    missing_fields,
)
from .workflows import run_coach, run_extraction


class ValidatedAIRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                # Do not echo prompts or non-JSON floats (NaN/Infinity) in errors.
                errors = [
                    {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
                return JSONResponse(status_code=422, content={"detail": errors})

        return handle


router = APIRouter(tags=["AI"], route_class=ValidatedAIRoute)


def draft_data(draft: AIInput) -> dict:
    return {
        "id": draft.id,
        "status": draft.status,
        "provider": draft.provider,
        "model": draft.model,
        "events": draft.response_data.get("events", []),
        "warnings": draft.response_data.get("warnings", []),
        "image_id": draft.image_id,
    }


def owned_draft(db: Session, user_id: int, draft_id: int, *, lock=False):
    query = select(AIInput).where(AIInput.id == draft_id, AIInput.user_id == user_id)
    draft = db.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
    if draft is None:
        raise HTTPException(404, "AI draft not found.")
    return draft


@router.post("/ai-log", status_code=201)
def create_draft(request: LogRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    image = owned_image(db, user.id, request.image_id) if request.image_id is not None else None
    try:
        image_bytes = get_image_bytes(image) if image is not None else None
        state = run_extraction(
            request,
            str(user_zone(db, user.id)),
            image_bytes=image_bytes,
            image_content_type=image.content_type if image is not None else None,
        )
    except ProviderUnavailable as exc:
        raise HTTPException(503, {"code": exc.code, "message": str(exc)}) from None
    except StorageUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    # Recheck under lock after inference: an unused image may have been deleted while the model ran.
    if image is not None:
        owned_image(db, user.id, image.id, lock=True)
    result = state["result"]
    draft = AIInput(
        user_id=user.id,
        input_text=request.text,
        image_id=request.image_id,
        provider=result.provider,
        model=result.model,
        response_data=state["extraction"].model_dump(mode="json"),
        status="draft",
    )
    db.add(draft)
    db.commit()
    return draft_data(draft)


@router.get("/ai-log")
def list_drafts(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    drafts = db.scalars(
        select(AIInput).where(AIInput.user_id == user.id, AIInput.status == "draft")
        .order_by(AIInput.created_at.desc(), AIInput.id.desc()).limit(limit)
    )
    return [draft_data(draft) for draft in drafts]


@router.post("/ai-log/confirm")
def confirm_draft(request: ConfirmRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    draft = owned_draft(db, user.id, request.draft_id, lock=True)
    if draft.status != "draft":
        raise HTTPException(409, f"This draft is already {draft.status}; it cannot be confirmed again.")
    missing = missing_fields(request.events)
    if missing:
        raise HTTPException(422, {"message": "Correct all unknown values before confirming.", "fields": missing})
    if draft.image_id is not None:
        owned_image(db, user.id, draft.image_id, lock=True)
    zone = user_zone(db, user.id)
    # Serialize this user's supplement lookup/create/upsert across simultaneous confirmations.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    created = []
    try:
        for event in request.events:
            occurred_at = event.occurred_at.astimezone(timezone.utc)
            if isinstance(event, MealEvent):
                record = Meal(
                    user_id=user.id, meal_type=event.meal_type, logged_at=occurred_at,
                    image_id=draft.image_id,
                    items=[MealItem(**item.model_dump()) for item in event.items],
                )
            elif isinstance(event, WaterEvent):
                record = WaterLog(user_id=user.id, amount_ml=event.amount_ml, logged_at=occurred_at)
            elif isinstance(event, WeightEvent):
                record = WeightRecord(user_id=user.id, weight_kg=event.weight_kg, recorded_at=occurred_at)
            elif isinstance(event, SupplementEvent):
                day = event.occurred_at.astimezone(zone).date()
                supplement = db.scalar(
                    select(Supplement).where(
                        Supplement.user_id == user.id, func.lower(Supplement.name) == event.name.lower()
                    ).order_by(Supplement.id).limit(1)
                )
                if supplement is None:
                    supplement = Supplement(
                        user_id=user.id, name=event.name, dosage=1, unit="dose", active=True,
                        active_periods=[{"start": day.isoformat(), "end": None}],
                    )
                    db.add(supplement)
                    db.flush()
                record = db.scalar(
                    select(SupplementLog).where(
                        SupplementLog.user_id == user.id, SupplementLog.supplement_id == supplement.id,
                        SupplementLog.log_date == day,
                    ).with_for_update()
                )
                if record is None:
                    record = SupplementLog(
                        user_id=user.id, supplement_id=supplement.id, log_date=day, taken=event.taken
                    )
                else:
                    record.taken = event.taken
            db.add(record)
            db.flush()
            created.append({"type": event.type, "id": record.id})
        response = {"status": "confirmed", "draft_id": draft.id, "records": created}
        draft.status = "confirmed"
        draft.response_data = {
            "events": [event.model_dump(mode="json") for event in request.events],
            "warnings": draft.response_data.get("warnings", []),
            "confirmation": response,
        }
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Confirmation conflicted with another update; no events were saved. Please retry.") from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503, "Confirmation could not be saved; no events were saved. Please retry.") from None
    return response


@router.get("/ai-log/{draft_id}")
def get_draft(draft_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return draft_data(owned_draft(db, user.id, draft_id))


@router.post("/ai-log/{draft_id}/discard")
def discard_draft(draft_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    draft = owned_draft(db, user.id, draft_id, lock=True)
    if draft.status != "draft":
        raise HTTPException(409, f"This draft is already {draft.status}.")
    draft.status = "discarded"
    db.commit()
    return {"status": "discarded", "draft_id": draft.id}


@router.post("/coach")
def coach(request: CoachRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        state = run_coach(db, user.id, request)
    except ProviderUnavailable as exc:
        raise HTTPException(503, {"code": exc.code, "message": str(exc)}) from None
    return {
        "answer": state["answer"],
        "facts": state["facts"],
        "provider": state["result"].provider,
        "model": state["result"].model,
    }
