from datetime import date as Date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .models import DailyTarget, Meal, MealItem, Profile, StoredImage, Supplement, SupplementLog, User, WaterLog, WeightRecord
from .schemas import Credentials, MealInput, ProfileInput, SupplementInput, SupplementLogInput, SupplementUpdate, TargetInput, WaterInput, WeightInput
from .security import create_access_token, dummy_password_hash, get_current_user, hash_password, verify_password
from .tracking import build_dashboard, build_history, get_profile, latest_weight, local_today, meal_data, profile_data, recommended_targets, supplement_data, supplement_log_data, target_data, target_for_day, water_data, weight_data


class ValidatedRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                # Exclude passwords and non-JSON floats from validation responses.
                errors = [
                    {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
                return JSONResponse(status_code=422, content={"detail": errors})

        return handle


router = APIRouter(route_class=ValidatedRoute)
Database = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
SelectedDate = Annotated[Date | None, Query(ge=Date(2, 1, 1), le=Date(9998, 12, 31))]


def owned(db: Session, model, identifier: int, user_id: int):
    if not 0 < identifier <= 2147483647:
        raise HTTPException(404, "Record not found.")
    record = db.scalar(select(model).where(model.id == identifier, model.user_id == user_id))
    if record is None:
        raise HTTPException(404, "Record not found.")
    return record


def token_response(user: User) -> dict:
    return {"access_token": create_access_token(user.id), "token_type": "bearer"}


@router.post("/auth/register", status_code=201)
def register(body: Credentials, db: Database):
    if db.scalar(select(User.id).where(User.email == body.email)) is not None:
        raise HTTPException(409, "An account with this email already exists.")
    user = User(email=body.email, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.scalar(select(User.id).where(User.email == body.email)) is not None:
            raise HTTPException(409, "An account with this email already exists.") from None
        raise
    return token_response(user)


@router.post("/auth/login")
def login(body: Credentials, db: Database):
    user = db.scalar(select(User).where(User.email == body.email))
    valid = verify_password(body.password, user.password_hash if user else dummy_password_hash)
    if not valid or user is None:
        raise HTTPException(401, "Incorrect email or password.", headers={"WWW-Authenticate": "Bearer"})
    return token_response(user)


@router.get("/auth/me")
def me(user: CurrentUser):
    return {"id": user.id, "email": user.email}


@router.get("/profile")
def read_profile(db: Database, user: CurrentUser):
    profile = get_profile(db, user.id)
    if profile is None:
        raise HTTPException(404, "Profile not found. Complete onboarding first.")
    return profile_data(db, profile)


@router.put("/profile")
def save_profile(body: ProfileInput, db: Database, user: CurrentUser):
    profile = get_profile(db, user.id)
    weight = latest_weight(db, user.id)
    if profile is None:
        profile = Profile(user_id=user.id)
        db.add(profile)
    for key, value in body.model_dump(exclude={"current_weight_kg"}).items():
        setattr(profile, key, value)
    if weight is None or weight.weight_kg != body.current_weight_kg:
        db.add(WeightRecord(user_id=user.id, weight_kg=body.current_weight_kg, recorded_at=datetime.now(timezone.utc)))
    db.commit()
    return profile_data(db, profile)


@router.get("/targets/recommend")
def recommend(db: Database, user: CurrentUser):
    return recommended_targets(db, user.id)


@router.get("/targets")
def read_target(db: Database, user: CurrentUser, date: SelectedDate = None):
    return target_data(target_for_day(db, user.id, date or local_today(db, user.id)))


@router.put("/targets")
def save_target(body: TargetInput, db: Database, user: CurrentUser):
    target = db.scalar(select(DailyTarget).where(
        DailyTarget.user_id == user.id, DailyTarget.effective_date == body.effective_date,
    ))
    if target is None:
        target = DailyTarget(user_id=user.id)
        db.add(target)
    for key, value in body.model_dump().items():
        setattr(target, key, value)
    db.commit()
    return target_data(target)


@router.post("/weight", status_code=201)
def add_weight(body: WeightInput, db: Database, user: CurrentUser):
    record = WeightRecord(user_id=user.id, **body.model_dump())
    db.add(record)
    db.commit()
    return weight_data(record)


@router.get("/weight")
def list_weight(db: Database, user: CurrentUser):
    records = db.scalars(select(WeightRecord).where(WeightRecord.user_id == user.id).order_by(
        WeightRecord.recorded_at.desc(), WeightRecord.id.desc(),
    )).all()
    return [weight_data(record) for record in records]


@router.delete("/weight/{identifier}", status_code=204)
def delete_weight(identifier: int, db: Database, user: CurrentUser):
    db.delete(owned(db, WeightRecord, identifier, user.id))
    db.commit()
    return Response(status_code=204)


@router.post("/water", status_code=201)
def add_water(body: WaterInput, db: Database, user: CurrentUser):
    log = WaterLog(user_id=user.id, **body.model_dump())
    db.add(log)
    db.commit()
    return water_data(log)


@router.get("/water")
def list_water(db: Database, user: CurrentUser):
    logs = db.scalars(select(WaterLog).where(WaterLog.user_id == user.id).order_by(
        WaterLog.logged_at.desc(), WaterLog.id.desc(),
    )).all()
    return [water_data(log) for log in logs]


@router.delete("/water/{identifier}", status_code=204)
def delete_water(identifier: int, db: Database, user: CurrentUser):
    db.delete(owned(db, WaterLog, identifier, user.id))
    db.commit()
    return Response(status_code=204)


@router.get("/supplements")
def list_supplements(db: Database, user: CurrentUser):
    supplements = db.scalars(select(Supplement).where(Supplement.user_id == user.id).order_by(Supplement.id)).all()
    return [supplement_data(supplement) for supplement in supplements]


@router.post("/supplements", status_code=201)
def add_supplement(body: SupplementInput, db: Database, user: CurrentUser):
    supplement = Supplement(
        user_id=user.id, **body.model_dump(), active=True,
        active_periods=[{"start": local_today(db, user.id).isoformat(), "end": None}],
    )
    db.add(supplement)
    db.commit()
    return supplement_data(supplement)


@router.put("/supplements/{identifier}")
def update_supplement(identifier: int, body: SupplementUpdate, db: Database, user: CurrentUser):
    supplement = owned(db, Supplement, identifier, user.id)
    if supplement.active != body.active:
        today = local_today(db, user.id).isoformat()
        periods = [dict(period) for period in supplement.active_periods]
        if body.active:
            # A same-day reactivation restores that day's single expectation.
            if periods and periods[-1]["end"] == today:
                periods[-1]["end"] = None
            else:
                periods.append({"start": today, "end": None})
        elif periods and periods[-1]["end"] is None:
            periods[-1]["end"] = today
        supplement.active_periods = periods
    for key, value in body.model_dump().items():
        setattr(supplement, key, value)
    db.commit()
    return supplement_data(supplement)


@router.post("/supplement-logs")
def save_supplement_log(body: SupplementLogInput, db: Database, user: CurrentUser):
    owned(db, Supplement, body.supplement_id, user.id)
    log = db.scalar(select(SupplementLog).where(
        SupplementLog.user_id == user.id, SupplementLog.supplement_id == body.supplement_id,
        SupplementLog.log_date == body.log_date,
    ))
    if log is None:
        log = SupplementLog(user_id=user.id, **body.model_dump())
        db.add(log)
    else:
        log.taken = body.taken
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        log = db.scalar(select(SupplementLog).where(
            SupplementLog.user_id == user.id, SupplementLog.supplement_id == body.supplement_id,
            SupplementLog.log_date == body.log_date,
        ))
        if log is None:
            raise
        log.taken = body.taken
        db.commit()
    return supplement_log_data(log)


@router.get("/supplement-logs")
def list_supplement_logs(db: Database, user: CurrentUser):
    logs = db.scalars(
        select(SupplementLog).join(Supplement, Supplement.id == SupplementLog.supplement_id).where(
            SupplementLog.user_id == user.id, Supplement.user_id == user.id,
        ).order_by(SupplementLog.log_date.desc(), SupplementLog.id.desc())
    ).all()
    return [supplement_log_data(log) for log in logs]


@router.delete("/supplement-logs/{identifier}", status_code=204)
def delete_supplement_log(identifier: int, db: Database, user: CurrentUser):
    log = owned(db, SupplementLog, identifier, user.id)
    owned(db, Supplement, log.supplement_id, user.id)
    db.delete(log)
    db.commit()
    return Response(status_code=204)


def validate_meal_image(body: MealInput, db: Session, user_id: int):
    if body.image_id is not None:
        owned(db, StoredImage, body.image_id, user_id)


@router.post("/meals", status_code=201)
def add_meal(body: MealInput, db: Database, user: CurrentUser):
    validate_meal_image(body, db, user.id)
    meal = Meal(
        user_id=user.id, **body.model_dump(exclude={"items"}),
        items=[MealItem(**item.model_dump()) for item in body.items],
    )
    db.add(meal)
    db.commit()
    return meal_data(meal)


@router.get("/meals")
def list_meals(db: Database, user: CurrentUser):
    meals = db.scalars(select(Meal).where(Meal.user_id == user.id).order_by(Meal.logged_at.desc(), Meal.id.desc())).all()
    return [meal_data(meal) for meal in meals]


@router.get("/meals/{identifier}")
def read_meal(identifier: int, db: Database, user: CurrentUser):
    return meal_data(owned(db, Meal, identifier, user.id))


@router.put("/meals/{identifier}")
def update_meal(identifier: int, body: MealInput, db: Database, user: CurrentUser):
    meal = owned(db, Meal, identifier, user.id)
    validate_meal_image(body, db, user.id)
    for key, value in body.model_dump(exclude={"items"}).items():
        setattr(meal, key, value)
    meal.items = [MealItem(**item.model_dump()) for item in body.items]
    db.commit()
    return meal_data(meal)


@router.delete("/meals/{identifier}", status_code=204)
def delete_meal(identifier: int, db: Database, user: CurrentUser):
    db.delete(owned(db, Meal, identifier, user.id))
    db.commit()
    return Response(status_code=204)


@router.get("/dashboard")
@router.get("/history/day")
def dashboard(db: Database, user: CurrentUser, date: SelectedDate = None):
    return build_dashboard(db, user.id, date or local_today(db, user.id))


@router.get("/history/week")
def weekly_history(db: Database, user: CurrentUser, date: SelectedDate = None):
    return build_history(db, user.id, date or local_today(db, user.id), "week")


@router.get("/history/month")
def monthly_history(db: Database, user: CurrentUser, date: SelectedDate = None):
    return build_history(db, user.id, date or local_today(db, user.id), "month")
