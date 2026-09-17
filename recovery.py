"""Email actions: hashed, expiring, single-use tokens; no tokens in server URLs."""
import asyncio
import hashlib
import logging
import secrets
from pathlib import Path
from email.utils import parseaddr

import httpx
from fastapi import BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from auth import hash_password, normalize_email

logger = logging.getLogger("pokemarket.recovery")


class EmailRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)

    @field_validator("email")
    @classmethod
    def email_address(cls, value):
        import re
        value = normalize_email(value)
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address.")
        return value


class ActionRequest(BaseModel):
    token: str = Field(min_length=32, max_length=128)


class ResetRequest(ActionRequest):
    password: str = Field(min_length=10, max_length=128)


async def limit_auth(database, request, action, identity="", maximum=10):
    # Never trust client-supplied X-Forwarded-For directly. Use the ASGI peer;
    # configure trusted proxy handling at deployment, not in this handler.
    peer = request.client.host if request.client else "unknown"
    checks = [(f"{action}:ip:{peer}", maximum * 5)]
    if identity:
        checks.append((f"{action}:identity:{identity}", maximum))
    for key, limit in checks:
        if not await asyncio.to_thread(database.take_auth_rate, key, limit, 900):
            raise HTTPException(429, "Too many attempts. Please try again in 15 minutes.", headers={"Retry-After": "900"})


async def send_action_email(settings, database, email, purpose):
    """Background operation; caller always gets the same recovery-request response."""
    try:
        user = await asyncio.to_thread(database.get_user_by_email, email)
        if user is None:
            return
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        ttl = 1800 if purpose == "reset" else 86400
        recipient = await asyncio.to_thread(database.issue_account_action, user["id"], purpose, digest, ttl)
        if recipient is None:
            return
        link = f"{settings.public_base_url.rstrip('/')}/account/{purpose}#{token}"
        subject = "Reset your PokeMarket password" if purpose == "reset" else "Verify your PokeMarket email"
        duration = "30 minutes" if purpose == "reset" else "24 hours"
        sender_name, sender_email = parseaddr(settings.email_from)
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.post("https://api.mailjet.com/v3.1/send",
                auth=httpx.BasicAuth(settings.mailjet_api_key.strip(), settings.mailjet_secret_key.strip()),
                json={"Messages": [{
                    "From": {"Email": sender_email, "Name": sender_name or "PokeMarket"},
                    "To": [{"Email": recipient}], "Subject": subject,
                    "TextPart": f"{subject}\n\nOpen this link and confirm the action:\n{link}\n\nThis link expires in {duration} and can only be used once. If you did not request this, ignore this email. Your password has not changed.",
                    "TrackClicks": "disabled", "TrackOpens": "disabled",
                }]})
        if response.status_code not in (200, 201, 202):
            # No provider response bodies, addresses, credentials or links in logs.
            logger.error("Account email rejected by provider (HTTP %s). Check sender verification and quota.", response.status_code)
            return
        results = response.json().get("Messages", [])
        if len(results) != 1 or results[0].get("Status") != "success":
            logger.error("Mailjet rejected account email. Check sender activation and Mailjet logs.")
    except Exception:
        logger.error("Account email delivery failed. Check database and email provider availability.")


def install_recovery(app, settings, require_database, require_user):
    def check_mail():
        if not settings.email_configured:
            raise HTTPException(503, "Account email is not configured yet. Please contact the app owner.")

    @app.post("/api/v1/auth/forgot-password", status_code=202)
    async def forgot(payload: EmailRequest, request: Request, background: BackgroundTasks, database=Depends(require_database)):
        await limit_auth(database, request, "recovery", payload.email, 3)
        check_mail()
        background.add_task(send_action_email, settings, database, payload.email, "reset")
        return {"message": "If an account exists for this email, a password-reset link will be sent. Check your inbox and spam folder."}

    @app.post("/api/v1/auth/send-verification", status_code=202)
    async def verification(request: Request, background: BackgroundTasks, user=Depends(require_user), database=Depends(require_database)):
        await limit_auth(database, request, "verification", user["id"], 3)
        check_mail()
        if not user["email_verified"]:
            background.add_task(send_action_email, settings, database, user["email"], "verify")
        return {"message": "If your email needs verification, a link will be sent. After confirming it, tap Refresh Verification Status."}

    @app.post("/api/v1/auth/verify-email")
    async def verify(payload: ActionRequest, request: Request, database=Depends(require_database)):
        await limit_auth(database, request, "consume", maximum=10)
        valid = await asyncio.to_thread(database.consume_account_action, hashlib.sha256(payload.token.encode()).hexdigest(), "verify")
        if not valid:
            raise HTTPException(400, "This link is invalid, expired or already used. Request a new email.")
        return {"message": "Email verified. Return to PokeMarket and refresh your account status."}

    @app.post("/api/v1/auth/reset-password")
    async def reset(payload: ResetRequest, request: Request, database=Depends(require_database)):
        await limit_auth(database, request, "consume", maximum=10)
        password = await asyncio.to_thread(hash_password, payload.password, settings.password_hash_iterations)
        valid = await asyncio.to_thread(database.consume_account_action, hashlib.sha256(payload.token.encode()).hexdigest(), "reset", password)
        if not valid:
            raise HTTPException(400, "This link is invalid, expired or already used. Request a new email.")
        return {"message": "Password changed. All previous sessions are invalid. Return to PokeMarket, sign out if needed, and sign in with your new password."}

    @app.get("/account/{purpose}", response_class=HTMLResponse, include_in_schema=False)
    async def account_page(purpose: str):
        if purpose not in {"reset", "verify"}:
            raise HTTPException(404)
        nonce = secrets.token_urlsafe(24)
        page = Path(__file__).with_name("account_action.html").read_text().replace("NONCE_PLACEHOLDER", nonce)
        return HTMLResponse(page, headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
        })
