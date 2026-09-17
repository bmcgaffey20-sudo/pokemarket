import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from database import Database, AccountAction, User
from main import app, require_database, settings


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'recovery.db'}")
    db.initialize()
    app.dependency_overrides[require_database] = lambda: db
    monkeypatch.setattr(settings, "auth_secret", "test-secret-that-is-more-than-32-characters")
    monkeypatch.setattr(settings, "password_hash_iterations", 10000)
    monkeypatch.setattr(settings, "mailjet_api_key", "test-api-key")
    monkeypatch.setattr(settings, "mailjet_secret_key", "test-secret-key")
    monkeypatch.setattr(settings, "email_from", "PokeMarket <mail@example.com>")
    messages = []
    class MailClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            assert url == "https://api.mailjet.com/v3.1/send"
            import httpx
            request = next(kwargs["auth"].auth_flow(httpx.Request("POST", url)))
            import base64
            assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"test-api-key:test-secret-key").decode()
            message = kwargs["json"]["Messages"][0]
            assert message["From"] == {"Email": "mail@example.com", "Name": "PokeMarket"}
            assert message["To"] == [{"Email": "owner@example.com"}]
            assert message["TrackClicks"] == message["TrackOpens"] == "disabled"
            messages.append(message)
            return type("Response", (), {"status_code": 200, "json": lambda self: {"Messages": [{"Status": "success"}]}})()
    monkeypatch.setattr("recovery.httpx.AsyncClient", MailClient)
    client = TestClient(app)
    result = client.post("/api/v1/auth/register", json={"email": "owner@example.com", "display_name": "Owner", "password": "original-password"})
    assert result.status_code == 201
    user = result.json()
    try:
        yield client, db, messages, user
    finally:
        app.dependency_overrides.clear()
        db.engine.dispose()


def token(message):
    return re.search(r"/account/(?:verify|reset)#([A-Za-z0-9_-]+)", message["TextPart"]).group(1)


def headers(user):
    return {"Authorization": "Bearer " + user["access_token"]}


def test_verification_is_explicit_single_use_and_private(recovery):
    client, db, messages, user = recovery
    assert not user["user"]["email_verified"]
    verification = token(messages[0])
    page = client.get("/account/verify")
    assert page.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert not db.get_user(user["user"]["id"])["email_verified"]
    with db.sessions() as session:
        row = session.get(AccountAction, hashlib.sha256(verification.encode()).hexdigest())
        assert row and row.token_hash != verification
    assert client.post("/api/v1/auth/verify-email", json={"token": verification}).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers(user)).json()["email_verified"]
    assert client.post("/api/v1/auth/verify-email", json={"token": verification}).status_code == 400
    assert client.post("/api/v1/auth/send-verification").status_code == 401


def test_reset_invalidates_all_sessions_and_links(recovery):
    client, db, messages, user = recovery
    other_session = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "original-password"}).json()
    verify_token = token(messages[0])
    request = client.post("/api/v1/auth/forgot-password", json={"email": "owner@example.com"})
    assert request.status_code == 202
    first = token(messages[-1])
    client.post("/api/v1/auth/forgot-password", json={"email": "owner@example.com"})
    second = token(messages[-1])
    assert client.post("/api/v1/auth/verify-email", json={"token": first}).status_code == 400
    reset = client.post("/api/v1/auth/reset-password", json={"token": first, "password": "new-secure-password"})
    assert reset.status_code == 200
    for session in (user, other_session):
        assert client.get("/api/v1/auth/me", headers=headers(session)).status_code == 401
        assert client.get("/api/v1/listings", headers=headers(session)).status_code == 401
    for used in (first, second):
        assert client.post("/api/v1/auth/reset-password", json={"token": used, "password": "another-password"}).status_code == 400
    assert client.post("/api/v1/auth/verify-email", json={"token": verify_token}).status_code == 400
    assert client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "original-password"}).status_code == 401
    new = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "new-secure-password"})
    assert new.status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers(new.json())).status_code == 200


def test_unknown_account_has_same_recovery_response(recovery):
    client, _, messages, _ = recovery
    known = client.post("/api/v1/auth/forgot-password", json={"email": "owner@example.com"})
    count = len(messages)
    unknown = client.post("/api/v1/auth/forgot-password", json={"email": "missing@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert len(messages) == count


def test_expiry_and_sensitive_validation(recovery):
    client, db, messages, _ = recovery
    value = token(messages[0])
    with db.sessions.begin() as session:
        session.get(AccountAction, hashlib.sha256(value.encode()).hexdigest()).expires_at = int(time.time()) - 1
    assert client.post("/api/v1/auth/verify-email", json={"token": value}).status_code == 400
    result = client.post("/api/v1/auth/reset-password", json={"token": value, "password": "SECRET"})
    assert result.status_code == 422
    assert "SECRET" not in result.text and value not in result.text


def test_limits_apply_to_login_recovery_and_verification(recovery):
    client, _, _, user = recovery
    for _ in range(10):
        assert client.post("/api/v1/auth/login", json={"email": "missing@example.com", "password": "wrong-password"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "missing@example.com", "password": "wrong-password"}).status_code == 429
    for _ in range(3):
        assert client.post("/api/v1/auth/forgot-password", json={"email": "owner@example.com"}).status_code == 202
        assert client.post("/api/v1/auth/send-verification", headers=headers(user)).status_code == 202
    assert client.post("/api/v1/auth/forgot-password", json={"email": "owner@example.com"}).status_code == 429
    assert client.post("/api/v1/auth/send-verification", headers=headers(user)).status_code == 429


def test_rate_limit_is_atomic_persistent_and_expires(recovery):
    _, db, _, _ = recovery
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: db.take_auth_rate("burst", 3, 900, now=1800), range(12)))
    assert sum(results) == 3
    other = Database(str(db.engine.url))
    assert not other.take_auth_rate("burst", 3, 900, now=1801)
    assert other.take_auth_rate("burst", 3, 900, now=2700)
    other.engine.dispose()


def test_recovery_configuration_missing_is_actionable(recovery, monkeypatch):
    client, _, _, _ = recovery
    monkeypatch.setattr(settings, "mailjet_secret_key", None)
    assert client.post("/api/v1/auth/forgot-password", json={"email": "owner@example.com"}).status_code == 503
    assert client.get("/api/v1/health").json()["email"] == "not_configured"


def test_user_migration_preserves_existing_accounts(recovery):
    _, db, _, user = recovery
    with db.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE users DROP COLUMN email_verified")
        connection.exec_driver_sql("ALTER TABLE users DROP COLUMN session_version")
    db.initialize()
    db.initialize()
    migrated = db.get_user(user["user"]["id"])
    assert migrated["email"] == "owner@example.com"
    assert migrated["session_version"] == 0
    assert not migrated["email_verified"]


def test_concurrent_redemption_only_changes_password_once(recovery):
    client, db, messages, user = recovery
    client.post("/api/v1/auth/forgot-password", json={"email": "owner@example.com"})
    digest = hashlib.sha256(token(messages[-1]).encode()).hexdigest()
    from auth import hash_password
    password = hash_password("concurrent-password", 10000)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: db.consume_account_action(digest, "reset", password), range(2)))
    assert sum(results) == 1
    assert db.get_user(user["user"]["id"])["session_version"] == 1
