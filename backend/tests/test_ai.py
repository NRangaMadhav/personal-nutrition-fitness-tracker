import base64
import io
from datetime import date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests
from botocore.exceptions import ClientError, EndpointConnectionError
from PIL import Image
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app import storage
from backend.app.ai import providers, workflows
from backend.app.ai.providers import AdapterResult, ProviderUnavailable
from backend.app.ai.schemas import CoachInterpretation, ConfirmRequest, Extraction
from backend.app.models import AIInput, Meal, StoredImage, Supplement, SupplementLog, User, WaterLog, WeightRecord
from backend.app.security import create_access_token


WHEN = "2026-09-01T22:00:00+00:00"


def water(amount=500):
    return {"type": "water", "occurred_at": WHEN, "amount_ml": amount}


def meal(**item_changes):
    item = {
        "food_name": "Yogurt", "quantity": 1, "unit": "cup",
        "calories": 100, "protein": 10, "carbs": 12, "fat": 2, "fiber": 0, "confidence": None,
    }
    item.update(item_changes)
    return {"type": "meal", "occurred_at": WHEN, "meal_type": "snack", "items": [item]}


@pytest.fixture
def users(db):
    records = [User(email=f"ai-{uuid4().hex}@example.com", password_hash="unused") for _ in range(2)]
    db.add_all(records)
    db.commit()
    return records


def auth(user):
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def row_count(db, model, user):
    return db.scalar(select(func.count()).select_from(model).where(model.user_id == user.id))


def saved_draft(db, user, events=None, image_id=None):
    draft = AIInput(
        user_id=user.id, input_text="test", image_id=image_id, provider="nvidia", model="mock-text",
        response_data={"events": events or [water()], "warnings": []}, status="draft",
    )
    db.add(draft)
    db.commit()
    return draft


@pytest.fixture
def extraction_mock(monkeypatch):
    calls = []
    output = {"events": [water()], "warnings": []}

    def invoke(messages, schema, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return AdapterResult(schema.model_validate(output), "nvidia", "mock-vision" if kwargs.get("vision") else "mock-text")

    monkeypatch.setattr(workflows, "invoke_provider", invoke)
    return output, calls


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.created = False
        self.private = False

    def head_bucket(self, **kwargs):
        if not self.created:
            raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

    def create_bucket(self, **kwargs):
        self.created = True

    def delete_bucket_policy(self, **kwargs):
        self.private = True

    def put_object(self, Key, Body, **kwargs):
        self.objects[Key] = Body

    def get_object(self, Key, **kwargs):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Key, **kwargs):
        self.objects.pop(Key, None)


@pytest.fixture
def object_store(monkeypatch):
    client = FakeS3()
    monkeypatch.setattr(storage, "storage_client", lambda: client)
    return client


