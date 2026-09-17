import asyncio
import re
import sqlite3
import uuid
import pytest

from fastapi.testclient import TestClient
from auth import create_access_token, decode_access_token, hash_password, verify_password
from database import Database, normalize_database_url
from main import app, apply_grading_safeguards, read_images, require_database, settings
from storage import R2Storage

client = TestClient(app)

def test_health():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["version"] == "2.8.1-connect-sync"
    assert r.json()["database"] in {"not_configured", "connected"}
    assert r.json()["auth"] in {"not_configured", "configured"}


def test_connect_return_pages_reopen_android_app():
    completed = client.get("/api/v1/payments/connect/return")
    assert completed.status_code == 200
    assert "pokemarket://account?stripe=return" in completed.text
    assert completed.headers["cache-control"] == "no-store"

    expired = client.get("/api/v1/payments/connect/refresh")
    assert expired.status_code == 200
    assert "pokemarket://account?stripe=refresh" in expired.text


def test_structural_damage_caps_optimistic_grade():
    condition = apply_grading_safeguards(
        {
            "estimated_condition": "Near Mint",
            "confidence": 0.93,
            "structural_damage_detected": True,
            "major_defects": [
                {
                    "type": "crease",
                    "severity": "moderate",
                    "location": "top center",
                    "evidence": "defect close-up",
                }
            ],
            "warnings": [],
        },
        [(b"jpeg", "image/jpeg", "defect_bend_crease_1")],
    )
    assert condition["estimated_condition"] == "Moderately Played"


def test_near_mint_confidence_is_capped_without_closeup():
    condition = apply_grading_safeguards(
        {
            "estimated_condition": "Near Mint",
            "confidence": 95,
            "structural_damage_detected": False,
            "major_defects": [],
            "warnings": [],
        },
        [(b"jpeg", "image/jpeg", "required_front_straight")],
    )
    assert condition["confidence"] == 0.75


class FakeUpload:
    content_type = "image/jpeg"

    def __init__(self, filename):
        self.filename = filename

    async def read(self):
        return b"jpeg"


def test_read_images_keeps_forced_angle_labels():
    files = [FakeUpload(f"phone_photo_{index}.jpg") for index in range(4)]
    labels = [
        "required_front_straight",
        "required_front_slight_left",
        "required_front_slight_right",
        "required_back",
    ]

    images = asyncio.run(read_images(files, forced_labels=labels))

    assert [image[2] for image in images] == labels


class FakeR2Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://signed.example/{Params['Key']}?expires={ExpiresIn}"

    def delete_object(self, **kwargs):
        self.objects.pop(kwargs["Key"], None)


def test_r2_storage_uses_private_listing_paths():
    fake = FakeR2Client()
    storage = R2Storage(
        type(
            "Settings",
            (),
            {
                "r2_bucket_name": "pokemart-images",
                "r2_endpoint": "https://example.r2.cloudflarestorage.com",
                "r2_access_key_id": "test",
                "r2_secret_access_key": "test",
                "r2_presigned_url_expiry_seconds": 3600,
            },
        )(),
        client=fake,
    )

    result = storage.upload_images(
        "listing_123",
        [(b"jpeg", "image/jpeg", "required_front_straight")],
    )

    assert re.fullmatch(
        r"listings/listing_123/originals/[0-9a-f]{32}/required_front_straight\.jpg",
        result[0]["object_key"],
    )
    assert result[0]["url"].startswith("https://signed.example/")


def test_render_postgres_url_uses_psycopg_driver():
    assert normalize_database_url("postgresql://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )


def test_initialize_adds_seller_id_to_existing_listing_table(tmp_path):
    path = tmp_path / "legacy-schema.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE listings (id VARCHAR(64) PRIMARY KEY, status VARCHAR(32) DEFAULT 'draft')")
    connection.commit()
    connection.close()
    database = Database(f"sqlite:///{path}")
    database.initialize()
    with database.engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(listings)")}
    assert "seller_id" in columns


