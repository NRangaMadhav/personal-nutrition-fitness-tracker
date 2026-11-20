import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


load_dotenv(Path(__file__).resolve().parents[2] / ".env")
test_url = os.environ.get("TEST_DATABASE_URL", "")
if not test_url or not urlsplit(test_url).path.endswith("_test"):
    raise RuntimeError("Set TEST_DATABASE_URL to a dedicated database whose name ends in _test.")
os.environ["DATABASE_URL"] = test_url

from backend.app.config import get_settings

get_settings.cache_clear()

from backend.app.database import Base, get_db, psycopg_url
from backend.app import models


@pytest.fixture(scope="session")
def test_engine():
    engine = create_engine(psycopg_url(test_url))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(test_engine):
    with test_engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as session:
            yield session
        transaction.rollback()


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    from backend.app.main import app

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
