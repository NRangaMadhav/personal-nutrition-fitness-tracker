from datetime import date

import pytest
from pydantic import ValidationError

from backend.app.ai.schemas import CoachRequest, LogRequest, WaterEvent


@pytest.mark.parametrize("timestamp", [
    "9999-12-31T23:59:00+00:00", "0001-01-01T00:00:00+00:00", "2026-09-08T12:00:00", 1788854400,
])
def test_ai_dates_follow_manual_tracking_validation(timestamp):
    with pytest.raises(ValidationError):
        WaterEvent(type="water", amount_ml=500, occurred_at=timestamp)
    with pytest.raises(ValidationError):
        LogRequest(text="water 500 ml", occurred_at=timestamp)


def test_ai_dates_normalize_to_utc():
    event = WaterEvent(type="water", amount_ml=500, occurred_at="2026-09-08T12:00:00+05:30")
    assert event.occurred_at.isoformat() == "2026-09-08T06:30:00+00:00"


def test_coach_rejects_dates_that_cannot_be_safely_aggregated():
    with pytest.raises(ValidationError):
        CoachRequest(question="How did I do?", start_date=date(9999, 12, 31), end_date=date(9999, 12, 31))


def test_coach_excludes_future_supplement_expectations(db, monkeypatch):
    from uuid import uuid4

    from backend.app import tracking
    from backend.app.ai import workflows
    from backend.app.ai.providers import AdapterResult
    from backend.app.ai.schemas import CoachInterpretation
    from backend.app.models import Supplement, User

    user = User(email=f"coach-dates-{uuid4().hex}@example.com", password_hash="not-used")
    db.add(user)
    db.flush()
    db.add(Supplement(user_id=user.id, name="Example", dosage=1, unit="dose", active=True,
                      active_periods=[{"start": "2026-09-01", "end": None}]))
    db.flush()
    monkeypatch.setattr(tracking, "local_today", lambda *args: date(2026, 9, 2))
    monkeypatch.setattr(workflows, "invoke_provider", lambda *args, **kwargs: AdapterResult(
        CoachInterpretation(answer="No measurements were logged."), "groq", "mock",
    ))
    result = workflows.run_coach(db, user.id, CoachRequest(
        question="How consistent was I?", start_date=date(2026, 9, 1), end_date=date(2026, 9, 4),
    ))
    assert [day["date"] for day in result["facts"]["days"]] == ["2026-09-01", "2026-09-02"]
    assert result["facts"]["summary"]["supplement_eligible_days"] == 2


def test_coach_repairs_unverified_number_format_once(db, monkeypatch):
    from uuid import uuid4

    from backend.app import tracking
    from backend.app.ai import workflows
    from backend.app.ai.providers import AdapterResult
    from backend.app.ai.schemas import CoachInterpretation
    from backend.app.models import User

    user = User(email=f"coach-repair-{uuid4().hex}@example.com", password_hash="not-used")
    db.add(user)
    db.flush()
    monkeypatch.setattr(tracking, "local_today", lambda *args: date(2026, 9, 2))
    replies = [
        CoachInterpretation(answer="You drank 9000 ml.", metric_references=[]),
        CoachInterpretation(answer="Your logged water is {{summary.totals.water_ml}} ml.",
                            metric_references=["summary.totals.water_ml"]),
    ]
    calls = []

    def provider(messages, *args, **kwargs):
        calls.append(messages)
        return AdapterResult(replies[len(calls) - 1], "nvidia", "mock")

    monkeypatch.setattr(workflows, "invoke_provider", provider)
    result = workflows.run_coach(db, user.id, CoachRequest(
        question="Water?", start_date=date(2026, 9, 2), end_date=date(2026, 9, 2),
    ))
    assert len(calls) == 2
    assert "9000" not in result["answer"]
    assert "0 ml" in result["answer"]
    assert len(calls[1]) > len(calls[0])
