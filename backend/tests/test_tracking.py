from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import jwt
import pytest
from sqlalchemy import func, select

from backend.app import api, tracking
from backend.app.config import get_settings
from backend.app.models import MealItem, StoredImage, User
from backend.app.security import verify_password


PASSWORD = "LocalTracker-123!"


def register(client, email="first@example.com"):
    response = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    assert set(response.json()) == {"access_token", "token_type"}
    assert response.json()["token_type"] == "bearer"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def two_users(client):
    return register(client), register(client, "second@example.com")


def profile_body(**changes):
    return {
        "date_of_birth": "1990-01-01", "sex": "male", "height_cm": 175,
        "current_weight_kg": 80, "goal_weight_kg": 70, "timezone": "Asia/Kolkata",
        **changes,
    }


def meal_body(logged_at="2024-01-08T12:00:00+05:30", **changes):
    return {
        "meal_type": "lunch", "logged_at": logged_at, "image_id": None,
        "items": [{
            "food_name": "Rice and lentils", "quantity": 2, "unit": "bowls",
            "calories": 600, "protein": 25, "carbs": 90, "fat": 15, "fiber": 12,
            "confidence": None,
        }],
        **changes,
    }


def post(client, path, headers, payload, expected=201):
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == expected, response.text
    return response.json()


def get(client, path, headers):
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def set_profile(client, headers, **changes):
    response = client.put("/profile", headers=headers, json=profile_body(**changes))
    assert response.status_code == 200, response.text
    return response.json()


def clear_weights(client, headers):
    for record in get(client, "/weight", headers):
        assert client.delete(f"/weight/{record['id']}", headers=headers).status_code == 204


def test_registration_login_and_normalized_email(client):
    headers = register(client, "  First@Example.COM  ")
    user = get(client, "/auth/me", headers)
    assert set(user) == {"id", "email"}
    assert user["email"] == "first@example.com"
    response = client.post("/auth/login", json={"email": "FIRST@example.com", "password": PASSWORD})
    assert response.status_code == 200
    logged_in = {"Authorization": f"Bearer {response.json()['access_token']}"}
    assert get(client, "/auth/me", logged_in) == user
    assert client.post("/auth/register", json={"email": user["email"], "password": PASSWORD}).status_code == 409
    assert client.post("/auth/login", json={"email": user["email"], "password": "incorrect123"}).status_code == 401
    assert client.post("/auth/login", json={"email": "missing@example.com", "password": PASSWORD}).status_code == 401


def test_passwords_are_argon2_hashes(client, db):
    headers = register(client)
    identifier = get(client, "/auth/me", headers)["id"]
    user = db.scalar(select(User).where(User.id == identifier))
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash
    assert verify_password(PASSWORD, user.password_hash)


@pytest.mark.parametrize("payload", [
    {"email": "invalid", "password": PASSWORD},
    {"email": "a@b@c.com", "password": PASSWORD},
    {"email": "space name@example.com", "password": PASSWORD},
    {"email": "a@example.com", "password": "short"},
    {"email": "a@example.com", "password": "x" * 129},
    {"email": "a@example.com", "password": PASSWORD, "is_admin": True},
])
def test_invalid_credentials(client, payload):
    assert client.post("/auth/register", json=payload).status_code == 422