def image_bytes(fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(output, format=fmt)
    return output.getvalue()


def upload(client, user, data=None):
    return client.post(
        "/images", headers=auth(user),
        files={"file": ("../untrusted-name.exe", data if data is not None else image_bytes(), "application/octet-stream")},
    )


def test_extraction_is_persisted_draft_without_confirmed_writes(client, db, users, extraction_mock):
    output, calls = extraction_mock
    output["events"] = [water(), meal(quantity=None, calories=None)]
    response = client.post("/ai-log", headers=auth(users[0]), json={"text": "water and yogurt", "occurred_at": WHEN})
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["status"] == "draft"
    assert draft["provider"] == "nvidia"
    assert draft["events"][1]["items"][0]["calories"] is None
    assert any("correction" in warning for warning in draft["warnings"])
    assert len(calls) == 1
    assert "Asia/Kolkata" in str(calls[0]["messages"])
    assert row_count(db, AIInput, users[0]) == 1
    for model in (Meal, WaterLog, WeightRecord, Supplement, SupplementLog):
        assert row_count(db, model, users[0]) == 0
    assert client.get("/ai-log", headers=auth(users[0])).json()[0]["id"] == draft["id"]


def test_confirmation_requires_all_unknowns_and_nonempty_selection(client, db, users):
    draft = saved_draft(db, users[0])
    unknown_meal_type = {**meal(), "meal_type": None}
    for events in ([], [water(), meal(protein=None)], [water(), water(-1)], [water(), unknown_meal_type]):
        response = client.post("/ai-log/confirm", headers=auth(users[0]), json={"draft_id": draft.id, "events": events})
        assert response.status_code == 422, response.text
    assert row_count(db, WaterLog, users[0]) == row_count(db, Meal, users[0]) == 0
    db.refresh(draft)
    assert draft.status == "draft"


def test_confirmation_saves_selected_edits_once_without_provider(client, db, users, monkeypatch):
    draft = saved_draft(db, users[0], [water(), meal()])

    def forbidden(*args, **kwargs):
        pytest.fail("Confirmation must never call an AI provider.")

    monkeypatch.setattr(workflows, "invoke_provider", forbidden)
    request = {"draft_id": draft.id, "events": [water(750)]}
    response = client.post("/ai-log/confirm", headers=auth(users[0]), json=request)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert client.post("/ai-log/confirm", headers=auth(users[0]), json=request).status_code == 409
    assert row_count(db, WaterLog, users[0]) == 1
    assert row_count(db, Meal, users[0]) == 0
    assert db.scalar(select(WaterLog).where(WaterLog.user_id == users[0].id)).amount_ml == 750
    assert client.get("/ai-log", headers=auth(users[0])).json() == []


def test_database_failure_rolls_back_whole_confirmation(client, db, users, monkeypatch):
    draft = saved_draft(db, users[0])
    original_flush = db.flush
    failed = False

    def fail_second_record(*args, **kwargs):
        nonlocal failed
        if any(isinstance(record, Meal) for record in db.new) and not failed:
            failed = True
            raise IntegrityError("mock constraint failure", {}, RuntimeError("mock"))
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(db, "flush", fail_second_record)
    response = client.post(
        "/ai-log/confirm", headers=auth(users[0]), json={"draft_id": draft.id, "events": [water(), meal()]}
    )
    assert response.status_code == 409, response.text
    assert failed
    assert row_count(db, WaterLog, users[0]) == row_count(db, Meal, users[0]) == 0
    db.refresh(draft)
    assert draft.status == "draft"


def test_draft_and_image_ownership(client, db, users, object_store, extraction_mock):
    draft = saved_draft(db, users[0])
    foreign_headers = auth(users[1])
    assert client.get(f"/ai-log/{draft.id}", headers=foreign_headers).status_code == 404
    assert client.post(f"/ai-log/{draft.id}/discard", headers=foreign_headers).status_code == 404
    assert client.post("/ai-log/confirm", headers=foreign_headers, json={"draft_id": draft.id, "events": [water()]}).status_code == 404
    image_id = upload(client, users[0]).json()["id"]
    assert client.get(f"/images/{image_id}", headers=foreign_headers).status_code == 404
    assert client.delete(f"/images/{image_id}", headers=foreign_headers).status_code == 404
    assert client.post("/ai-log", headers=foreign_headers, json={"image_id": image_id, "occurred_at": WHEN}).status_code == 404
    corrupt_draft = saved_draft(db, users[1], image_id=image_id)
    assert client.post(
        "/ai-log/confirm", headers=foreign_headers, json={"draft_id": corrupt_draft.id, "events": [meal()]}
    ).status_code == 404
    assert row_count(db, Meal, users[1]) == 0


def test_image_only_vision_draft_and_confirmed_meal_attachment(client, db, users, object_store, extraction_mock):
    output, calls = extraction_mock
    output["events"] = [meal(), water()]
    image_id = upload(client, users[0]).json()["id"]
    response = client.post("/ai-log", headers=auth(users[0]), json={"image_id": image_id, "occurred_at": WHEN})
    assert response.status_code == 201, response.text
    draft = response.json()
    assert calls[0]["vision"] is True
    assert "data:image/jpeg;base64," in str(calls[0]["messages"])
    assert client.delete(f"/images/{image_id}", headers=auth(users[0])).status_code == 409
    confirmed = client.post("/ai-log/confirm", headers=auth(users[0]), json={"draft_id": draft["id"], "events": output["events"]})
    assert confirmed.status_code == 200, confirmed.text
    assert db.scalar(select(Meal).where(Meal.user_id == users[0].id)).image_id == image_id
    assert client.delete(f"/images/{image_id}", headers=auth(users[0])).status_code == 409


def test_supplement_names_match_and_upsert_on_user_local_day(client, db, users):
    draft = saved_draft(db, users[0])
    events = [
        {"type": "supplement", "occurred_at": WHEN, "name": "Fish Oil", "taken": True},
        {"type": "supplement", "occurred_at": WHEN, "name": "fish oil", "taken": False},
        {"type": "weight", "occurred_at": WHEN, "weight_kg": 70.5},
    ]
    response = client.post("/ai-log/confirm", headers=auth(users[0]), json={"draft_id": draft.id, "events": events})
    assert response.status_code == 200, response.text
    assert row_count(db, Supplement, users[0]) == row_count(db, SupplementLog, users[0]) == 1
    supplement = db.scalar(select(Supplement).where(Supplement.user_id == users[0].id))
    log = db.scalar(select(SupplementLog).where(SupplementLog.user_id == users[0].id))
    assert supplement.dosage == 1 and supplement.unit == "dose"
    assert supplement.active_periods == [{"start": "2026-09-02", "end": None}]
    assert log.log_date == date(2026, 9, 2) and log.taken is False
    assert row_count(db, WeightRecord, users[0]) == 1


@pytest.mark.parametrize("fmt,content_type", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_upload_preserves_original_private_bytes(client, db, users, object_store, fmt, content_type):
    original = image_bytes(fmt)
    response = upload(client, users[0], original)
    assert response.status_code == 201, response.text
    stored = db.get(StoredImage, response.json()["id"])
    assert stored.object_key.startswith(f"users/{users[0].id}/")
    assert ".." not in stored.object_key and "untrusted" not in stored.object_key
    assert stored.content_type == content_type
    assert object_store.created and object_store.private
    result = client.get(f"/images/{stored.id}", headers=auth(users[0]))
    assert result.content == original
    assert result.headers["cache-control"] == "private, no-store"


def test_unsafe_uploads_and_remote_urls_are_rejected(client, users, object_store, monkeypatch):
    assert upload(client, users[0], b"<svg onload='evil()'/>").status_code == 415
    assert upload(client, users[0], b"x" * (storage.MAX_IMAGE_BYTES + 1)).status_code == 413
    assert upload(client, users[0], image_bytes()[:30]).status_code == 415
    monkeypatch.setattr(storage, "MAX_IMAGE_PIXELS", 3)
    assert upload(client, users[0]).status_code == 413
    assert client.post("/images", headers=auth(users[0]), json={"url": "http://127.0.0.1/private"}).status_code == 422
    assert client.post("/ai-log", headers=auth(users[0]), json={"image_url": "http://private", "occurred_at": WHEN}).status_code == 422
    assert not object_store.objects


def test_pillow_bomb_is_rejected(client, users, object_store, monkeypatch):
    data = image_bytes()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    assert upload(client, users[0], data).status_code == 413
    assert not object_store.objects


def test_discard_then_delete_unused_image(client, db, users, object_store):
    image_id = upload(client, users[0]).json()["id"]
    draft = saved_draft(db, users[0], image_id=image_id)
    assert client.delete(f"/images/{image_id}", headers=auth(users[0])).status_code == 409
    assert client.post(f"/ai-log/{draft.id}/discard", headers=auth(users[0])).status_code == 200
    assert client.delete(f"/images/{image_id}", headers=auth(users[0])).status_code == 200
    db.refresh(draft)
    assert draft.image_id is None
    assert not object_store.objects
    assert client.post("/ai-log/confirm", headers=auth(users[0]), json={"draft_id": draft.id, "events": [water()]}).status_code == 409


def test_storage_failure_is_useful_and_saves_no_metadata(client, db, users, object_store, monkeypatch):
    def unavailable(**kwargs):
        raise EndpointConnectionError(endpoint_url="http://127.0.0.1:9000")

    monkeypatch.setattr(object_store, "head_bucket", unavailable)
    response = upload(client, users[0])
    assert response.status_code == 503
    assert "storage" in response.json()["detail"].lower()
    assert row_count(db, StoredImage, users[0]) == 0


def test_provider_failure_never_saves_a_draft(client, db, users, monkeypatch):
    def unavailable(*args, **kwargs):
        raise ProviderUnavailable("The image upload timed out.", code="ai_upload_timeout")

    monkeypatch.setattr(workflows, "invoke_provider", unavailable)
    response = client.post("/ai-log", headers=auth(users[0]), json={"text": "water", "occurred_at": WHEN})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ai_upload_timeout"
    assert row_count(db, AIInput, users[0]) == row_count(db, WaterLog, users[0]) == 0


def test_large_image_draft_uses_compressed_copy_and_keeps_original(
    client, db, users, object_store, extraction_mock
):
    output, calls = extraction_mock
    output["events"] = [meal()]
    original = io.BytesIO()
    Image.effect_noise((720, 1600), 80).convert("RGB").save(original, format="PNG")
    original = original.getvalue()
    assert len(original) > 1_000_000
    image_id = upload(client, users[0], original).json()["id"]
    response = client.post(
        "/ai-log", headers=auth(users[0]),
        json={"image_id": image_id, "text": "Lunch", "occurred_at": WHEN},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "draft"
    content = calls[0]["messages"][-1].content
    image_url = next(block["image_url"]["url"] for block in content if block["type"] == "image_url")
    assert image_url.startswith("data:image/jpeg;base64,")
    prepared = base64.b64decode(image_url.split(",", 1)[1])
    assert len(prepared) <= 128 * 1024
    assert client.get(f"/images/{image_id}", headers=auth(users[0])).content == original
    assert row_count(db, Meal, users[0]) == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "400"])
def test_draft_numbers_are_strict_and_finite(bad):
    with pytest.raises(ValidationError):
        ConfirmRequest.model_validate({"draft_id": 1, "events": [meal(calories=bad)]})


def test_event_timestamps_require_timezone():
    with pytest.raises(ValidationError):
        Extraction.model_validate({"events": [{**water(), "occurred_at": "2026-09-01T12:00:00"}]})


def test_nonfinite_request_returns_validation_error_not_server_error(client, users):
    response = client.post(
        "/ai-log/confirm",
        headers={**auth(users[0]), "Content-Type": "application/json"},
        content='{"draft_id":1,"events":[{"type":"weight","occurred_at":"2026-09-01T00:00:00Z","weight_kg":NaN}]}',
    )
    assert response.status_code == 422, response.text
    assert "input" not in response.json()["detail"][0]


def test_coach_no_records_is_explicit_and_isolated(client, db, users, monkeypatch):
    db.add(WaterLog(user_id=users[1].id, amount_ml=999, logged_at=datetime.fromisoformat(WHEN)))
    db.commit()
    calls = []

    def coach_provider(messages, schema, **kwargs):
        calls.append(messages)
        return AdapterResult(CoachInterpretation(answer="Start recording meals and hydration to establish a baseline."), "groq", "mock")

    monkeypatch.setattr(workflows, "invoke_provider", coach_provider)
    response = client.post(
        "/coach", headers=auth(users[0]),
        json={"question": "How am I doing?", "start_date": "2026-09-01", "end_date": "2026-09-02", "provider": "groq"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert "No confirmed records" in data["answer"]
    assert data["facts"]["summary"]["has_records"] is False
    assert data["facts"]["summary"]["average_calories_per_meal_logged_day"] is None
    assert data["facts"]["summary"]["totals"]["water_ml"] == 0
    assert "999" not in str(calls)
    assert row_count(db, AIInput, users[0]) == row_count(db, WaterLog, users[0]) == 0


def test_coach_replaces_only_verified_metrics(client, db, users, monkeypatch):
    db.add(WaterLog(user_id=users[0].id, amount_ml=500, logged_at=datetime.fromisoformat(WHEN)))
    db.commit()
    output = CoachInterpretation(
        answer="Your recorded water was {{summary.totals.water_ml}} ml.",
        metric_references=["summary.totals.water_ml"],
    )
    monkeypatch.setattr(workflows, "invoke_provider", lambda *args, **kwargs: AdapterResult(output, "nvidia", "mock"))
    request = {"question": "Water?", "start_date": "2026-09-02", "end_date": "2026-09-02"}
    response = client.post("/coach", headers=auth(users[0]), json=request)
    assert response.status_code == 200, response.text
    assert "500" in response.json()["answer"]
    output.answer = "You drank 9000 ml."
    assert client.post("/coach", headers=auth(users[0]), json=request).status_code == 503
    output.answer = "You drank {{invented.path}} ml."
    output.metric_references = ["invented.path"]
    assert client.post("/coach", headers=auth(users[0]), json=request).status_code == 503


def test_coach_rejects_unbounded_ranges(client, users):
    response = client.post(
        "/coach", headers=auth(users[0]),
        json={"question": "Trend?", "start_date": "2025-01-01", "end_date": "2026-09-01"},
    )
    assert response.status_code == 422


def test_provider_auto_falls_back_and_explicit_choice_does_not(monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "get_settings", lambda: SimpleNamespace(ai_primary_provider="nvidia"))

    class Adapter:
        def __init__(self, name):
            self.name = name

        def invoke(self, messages, schema):
            calls.append(self.name)
            if self.name == "nvidia":
                raise ProviderUnavailable("nvidia unavailable")
            return AdapterResult(Extraction(events=[]), "groq", "vision-mock")

    monkeypatch.setattr(providers, "get_adapter", lambda name, **kwargs: Adapter(name))
    result = providers.invoke_provider([], Extraction, provider="auto", vision=True)
    assert calls == ["nvidia", "groq"] and result.provider == "groq"
    calls.clear()
    with pytest.raises(ProviderUnavailable):
        providers.invoke_provider([], Extraction, provider="nvidia")
    assert calls == ["nvidia"]


def test_manual_json_parser_does_not_accept_invalid_success():
    assert providers.parse_json('```json\n{"events":[],"warnings":[]}\n```', Extraction).events == []
    with pytest.raises(ValidationError):
        providers.parse_json('{"events":[{"type":"water","occurred_at":"2026-09-01T00:00:00Z","amount_ml":"500"}]}', Extraction)
    with pytest.raises(ValueError):
        providers.parse_json("Here is a guess, not JSON.", Extraction)


def test_structured_incompatibility_uses_real_validated_manual_response():
    class Client:
        calls = 0

        def with_structured_output(self, schema):
            raise NotImplementedError("Model has no tool mode")

        def invoke(self, messages):
            self.calls += 1
            return SimpleNamespace(content='{"events":[],"warnings":["No matching event"]}')

    adapter = providers.LangChainAdapter.__new__(providers.LangChainAdapter)
    adapter.name, adapter.model, adapter.client = "nvidia", "mock", Client()
    result = adapter.invoke([], Extraction)
    assert result.data.warnings == ["No matching event"]
    assert adapter.client.calls == 1
    adapter.client.invoke = lambda messages: SimpleNamespace(content="not valid JSON")
    with pytest.raises(ProviderUnavailable):
        adapter.invoke([], Extraction)


def test_vision_uses_separate_configured_model(monkeypatch):
    settings = SimpleNamespace(
        nvidia_api_key="test-only", nvidia_model="text-model", nvidia_vision_model="vision-model",
        groq_api_key="", groq_model="other-text", groq_vision_model="",
    )
    calls = []
    monkeypatch.setattr(providers, "get_settings", lambda: settings)
    monkeypatch.setattr(providers, "LangChainAdapter", lambda name, model, key: calls.append((name, model)))
    providers.get_adapter("nvidia", vision=True)
    providers.get_adapter("nvidia", vision=False)
    assert calls == [("nvidia", "vision-model"), ("nvidia", "text-model")]
    with pytest.raises(ProviderUnavailable):
        providers.get_adapter("groq", vision=True)


def test_nvidia_transport_errors_are_typed_and_sanitized():
    response = requests.Response()
    response.status_code = 401
    response._content = b'{"error":"invalid credential"}'
    with pytest.raises(ProviderUnavailable, match="credentials"):
        providers.check_nvidia_response(response)
    response.status_code = 400
    response._content = b'{"error":"tool calling unsupported; sensitive request text"}'
    with pytest.raises(providers.StructuredOutputUnsupported, match="structured-output") as exc:
        providers.check_nvidia_response(response)
    assert "sensitive" not in str(exc.value)
    response.status_code = 400
    response._content = b'{"error":{"message":"unknown field `guided_json`"}}'
    with pytest.raises(providers.StructuredOutputUnsupported):
        providers.check_nvidia_response(response)
    response.status_code = 404
    with pytest.raises(ProviderUnavailable, match="model-list presence"):
        providers.check_nvidia_response(response)
    response.status_code = 200
    response._content = b'{"error":{"object":"error","message":"sensitive response"}}'
    with pytest.raises(ProviderUnavailable) as exc:
        providers.check_nvidia_response(response)
    assert "sensitive" not in str(exc.value)


def test_nvidia_client_timeout_and_retry_options_are_not_model_payload():
    adapter = providers.LangChainAdapter("nvidia", "meta/llama-3.3-70b-instruct", "test-only-api-key")
    assert adapter.client._client.timeout == 60
    assert adapter.client._client.get_session_fn is providers.BoundedNvidiaSession
    assert "max_tries" not in adapter.client.model_kwargs
    with providers.BoundedNvidiaSession() as session:
        retries = session.get_adapter("https://integrate.api.nvidia.com").max_retries
        assert retries.total == 1 and retries.read == 0
        assert retries.is_retry("POST", 503)
        assert retries.respect_retry_after_header is False


def test_unsupported_structured_model_gets_one_portable_manual_request():
    calls = []

    class Client:
        def with_structured_output(self, schema):
            pytest.fail("A model without structured capability must not probe unsupported modes.")

        def invoke(self, messages):
            calls.append(messages)
            return SimpleNamespace(content='{"events":[],"warnings":[]}')

    adapter = providers.LangChainAdapter.__new__(providers.LangChainAdapter)
    adapter.name, adapter.model, adapter.client = "nvidia", "mock", Client()
    adapter.structured_supported = False
    result = adapter.invoke(
        [SystemMessage(content="Extract events."), HumanMessage(content="synthetic")], Extraction
    )
    assert result.provider == "nvidia"
    assert len(calls) == 1
    assert [type(message) for message in calls[0]] == [SystemMessage, HumanMessage]
    assert "Extract events." in calls[0][0].content
    assert "schema" in calls[0][0].content
