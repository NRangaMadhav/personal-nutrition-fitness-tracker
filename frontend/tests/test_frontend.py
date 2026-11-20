"""Offline tests only: all HTTP is mocked and no model or database is contacted."""

import copy
import sys
from datetime import date, time
from pathlib import Path

import httpx
import pytest

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))

from api_client import ApiClient, ApiError
from validation import clean_events, clean_items, complete_history_days, history_chart_rows, local_timestamp


def food(**overrides):
    return {"food_name": "Rice", "quantity": 1.0, "unit": "bowl", "calories": 200.0, "protein": 4.0, "carbs": 40.0, "fat": 1.0, "fiber": 1.0, **overrides}


def test_portion_totals_are_not_multiplied():
    assert clean_items([food(quantity=2)]) == [food(quantity=2.0)]


@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf"), True, ""])
def test_missing_invalid_nutrition_rejected(value):
    with pytest.raises(ValueError):
        clean_items([food(calories=value)])


def test_event_confirmation_requires_aware_times_and_explicit_status():
    with pytest.raises(ValueError, match="timezone"):
        clean_events([{"type": "water", "occurred_at": "2026-09-08T12:30:00", "amount_ml": 250}])
    with pytest.raises(ValueError, match="taken or not taken"):
        clean_events([{"type": "supplement", "occurred_at": "2026-09-08T12:30:00+05:30", "name": "Vitamin D", "taken": None}])
    events = clean_events([{"type": "meal", "occurred_at": "2026-09-08T12:30:00+05:30", "meal_type": "lunch", "items": [food(confidence=0.5)]}])
    assert "confidence" not in events[0]["items"][0]


def test_selected_day_timezone_and_nonexistent_dst():
    assert local_timestamp(date(2026, 9, 8), time(12, 30), "Asia/Kolkata") == "2026-09-08T12:30:00+05:30"
    with pytest.raises(ValueError, match="does not exist"):
        local_timestamp(date(2026, 3, 8), time(2, 30), "America/New_York")


def test_history_preserves_missing_weights_and_zero_log_days():
    days = complete_history_days([
        {"date": "2026-09-08", "weight_kg": 65, "weight_date": "2026-09-07", "totals": {"calories": 1200},
         "weight_records": [], "supplements": [{"active": True, "status": "taken"}, {"active": True, "status": None}]},
        {"date": "2026-09-10", "weight_records": [{"weight_kg": 64.5, "recorded_at": "2026-09-10T10:00:00+05:30"}]},
    ], "2026-09-08", "2026-09-10")
    rows = history_chart_rows(days)
    assert len(rows) == 3
    assert rows[0]["weight_kg"] is None
    assert rows[0]["adherence"] == 50
    assert rows[1]["calories"] == 0
    assert rows[1]["weight_kg"] is None
    assert rows[2]["weight_kg"] == 64.5


def test_weight_chart_does_not_connect_across_unmeasured_days(monkeypatch):
    import components

    rendered = {}
    monkeypatch.setattr(components.st, "markdown", lambda *args, **kwargs: None)
    monkeypatch.setattr(components.st, "vega_lite_chart", lambda **kwargs: rendered.update(kwargs))
    components.trend_chart([
        {"date": "2026-09-06", "weight_kg": 65},
        {"date": "2026-09-07", "weight_kg": 64.9},
        {"date": "2026-09-08", "weight_kg": None},
        {"date": "2026-09-09", "weight_kg": 64.8},
    ], "weight_kg", "Weight measurements", "kg")
    points = rendered["spec"]["data"]["values"]
    assert [point["weight_kg"] for point in points] == [65, 64.9, 64.8]
    assert points[0]["_segment"] == points[1]["_segment"]
    assert points[1]["_segment"] != points[2]["_segment"]
    assert rendered["spec"]["encoding"]["detail"]["field"] == "_segment"


def test_http_auth_timeout_and_error_redaction():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = ApiClient("session-token", "http://offline.test", transport=httpx.MockTransport(handler))
    assert client.post("/ai-log", ai=True, json={"text": "test"}) == {"ok": True}
    assert requests[0].headers["Authorization"] == "Bearer session-token"
    assert requests[0].extensions["timeout"]["read"] == 180
    client = ApiClient(base_url="http://offline.test", transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"detail": "secret service details"})))
    with pytest.raises(ApiError) as exc:
        client.get("/dashboard")
    assert "secret service details" not in str(exc.value)


