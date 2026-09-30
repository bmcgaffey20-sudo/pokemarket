"""All-time seller health, computed from paid orders and verified payment disputes."""
from fastapi import Depends, Query
from sqlalchemy import Column, String, Integer, select
from database import Base, Order, User, utc_now
from shipping_performance import ShippingPerformance, deadline

class PaymentDispute(Base):
    __tablename__ = "payment_disputes"
    id = Column(String(100), primary_key=True)
    payment_intent = Column(String(100), nullable=False, index=True)
    status = Column(String(40), nullable=False)
    event_time = Column(Integer, nullable=False, default=0)


def record_dispute(db, obj, event_time, intent):
    if not intent: raise ValueError("Dispute payment intent unavailable")
    with db.sessions.begin() as s:
        row = s.get(PaymentDispute, obj["id"], with_for_update=True)
        if row is None:
            s.add(PaymentDispute(id=obj["id"], payment_intent=intent,
                                 status=obj.get("status", "needs_response"), event_time=event_time))
        elif event_time > row.event_time:
            row.status = obj.get("status", row.status); row.event_time = event_time


def metric(label, numerator, denominator, target, minimum):
    rate = 100 * numerator / denominator if denominator else None
    warning = rate is not None and (rate < target if minimum else rate > target)
    return dict(label=label, numerator=numerator, denominator=denominator,
                percent=round(rate, 2) if rate is not None else None,
                target=target, minimum=minimum, warning=warning)


def health(s, uid):
    orders = s.scalars(select(Order).where(Order.seller_id == uid, Order.paid_at.is_not(None))).all()
    perf = {p.order_id: p for p in s.scalars(select(ShippingPerformance).where(ShippingPerformance.seller_id == uid))}
    intents = {o.stripe_payment_intent_id for o in orders if o.stripe_payment_intent_id}
    disputes = s.scalars(select(PaymentDispute).where(PaymentDispute.payment_intent.in_(intents))).all() if intents else []
    # Won disputes remain in the historical chargeback rate; inquiries do not.
    charged = {d.payment_intent for d in disputes if not d.status.startswith("warning_")}
    lost = {d.payment_intent for d in disputes if d.status == "lost"}
    eligible = [o for o in orders if o.id in perf or (o.status in {"paid", "shipped", "completed"} and utc_now() > deadline(o.paid_at))]
    ontime = sum(o.id in perf and perf[o.id].points > 0 for o in eligible)
    incidents = sum(bool(o.return_reason or o.stripe_refund_id or o.status in {"refunded", "refund_pending"} or o.stripe_payment_intent_id in charged) for o in orders)
    resolved = [o for o in orders if (o.status == "completed" and o.payout_status == "paid") or o.status == "refunded" or o.stripe_payment_intent_id in lost]
    successes = sum(o.status == "completed" and o.payout_status == "paid" and not o.return_reason and not o.stripe_refund_id and o.stripe_payment_intent_id not in charged for o in resolved)
    metrics = [metric("On-time tracking", ontime, len(eligible), 96, True),
               metric("Buyer disputes and refunds", incidents, len(orders), 2, False),
               metric("Chargebacks", sum(o.stripe_payment_intent_id in charged for o in orders), len(orders), 2, False),
               metric("Successful transactions", successes, len(resolved), 95, True)]
    return dict(user_id=uid, period="All time", paid_sales=len(orders),
                at_risk=any(m["warning"] for m in metrics), metrics=metrics,
                updated_at=utc_now().isoformat())


def install_health(app, require_database, require_user, require_admin):
    @app.get("/api/v1/account/health")
    def own(user=Depends(require_user), db=Depends(require_database)):
        with db.sessions() as s: return health(s, user["id"])

    @app.get("/api/v1/admin/account-warnings")
    def warnings(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                 admin=Depends(require_admin), db=Depends(require_database)):
        with db.sessions() as s:
            ids = s.scalars(select(Order.seller_id).where(Order.paid_at.is_not(None)).distinct().order_by(Order.seller_id)).all()
            result = []
            for uid in ids:
                stats = health(s, uid)
                if stats["at_risk"]:
                    user = s.get(User, uid)
                    if user: result.append(dict(id=uid, email=user.email, display_name=user.display_name, health=stats))
            return dict(total=len(result), items=result[offset:offset+limit])
