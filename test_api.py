from fastapi.testclient import TestClient
from main import app, apply_grading_safeguards
from storage import R2Storage

client = TestClient(app)

def test_health():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["version"] == "2.3.0-r2-storage"


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
                "r2_bucket_name": "pokemarket-images",
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

    assert result[0]["object_key"] == (
        "listings/listing_123/required_front_straight.jpg"
    )
    assert result[0]["url"].startswith("https://signed.example/")