@pytest.mark.parametrize("path", [
    "/auth/me", "/profile", "/targets", "/targets/recommend", "/weight", "/water",
    "/supplements", "/supplement-logs", "/meals", "/dashboard",
    "/history/day", "/history/week", "/history/month",
])
def test_authentication_required(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_expired_and_wrong_signature_tokens(client):
    headers = register(client)
    user_id = get(client, "/auth/me", headers)["id"]
    now = datetime.now(timezone.utc)
    settings = get_settings()
    base = {"sub": str(user_id), "iat": now - timedelta(hours=2), "exp": now + timedelta(hours=1), "type": "access"}
    invalid_tokens = [
        "not.a.token",
        jwt.encode({**base, "exp": now - timedelta(seconds=1)}, settings.jwt_secret_key, algorithm="HS256"),
        jwt.encode(base, "an-entirely-different-secret-key-123456789", algorithm="HS256"),
        jwt.encode({**base, "sub": "not-a-user-id"}, settings.jwt_secret_key, algorithm="HS256"),
        jwt.encode({**base, "sub": "9" * 100}, settings.jwt_secret_key, algorithm="HS256"),
        jwt.encode({**base, "sub": "2147483647"}, settings.jwt_secret_key, algorithm="HS256"),
        jwt.encode({**base, "type": "refresh"}, settings.jwt_secret_key, algorithm="HS256"),
        jwt.encode({"sub": str(user_id)}, settings.jwt_secret_key, algorithm="HS256"),
        jwt.encode(base, "", algorithm="none"),
    ]
    for token in invalid_tokens:
        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, response.text
    assert client.get("/auth/me", headers={"Authorization": "Basic credentials"}).status_code == 401


def test_profile_current_weight_is_latest_record_and_not_duplicated(client):
    headers = register(client)
    assert client.get("/profile", headers=headers).status_code == 404
    result = set_profile(client, headers)
    assert set(result) == set(profile_body())
    assert len(get(client, "/weight", headers)) == 1
    set_profile(client, headers, height_cm=176)
    assert len(get(client, "/weight", headers)) == 1
    set_profile(client, headers, current_weight_kg=79)
    assert len(get(client, "/weight", headers)) == 2
    assert get(client, "/profile", headers)["current_weight_kg"] == 79
    post(client, "/weight", headers, {"weight_kg": 90, "recorded_at": "2020-01-01T00:00:00Z"})
    assert get(client, "/profile", headers)["current_weight_kg"] == 79
    latest = get(client, "/weight", headers)[0]
    assert client.delete(f"/weight/{latest['id']}", headers=headers).status_code == 204
    assert get(client, "/profile", headers)["current_weight_kg"] == 80
    clear_weights(client, headers)
    assert get(client, "/profile", headers)["current_weight_kg"] is None
    assert get(client, "/dashboard", headers)["bmi"] is None
    set_profile(client, headers)
    assert len(get(client, "/weight", headers)) == 1


@pytest.mark.parametrize("changes", [
    {"date_of_birth": "2999-01-01"},
    {"sex": "other"},
    {"height_cm": 0},
    {"height_cm": "NaN"},
    {"current_weight_kg": -1},
    {"current_weight_kg": "Infinity"},
    {"goal_weight_kg": 0},
    {"timezone": "Not/A_Timezone"},
])
def test_profile_validation_is_atomic(client, changes):
    headers = register(client)
    response = client.put("/profile", headers=headers, json=profile_body(**changes))
    assert response.status_code == 422, response.text
    assert client.get("/profile", headers=headers).status_code == 404
    assert get(client, "/weight", headers) == []


def test_profile_default_timezone_and_no_goal(client):
    headers = register(client)
    body = profile_body(goal_weight_kg=None)
    del body["timezone"]
    response = client.put("/profile", headers=headers, json=body)
    assert response.status_code == 200
    assert response.json()["timezone"] == "Asia/Kolkata"
    assert response.json()["goal_weight_kg"] is None


@pytest.mark.parametrize("value", ["not-a-date", "2024-02-30", "0001-01-01", "9999-12-31"])
def test_invalid_calendar_dates_return_validation_errors(client, value):
    headers = register(client)
    for route in ("/dashboard", "/history/day", "/history/week", "/history/month", "/targets"):
        assert client.get(f"{route}?date={value}", headers=headers).status_code == 422


def test_targets_preserve_effective_date_history_and_manual_without_profile(client):
    headers = register(client)
    assert get(client, "/targets?date=2024-01-01", headers) is None
    first = {"effective_date": "2024-01-08", "calories_target": 2100, "protein_target": 90, "water_target_ml": 2500}
    second = {**first, "effective_date": "2024-01-10", "calories_target": 2300}
    for body in (first, second):
        assert client.put("/targets", headers=headers, json=body).status_code == 200
    assert get(client, "/targets?date=2024-01-07", headers) is None
    assert get(client, "/targets?date=2024-01-09", headers)["calories_target"] == 2100
    assert get(client, "/targets?date=2024-01-10", headers)["calories_target"] == 2300
    target_id = get(client, "/targets?date=2024-01-10", headers)["id"]
    assert client.put("/targets", headers=headers, json={**second, "calories_target": 2400}).json()["id"] == target_id
    assert get(client, "/dashboard?date=2024-01-09", headers)["targets"]["calories_target"] == 2100
    assert get(client, "/targets?date=2024-01-11", headers)["calories_target"] == 2400
    assert client.put("/targets", headers=headers, json={**first, "water_target_ml": -1}).status_code == 422
    assert get(client, "/targets?date=2024-01-09", headers)["water_target_ml"] == 2500


def test_adult_target_formula_is_explained_and_not_saved_automatically(client):
    headers = register(client)
    assert client.get("/targets/recommend", headers=headers).status_code == 404
    set_profile(client, headers)
    recommendation = get(client, "/targets/recommend", headers)
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    age = today.year - 1990
    assert recommendation["calories_target"] == round((10 * 80 + 6.25 * 175 - 5 * age + 5) * 1.2)
    assert recommendation["protein_target"] == 96
    assert recommendation["water_target_ml"] == 2400
    for term in ("Mifflin", "sedentary", "1.2", "30 ml/kg", "No automatic weight-loss deficit"):
        assert term in recommendation["explanation"]
    assert get(client, "/targets", headers) is None
    set_profile(client, headers, sex="female", goal_weight_kg=90)
    assert get(client, "/targets/recommend", headers)["calories_target"] == round((10 * 80 + 6.25 * 175 - 5 * age - 161) * 1.2)


def test_manual_targets_available_for_unspecified_and_minor(client):
    headers = register(client)
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    for changes in ({"sex": "unspecified"}, {"date_of_birth": today.replace(year=today.year - 17).isoformat()}):
        set_profile(client, headers, **changes)
        response = client.get("/targets/recommend", headers=headers)
        assert response.status_code == 422
        assert "manual targets" in response.json()["detail"]
        body = {"effective_date": today.isoformat(), "calories_target": 2000, "protein_target": 80, "water_target_ml": 2000}
        assert client.put("/targets", headers=headers, json=body).status_code == 200


def test_weight_water_local_midnight_and_historical_source_of_truth(client):
    headers = register(client)
    set_profile(client, headers)
    clear_weights(client, headers)
    for weight, stamp in (
        (82, "2024-01-07T18:29:59Z"),
        (80, "2024-01-07T18:30:00Z"),
        (78, "2024-01-08T18:30:00Z"),
        (60, "2099-01-01T00:00:00Z"),
    ):
        post(client, "/weight", headers, {"weight_kg": weight, "recorded_at": stamp})
    for amount, stamp in (
        (100, "2024-01-07T18:29:59Z"),
        (200, "2024-01-07T18:30:00Z"),
        (300, "2024-01-08T18:29:59Z"),
        (400, "2024-01-08T18:30:00Z"),
    ):
        post(client, "/water", headers, {"amount_ml": amount, "logged_at": stamp})
    before = get(client, "/dashboard?date=2024-01-06", headers)
    assert before["weight_kg"] is None
    selected = get(client, "/dashboard?date=2024-01-08", headers)
    assert selected["weight_kg"] == 80
    assert selected["weight_date"] == "2024-01-08"
    assert selected["bmi"] == round(80 / 1.75 ** 2, 2)
    assert selected["totals"]["water_ml"] == 500
    assert len(selected["water_logs"]) == 2
    assert len(selected["weight_records"]) == 1
    carried = get(client, "/dashboard?date=2024-01-10", headers)
    assert carried["weight_kg"] == 78 and carried["weight_date"] == "2024-01-09"
    assert carried["weight_records"] == []
    assert get(client, "/profile", headers)["current_weight_kg"] == 78
    assert get(client, "/dashboard?date=2099-01-01", headers)["weight_kg"] == 78
    assert selected == get(client, "/history/day?date=2024-01-08", headers)


@pytest.mark.parametrize("path,payload", [
    ("/weight", {"weight_kg": 0, "recorded_at": "2024-01-08T00:00:00Z"}),
    ("/weight", {"weight_kg": "NaN", "recorded_at": "2024-01-08T00:00:00Z"}),
    ("/weight", {"weight_kg": 70, "recorded_at": "2024-01-08T00:00:00"}),
    ("/weight", {"weight_kg": 70, "recorded_at": 1704672000}),
    ("/water", {"amount_ml": -1, "logged_at": "2024-01-08T00:00:00Z"}),
    ("/water", {"amount_ml": 10001, "logged_at": "2024-01-08T00:00:00Z"}),
    ("/water", {"amount_ml": 100.5, "logged_at": "2024-01-08T00:00:00Z"}),
    ("/water", {"amount_ml": 100, "logged_at": "2024-01-08"}),
    ("/meals", meal_body(logged_at="2024-01-08T12:00:00")),
    ("/meals", meal_body(items=[])),
    ("/meals", meal_body(meal_type="invalid")),
    ("/supplements", {"name": " ", "dosage": 1, "unit": "pill"}),
    ("/supplements", {"name": "Iron", "dosage": -1, "unit": "mg"}),
])
def test_invalid_tracking_inputs(client, path, payload):
    headers = register(client)
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == 422, response.text
    assert get(client, path, headers) == []


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_raw_nonfinite_values_are_validation_errors_not_server_errors(client, value):
    headers = register(client)
    response = client.post(
        "/weight",
        headers={**headers, "Content-Type": "application/json"},
        content=f'{{"weight_kg":{value},"recorded_at":"2024-01-08T00:00:00Z"}}',
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "weight_kg"]
    assert "input" not in response.json()["detail"][0]
    assert get(client, "/weight", headers) == []


def test_password_validation_does_not_echo_secrets(client):
    response = client.post("/auth/register", json={"email": "test@example.com", "password": "Secret!"})
    assert response.status_code == 422
    assert "Secret!" not in response.text


def test_meal_portion_totals_update_delete_and_atomic_validation(client, db):
    headers = register(client)
    meal = post(client, "/meals", headers, meal_body())
    assert meal["totals"] == {"calories": 600, "protein": 25, "carbs": 90, "fat": 15, "fiber": 12}
    assert "user_id" not in meal and "meal_id" not in meal["items"][0]
    assert get(client, f"/meals/{meal['id']}", headers) == meal
    assert get(client, "/dashboard?date=2024-01-08", headers)["totals"]["calories"] == 600
    original_items = meal["items"]
    invalid = meal_body(image_id=2147483647, meal_type="dinner")
    assert client.put(f"/meals/{meal['id']}", headers=headers, json=invalid).status_code == 404
    assert get(client, f"/meals/{meal['id']}", headers)["items"] == original_items
    assert get(client, f"/meals/{meal['id']}", headers)["meal_type"] == "lunch"
    item = meal_body()["items"][0]
    for key, value in (("quantity", 0), ("calories", -1), ("protein", "NaN"), ("fat", "Infinity"), ("confidence", 1.1), ("food_name", " ")):
        assert client.put(f"/meals/{meal['id']}", headers=headers, json=meal_body(items=[{**item, key: value}])).status_code == 422
    response = client.put(f"/meals/{meal['id']}", headers=headers, json=meal_body(items=[{**item, "calories": 500}, item]))
    assert response.status_code == 200
    assert response.json()["totals"]["calories"] == 1100
    assert len(response.json()["items"]) == 2
    assert db.scalar(select(func.count()).select_from(MealItem)) == 2
    assert get(client, "/dashboard?date=2024-01-08", headers)["totals"]["protein"] == 50
    assert client.delete(f"/meals/{meal['id']}", headers=headers).status_code == 204
    assert client.get(f"/meals/{meal['id']}", headers=headers).status_code == 404
    assert get(client, "/meals", headers) == []
    assert db.scalar(select(func.count()).select_from(MealItem)) == 0
    assert get(client, "/dashboard?date=2024-01-08", headers)["totals"]["calories"] == 0


def test_water_delete_removes_only_selected_log(client):
    headers = register(client)
    first = post(client, "/water", headers, {"amount_ml": 250, "logged_at": "2024-01-08T10:00:00Z"})
    second = post(client, "/water", headers, {"amount_ml": 300, "logged_at": "2024-01-08T11:00:00Z"})
    assert client.delete(f"/water/{first['id']}", headers=headers).status_code == 204
    assert get(client, "/water", headers) == [second]
    assert get(client, "/dashboard?date=2024-01-08", headers)["totals"]["water_ml"] == 300
    assert client.delete(f"/water/{first['id']}", headers=headers).status_code == 404


def test_supplement_activation_history_not_taken_vs_unlogged(client, monkeypatch):
    headers = register(client)
    monkeypatch.setattr(api, "local_today", lambda db, user_id: date(2024, 1, 8))
    supplement = post(client, "/supplements", headers, {"name": "Vitamin D", "dosage": 1, "unit": "tablet"})
    identifier = supplement["id"]
    assert supplement["active_periods"] == [{"start": "2024-01-08", "end": None}]
    assert get(client, "/dashboard?date=2024-01-07", headers)["supplements"] == []
    assert get(client, "/dashboard?date=2024-01-08", headers)["supplements"][0]["status"] is None
    payload = {"supplement_id": identifier, "log_date": "2024-01-08", "taken": False}
    log = post(client, "/supplement-logs", headers, payload, expected=200)
    assert get(client, "/dashboard?date=2024-01-08", headers)["supplements"][0]["status"] == "not_taken"
    updated = post(client, "/supplement-logs", headers, {**payload, "taken": True}, expected=200)
    assert updated["id"] == log["id"]
    assert len(get(client, "/supplement-logs", headers)) == 1
    monkeypatch.setattr(api, "local_today", lambda db, user_id: date(2024, 1, 10))
    edit = {"name": "Vitamin D", "dosage": 1, "unit": "tablet", "active": False}
    response = client.put(f"/supplements/{identifier}", headers=headers, json=edit)
    assert response.status_code == 200
    assert response.json()["active_periods"] == [{"start": "2024-01-08", "end": "2024-01-10"}]
    assert get(client, "/dashboard?date=2024-01-08", headers)["supplements"][0]["active"] is True
    assert get(client, "/dashboard?date=2024-01-10", headers)["supplements"] == []
    history = get(client, "/history/week?date=2024-01-08", headers)["summary"]
    assert history["supplement_adherence"] == 50
    assert history["supplement_eligible_days"] == 2
    assert history["supplement_logged_days"] == 1
    assert history["supplement_data_coverage_percent"] == 50
    monkeypatch.setattr(api, "local_today", lambda db, user_id: date(2024, 1, 12))
    assert client.put(f"/supplements/{identifier}", headers=headers, json={**edit, "active": True}).status_code == 200
    periods = get(client, "/supplements", headers)[0]["active_periods"]
    assert periods == [{"start": "2024-01-08", "end": "2024-01-10"}, {"start": "2024-01-12", "end": None}]
    assert get(client, "/history/week?date=2024-01-08", headers)["summary"]["supplement_adherence"] == 20
    assert client.delete(f"/supplement-logs/{log['id']}", headers=headers).status_code == 204
    assert get(client, "/dashboard?date=2024-01-08", headers)["supplements"][0]["status"] is None
    assert get(client, "/history/week?date=2024-01-08", headers)["summary"]["supplement_logged_days"] == 0


def test_same_day_activation_does_not_double_expectations(client, monkeypatch):
    headers = register(client)
    monkeypatch.setattr(api, "local_today", lambda db, user_id: date(2024, 1, 8))
    supplement = post(client, "/supplements", headers, {"name": "Zinc", "dosage": 1, "unit": "dose"})
    payload = {"name": "Zinc", "dosage": 1, "unit": "dose", "active": False}
    for active in (False, True, True):
        response = client.put(f"/supplements/{supplement['id']}", headers=headers, json={**payload, "active": active})
        assert response.status_code == 200
    assert response.json()["active_periods"] == [{"start": "2024-01-08", "end": None}]
    assert get(client, "/history/week?date=2024-01-08", headers)["summary"]["supplement_eligible_days"] == 7


def test_missing_day_analytics_use_metric_coverage_not_zero_filling(client):
    headers = register(client)
    empty = get(client, "/history/week?date=2024-01-10", headers)
    assert empty["start"] == "2024-01-08" and empty["end"] == "2024-01-14"
    assert len(empty["days"]) == 7
    for key in ("average_calories", "average_protein", "average_water_ml", "starting_weight_kg",
                "latest_weight_kg", "weight_change_kg", "supplement_adherence", "weight_goal_progress_percent"):
        assert empty["summary"][key] is None
    assert empty["summary"]["tracking_consistency_percent"] == 0
    post(client, "/meals", headers, meal_body())
    item = meal_body()["items"][0]
    post(client, "/meals", headers, meal_body(logged_at="2024-01-10T12:00:00+05:30", items=[{**item, "calories": 400, "protein": 15}]))
    post(client, "/water", headers, {"amount_ml": 200, "logged_at": "2024-01-09T10:00:00Z"})
    post(client, "/water", headers, {"amount_ml": 300, "logged_at": "2024-01-09T11:00:00Z"})
    post(client, "/weight", headers, {"weight_kg": 80, "recorded_at": "2024-01-08T10:00:00Z"})
    post(client, "/weight", headers, {"weight_kg": 78, "recorded_at": "2024-01-12T10:00:00Z"})
    history = get(client, "/history/week?date=2024-01-11", headers)
    summary = history["summary"]
    assert summary["average_calories"] == 500
    assert summary["average_protein"] == 20
    assert summary["average_water_ml"] == 500
    assert summary["days_tracked"] == 4
    assert summary["meals_logged"] == 2
    assert summary["calorie_days_logged"] == 2 and summary["water_days_logged"] == 1
    assert summary["starting_weight_kg"] == 80 and summary["latest_weight_kg"] == 78
    assert summary["weight_change_kg"] == -2
    assert summary["tracking_consistency_percent"] == round(4 / 7 * 100, 2)
    missing_day = next(day for day in history["days"] if day["date"] == "2024-01-11")
    assert missing_day["totals"]["calories"] == 0
    assert missing_day["weight_kg"] == 80 and missing_day["weight_records"] == []


def test_calendar_month_leap_year_and_future_denominators(client, monkeypatch):
    headers = register(client)
    month = get(client, "/history/month?date=2024-02-15", headers)
    assert month["start"] == "2024-02-01" and month["end"] == "2024-02-29"
    assert len(month["days"]) == 29
    monkeypatch.setattr(tracking, "local_today", lambda db, user_id: date(2024, 1, 10))
    post(client, "/water", headers, {"amount_ml": 250, "logged_at": "2024-01-10T12:00:00Z"})
    post(client, "/water", headers, {"amount_ml": 900, "logged_at": "2024-01-11T12:00:00Z"})
    week = get(client, "/history/week?date=2024-01-08", headers)["summary"]
    assert week["eligible_calendar_days"] == 3
    assert week["days_tracked"] == 1
    assert week["average_water_ml"] == 250
    assert week["tracking_consistency_percent"] == round(100 / 3, 2)
    future = get(client, "/history/week?date=2024-01-15", headers)["summary"]
    assert future["eligible_calendar_days"] == 0
    assert future["tracking_consistency_percent"] is None
    assert future["average_water_ml"] is None


@pytest.mark.parametrize("baseline,goal,latest,progress", [
    (80, 70, 75, 50), (60, 70, 65, 50), (80, 70, 85, 0),
    (80, 70, 65, 100), (70, 70, 70, 100), (70, 70, 72, None),
])
def test_weight_goal_loss_gain_maintenance_and_clamping(client, baseline, goal, latest, progress):
    headers = register(client)
    set_profile(client, headers, current_weight_kg=baseline, goal_weight_kg=goal)
    clear_weights(client, headers)
    post(client, "/weight", headers, {"weight_kg": baseline, "recorded_at": "2024-01-01T12:00:00Z"})
    post(client, "/weight", headers, {"weight_kg": latest, "recorded_at": "2024-01-12T12:00:00Z"})
    summary = get(client, "/history/week?date=2024-01-08", headers)["summary"]
    assert summary["starting_weight_kg"] == baseline
    assert summary["latest_weight_kg"] == latest
    assert summary["weight_goal_progress_percent"] == progress


def test_first_and_last_same_day_weight_produce_change(client):
    headers = register(client)
    post(client, "/weight", headers, {"weight_kg": 80, "recorded_at": "2024-01-08T05:00:00Z"})
    post(client, "/weight", headers, {"weight_kg": 79, "recorded_at": "2024-01-08T12:00:00Z"})
    summary = get(client, "/history/week?date=2024-01-08", headers)["summary"]
    assert summary["starting_weight_kg"] == 80
    assert summary["latest_weight_kg"] == 79
    assert summary["weight_change_kg"] == -1


def test_timezone_dst_day_bounds(client):
    headers = register(client)
    set_profile(client, headers, timezone="America/New_York")
    clear_weights(client, headers)
    for stamp, amount in (
        ("2024-03-10T04:59:59Z", 100), ("2024-03-10T05:00:00Z", 200),
        ("2024-03-11T03:59:59Z", 300), ("2024-03-11T04:00:00Z", 400),
    ):
        post(client, "/water", headers, {"amount_ml": amount, "logged_at": stamp})
    dashboard = get(client, "/dashboard?date=2024-03-10", headers)
    assert dashboard["totals"]["water_ml"] == 500
    assert len(dashboard["water_logs"]) == 2


def test_all_records_and_relationship_references_are_user_scoped(client, two_users, monkeypatch):
    owner, other = two_users
    set_profile(client, owner)
    weight = post(client, "/weight", owner, {"weight_kg": 75, "recorded_at": "2024-01-08T10:00:00Z"})
    water = post(client, "/water", owner, {"amount_ml": 500, "logged_at": "2024-01-08T10:00:00Z"})
    meal = post(client, "/meals", owner, meal_body())
    monkeypatch.setattr(api, "local_today", lambda db, user_id: date(2024, 1, 8))
    supplement = post(client, "/supplements", owner, {"name": "Private supplement", "dosage": 1, "unit": "dose"})
    log_body = {"supplement_id": supplement["id"], "log_date": "2024-01-08", "taken": True}
    log = post(client, "/supplement-logs", owner, log_body, expected=200)
    assert client.put("/targets", headers=owner, json={
        "effective_date": "2024-01-08", "calories_target": 2000, "protein_target": 100, "water_target_ml": 2500,
    }).status_code == 200
    assert client.get("/profile", headers=other).status_code == 404
    assert get(client, "/targets?date=2024-01-08", other) is None
    for path in ("/weight", "/water", "/meals", "/supplements", "/supplement-logs"):
        assert get(client, path, other) == []
    for path, identifier in (("/weight", weight["id"]), ("/water", water["id"]), ("/meals", meal["id"]), ("/supplement-logs", log["id"])):
        assert client.delete(f"{path}/{identifier}", headers=other).status_code == 404
    assert client.get(f"/meals/{meal['id']}", headers=other).status_code == 404
    assert client.put(f"/meals/{meal['id']}", headers=other, json=meal_body()).status_code == 404
    assert client.put(f"/supplements/{supplement['id']}", headers=other, json={
        "name": "Stolen", "dosage": 2, "unit": "dose", "active": False,
    }).status_code == 404
    assert client.post("/supplement-logs", headers=other, json=log_body).status_code == 404
    dashboard = get(client, "/dashboard?date=2024-01-08", other)
    assert dashboard["weight_kg"] is None
    assert dashboard["targets"] is None
    assert dashboard["meals"] == [] and dashboard["supplements"] == []
    assert all(value == 0 for value in dashboard["totals"].values())
    history = get(client, "/history/week?date=2024-01-08", other)["summary"]
    assert history["days_tracked"] == 0 and history["supplement_adherence"] is None
    assert get(client, "/dashboard?date=2024-01-08", owner)["totals"]["calories"] == 600
    assert get(client, "/supplement-logs", owner)[0]["taken"] is True


def test_owned_meal_images_and_invalid_references_leave_no_changes(client, two_users, db):
    owner, other = two_users
    owner_id = get(client, "/auth/me", owner)["id"]
    other_id = get(client, "/auth/me", other)["id"]
    owned_image = StoredImage(user_id=owner_id, object_key="test-owner-image", content_type="image/jpeg")
    foreign_image = StoredImage(user_id=other_id, object_key="test-other-image", content_type="image/jpeg")
    db.add_all([owned_image, foreign_image])
    db.commit()
    assert client.post("/meals", headers=owner, json=meal_body(image_id=foreign_image.id)).status_code == 404
    assert get(client, "/meals", owner) == []
    meal = post(client, "/meals", owner, meal_body(image_id=owned_image.id))
    assert meal["image_id"] == owned_image.id
    response = client.put(f"/meals/{meal['id']}", headers=owner, json=meal_body(image_id=foreign_image.id, meal_type="dinner"))
    assert response.status_code == 404
    unchanged = get(client, f"/meals/{meal['id']}", owner)
    assert unchanged["image_id"] == owned_image.id
    assert unchanged["meal_type"] == "lunch"
    assert unchanged["items"] == meal["items"]
    response = client.put(f"/meals/{meal['id']}", headers=owner, json=meal_body(image_id=None))
    assert response.status_code == 200 and response.json()["image_id"] is None
