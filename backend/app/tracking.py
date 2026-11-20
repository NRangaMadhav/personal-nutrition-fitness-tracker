import calendar
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import DailyTarget, Meal, Profile, Supplement, SupplementLog, WaterLog, WeightRecord


NUTRIENTS = ("calories", "protein", "carbs", "fat", "fiber")
DEFAULT_TIMEZONE = "Asia/Kolkata"


def get_profile(db: Session, user_id: int) -> Profile | None:
    return db.scalar(select(Profile).where(Profile.user_id == user_id))


def user_zone(db: Session, user_id: int) -> ZoneInfo:
    profile = get_profile(db, user_id)
    return ZoneInfo(profile.timezone if profile else DEFAULT_TIMEZONE)


def local_today(db: Session, user_id: int) -> date:
    return datetime.now(user_zone(db, user_id)).date()


def day_bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    return start, end


def latest_weight(db: Session, user_id: int, before: datetime | None = None) -> WeightRecord | None:
    query = select(WeightRecord).where(
        WeightRecord.user_id == user_id,
        WeightRecord.recorded_at <= datetime.now(timezone.utc),
    )
    if before is not None:
        query = query.where(WeightRecord.recorded_at < before)
    return db.scalar(query.order_by(WeightRecord.recorded_at.desc(), WeightRecord.id.desc()).limit(1))


def profile_data(db: Session, profile: Profile) -> dict:
    weight = latest_weight(db, profile.user_id)
    return {
        "date_of_birth": profile.date_of_birth,
        "sex": profile.sex,
        "height_cm": profile.height_cm,
        "current_weight_kg": weight.weight_kg if weight else None,
        "goal_weight_kg": profile.goal_weight_kg,
        "timezone": profile.timezone,
    }


def target_data(target: DailyTarget | None) -> dict | None:
    if target is None:
        return None
    return {
        "id": target.id,
        "effective_date": target.effective_date,
        "calories_target": target.calories_target,
        "protein_target": target.protein_target,
        "water_target_ml": target.water_target_ml,
    }


def target_for_day(db: Session, user_id: int, day: date) -> DailyTarget | None:
    return db.scalar(
        select(DailyTarget).where(
            DailyTarget.user_id == user_id, DailyTarget.effective_date <= day,
        ).order_by(DailyTarget.effective_date.desc()).limit(1)
    )


def recommended_targets(db: Session, user_id: int) -> dict:
    profile = get_profile(db, user_id)
    if profile is None:
        raise HTTPException(404, "Complete your profile before requesting target recommendations.")
    today = local_today(db, user_id)
    age = today.year - profile.date_of_birth.year - (
        (today.month, today.day) < (profile.date_of_birth.month, profile.date_of_birth.day)
    )
    if age < 18:
        raise HTTPException(422, "Automatic targets are for adults aged 18 or older. Set manual targets with professional guidance.")
    if profile.sex == "unspecified":
        raise HTTPException(422, "The Mifflin–St Jeor calorie formula requires male or female sex. Please set manual targets.")
    weight = latest_weight(db, user_id)
    if weight is None:
        raise HTTPException(422, "Add a current weight before requesting target recommendations, or set manual targets.")
    bmr = 10 * weight.weight_kg + 6.25 * profile.height_cm - 5 * age + (5 if profile.sex == "male" else -161)
    if bmr <= 0:
        raise HTTPException(422, "This profile is outside the formula's usable range. Please set manual targets.")
    return {
        "calories_target": round(bmr * 1.2),
        "protein_target": round(weight.weight_kg * 1.2, 1),
        "water_target_ml": round(weight.weight_kg * 30),
        "explanation": (
            f"Adult estimate (age {age}): Mifflin–St Jeor basal metabolic rate "
            f"(10 × weight kg + 6.25 × height cm − 5 × age {'+ 5' if profile.sex == 'male' else '− 161'}) "
            "× sedentary activity factor 1.2 for maintenance calories. "
            "Protein: 1.2 g/kg; water: 30 ml/kg. No automatic weight-loss deficit "
            "or weight-gain surplus is applied. These are general estimates, not medical advice; "
            "review them and save or enter your own targets."
        ),
    }


