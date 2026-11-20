from unittest.mock import patch

from sqlalchemy.exc import OperationalError


def test_health_reports_database_connection(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


def test_health_reports_database_outage(client):
    failure = OperationalError("SELECT 1", {}, ConnectionError("offline"))
    with patch("backend.app.main.engine.connect", side_effect=failure):
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
