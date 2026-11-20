from datetime import date, datetime, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


PositiveWeight = Annotated[float, Field(gt=0, le=650)]
ShortText = Annotated[str, Field(min_length=1, max_length=30)]
Nutrient = Annotated[float, Field(ge=0, le=5000)]
TrackingDate = Annotated[date, Field(ge=date(2, 1, 1), le=date(9998, 12, 31))]


class Credentials(InputModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        local, separator, domain = value.partition("@")
        if (
            not separator or not local or not domain or "@" in domain
            or "." not in domain or domain.startswith(".") or domain.endswith(".")
            or any(character.isspace() for character in value)
            or len(local) > 64
        ):
            raise ValueError("Enter a valid email address.")
        return value


class ProfileInput(InputModel):
    date_of_birth: date
    sex: Literal["male", "female", "unspecified"]
    height_cm: float = Field(ge=50, le=275)
    current_weight_kg: PositiveWeight
    goal_weight_kg: PositiveWeight | None = None
    timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use a valid IANA timezone, for example Asia/Kolkata.") from None
        return value

    @model_validator(mode="after")
    def validate_birth_date(self):
        today = datetime.now(ZoneInfo(self.timezone)).date()
        if self.date_of_birth > today:
            raise ValueError("Date of birth cannot be in the future.")
        return self


class TargetInput(InputModel):
    effective_date: TrackingDate
    calories_target: float = Field(gt=0, le=20000)
    protein_target: float = Field(gt=0, le=1000)
    water_target_ml: int = Field(gt=0, le=20000)


def aware_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("Use a timezone-aware ISO datetime.") from None
    if not isinstance(value, datetime):
        raise ValueError("Use a timezone-aware ISO datetime, not a numeric timestamp.")
    if not 2 <= value.year <= 9998:
        raise ValueError("Tracking datetimes must use a year between 2 and 9998.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware ISO datetime with an offset is required.")
    return value.astimezone(timezone.utc)


class WeightInput(InputModel):
    weight_kg: PositiveWeight
    recorded_at: datetime

    _aware = field_validator("recorded_at", mode="before")(aware_utc)


class WaterInput(InputModel):
    amount_ml: int = Field(gt=0, le=10000)
    logged_at: datetime

    _aware = field_validator("logged_at", mode="before")(aware_utc)


class SupplementInput(InputModel):
    name: str = Field(min_length=1, max_length=100)
    dosage: float = Field(gt=0, le=100000)
    unit: ShortText

    @field_validator("name", "unit")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value


class SupplementUpdate(SupplementInput):
    active: bool


class SupplementLogInput(InputModel):
    supplement_id: int = Field(gt=0)
    log_date: TrackingDate
    taken: bool


class MealItemInput(InputModel):
    food_name: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0, le=100000)
    unit: ShortText
    calories: float = Field(ge=0, le=20000)
    protein: Nutrient
    carbs: Nutrient
    fat: Nutrient
    fiber: Nutrient
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("food_name", "unit")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value


class MealInput(InputModel):
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"]
    logged_at: datetime
    image_id: int | None = Field(default=None, gt=0)
    items: list[MealItemInput] = Field(min_length=1, max_length=100)

    _aware = field_validator("logged_at", mode="before")(aware_utc)
