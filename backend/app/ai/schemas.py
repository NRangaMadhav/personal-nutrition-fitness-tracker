from datetime import date
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from ..schemas import TrackingDate, aware_utc

ProviderChoice = Literal["auto", "nvidia", "groq"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Unit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]
Nonnegative = Annotated[float, Field(strict=True, ge=0, le=5000, allow_inf_nan=False)]
Calories = Annotated[float, Field(strict=True, ge=0, le=20_000, allow_inf_nan=False)]
Quantity = Annotated[float, Field(strict=True, gt=0, le=100_000, allow_inf_nan=False)]
Confidence = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DatedSchema(Schema):
    occurred_at: AwareDatetime

    _aware = field_validator("occurred_at", mode="before")(aware_utc)


class FoodItem(Schema):
    food_name: Name
    quantity: Quantity | None
    unit: Unit | None
    calories: Calories | None
    protein: Nonnegative | None
    carbs: Nonnegative | None
    fat: Nonnegative | None
    fiber: Nonnegative | None
    confidence: Confidence | None = None


class MealEvent(DatedSchema):
    type: Literal["meal"]
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] | None
    items: list[FoodItem] = Field(min_length=1, max_length=50)


class WaterEvent(DatedSchema):
    type: Literal["water"]
    amount_ml: Annotated[int, Field(strict=True, gt=0, le=10_000)] | None


class WeightEvent(DatedSchema):
    type: Literal["weight"]
    weight_kg: Annotated[float, Field(strict=True, gt=0, le=650, allow_inf_nan=False)] | None


class SupplementEvent(DatedSchema):
    type: Literal["supplement"]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    taken: StrictBool


Event = Annotated[MealEvent | WaterEvent | WeightEvent | SupplementEvent, Field(discriminator="type")]


class Extraction(Schema):
    events: list[Event] = Field(max_length=30)
    warnings: list[Annotated[str, StringConstraints(max_length=500)]] = Field(default_factory=list, max_length=60)


class LogRequest(DatedSchema):
    text: str = Field(default="", max_length=12_000)
    image_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    provider: ProviderChoice = "auto"

    @model_validator(mode="after")
    def has_input(self):
        if not self.text.strip() and self.image_id is None:
            raise ValueError("Provide text or an uploaded image.")
        return self


class ConfirmRequest(Schema):
    draft_id: Annotated[int, Field(strict=True, gt=0)]
    events: list[Event] = Field(min_length=1, max_length=30)


class CoachRequest(Schema):
    question: str = Field(min_length=1, max_length=4000)
    start_date: TrackingDate
    end_date: TrackingDate
    provider: ProviderChoice = "auto"

    @model_validator(mode="after")
    def valid_range(self):
        if not self.question.strip():
            raise ValueError("Ask a nonempty question.")
        if not 0 <= (self.end_date - self.start_date).days < 93:
            raise ValueError("Choose an ordered date range of at most 93 days.")
        return self


class CoachInterpretation(Schema):
    answer: str = Field(
        min_length=1, max_length=6000,
        description="Prose containing {{exact.metric.path}} placeholders instead of numeric values. No digits outside placeholders.",
    )
    metric_references: list[str] = Field(
        default_factory=list, max_length=30,
        description="Bare paths from facts.metrics used in answer, WITHOUT {{ }} braces.",
    )


def missing_fields(events: list[Event]) -> list[str]:
    missing = []
    for index, event in enumerate(events):
        if isinstance(event, MealEvent):
            if event.meal_type is None:
                missing.append(f"events[{index}].meal_type")
            for item_index, item in enumerate(event.items):
                for field in ("quantity", "unit", "calories", "protein", "carbs", "fat", "fiber"):
                    if getattr(item, field) is None:
                        missing.append(f"events[{index}].items[{item_index}].{field}")
        elif isinstance(event, WaterEvent) and event.amount_ml is None:
            missing.append(f"events[{index}].amount_ml")
        elif isinstance(event, WeightEvent) and event.weight_kg is None:
            missing.append(f"events[{index}].weight_kg")
    return missing
