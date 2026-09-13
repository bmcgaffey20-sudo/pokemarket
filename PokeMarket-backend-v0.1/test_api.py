from fastapi.testclient import TestClient
from app.main import app
c=TestClient(app)
def test_health():
    r=c.get("/api/v1/health");assert r.status_code==200;assert r.json()["version"]=="2.0.0"
def test_stub():
    r=c.post("/api/v1/scan/analyze",files=[("files",("a.jpg",b"x","image/jpeg"))]);assert r.status_code==200;assert r.json()["provider"]=="stub"
