from io import BytesIO
from types import SimpleNamespace
from PIL import Image
from botocore.exceptions import ClientError
from storage import R2Storage
from test_marketplace import market


def test_preview_prefers_top_center_and_requires_publication(market, monkeypatch):
    client, db, _, _ = market
    requested = []
    class Storage:
        def presign_object(self, key): return "https://example.com/original"
        def listing_thumbnail(self, key, max_bytes):
            requested.append(key)
            return "https://example.com/preview.jpg"
    monkeypatch.setattr("main.get_r2_storage", lambda: Storage())
    assert client.get("/api/v1/marketplace/card/thumbnail").status_code == 404
    assert client.post("/api/v1/listings/card/publish").status_code == 200
    response = client.get("/api/v1/marketplace/card/thumbnail", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "https://example.com/preview.jpg"
    assert requested == ["listings/card/front_straight"]
    client.post("/api/v1/listings/card/unpublish")
    assert client.get("/api/v1/marketplace/card/thumbnail").status_code == 404


def test_preview_is_cached_and_original_untouched_and_deleted_with_original():
    class Client:
        def __init__(self): self.objects = {}; self.downloads = 0
        def head_object(self, Bucket, Key):
            if Key not in self.objects:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {}
        def get_object(self, Bucket, Key):
            self.downloads += 1
            return {"Body": BytesIO(self.objects[Key])}
        def put_object(self, Bucket, Key, Body, **kwargs): self.objects[Key] = Body
        def generate_presigned_url(self, operation, Params, ExpiresIn): return "https://example.com/" + Params["Key"]
        def delete_object(self, Bucket, Key): self.objects.pop(Key, None)
    client = Client()
    settings = SimpleNamespace(r2_bucket_name="bucket", r2_endpoint="https://example.com", r2_access_key_id="test", r2_secret_access_key="test", r2_presigned_url_expiry_seconds=3600)
    storage = R2Storage(settings, client)
    key = "listings/card/originals/upload/required_front_straight.jpg"
    stream = BytesIO()
    Image.new("RGB", (1800, 2400), "red").save(stream, "JPEG")
    original = stream.getvalue()
    client.objects[key] = original
    first = storage.listing_thumbnail(key, 12_000_000)
    assert storage.listing_thumbnail(key, 12_000_000) == first
    assert client.downloads == 1
    assert client.objects[key] == original
    with Image.open(BytesIO(client.objects[storage.thumbnail_key(key)])) as preview:
        assert preview.size == (480, 640)
        assert preview.format == "JPEG"
    storage.delete_objects([key])
    assert client.objects == {}
