import re
from pathlib import PurePosixPath
from uuid import uuid4


LISTING_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LABEL_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class R2ConfigurationError(RuntimeError):
    pass


class R2UploadError(RuntimeError):
    pass


class R2Storage:
    def __init__(self, settings, client=None):
        missing = [
            name
            for name, value in (
                ("R2_BUCKET_NAME", settings.r2_bucket_name),
                ("R2_ENDPOINT", settings.r2_endpoint),
                ("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
                ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
            )
            if not value
        ]
        if missing:
            raise R2ConfigurationError(
                "Missing required R2 environment variables: " + ", ".join(missing)
            )

        self.bucket_name = settings.r2_bucket_name
        self.url_expiry = settings.r2_presigned_url_expiry_seconds

        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=settings.r2_endpoint.rstrip("/"),
                aws_access_key_id=settings.r2_access_key_id,
                aws_secret_access_key=settings.r2_secret_access_key,
                region_name="auto",
                config=Config(signature_version="s3v4"),
            )
        self.client = client

    @staticmethod
    def validate_listing_id(listing_id):
        value = (listing_id or "").strip()
        if not LISTING_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "listing_id must be 1-64 characters using letters, numbers, '-' or '_'."
            )
        return value

    @staticmethod
    def _object_key(listing_id, upload_id, label, content_type):
        safe_label = LABEL_PATTERN.sub("_", label).strip("_") or "photo"
        extension = CONTENT_TYPE_EXTENSIONS[content_type]
        return str(
            PurePosixPath(
                "listings",
                listing_id,
                "originals",
                upload_id,
                safe_label + extension,
            )
        )

    def upload_images(self, listing_id, images):
        listing_id = self.validate_listing_id(listing_id)
        upload_id = uuid4().hex
        uploaded_keys = []
        stored = []

        try:
            for data, content_type, label in images:
                object_key = self._object_key(listing_id, upload_id, label, content_type)
                self.client.put_object(
                    Bucket=self.bucket_name,
                    Key=object_key,
                    Body=data,
                    ContentType=content_type,
                    CacheControl="private, max-age=3600",
                )
                uploaded_keys.append(object_key)
                url = self.client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket_name, "Key": object_key},
                    ExpiresIn=self.url_expiry,
                )
                stored.append(
                    {
                        "label": label,
                        "object_key": object_key,
                        "content_type": content_type,
                        "size_bytes": len(data),
                        "url": url,
                    }
                )
        except Exception as exc:
            for object_key in uploaded_keys:
                try:
                    self.client.delete_object(Bucket=self.bucket_name, Key=object_key)
                except Exception:
                    pass
            raise R2UploadError(f"R2 image upload failed: {exc}") from exc

        return stored

    def create_direct_upload_session(self, listing_id, image_specs):
        """Create narrowly scoped PUT URLs so originals bypass the API server."""
        listing_id = self.validate_listing_id(listing_id)
        upload_id = uuid4().hex
        uploads = []
        for spec in image_specs:
            object_key = self._object_key(
                listing_id,
                upload_id,
                spec["label"],
                spec["content_type"],
            )
            upload_url = self.client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": object_key,
                    "ContentType": spec["content_type"],
                },
                ExpiresIn=self.url_expiry,
            )
            uploads.append({
                **spec,
                "object_key": object_key,
                "upload_url": upload_url,
            })
        return upload_id, uploads

    def verify_direct_uploads(self, listing_id, upload_id, images, max_image_bytes):
        listing_id = self.validate_listing_id(listing_id)
        expected_prefix = f"listings/{listing_id}/originals/{upload_id}/"
        verified = []
        try:
            for image in images:
                key = image["object_key"]
                if not key.startswith(expected_prefix) or ".." in key:
                    raise ValueError("Direct-upload object key is outside its session.")
                head = self.client.head_object(Bucket=self.bucket_name, Key=key)
                actual_size = int(head.get("ContentLength", 0))
                actual_type = str(head.get("ContentType", "")).split(";", 1)[0]
                if actual_size <= 0 or actual_size > max_image_bytes:
                    raise ValueError("Uploaded image has an invalid size.")
                if actual_size != int(image["size_bytes"]):
                    raise ValueError("Uploaded image size does not match its declaration.")
                if actual_type != image["content_type"]:
                    raise ValueError("Uploaded image content type does not match its declaration.")
                verified.append({
                    "label": image["label"],
                    "object_key": key,
                    "content_type": actual_type,
                    "size_bytes": actual_size,
                    "url": self.presign_object(key),
                })
        except ValueError:
            self.delete_objects([image["object_key"] for image in images])
            raise
        except Exception as exc:
            raise R2UploadError(f"Could not verify direct R2 upload: {exc}") from exc
        return verified

    def delete_objects(self, object_keys):
        failures = []
        for object_key in object_keys:
            try:
                self.client.delete_object(Bucket=self.bucket_name, Key=object_key)
            except Exception as exc:
                failures.append(f"{object_key}: {exc}")
        if failures:
            raise R2UploadError("Could not delete R2 objects: " + "; ".join(failures))

    def presign_object(self, object_key):
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": object_key},
            ExpiresIn=self.url_expiry,
        )