def test_http_401_and_missing_profile():
    client = ApiClient(base_url="http://offline.test", transport=httpx.MockTransport(lambda _: httpx.Response(401)))
    with pytest.raises(ApiError) as exc:
        client.get("/auth/me")
    assert exc.value.status == 401
    client = ApiClient(base_url="http://offline.test", transport=httpx.MockTransport(lambda _: httpx.Response(404)))
    assert client.get("/profile", missing_ok=True) is None


@pytest.mark.parametrize("code,expected", [
    ("ai_upload_timeout", "Uploading the image"),
    ("ai_invalid_output", "required nutrition format"),
    ("ai_not_configured", "vision-capable"),
    ("ai_model_unavailable", "model is unavailable"),
    ("ai_rate_limited", "rate limit"),
])
def test_safe_ai_error_codes_explain_failure_without_exposing_provider_body(code, expected):
    client = ApiClient(
        base_url="http://offline.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(
            503, json={"detail": {"code": code, "message": "private provider response"}}
        )),
    )
    with pytest.raises(ApiError) as exc:
        client.post("/ai-log", ai=True, json={"text": "synthetic"})
    assert expected in str(exc.value)
    assert "No tracking records were confirmed" in str(exc.value)
    assert "private provider response" not in str(exc.value)


@pytest.mark.parametrize("detail", [
    {"code": "unknown", "message": "private provider response"},
    {"code": [], "message": "private provider response"},
    "private provider response",
])
def test_unknown_server_errors_stay_redacted(detail):
    client = ApiClient(
        base_url="http://offline.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"detail": detail})),
    )
    with pytest.raises(ApiError) as exc:
        client.get("/dashboard")
    assert "private provider response" not in str(exc.value)


PROFILE = {
    "date_of_birth": "2000-01-01", "sex": "male", "height_cm": 170.0,
    "current_weight_kg": 65.0, "goal_weight_kg": None, "timezone": "Asia/Kolkata",
}
TARGETS = {"effective_date": "2026-01-01", "calories_target": 2000.0, "protein_target": 80.0, "water_target_ml": 2500}


def dashboard(day="2026-09-08"):
    return {
        "date": day, "weight_kg": 65.0, "weight_date": "2026-09-07", "bmi": 22.5,
        "totals": {"calories": 3000, "protein": 90, "carbs": 300, "fat": 70, "fiber": 20, "water_ml": 3000},
        "targets": TARGETS, "meals": [], "water_logs": [], "weight_records": [], "supplements": [], "supplement_logs": [],
    }


