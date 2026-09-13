from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_scan_analyze_stub():
    response = client.post(
        "/api/v1/scan/analyze",
        json={
            "image_count": 10,
            "views": ["top_straight", "top_left", "top_right", "back"],
            "include_condition": True,
            "include_authenticity": True,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "stub"
    assert data["identification"]["status"] == "needs_ai_provider"