def weight_data(record: WeightRecord) -> dict:
    return {"id": record.id, "weight_kg": record.weight_kg, "recorded_at": record.recorded_at.astimezone(timezone.utc)}


def water_data(log: WaterLog) -> dict:
    return {"id": log.id, "amount_ml": log.amount_ml, "logged_at": log.logged_at.astimezone(timezone.utc)}


def supplement_data(supplement: Supplement) -> dict:
    return {
        "id": supplement.id, "name": supplement.name, "dosage": supplement.dosage,
        "unit": supplement.unit, "active": supplement.active,
        "active_periods": supplement.active_periods,
    }


def supplement_log_data(log: SupplementLog) -> dict:
    return {"id": log.id, "supplement_id": log.supplement_id, "log_date": log.log_date, "taken": log.taken}


def meal_data(meal: Meal) -> dict:
    items = [
        {
            "id": item.id, "food_name": item.food_name, "quantity": item.quantity,
            "unit": item.unit, **{key: getattr(item, key) for key in NUTRIENTS},
            "confidence": item.confidence,
        }
        for item in sorted(meal.items, key=lambda item: item.id)
    ]
    return {
        "id": meal.id, "meal_type": meal.meal_type, "logged_at": meal.logged_at.astimezone(timezone.utc),
        "image_id": meal.image_id, "items": items,
        "totals": {key: round(sum(item[key] for item in items), 2) for key in NUTRIENTS},
    }


def supplement_eligible(supplement: Supplement, day: date) -> bool:
    key = day.isoformat()
    return any(
        period["start"] <= key and (period["end"] is None or key < period["end"])
        for period in supplement.active_periods
    )


def build_dashboard(db: Session, user_id: int, day: date) -> dict:
    profile = get_profile(db, user_id)
    zone = ZoneInfo(profile.timezone if profile else DEFAULT_TIMEZONE)
    start, end = day_bounds(day, zone)
    weight = latest_weight(db, user_id, end)
    meals = db.scalars(
        select(Meal).options(selectinload(Meal.items)).where(
            Meal.user_id == user_id, Meal.logged_at >= start, Meal.logged_at < end,
        ).order_by(Meal.logged_at, Meal.id)
    ).all()
    water_logs = db.scalars(
        select(WaterLog).where(
            WaterLog.user_id == user_id, WaterLog.logged_at >= start, WaterLog.logged_at < end,
        ).order_by(WaterLog.logged_at, WaterLog.id)
    ).all()
    weights = db.scalars(
        select(WeightRecord).where(
            WeightRecord.user_id == user_id,
            WeightRecord.recorded_at >= start, WeightRecord.recorded_at < end,
            WeightRecord.recorded_at <= datetime.now(timezone.utc),
        ).order_by(WeightRecord.recorded_at, WeightRecord.id)
    ).all()
    supplements = db.scalars(
        select(Supplement).where(Supplement.user_id == user_id).order_by(Supplement.id)
    ).all()
    logs = db.scalars(
        select(SupplementLog).join(Supplement, Supplement.id == SupplementLog.supplement_id).where(
            SupplementLog.user_id == user_id, Supplement.user_id == user_id,
            SupplementLog.log_date == day,
        ).order_by(SupplementLog.id)
    ).all()
    logs_by_supplement = {log.supplement_id: log for log in logs}
    supplement_rows = []
    for supplement in supplements:
        eligible = supplement_eligible(supplement, day)
        log = logs_by_supplement.get(supplement.id)
        if eligible or log is not None:
            supplement_rows.append({
                **supplement_data(supplement), "active": eligible,
                "status": None if log is None else ("taken" if log.taken else "not_taken"),
            })
    meal_rows = [meal_data(meal) for meal in meals]
    totals = {
        key: round(sum(getattr(item, key) for meal in meals for item in meal.items), 2)
        for key in NUTRIENTS
    }
    totals["water_ml"] = sum(log.amount_ml for log in water_logs)
    return {
        "date": day,
        "weight_kg": weight.weight_kg if weight else None,
        "weight_date": weight.recorded_at.astimezone(zone).date() if weight else None,
        "bmi": round(weight.weight_kg / (profile.height_cm / 100) ** 2, 2) if weight and profile else None,
        "totals": totals, "targets": target_data(target_for_day(db, user_id, day)),
        "meals": meal_rows, "water_logs": [water_data(log) for log in water_logs],
        "weight_records": [weight_data(record) for record in weights],
        "supplements": supplement_rows, "supplement_logs": [supplement_log_data(log) for log in logs],
    }


