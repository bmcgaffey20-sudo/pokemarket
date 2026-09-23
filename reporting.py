import base64
import csv
import io
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

import httpx


REPORT_COLUMNS = (
    "order_id", "transaction_date_utc", "listing_title", "status", "currency",
    "item_total", "shipping_total", "commission", "seller_payout", "payout_status",
    "buyer_name", "buyer_email", "buyer_address", "seller_name", "seller_email",
    "seller_address", "tracking_carrier", "tracking_number", "tracking_status",
    "deadline_utc", "return_tracking_carrier", "return_tracking_number",
)


def completed_week(now=None):
    now = now or datetime.now(timezone.utc)
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return this_monday - timedelta(days=7), this_monday


def _money(cents):
    return f"{int(cents or 0) / 100:.2f}"


def _address(row, prefix):
    parts = [row.get(f"{prefix}_name"), row.get(f"{prefix}_line1"), row.get(f"{prefix}_line2"),
             row.get(f"{prefix}_city"), row.get(f"{prefix}_state"), row.get(f"{prefix}_postal_code"),
             row.get(f"{prefix}_country")]
    return ", ".join(str(part) for part in parts if part)


def build_sales_csv(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=REPORT_COLUMNS)
    writer.writeheader()
    for row in rows:
        deadline = row.get("active_deadline_at")
        writer.writerow({
            "order_id": row["id"], "transaction_date_utc": row.get("paid_at") or row.get("created_at"),
            "listing_title": row.get("listing_title"), "status": row.get("status"), "currency": row.get("currency"),
            "item_total": _money(row.get("item_cents")), "shipping_total": _money(row.get("shipping_cents")),
            "commission": _money(row.get("commission_cents")), "seller_payout": _money(row.get("seller_amount_cents")),
            "payout_status": row.get("payout_status"), "buyer_name": row.get("buyer_display_name"),
            "buyer_email": row.get("buyer_email"), "buyer_address": _address(row, "shipping"),
            "seller_name": row.get("seller_display_name"), "seller_email": row.get("seller_email"),
            "seller_address": _address(row, "seller_shipping"), "tracking_carrier": row.get("tracking_carrier"),
            "tracking_number": row.get("tracking_number"), "tracking_status": row.get("tracking_status"),
            "deadline_utc": deadline, "return_tracking_carrier": row.get("return_tracking_carrier"),
            "return_tracking_number": row.get("return_tracking_number"),
        })
    return output.getvalue().encode("utf-8")


async def send_weekly_sales_report(settings, rows, period_start, period_end):
    sender_name, sender_email = parseaddr(settings.email_from or "")
    end_label = (period_end - timedelta(days=1)).strftime("%b %d, %Y")
    subject = f"PokeMarket Sales Report — {period_start.strftime('%b %d')}–{end_label}"
    filename = f"pokemarket-sales-{period_start.date()}-to-{(period_end - timedelta(days=1)).date()}.csv"
    payload = {"Messages": [{
        "From": {"Email": sender_email, "Name": sender_name or "PokeMarket"},
        "To": [{"Email": settings.admin_report_recipient}], "Subject": subject,
        "TextPart": f"Attached is the PokeMarket sales report for {period_start.date()} through {(period_end - timedelta(days=1)).date()} ({len(rows)} sales). Shipping details are retained in the live admin panel for 30 days.",
        "Attachments": [{"ContentType": "text/csv", "Filename": filename,
                         "Base64Content": base64.b64encode(build_sales_csv(rows)).decode("ascii")}],
    }]}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post("https://api.mailjet.com/v3.1/send", auth=(settings.mailjet_api_key, settings.mailjet_secret_key), json=payload)
    response.raise_for_status()
    return subject