def test_database_saves_listing_metadata_and_replaces_image_keys(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'pokemarket-test.db'}")
    database.initialize()
    seller, claimed = database.create_user(
        "seller-1",
        "seller@example.com",
        "Seller One",
        "hash",
        "salt",
        1,
        False,
    )
    assert claimed == 0

    first = [
        {
            "label": "required_front_straight",
            "object_key": "listings/demo/originals/first/front.jpg",
            "content_type": "image/jpeg",
            "size_bytes": 4_000_000,
        }
    ]
    record, stale_keys = database.replace_images("demo", first, seller["id"])
    assert stale_keys == []
    assert record["photos_persisted"] is True
    assert record["images"][0]["object_key"] == first[0]["object_key"]

    saved = database.upsert_listing(
        "demo",
        {
            "scan_id": "scan-123",
            "title": "Dracozolt VMAX Evolving Skies #059/203",
            "card_name": "Dracozolt VMAX",
            "status": "draft",
            "price_cents": 1299,
        },
        seller["id"],
    )
    assert saved["title"] == "Dracozolt VMAX Evolving Skies #059/203"
    assert saved["images"][0]["object_key"] == first[0]["object_key"]

    second = [
        {
            "label": "required_front_straight",
            "object_key": "listings/demo/originals/second/front.jpg",
            "content_type": "image/jpeg",
            "size_bytes": 4_100_000,
        }
    ]
    replaced, stale_keys = database.replace_images("demo", second, seller["id"])
    assert stale_keys == [first[0]["object_key"]]
    assert [image["object_key"] for image in replaced["images"]] == [
        second[0]["object_key"]
    ]


def test_password_hash_and_signed_token_round_trip():
    salt, password_hash, iterations = hash_password("correct horse battery", 10_000)
    assert verify_password("correct horse battery", salt, password_hash, iterations)
    assert not verify_password("wrong password", salt, password_hash, iterations)
    secret = "test-secret-that-is-definitely-at-least-32-characters"
    token = create_access_token("seller-123", secret, 60)
    assert decode_access_token(token, secret)["sub"] == "seller-123"


def test_accounts_scope_listing_queries_and_first_user_claims_legacy(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'accounts-test.db'}")
    database.initialize()
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO listings (id, status, currency, photos_persisted, created_at, updated_at) "
            "VALUES ('legacy-card', 'draft', 'USD', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    old_secret = settings.auth_secret
    old_iterations = settings.password_hash_iterations
    old_claim_code = settings.legacy_claim_code
    settings.auth_secret = "test-secret-that-is-definitely-at-least-32-characters"
    settings.legacy_claim_code = "original-owner-setup-code"
    settings.password_hash_iterations = 10_000
    app.dependency_overrides[require_database] = lambda: database
    try:
        rejected = client.post(
            "/api/v1/auth/register",
            json={
                "email": "intruder@example.com",
                "display_name": "Wrong Code",
                "password": "long-password-zero",
                "legacy_claim_code": "incorrect-code",
            },
        )
        assert rejected.status_code == 403
        first = client.post(
            "/api/v1/auth/register",
            json={
                "email": "first@example.com",
                "display_name": "First Seller",
                "password": "long-password-one",
                "legacy_claim_code": "original-owner-setup-code",
            },
        )
        assert first.status_code == 201
        assert first.json()["legacy_listings_claimed"] == 1
        first_headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
        assert [item["id"] for item in client.get("/api/v1/listings", headers=first_headers).json()] == ["legacy-card"]

        second = client.post(
            "/api/v1/auth/register",
            json={
                "email": "second@example.com",
                "display_name": "Second Seller",
                "password": "long-password-two",
            },
        )
        assert second.status_code == 201
        second_headers = {"Authorization": f"Bearer {second.json()['access_token']}"}
        assert client.get("/api/v1/listings", headers=second_headers).json() == []

        created = client.put(
            f"/api/v1/listings/{uuid.uuid4()}",
            headers=second_headers,
            json={"title": "Second seller card", "status": "draft"},
        )
        assert created.status_code == 200
        assert created.json()["seller_id"] == second.json()["user"]["id"]
        assert len(client.get("/api/v1/listings", headers=second_headers).json()) == 1
        assert len(client.get("/api/v1/listings", headers=first_headers).json()) == 1
        over_limit = client.put(
            f"/api/v1/listings/{uuid.uuid4()}",
            headers=second_headers,
            json={"title": "Too expensive", "status": "draft", "price_cents": 8_001},
        )
        assert over_limit.status_code == 403

        login = client.post(
            "/api/v1/auth/login",
            json={"email": "first@example.com", "password": "long-password-one"},
        )
        assert login.status_code == 200
        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json()["email"] == "first@example.com"
        assert client.get("/api/v1/listings").status_code == 401
    finally:
        app.dependency_overrides.clear()
        settings.auth_secret = old_secret
        settings.password_hash_iterations = old_iterations
        settings.legacy_claim_code = old_claim_code