@pytest.fixture
def offline_app(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    calls = []

    def request(self, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/auth/me":
            return {"id": 1, "email": "tester@example.com"}
        if path == "/profile":
            return copy.deepcopy(PROFILE)
        if path == "/targets":
            return copy.deepcopy(TARGETS)
        if path == "/supplements":
            return []
        if path == "/dashboard" or path == "/history/day":
            return dashboard(kwargs.get("params", {}).get("date", "2026-09-08"))
        if path in {"/history/week", "/history/month"}:
            return {
                "start": "2026-09-07", "end": "2026-09-13",
                "summary": {"days_tracked": 1, "meals_logged": 1, "average_calories": 3000 / 7},
                "days": [dashboard()],
            }
        if path == "/ai-log/confirm":
            return {"status": "confirmed"}
        if path == "/water" and method == "POST":
            return {"id": 1, **kwargs["json"]}
        raise AssertionError(f"Unexpected mocked API call: {method} {path}")

    monkeypatch.setattr(ApiClient, "request", request)
    app = AppTest.from_file(str(FRONTEND / "app.py"), default_timeout=20)
    app.session_state["access_token"] = "offline-token"
    return app, calls


def assert_clean(app):
    assert not app.exception, [str(item.value) for item in app.exception]
    assert not app.error, [item.value for item in app.error]


def test_dashboard_navigation_and_water_are_offline(offline_app):
    app, calls = offline_app
    app.run()
    assert_clean(app)
    assert any("150%" in item.value for item in app.markdown)
    app.button(key="quick_water_250").click().run()
    assert_clean(app)
    water = next(kwargs["json"] for method, path, kwargs in calls if method == "POST" and path == "/water")
    assert water["amount_ml"] == 250
    assert water["logged_at"].endswith("+05:30")
    for page in ("History", "AI Coach", "Settings", "Dashboard"):
        app.radio(key="navigation").set_value(page).run()
        assert_clean(app)


def test_history_week_charts_render_offline(offline_app):
    app, _ = offline_app
    app.run()
    app.radio(key="navigation").set_value("History").run()
    app.radio(key="history_period").set_value("Week").run()
    assert_clean(app)
    assert any("7 calendar days" in item.value for item in app.info)


def test_log_dates_follow_changed_dashboard_day(offline_app):
    app, _ = offline_app
    app.run()
    selected = date(2026, 8, 17)
    app.date_input(key="dashboard_day").set_value(selected).run()
    assert_clean(app)
    for key in ("manual_meal_0_date", "water_date", "weight_date", "ai_input_0_date"):
        assert app.date_input(key=key).value == selected


def test_draft_never_auto_saves_and_survives_navigation(offline_app):
    app, calls = offline_app
    app.session_state["ai_draft"] = {"id": 4, "status": "draft", "provider": "test", "model": "offline", "warnings": []}
    app.session_state["ai_draft_events"] = [{"key": "water", "event": {"type": "water", "occurred_at": "2026-09-08T12:00:00+05:30", "amount_ml": None}}]
    app.session_state["ai_draft_snapshots"] = {}
    app.run()
    assert_clean(app)
    assert not any(path == "/ai-log/confirm" for _, path, _ in calls)
    app.number_input(key="ui_draft_4_water_water").set_value(500).run()
    app.radio(key="navigation").set_value("History").run()
    app.radio(key="navigation").set_value("Dashboard").run()
    assert_clean(app)
    assert app.number_input(key="ui_draft_4_water_water").value == 500
    app.checkbox(key="ui_draft_4_ack").check().run()
    app.button(key="confirm_ai_draft").click().run()
    assert_clean(app)
    saved = next(kwargs["json"] for _, path, kwargs in calls if path == "/ai-log/confirm")
    assert saved["events"][0]["amount_ml"] == 500


def test_meal_draft_editor_retains_corrections_across_navigation(offline_app):
    app, _ = offline_app
    app.session_state["ai_draft"] = {"id": 5, "status": "draft", "provider": "test", "model": "offline", "warnings": []}
    app.session_state["ai_draft_events"] = [{
        "key": "meal", "event": {"type": "meal", "occurred_at": "2026-09-08T12:00:00+05:30", "meal_type": "lunch", "items": [food(calories=None)]},
    }]
    app.session_state["ai_draft_snapshots"] = {}
    app.run()
    assert_clean(app)
    app.session_state["ui_draft_5_meal_items"] = {"edited_rows": {0: {"calories": 350.0}}, "added_rows": [], "deleted_rows": []}
    app.run()
    assert_clean(app)
    assert app.session_state["ai_draft_reviewed"][0]["items"][0]["calories"] == 350
    app.radio(key="navigation").set_value("History").run()
    app.radio(key="navigation").set_value("Dashboard").run()
    assert_clean(app)
    assert app.session_state["ai_draft_reviewed"][0]["items"][0]["calories"] == 350


def test_empty_meal_cannot_be_saved(offline_app):
    app, calls = offline_app
    app.run()
    next(button for button in app.button if button.label == "Save meal").click().run()
    assert any("name and portion unit" in item.value for item in app.error)
    assert not any(path == "/meals" and method == "POST" for method, path, _ in calls)


def test_profile_onboarding_renders_without_targets(offline_app, monkeypatch):
    app, _ = offline_app
    previous = ApiClient.request

    def without_profile(self, method, path, **kwargs):
        if path == "/profile":
            return None
        return previous(self, method, path, **kwargs)

    monkeypatch.setattr(ApiClient, "request", without_profile)
    app.run()
    assert_clean(app)
    assert any("Step 1 of 2" in item.value for item in app.markdown)


def test_manual_targets_onboarding_requires_confirmation(offline_app, monkeypatch):
    app, calls = offline_app
    previous = ApiClient.request

    def without_targets(self, method, path, **kwargs):
        if path == "/targets" and method == "GET":
            return None
        return previous(self, method, path, **kwargs)

    monkeypatch.setattr(ApiClient, "request", without_targets)
    app.run()
    assert_clean(app)
    assert any("Step 2 of 2" in item.value for item in app.markdown)
    next(button for button in app.button if button.label == "Confirm & save targets").click().run()
    assert any("confirm" in item.value.lower() for item in app.warning)
    assert not any(path == "/targets" and method == "PUT" for method, path, _ in calls)


def test_expired_session_clears_private_state(offline_app, monkeypatch):
    app, _ = offline_app
    app.session_state["private_note"] = "not shared"

    def unauthorized(self, *args, **kwargs):
        raise ApiError("Expired", 401)

    monkeypatch.setattr(ApiClient, "request", unauthorized)
    app.run()
    assert not app.exception
    assert "access_token" not in app.session_state
    assert "private_note" not in app.session_state
    assert any("expired" in notice.value for notice in app.info)
