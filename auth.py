import base64
import hashlib
import hmac
import json
import secrets
import time


class AuthConfigurationError(RuntimeError):
    pass


class InvalidTokenError(ValueError):
    pass


def normalize_email(value):
    return (value or "").strip().lower()


def hash_password(password, iterations=600_000):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return (
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
        iterations,
    )


def verify_password(password, salt_text, expected_text, iterations):
    try:
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        int(iterations),
    )
    return hmac.compare_digest(actual, expected)


def _b64url_encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value):
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _require_secret(secret):
    value = (secret or "").strip()
    if len(value) < 32:
        raise AuthConfigurationError(
            "AUTH_SECRET must be configured with at least 32 random characters."
        )
    return value.encode("utf-8")


def create_access_token(user_id, secret, ttl_seconds):
    key = _require_secret(secret)
    now = int(time.time())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + int(ttl_seconds),
        "nonce": secrets.token_urlsafe(8),
    }
    encoded = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _b64url_encode(
        hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}"


def decode_access_token(token, secret):
    key = _require_secret(secret)
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _b64url_encode(
            hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidTokenError("Invalid access token.")
        payload = json.loads(_b64url_decode(encoded))
        if int(payload.get("exp", 0)) <= int(time.time()):
            raise InvalidTokenError("Access token has expired.")
        user_id = str(payload.get("sub") or "").strip()
        if not user_id:
            raise InvalidTokenError("Invalid access token.")
        return payload
    except InvalidTokenError:
        raise
    except Exception as exc:
        raise InvalidTokenError("Invalid access token.") from exc
