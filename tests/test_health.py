import psycopg
import pytest
from fastapi.testclient import TestClient

from api import main
from core.config.settings import get_settings

client = TestClient(main.app)


def test_health_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "check_db", lambda dsn: {"db": "ok", "pgvector": "0.8.0"})
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["integrations"] == "mock"  # mocks are the default (ADR)
    assert body["outbound_mode"] == "off"  # outbound off by default (LEAD-017)


def test_health_db_down_does_not_leak_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(dsn: str) -> dict[str, str]:
        raise psycopg.OperationalError(f"could not connect to {dsn}")

    monkeypatch.setattr(main, "check_db", boom)
    r = client.get("/health")
    assert r.status_code == 503
    assert get_settings().database_url not in r.text


def test_health_missing_pgvector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "check_db", lambda dsn: {"db": "ok", "pgvector": None})
    assert client.get("/health").status_code == 503


@pytest.mark.integration
def test_real_db_has_pgvector() -> None:
    result = main.check_db(get_settings().database_url)
    assert result["pgvector"] is not None
