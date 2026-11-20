from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = (CheckConstraint("height_cm > 0 AND height_cm <= 300"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    date_of_birth: Mapped[date] = mapped_column(Date)
    sex: Mapped[str] = mapped_column(String(20))
    height_cm: Mapped[float] = mapped_column(Float)
    goal_weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")


class DailyTarget(Base):
    __tablename__ = "daily_targets"
    __table_args__ = (
        UniqueConstraint("user_id", "effective_date"),
        CheckConstraint("calories_target > 0 AND calories_target <= 20000"),
        CheckConstraint("protein_target > 0 AND protein_target <= 2000"),
        CheckConstraint("water_target_ml > 0 AND water_target_ml <= 30000"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    calories_target: Mapped[float] = mapped_column(Float)
    protein_target: Mapped[float] = mapped_column(Float)
    water_target_ml: Mapped[int] = mapped_column(Integer)


class WeightRecord(Base):
    __tablename__ = "weight_records"
    __table_args__ = (CheckConstraint("weight_kg > 0 AND weight_kg <= 700"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    weight_kg: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class WaterLog(Base):
    __tablename__ = "water_logs"
    __table_args__ = (CheckConstraint("amount_ml > 0 AND amount_ml <= 30000"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount_ml: Mapped[int] = mapped_column(Integer)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Supplement(Base):
    __tablename__ = "supplements"
    __table_args__ = (CheckConstraint("dosage > 0 AND dosage <= 100000"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    dosage: Mapped[float] = mapped_column(Float, default=1)
    unit: Mapped[str] = mapped_column(String(30), default="dose")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    active_periods: Mapped[list] = mapped_column(JSON, default=list)


class SupplementLog(Base):
    __tablename__ = "supplement_logs"
    __table_args__ = (UniqueConstraint("user_id", "supplement_id", "log_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    supplement_id: Mapped[int] = mapped_column(ForeignKey("supplements.id", ondelete="CASCADE"))
    log_date: Mapped[date] = mapped_column(Date, index=True)
    taken: Mapped[bool] = mapped_column(Boolean)


class StoredImage(Base):
    __tablename__ = "stored_images"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    object_key: Mapped[str] = mapped_column(String(255), unique=True)
    content_type: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Meal(Base):
    __tablename__ = "meals"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    meal_type: Mapped[str] = mapped_column(String(20))
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    image_id: Mapped[int | None] = mapped_column(ForeignKey("stored_images.id"), nullable=True)
    items: Mapped[list["MealItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class MealItem(Base):
    __tablename__ = "meal_items"
    __table_args__ = (
        CheckConstraint("quantity > 0 AND quantity <= 100000"),
        CheckConstraint("calories >= 0 AND calories <= 100000"),
        CheckConstraint("protein >= 0 AND protein <= 100000"),
        CheckConstraint("carbs >= 0 AND carbs <= 100000"),
        CheckConstraint("fat >= 0 AND fat <= 100000"),
        CheckConstraint("fiber >= 0 AND fiber <= 100000"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    meal_id: Mapped[int] = mapped_column(ForeignKey("meals.id", ondelete="CASCADE"), index=True)
    food_name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(30))
    calories: Mapped[float] = mapped_column(Float)
    protein: Mapped[float] = mapped_column(Float)
    carbs: Mapped[float] = mapped_column(Float)
    fat: Mapped[float] = mapped_column(Float)
    fiber: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class AIInput(Base):
    __tablename__ = "ai_inputs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    input_text: Mapped[str] = mapped_column(Text, default="")
    image_id: Mapped[int | None] = mapped_column(ForeignKey("stored_images.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(150))
    response_data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
