import asyncio
import json
import logging

import httpx

logger = logging.getLogger("pokemarket.notifications")


async def send_push(settings, database, user_ids, title, body, data=None):
    """Send an optional FCM notification without blocking marketplace actions."""
    if not settings.firebase_configured:
        return 0
    tokens = await asyncio.to_thread(database.device_tokens_for_users, user_ids)
    if not tokens:
        return 0
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import service_account

        info = json.loads(settings.firebase_service_account_json)
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"],
        )
        await asyncio.to_thread(credentials.refresh, GoogleRequest())
    except Exception:
        logger.exception("Firebase credentials could not be loaded")
        return 0

    endpoint = (
        "https://fcm.googleapis.com/v1/projects/"
        f"{settings.firebase_project_id}/messages:send"
    )
    sent = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for token in tokens:
            payload = {
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": body},
                    "data": {key: str(value) for key, value in (data or {}).items()},
                    "android": {"priority": "high"},
                }
            }
            try:
                response = await client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {credentials.token}"},
                    json=payload,
                )
                if response.status_code in {400, 404} and "UNREGISTERED" in response.text:
                    await asyncio.to_thread(database.delete_device_token, token)
                elif response.is_success:
                    sent += 1
                else:
                    logger.warning("FCM rejected notification: HTTP %s", response.status_code)
            except Exception:
                logger.exception("FCM notification failed")
    return sent
