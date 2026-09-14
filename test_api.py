from fastapi.testclient import TestClient
from main import app, apply_grading_safeguards

client = TestClient(app)

def test_health():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["version"] == "2.2.0-rigorous-scan"


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
