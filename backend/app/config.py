import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class Settings(BaseModel):
    database_url: str
    jwt_secret_key: str = Field(min_length=32)
    token_hours: int = 12
    nvidia_api_key: str = ""
    groq_api_key: str = ""
    nvidia_model: str = "nvidia/nemotron-3.5-lightning-30b-a3b"
    nvidia_vision_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    groq_model: str = "openai/gpt-oss-120b"
    groq_vision_model: str = ""
    ai_primary_provider: str = "nvidia"
    object_storage_endpoint: str = "http://127.0.0.1:9000"
    object_storage_access_key: str = ""
    object_storage_secret_key: str = ""
    object_storage_bucket: str = "nutrition-images"


@lru_cache
def get_settings() -> Settings:
    values = {}
    for name in Settings.model_fields:
        value = os.getenv(name.upper())
        if value is not None:
            values[name] = value
    if not values.get("database_url") or not values.get("jwt_secret_key"):
        raise RuntimeError("Set DATABASE_URL and JWT_SECRET_KEY in the project's .env file.")
    return Settings.model_validate(values)
