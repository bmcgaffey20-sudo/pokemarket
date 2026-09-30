"""Tracking-submission performance. Weekends excluded; Pacific business dates."""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import Column, String, Integer, DateTime, select, func
from database import Base, Order, utc_now
import holidays

class ShippingPerformance(Base):
    __tablename__='shipping_performance'
    order_id=Column(String(36),primary_key=True)
    seller_id=Column(String(36),nullable=False,index=True)
    points=Column(Integer,nullable=False)
    due_at=Column(DateTime(timezone=True),nullable=False)
    submitted_at=Column(DateTime(timezone=True),nullable=False)


def deadline(paid):
    paid=paid.replace(tzinfo=timezone.utc) if paid.tzinfo is None else paid
    due=paid.astimezone(ZoneInfo('America/Los_Angeles'))
    closed=holidays.US(years=[due.year, due.year + 1])
    days=0
    while days<3:
        due+=timedelta(days=1)
        if due.weekday()<5 and due.date() not in closed:days+=1
    return due.replace(hour=23,minute=59,second=59,microsecond=0).astimezone(timezone.utc)


def award(session,order):
    if not order.paid_at or session.get(ShippingPerformance,order.id):return
    paid=order.paid_at.replace(tzinfo=timezone.utc) if order.paid_at.tzinfo is None else order.paid_at
    now=utc_now();due=deadline(paid)
    points=2 if now<=paid+timedelta(hours=24) else 1 if now<=due else 0
    session.add(ShippingPerformance(order_id=order.id,seller_id=order.seller_id,points=points,due_at=due,submitted_at=now))


def score(session,user_id):
    count,points=session.execute(select(func.count(),func.coalesce(func.sum(ShippingPerformance.points),0)).where(ShippingPerformance.seller_id==user_id)).one()
    on_time=session.scalar(select(func.count()).select_from(ShippingPerformance).where(ShippingPerformance.seller_id==user_id,ShippingPerformance.points>0))
    pending = session.scalars(select(Order).where(Order.seller_id==user_id, Order.status=='paid', Order.paid_at.is_not(None))).all()
    late = sum(1 for o in pending if utc_now() > deadline(o.paid_at))
    return {'shipping_score':points,'shipping_scored_sales':count,'shipping_on_time_sales':on_time,'shipping_overdue_sales':late}
