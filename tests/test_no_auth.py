import pytest

from app.core.config import settings


@pytest.fixture
def no_auth(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)


def test_config_reports_auth_mode(client, no_auth):
    assert client.get("/api/auth/config").json()["auth_enabled"] is False


def test_role_header_replaces_login(client, no_auth):
    assert client.get("/api/auth/me").json()["role"] == "vendor"            # default role
    assert client.get("/api/auth/me", headers={"X-Role": "qc"}).json()["role"] == "qc"
    assert client.get("/api/pos").status_code == 200                       # no token needed
    assert client.get("/api/auth/me", headers={"X-Role": "boss"}).status_code == 400


def test_role_rules_still_apply(client, no_auth):
    # only the approver may decide; a vendor still gets 403
    r = client.post("/api/inspections/1/decision", headers={"X-Role": "vendor"}, json={"decision": "passed"})
    assert r.status_code == 403
    # dashboard is for QC roles only
    assert client.get("/api/dashboard/summary", headers={"X-Role": "vendor"}).status_code == 403
    assert client.get("/api/dashboard/summary", headers={"X-Role": "qc"}).status_code == 200


def test_login_required_when_enabled(client):
    assert settings.auth_enabled is True
    assert client.get("/api/pos").status_code == 401
