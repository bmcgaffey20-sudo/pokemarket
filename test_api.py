import asyncio
import re
import sqlite3
import uuid
import pytest
from sqlalchemy import inspect

from fastapi.testclient import TestClient
from auth import create_access_token, decode_access_token, hash_password, verify_password
from ai import scan_configuration
from database import Database, normalize_database_url
from main import app, apply_grading_safeguards, read_images, require_database, settings
from storage import R2Storage

client = TestClient(app)

def test_health():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["version"] == "2.18.1-two-photo"
    assert r.json()["database"] in {"not_configured", "connected"}
    assert r.json()["auth"] in {"not_configured", "configured"}


@pytest.mark.parametrize("market", ["pokemon", "magic", "sports"])
@pytest.mark.parametrize("grading_status", ["graded", "ungraded"])
def test_scan_configuration_covers_each_market_and_grading_path(market, grading_status):
    prompt, schema = scan_configuration(market, grading_status)
    identity = schema["properties"]["identification"]
    assert "grading_company" in identity["properties"]
    assert "grade" in identity["properties"]
    assert "certification_number" in identity["properties"]
    if market == "sports":
        assert {"sport", "player", "team", "year", "manufacturer", "parallel", "serial_number"} <= set(identity["properties"])
    if market == "pokemon":
        assert "TCGdex" in prompt
    else:
        assert "tcgdex_id as null" in prompt
    assert ("GRADED card" in prompt) is (grading_status == "graded")


def test_scan_configuration_rejects_unknown_category():
    with pytest.raises(ValueError):
        scan_configuration("unknown", "ungraded")


def test_connect_return_pages_reopen_android_app():
    completed = client.get("/api/v1/payments/connect/return")
    assert completed.status_code == 200
    assert "pokemarket://account?stripe=return" in completed.text
    assert completed.headers["cache-control"] == "no-store"

    expired = client.get("/api/v1/payments/connect/refresh")
    assert expired.status_code == 200
    assert "pokemarket://account?stripe=refresh" in expired.text


def test_checkout_return_pages_reopen_android_app():
    completed = client.get("/checkout/success?session_id=cs_test_example")
    assert completed.status_code == 200
    assert "pokemarket://account?checkout=success" in completed.text
    assert completed.headers["cache-control"] == "no-store"

    cancelled = client.get("/checkout/cancelled")
    assert cancelled.status_code == 200
    assert "pokemarket://account?checkout=cancelled" in cancelled.text


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

    def head_object(self, **kwargs):
        value = self.objects[kwargs["Key"]]
        body = value.get("Body", b"")
        return {
            "ContentLength": len(body),
            "ContentType": value.get("ContentType", "application/octet-stream"),
        }


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


def test_direct_r2_upload_session_and_verification():
    fake = FakeR2Client()
    storage = R2Storage(
        type("Settings", (), {
            "r2_bucket_name": "pokemart-images",
            "r2_endpoint": "https://example.r2.cloudflarestorage.com",
            "r2_access_key_id": "test",
            "r2_secret_access_key": "test",
            "r2_presigned_url_expiry_seconds": 3600,
        })(),
        client=fake,
    )
    upload_id, targets = storage.create_direct_upload_session(
        "listing_123",
        [{"label": "required_front_straight", "content_type": "image/jpeg", "size_bytes": 4}],
    )
    assert targets[0]["upload_url"].startswith("https://signed.example/")
    fake.objects[targets[0]["object_key"]] = {"Body": b"jpeg", "ContentType": "image/jpeg"}
    verified = storage.verify_direct_uploads(
        "listing_123",
        upload_id,
        [{
            "label": targets[0]["label"],
            "object_key": targets[0]["object_key"],
            "content_type": "image/jpeg",
            "size_bytes": 4,
        }],
        12_000_000,
    )
    assert verified[0]["size_bytes"] == 4


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


def test_initialize_adds_beta_control_columns_without_data_loss(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'beta-migration.db'}")
    database.initialize()
    with database.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE users DROP COLUMN is_admin")
        connection.exec_driver_sql("DROP INDEX ix_orders_delivery_confirmation_due_at")
        connection.exec_driver_sql("DROP INDEX ix_orders_return_confirmation_due_at")
        for column in (
            "return_reason", "return_notes", "return_tracking_number",
            "return_requested_at", "return_approved_at", "return_received_at",
            "delivery_confirmation_due_at", "return_confirmation_due_at",
        ):
            connection.exec_driver_sql(f"ALTER TABLE orders DROP COLUMN {column}")
        connection.exec_driver_sql("DROP INDEX ix_scan_jobs_next_attempt_at")
        connection.exec_driver_sql("ALTER TABLE scan_jobs DROP COLUMN next_attempt_at")

    database.initialize()

    inspector = inspect(database.engine)
    assert "is_admin" in {column["name"] for column in inspector.get_columns("users")}
    assert "return_reason" in {column["name"] for column in inspector.get_columns("orders")}
    assert "delivery_confirmation_due_at" in {column["name"] for column in inspector.get_columns("orders")}
    assert "return_confirmation_due_at" in {column["name"] for column in inspector.get_columns("orders")}
    assert "next_attempt_at" in {column["name"] for column in inspector.get_columns("scan_jobs")}
    assert "device_tokens" in inspector.get_table_names()


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


def test_legacy_scan_id_can_resolve_listing_deletion(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'legacy-delete.db'}")
    database.initialize()
    seller, _ = database.create_user(
        "seller-legacy",
        "legacy-delete@example.com",
        "Legacy Seller",
        "hash",
        "salt",
        1,
        False,
    )
    database.upsert_listing(
        "cloud-listing-id",
        {"scan_id": "old-phone-card-id", "title": "Legacy card", "status": "draft"},
        seller["id"],
    )

    deletion = database.prepare_listing_deletion("old-phone-card-id", seller["id"])

    assert deletion["listing_ids"] == ["cloud-listing-id"]
    assert deletion["object_keys"] == []
    assert database.delete_owned_listings(deletion["listing_ids"], seller["id"]) == 1
    assert database.get_listing("cloud-listing-id", seller["id"]) is None


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

@pytest.mark.parametrize("labels", [
    ["required_front_straight", "required_back"],
    ["required_front_straight", "required_back", "defect_crease_1"],
    ["required_front_straight", "required_front_slight_left", "required_front_slight_right", "required_back"],
])
def test_two_photo_and_legacy_evidence_accepted(labels):
    from main import validate_image_labels
    from schemas import DirectUploadSessionRequest
    validate_image_labels(labels)
    request = DirectUploadSessionRequest(images=[
        {"label": label, "content_type": "image/jpeg", "size_bytes": 100} for label in labels
    ])
    assert len(request.images) == len(labels)


@pytest.mark.parametrize("labels", [
    ["required_front_straight"],
    ["required_back"],
    ["required_front_straight", "required_front_straight", "required_back"],
    ["required_front_straight", "required_back", "unknown"],
    ["required_front_straight", "required_back"] + [f"defect_{i}" for i in range(6)],
])
def test_incomplete_or_invalid_two_photo_evidence_rejected(labels):
    from main import validate_image_labels
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        validate_image_labels(labels)