def build_history(db: Session, user_id: int, day: date, period: str) -> dict:
    if period == "week":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=6)
    elif period == "month":
        start = day.replace(day=1)
        end = day.replace(day=calendar.monthrange(day.year, day.month)[1])
    else:
        raise ValueError("History period must be week or month.")
    days = [build_dashboard(db, user_id, start + timedelta(days=offset)) for offset in range((end - start).days + 1)]
    today = local_today(db, user_id)
    elapsed = [dashboard for dashboard in days if dashboard["date"] <= today]
    tracked = [
        dashboard for dashboard in elapsed
        if any(dashboard[key] for key in ("meals", "water_logs", "weight_records", "supplement_logs"))
    ]
    meal_days = [dashboard for dashboard in elapsed if dashboard["meals"]]
    water_days = [dashboard for dashboard in elapsed if dashboard["water_logs"]]
    starting_weight = None
    latest = elapsed[-1]["weight_kg"] if elapsed else None
    if elapsed:
        period_start, _ = day_bounds(start, user_zone(db, user_id))
        previous = latest_weight(db, user_id, period_start)
        if previous is not None:
            starting_weight = previous.weight_kg
        else:
            first_record = next(
                (row["weight_records"][0] for row in elapsed if row["weight_records"]), None,
            )
            if first_record is not None:
                starting_weight = first_record["weight_kg"]
    eligible = [supplement for dashboard in elapsed for supplement in dashboard["supplements"] if supplement["active"]]
    taken = sum(supplement["status"] == "taken" for supplement in eligible)
    logged = sum(supplement["status"] is not None for supplement in eligible)
    profile = get_profile(db, user_id)
    progress = None
    if profile is not None and profile.goal_weight_kg is not None and latest is not None and elapsed:
        _, effective_end = day_bounds(elapsed[-1]["date"], user_zone(db, user_id))
        baseline = db.scalar(
            select(WeightRecord).where(
                WeightRecord.user_id == user_id, WeightRecord.recorded_at < effective_end,
                WeightRecord.recorded_at <= datetime.now(timezone.utc),
            ).order_by(WeightRecord.recorded_at, WeightRecord.id).limit(1)
        )
        if baseline is not None:
            goal_difference = profile.goal_weight_kg - baseline.weight_kg
            if goal_difference != 0:
                progress = round(max(0, min(100, (latest - baseline.weight_kg) / goal_difference * 100)), 2)
            elif latest == profile.goal_weight_kg:
                progress = 100.0
    summary = {
        "starting_weight_kg": starting_weight,
        "latest_weight_kg": latest,
        "weight_change_kg": round(latest - starting_weight, 2) if latest is not None and starting_weight is not None else None,
        "average_calories": round(sum(row["totals"]["calories"] for row in meal_days) / len(meal_days), 2) if meal_days else None,
        "average_protein": round(sum(row["totals"]["protein"] for row in meal_days) / len(meal_days), 2) if meal_days else None,
        "average_water_ml": round(sum(row["totals"]["water_ml"] for row in water_days) / len(water_days), 2) if water_days else None,
        "days_tracked": len(tracked),
        "meals_logged": sum(len(row["meals"]) for row in elapsed),
        "supplement_adherence": round(taken / len(eligible) * 100, 2) if eligible else None,
        "supplement_eligible_days": len(eligible),
        "supplement_logged_days": logged,
        "supplement_taken_days": taken,
        "supplement_data_coverage_percent": round(logged / len(eligible) * 100, 2) if eligible else None,
        "weight_goal_progress_percent": progress,
        "tracking_consistency_percent": round(len(tracked) / len(elapsed) * 100, 2) if elapsed else None,
        "eligible_calendar_days": len(elapsed),
        "calorie_days_logged": len(meal_days),
        "protein_days_logged": len(meal_days),
        "water_days_logged": len(water_days),
    }
    return {"start": start, "end": end, "summary": summary, "days": days}
