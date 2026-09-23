from datetime import date, datetime, timedelta, timezone
import hashlib
import time

from sqlalchemy import (
    and_,
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, aliased, mapped_column, relationship, sessionmaker


class DatabaseConfigurationError(RuntimeError):
    pass


class DatabaseOperationError(RuntimeError):
    pass


class UserAlreadyExistsError(DatabaseOperationError):
    pass


class ListingOwnershipError(DatabaseOperationError):
    pass


class ListingValidationError(DatabaseOperationError):
    pass


def validate_publication(listing, seller):
    for field in ("title", "description", "card_name", "estimated_condition"):
        if not (getattr(listing, field) or "").strip():
            raise ListingValidationError(f"Add {field.replace('_', ' ')} before publishing.")
    if listing.currency != "USD" or not listing.price_cents or listing.price_cents < 1:
        raise ListingValidationError("Set a positive USD price before publishing.")
    if seller.max_listing_cents is not None and listing.price_cents > seller.max_listing_cents:
        raise ListingValidationError("Price exceeds your seller tier limit.")
    if not all((seller.seller_address_name, seller.seller_address_line1, seller.seller_address_city, seller.seller_address_state, seller.seller_address_postal_code)):
        raise ListingValidationError("Add your seller return address in Account before publishing.")
    required = {"required_front_straight", "required_front_slight_left", "required_front_slight_right", "required_back"}
    if not listing.photos_persisted or not required.issubset({i.label for i in listing.images if i.size_bytes > 0 and i.object_key}):
        raise ListingValidationError("Upload all four required card photos before publishing.")


def utc_now():
    return datetime.now(timezone.utc)


RARITY_TIERS = {
    "rare", "double_rare", "ultra_rare", "illustration_rare",
    "special_illustration_rare", "hyper_illustration_rare",
}


def normalize_rarity(value):
    text_value = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
    text_value = " ".join(text_value.split())
    if "special illustration" in text_value:
        return "special_illustration_rare"
    if "hyper illustration" in text_value or "hyper rare" in text_value:
        return "hyper_illustration_rare"
    if "illustration" in text_value:
        return "illustration_rare"
    if "double" in text_value:
        return "double_rare"
    if "ultra" in text_value:
        return "ultra_rare"
    return "rare"


def rarity_from_ai_result(result):
    if not isinstance(result, dict):
        return "rare"
    identification = result.get("identification") if isinstance(result.get("identification"), dict) else {}
    tcgdex = result.get("tcgdex") if isinstance(result.get("tcgdex"), dict) else {}
    return normalize_rarity(identification.get("rarity") or tcgdex.get("rarity") or result.get("rarity") or identification.get("variant"))


def normalize_database_url(url):
    value = (url or "").strip()
    if not value:
        raise DatabaseConfigurationError("Missing required environment variable: DATABASE_URL")
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://"):]
    return value


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    password_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    password_iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seller_tier: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    completed_buys: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_sales: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_sales_over_100: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_listing_cents: Mapped[int | None] = mapped_column(Integer, default=8_000, nullable=True)
    stripe_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    seller_address_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    seller_address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    seller_address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    seller_address_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    seller_address_state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    seller_address_postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    seller_address_country: Mapped[str | None] = mapped_column(String(2), nullable=True)


class AccountAction(Base):
    __tablename__ = "account_actions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)
    session_version: Mapped[int] = mapped_column(Integer)


class AuthRateBucket(Base):
    __tablename__ = "auth_rate_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seller_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scan_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False, index=True)
    publication_approved: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    card_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    set_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    card_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tcgdex_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    estimated_condition: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rarity_tier: Mapped[str] = mapped_column(String(40), default="rare", server_default="rare", nullable=False, index=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False, index=True)
    photos_persisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False, index=True
    )

    images: Mapped[list["ListingImage"]] = relationship(
        back_populates="listing",
        cascade="all, delete-orphan",
        order_by="ListingImage.sort_order",
    )


class ListingImage(Base):
    __tablename__ = "listing_images"
    __table_args__ = (
        UniqueConstraint("listing_id", "label", name="uq_listing_image_label"),
        UniqueConstraint("object_key", name="uq_listing_image_object_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_defect: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    listing: Mapped[Listing] = relationship(back_populates="images")


class ListingView(Base):
    __tablename__ = "listing_views"
    __table_args__ = (UniqueConstraint("listing_id", "viewer_hash", "viewed_on", name="uq_listing_daily_view"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[str] = mapped_column(String(64), ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    viewer_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    viewed_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("stripe_checkout_session_id", name="uq_order_checkout_session"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    listing_id: Mapped[str] = mapped_column(String(64), ForeignKey("listings.id", ondelete="RESTRICT"), index=True)
    buyer_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    seller_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending_payment", nullable=False, index=True)
    item_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    shipping_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    commission_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    seller_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tracking_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tracking_carrier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tracking_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shipping_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    shipping_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    shipping_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    shipping_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shipping_state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shipping_postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shipping_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    seller_shipping_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    seller_shipping_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    seller_shipping_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    seller_shipping_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    seller_shipping_state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    seller_shipping_postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    seller_shipping_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_confirmation_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payout_status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    stripe_transfer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stripe_refund_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    return_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    return_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    return_tracking_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    return_tracking_carrier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    return_tracking_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    return_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    return_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    return_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    return_confirmation_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    pii_redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_reminder_3d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_reminder_1d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hold_reminder_3d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hold_reminder_1d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    return_reminder_3d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    return_reminder_1d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class OrderAuditEvent(Base):
    __tablename__ = "order_audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)


class AdminReportRun(Base):
    __tablename__ = "admin_report_runs"
    __table_args__ = (UniqueConstraint("period_start", "period_end", name="uq_admin_report_period"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recipient: Mapped[str] = mapped_column(String(254), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="sent")
    subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScanJob(Base):
    __tablename__ = "scan_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    listing_id: Mapped[str] = mapped_column(String(64), ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    include_condition: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    include_authenticity: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class DeviceToken(Base):
    __tablename__ = "device_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), default="android", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class Database:
    def __init__(self, database_url, pool_size=3, max_overflow=2, pool_timeout=15):
        url = normalize_database_url(database_url)
        engine_options = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False}
        else:
            engine_options.update(
                pool_recycle=300,
                pool_size=max(1, int(pool_size)),
                max_overflow=max(0, int(max_overflow)),
                pool_timeout=max(1, int(pool_timeout)),
            )
        self.engine = create_engine(url, **engine_options)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def initialize(self):
        Base.metadata.create_all(self.engine)
        user_columns = {column["name"] for column in inspect(self.engine).get_columns("users")}
        with self.engine.begin() as connection:
            for name, definition in (
                ("email_verified", "BOOLEAN NOT NULL DEFAULT false"),
                ("session_version", "INTEGER NOT NULL DEFAULT 0"),
                ("is_admin", "BOOLEAN NOT NULL DEFAULT false"),
                ("seller_address_name", "VARCHAR(160)"),
                ("seller_address_line1", "VARCHAR(200)"),
                ("seller_address_line2", "VARCHAR(200)"),
                ("seller_address_city", "VARCHAR(120)"),
                ("seller_address_state", "VARCHAR(120)"),
                ("seller_address_postal_code", "VARCHAR(32)"),
                ("seller_address_country", "VARCHAR(2)"),
            ):
                if name not in user_columns:
                    connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))
        # create_all is intentionally non-destructive and does not add columns to
        # the existing Neon table. Apply this one safe migration in place.
        columns = {column["name"] for column in inspect(self.engine).get_columns("listings")}
        if "seller_id" not in columns:
            with self.engine.begin() as connection:
                connection.execute(text("ALTER TABLE listings ADD COLUMN seller_id VARCHAR(36)"))
        if "publication_approved" not in columns:
            with self.engine.begin() as connection:
                connection.execute(text("ALTER TABLE listings ADD COLUMN publication_approved BOOLEAN NOT NULL DEFAULT false"))
                connection.execute(text("UPDATE listings SET status = 'draft' WHERE status = 'published'"))
        with self.engine.begin() as connection:
            if "rarity_tier" not in columns:
                connection.execute(text("ALTER TABLE listings ADD COLUMN rarity_tier VARCHAR(40) NOT NULL DEFAULT 'rare'"))
            if "view_count" not in columns:
                connection.execute(text("ALTER TABLE listings ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0"))
        with self.engine.begin() as connection:
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_listings_seller_id ON listings (seller_id)")
            )
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_listings_view_count ON listings (view_count)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_listings_rarity_tier ON listings (rarity_tier)"))
        user_columns = {column["name"] for column in inspect(self.engine).get_columns("users")}
        if "stripe_account_id" not in user_columns:
            with self.engine.begin() as connection:
                connection.execute(text("ALTER TABLE users ADD COLUMN stripe_account_id VARCHAR(64)"))
        order_columns = {column["name"] for column in inspect(self.engine).get_columns("orders")}
        with self.engine.begin() as connection:
            if "payout_status" not in order_columns:
                connection.execute(text("ALTER TABLE orders ADD COLUMN payout_status VARCHAR(24) NOT NULL DEFAULT 'pending'"))
            if "stripe_transfer_id" not in order_columns:
                connection.execute(text("ALTER TABLE orders ADD COLUMN stripe_transfer_id VARCHAR(128)"))
            if "stripe_refund_id" not in order_columns:
                connection.execute(text("ALTER TABLE orders ADD COLUMN stripe_refund_id VARCHAR(128)"))
            for name, definition in (
                ("shipping_name", "VARCHAR(160)"),
                ("shipping_line1", "VARCHAR(200)"),
                ("shipping_line2", "VARCHAR(200)"),
                ("shipping_city", "VARCHAR(120)"),
                ("shipping_state", "VARCHAR(120)"),
                ("shipping_postal_code", "VARCHAR(32)"),
                ("shipping_country", "VARCHAR(2)"),
                ("delivery_confirmation_due_at", "TIMESTAMP"),
                ("return_reason", "VARCHAR(64)"),
                ("return_notes", "TEXT"),
                ("return_tracking_number", "VARCHAR(128)"),
                ("return_requested_at", "TIMESTAMP"),
                ("return_approved_at", "TIMESTAMP"),
                ("return_received_at", "TIMESTAMP"),
                ("return_confirmation_due_at", "TIMESTAMP"),
                ("tracking_carrier", "VARCHAR(32)"),
                ("tracking_status", "VARCHAR(32)"),
                ("seller_shipping_name", "VARCHAR(160)"),
                ("seller_shipping_line1", "VARCHAR(200)"),
                ("seller_shipping_line2", "VARCHAR(200)"),
                ("seller_shipping_city", "VARCHAR(120)"),
                ("seller_shipping_state", "VARCHAR(120)"),
                ("seller_shipping_postal_code", "VARCHAR(32)"),
                ("seller_shipping_country", "VARCHAR(2)"),
                ("return_tracking_carrier", "VARCHAR(32)"),
                ("return_tracking_status", "VARCHAR(32)"),
                ("paid_at", "TIMESTAMP"),
                ("pii_redacted_at", "TIMESTAMP"),
                ("delivery_reminder_3d_at", "TIMESTAMP"),
                ("delivery_reminder_1d_at", "TIMESTAMP"),
                ("hold_reminder_3d_at", "TIMESTAMP"),
                ("hold_reminder_1d_at", "TIMESTAMP"),
                ("return_reminder_3d_at", "TIMESTAMP"),
                ("return_reminder_1d_at", "TIMESTAMP"),
            ):
                if name not in order_columns:
                    connection.execute(text(f"ALTER TABLE orders ADD COLUMN {name} {definition}"))
        scan_columns = {column["name"] for column in inspect(self.engine).get_columns("scan_jobs")}
        if "next_attempt_at" not in scan_columns:
            with self.engine.begin() as connection:
                connection.execute(text("ALTER TABLE scan_jobs ADD COLUMN next_attempt_at TIMESTAMP"))
        with self.engine.begin() as connection:
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_orders_delivery_confirmation_due_at ON orders (delivery_confirmation_due_at)")
            )
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_orders_return_confirmation_due_at ON orders (return_confirmation_due_at)")
            )
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_paid_at ON orders (paid_at)"))
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_scan_jobs_next_attempt_at ON scan_jobs (next_attempt_at)")
            )

    def ping(self):
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    @staticmethod
    def _user_dict(user):
        return {
            "id": user.id,
            "email": user.email,
            "email_verified": user.email_verified,
            "session_version": user.session_version,
            "display_name": user.display_name,
            "created_at": user.created_at,
            "seller_tier": user.seller_tier,
            "completed_buys": user.completed_buys,
            "successful_sales": user.successful_sales,
            "successful_sales_over_100": user.successful_sales_over_100,
            "max_listing_cents": user.max_listing_cents,
            "stripe_account_id": user.stripe_account_id,
            "stripe_connected": bool(user.stripe_account_id),
            "is_admin": user.is_admin,
        }

    @staticmethod
    def _seller_address_dict(user):
        if user is None:
            return None
        return {
            "name": user.seller_address_name or "", "line1": user.seller_address_line1 or "",
            "line2": user.seller_address_line2 or "", "city": user.seller_address_city or "",
            "state": user.seller_address_state or "", "postal_code": user.seller_address_postal_code or "",
            "country": user.seller_address_country or "US",
            "configured": bool(user.seller_address_name and user.seller_address_line1 and user.seller_address_city and user.seller_address_state and user.seller_address_postal_code),
        }

    @staticmethod
    def _listing_dict(listing):
        return {
            "id": listing.id,
            "seller_id": listing.seller_id,
            "scan_id": listing.scan_id,
            "status": listing.status,
            "title": listing.title,
            "description": listing.description,
            "price_cents": listing.price_cents,
            "currency": listing.currency,
            "card_name": listing.card_name,
            "set_name": listing.set_name,
            "card_number": listing.card_number,
            "tcgdex_id": listing.tcgdex_id,
            "estimated_condition": listing.estimated_condition,
            "ai_result": listing.ai_result,
            "rarity_tier": listing.rarity_tier,
            "view_count": listing.view_count,
            "photos_persisted": listing.photos_persisted,
            "created_at": listing.created_at,
            "updated_at": listing.updated_at,
            "images": [
                {
                    "id": image.id,
                    "label": image.label,
                    "object_key": image.object_key,
                    "content_type": image.content_type,
                    "size_bytes": image.size_bytes,
                    "sort_order": image.sort_order,
                    "is_defect": image.is_defect,
                    "created_at": image.created_at,
                }
                for image in listing.images
            ],
        }

    def create_user(
        self,
        user_id,
        email,
        display_name,
        password_hash,
        password_salt,
        password_iterations,
        claim_legacy=False,
    ):
        try:
            with self.sessions.begin() as session:
                user = User(
                    id=user_id,
                    email=email,
                    display_name=display_name,
                    password_hash=password_hash,
                    password_salt=password_salt,
                    password_iterations=password_iterations,
                )
                session.add(user)
                session.flush()
                claimed = 0
                if claim_legacy:
                    claimed = session.execute(
                        update(Listing)
                        .where(Listing.seller_id.is_(None))
                        .values(seller_id=user.id, updated_at=utc_now())
                    ).rowcount
                result = self._user_dict(user)
            return result, claimed
        except IntegrityError as exc:
            raise UserAlreadyExistsError("An account already exists for this email.") from exc
        except DatabaseOperationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError(f"Could not create account: {exc}") from exc

    def get_user_by_email(self, email):
        with self.sessions() as session:
            user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if user is None:
                return None
            result = self._user_dict(user)
            result.update(
                password_hash=user.password_hash,
                password_salt=user.password_salt,
                password_iterations=user.password_iterations,
            )
            return result

    def get_user(self, user_id):
        with self.sessions() as session:
            user = session.get(User, user_id)
            return self._user_dict(user) if user else None

    def set_admin(self, user_id, enabled=True):
        with self.sessions.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if user is None:
                return None
            user.is_admin = bool(enabled)
            return self._user_dict(user)

    def get_seller_address(self, user_id):
        with self.sessions() as session:
            return self._seller_address_dict(session.get(User, user_id))

    def update_seller_address(self, user_id, address):
        with self.sessions.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if user is None:
                return None
            for field in ("name", "line1", "line2", "city", "state", "postal_code", "country"):
                setattr(user, f"seller_address_{field}", address.get(field) or None)
            return self._seller_address_dict(user)

    def list_users(self, limit=100, offset=0, query=""):
        with self.sessions() as session:
            statement = select(User)
            if query.strip():
                term = f"%{query.strip().lower()}%"
                statement = statement.where(
                    User.email.ilike(term) | User.display_name.ilike(term)
                )
            users = session.execute(
                statement.order_by(User.created_at.desc()).limit(limit).offset(offset)
            ).scalars().all()
            return [self._user_dict(user) for user in users]

    def set_seller_tier(self, user_id, tier):
        limits = {1: 8_000, 2: 10_000, 3: 20_000, 4: 25_000, 5: 100_000}
        if tier not in limits:
            raise ListingValidationError("Seller tier must be between 1 and 5.")
        with self.sessions.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if user is None:
                return None
            user.seller_tier = tier
            user.max_listing_cents = limits[tier]
            return self._user_dict(user)

    def register_device_token(self, user_id, token, platform="android"):
        with self.sessions.begin() as session:
            record = session.execute(
                select(DeviceToken).where(DeviceToken.token == token)
            ).scalar_one_or_none()
            if record is None:
                record = DeviceToken(user_id=user_id, token=token, platform=platform)
                session.add(record)
            else:
                record.user_id = user_id
                record.platform = platform
                record.updated_at = utc_now()
            session.flush()
            return {"status": "registered"}

    def device_tokens_for_users(self, user_ids):
        ids = list(set(user_ids))
        if not ids:
            return []
        with self.sessions() as session:
            return [
                row[0] for row in session.execute(
                    select(DeviceToken.token).where(DeviceToken.user_id.in_(ids))
                ).all()
            ]

    def delete_device_token(self, token):
        with self.sessions.begin() as session:
            return session.execute(delete(DeviceToken).where(DeviceToken.token == token)).rowcount

    def set_stripe_account(self, user_id, account_id):
        with self.sessions.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if user is None:
                return None
            user.stripe_account_id = account_id
            return self._user_dict(user)

    def take_auth_rate(self, key, limit, seconds, now=None):
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from sqlalchemy.dialects.postgresql import insert as postgres_insert
        now = int(time.time()) if now is None else now
        window = now // seconds
        digest = hashlib.sha256(f"{key}:{window}".encode()).hexdigest()
        insert = sqlite_insert if self.engine.dialect.name == "sqlite" else postgres_insert
        with self.sessions.begin() as session:
            session.execute(delete(AuthRateBucket).where(AuthRateBucket.expires_at < now))
            statement = insert(AuthRateBucket).values(key=digest, count=1, expires_at=(window + 1) * seconds)
            statement = statement.on_conflict_do_update(index_elements=["key"], set_={"count": AuthRateBucket.count + 1})
            count = session.execute(statement.returning(AuthRateBucket.count)).scalar_one()
        return count <= limit

    def issue_account_action(self, user_id, purpose, token_hash, ttl):
        now = int(time.time())
        with self.sessions.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if user is None or (purpose == "verify" and user.email_verified):
                return None
            session.execute(delete(AccountAction).where(AccountAction.expires_at <= now))
            session.add(AccountAction(token_hash=token_hash, user_id=user.id, purpose=purpose, expires_at=now + ttl, session_version=user.session_version))
            return user.email

    def consume_account_action(self, token_hash, purpose, password=None):
        now = int(time.time())
        with self.sessions.begin() as session:
            action = session.get(AccountAction, token_hash)
            if action is None or action.purpose != purpose or action.expires_at <= now:
                return False
            user = session.get(User, action.user_id, with_for_update=True)
            if user is None or action.session_version != user.session_version:
                return False
            # Conditional delete makes each token single-use even on SQLite.
            removed = session.execute(delete(AccountAction).where(AccountAction.token_hash == token_hash, AccountAction.expires_at > now)).rowcount
            if removed != 1:
                return False
            if purpose == "reset":
                user.password_salt, user.password_hash, user.password_iterations = password
                user.session_version += 1
                session.execute(delete(AccountAction).where(AccountAction.user_id == user.id))
            else:
                user.email_verified = True
                session.execute(delete(AccountAction).where(AccountAction.user_id == user.id, AccountAction.purpose == "verify"))
            return True

    def record_login(self, user_id):
        with self.sessions.begin() as session:
            session.execute(
                update(User).where(User.id == user_id).values(last_login_at=utc_now())
            )

    def ensure_listing_owner(self, listing_id, seller_id, create=False):
        try:
            with self.sessions.begin() as session:
                listing = session.get(Listing, listing_id)
                if listing is None:
                    if not create:
                        return None
                    listing = Listing(id=listing_id, seller_id=seller_id)
                    session.add(listing)
                    session.flush()
                elif listing.seller_id != seller_id:
                    raise ListingOwnershipError("Listing not found.")
                return self._listing_dict(listing)
        except ListingOwnershipError:
            raise
        except Exception as exc:
            raise DatabaseOperationError(f"Could not verify listing ownership: {exc}") from exc

    def replace_images(self, listing_id, stored_images, seller_id):
        try:
            with self.sessions.begin() as session:
                listing = session.get(Listing, listing_id, with_for_update=True)
                if listing is None:
                    listing = Listing(id=listing_id, seller_id=seller_id)
                    session.add(listing)
                    session.flush()
                elif listing.seller_id != seller_id:
                    raise ListingOwnershipError("Listing not found.")

                stale_keys = list(
                    session.execute(
                        select(ListingImage.object_key).where(
                            ListingImage.listing_id == listing_id
                        )
                    ).scalars()
                )
                session.execute(delete(ListingImage).where(ListingImage.listing_id == listing_id))
                session.flush()

                for index, image in enumerate(stored_images):
                    session.add(
                        ListingImage(
                            listing=listing,
                            label=image["label"],
                            object_key=image["object_key"],
                            content_type=image["content_type"],
                            size_bytes=image["size_bytes"],
                            sort_order=index,
                            is_defect=image["label"].startswith("defect_"),
                        )
                    )

                listing.photos_persisted = True
                listing.publication_approved = False
                # Replacing photos requires a fresh publication review.
                if listing.status == "published":
                    listing.status = "draft"
                listing.updated_at = utc_now()
                session.flush()
                session.expire(listing, ["images"])
                result = self._listing_dict(listing)
            return result, stale_keys
        except ListingOwnershipError:
            raise
        except Exception as exc:
            raise DatabaseOperationError(f"Could not save image records: {exc}") from exc

    def upsert_listing(self, listing_id, values, seller_id):
        try:
            with self.sessions.begin() as session:
                listing = session.get(Listing, listing_id, with_for_update=True)
                if listing is None:
                    listing = Listing(id=listing_id, seller_id=seller_id)
                    session.add(listing)
                elif listing.seller_id != seller_id:
                    raise ListingOwnershipError("Listing not found.")
                if values.get("status") == "published" and listing.status != "published":
                    raise ListingValidationError("Use Publish Listing to make this draft public.")
                if "ai_result" in values:
                    listing.rarity_tier = rarity_from_ai_result(values.get("ai_result"))
                for key, value in values.items():
                    setattr(listing, key, value)
                if listing.status == "published":
                    validate_publication(listing, session.get(User, seller_id))
                else:
                    listing.publication_approved = False
                listing.updated_at = utc_now()
                session.flush()
                session.refresh(listing)
                return self._listing_dict(listing)
        except (ListingOwnershipError, ListingValidationError):
            raise
        except Exception as exc:
            raise DatabaseOperationError(f"Could not save listing: {exc}") from exc

    def get_listing(self, listing_id, seller_id):
        with self.sessions() as session:
            listing = session.execute(
                select(Listing).where(
                    Listing.id == listing_id,
                    Listing.seller_id == seller_id,
                )
            ).scalar_one_or_none()
            return self._listing_dict(listing) if listing else None

    def prepare_listing_deletion(self, listing_id, seller_id):
        """Resolve owned records and R2 keys before an irreversible deletion."""
        with self.sessions() as session:
            listing = session.get(Listing, listing_id)
            if listing is None:
                # Early Android releases sometimes retained the scan ID as the
                # local card ID after the cloud listing received a different ID.
                # scan_id is unique, so it is a safe backwards-compatible alias.
                listing = session.execute(
                    select(Listing).where(
                        Listing.scan_id == listing_id,
                        Listing.seller_id == seller_id,
                    )
                ).scalar_one_or_none()
            if listing is None or listing.seller_id != seller_id:
                raise ListingOwnershipError("Listing not found.")

            listings = [listing]
            # Release 0.15.0 could split metadata and uploaded photos across two
            # owned drafts. Clean up that narrowly identifiable orphan as well.
            if listing.scan_id and listing.scan_id != listing.id and not listing.images:
                linked = session.get(Listing, listing.scan_id)
                if (
                    linked is not None
                    and linked.seller_id == seller_id
                    and linked.photos_persisted
                    and not (linked.title or "").strip()
                ):
                    listings.append(linked)

            ids = [item.id for item in listings]
            order_exists = session.execute(
                select(Order.id).where(Order.listing_id.in_(ids)).limit(1)
            ).first()
            if order_exists:
                raise ListingValidationError(
                    "Cards with checkout or order history cannot be deleted. Archive the listing instead."
                )
            keys = [image.object_key for item in listings for image in item.images]
            return {"listing_ids": ids, "object_keys": keys}

    def delete_owned_listings(self, listing_ids, seller_id):
        try:
            with self.sessions.begin() as session:
                listings = session.execute(
                    select(Listing).where(
                        Listing.id.in_(listing_ids),
                        Listing.seller_id == seller_id,
                    )
                ).scalars().all()
                if len(listings) != len(set(listing_ids)):
                    raise ListingOwnershipError("Listing not found.")
                if session.execute(
                    select(Order.id).where(Order.listing_id.in_(listing_ids)).limit(1)
                ).first():
                    raise ListingValidationError(
                        "Cards with checkout or order history cannot be deleted. Archive the listing instead."
                    )
                for listing in listings:
                    session.delete(listing)
            return len(listings)
        except (ListingOwnershipError, ListingValidationError):
            raise
        except Exception as exc:
            raise DatabaseOperationError(f"Could not delete listing: {exc}") from exc

    @staticmethod
    def _scan_job_dict(job):
        return {
            "job_id": job.id,
            "listing_id": job.listing_id,
            "user_id": job.user_id,
            "status": job.status,
            "include_condition": job.include_condition,
            "include_authenticity": job.include_authenticity,
            "result": job.result,
            "error": job.error,
            "attempts": job.attempts,
            "next_attempt_at": job.next_attempt_at,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }

    def create_scan_job(self, job_id, listing_id, user_id, include_condition=True, include_authenticity=True):
        try:
            with self.sessions.begin() as session:
                listing = session.get(Listing, listing_id)
                if listing is None or listing.seller_id != user_id or not listing.photos_persisted:
                    raise ListingOwnershipError("Listing photos are not available for analysis.")
                job = ScanJob(
                    id=job_id,
                    listing_id=listing_id,
                    user_id=user_id,
                    include_condition=include_condition,
                    include_authenticity=include_authenticity,
                )
                session.add(job)
                session.flush()
                return self._scan_job_dict(job)
        except ListingOwnershipError:
            raise
        except Exception as exc:
            raise DatabaseOperationError(f"Could not queue scan: {exc}") from exc

    def get_scan_job(self, job_id, user_id):
        with self.sessions() as session:
            job = session.get(ScanJob, job_id)
            if job is None or job.user_id != user_id:
                return None
            return self._scan_job_dict(job)

    def claim_next_scan_job(self):
        with self.sessions.begin() as session:
            statement = select(ScanJob).where(
                ScanJob.status == "queued",
                (ScanJob.next_attempt_at.is_(None)) | (ScanJob.next_attempt_at <= utc_now()),
            ).order_by(ScanJob.created_at).limit(1)
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            job = session.execute(statement).scalar_one_or_none()
            if job is None:
                return None
            job.status = "processing"
            job.attempts += 1
            job.next_attempt_at = None
            job.updated_at = utc_now()
            session.flush()
            return self._scan_job_dict(job)

    def requeue_scan_job(self, job_id, delay_seconds, error):
        with self.sessions.begin() as session:
            job = session.get(ScanJob, job_id, with_for_update=True)
            if job is None:
                return None
            job.status = "queued"
            job.error = error
            job.next_attempt_at = utc_now() + timedelta(seconds=max(1, int(delay_seconds)))
            job.updated_at = utc_now()
            return self._scan_job_dict(job)

    def finish_scan_job(self, job_id, result=None, error=None):
        with self.sessions.begin() as session:
            job = session.get(ScanJob, job_id, with_for_update=True)
            if job is None:
                return None
            job.status = "complete" if error is None else "failed"
            job.result = result if error is None else None
            job.error = error
            job.next_attempt_at = None
            job.updated_at = utc_now()
            return self._scan_job_dict(job)

    def requeue_interrupted_scan_jobs(self):
        with self.sessions.begin() as session:
            return session.execute(
                update(ScanJob)
                .where(ScanJob.status == "processing")
                .values(status="queued", updated_at=utc_now())
            ).rowcount

    def set_publication(self, listing_id, seller_id, publish):
        with self.sessions.begin() as session:
            listing = session.get(Listing, listing_id, with_for_update=True)
            if listing is None or listing.seller_id != seller_id:
                raise ListingOwnershipError("Listing not found.")
            if listing.status not in {"draft", "published"}:
                raise ListingValidationError("Only a draft or published listing can use this action.")
            if publish:
                validate_publication(listing, session.get(User, seller_id))
            listing.status = "published" if publish else "draft"
            listing.publication_approved = publish
            listing.updated_at = utc_now()
            session.flush()
            return self._listing_dict(listing)

    def marketplace(self, limit=20, offset=0, query="", listing_id=None, top_viewed=False):
        with self.sessions() as session:
            statement = select(Listing, User).join(User, Listing.seller_id == User.id).where(Listing.status == "published", Listing.publication_approved.is_(True))
            if listing_id is not None:
                statement = statement.where(Listing.id == listing_id)
            if query.strip():
                term = query.strip()
                statement = statement.where(or_(
                    Listing.title.icontains(term, autoescape=True),
                    Listing.card_name.icontains(term, autoescape=True),
                    Listing.set_name.icontains(term, autoescape=True),
                    Listing.card_number.icontains(term, autoescape=True),
                ))
            # A new marketplace has no view history; show published listings
            # until the first genuine detail view establishes a ranking.
            if top_viewed:
                viewed = session.scalar(select(Listing.id).where(
                    Listing.status == "published", Listing.publication_approved.is_(True),
                    Listing.view_count > 0,
                ).limit(1))
                if viewed is not None:
                    statement = statement.where(Listing.view_count > 0)
            ordering = (Listing.view_count.desc(), Listing.updated_at.desc(), Listing.id) if top_viewed else (Listing.updated_at.desc(), Listing.id)
            rows = session.execute(statement.order_by(*ordering).limit(limit).offset(offset)).all()
            results = []
            for listing, seller in rows:
                record = self._listing_dict(listing)
                # Explicit public allowlist: no email, scan payload, local notes or credentials.
                public = {key: record[key] for key in ("id", "title", "description", "price_cents", "currency", "card_name", "set_name", "card_number", "estimated_condition", "rarity_tier", "view_count", "updated_at", "images")}
                ai = listing.ai_result if isinstance(listing.ai_result, dict) else {}
                verified = ai.get("tcgdex") if isinstance(ai.get("tcgdex"), dict) else {}
                identified = ai.get("identification") if isinstance(ai.get("identification"), dict) else {}
                types = verified.get("types")
                public["card_type"] = (types[0] if isinstance(types, list) and types and isinstance(types[0], str) else identified.get("card_type"))
                public["seller"] = {"display_name": seller.display_name, "tier": seller.seller_tier}
                results.append(public)
            return results

    def increment_listing_view(self, listing_id, viewer_hash, viewed_on=None):
        viewed_on = viewed_on or utc_now().date()
        try:
            with self.sessions.begin() as session:
                listing = session.get(Listing, listing_id, with_for_update=True)
                if listing is None or listing.status != "published" or not listing.publication_approved:
                    return None
                exists = session.execute(select(ListingView.id).where(
                    ListingView.listing_id == listing_id,
                    ListingView.viewer_hash == viewer_hash,
                    ListingView.viewed_on == viewed_on,
                )).scalar_one_or_none()
                if exists is None:
                    session.add(ListingView(listing_id=listing_id, viewer_hash=viewer_hash, viewed_on=viewed_on))
                    listing.view_count += 1
                return listing.view_count
        except IntegrityError:
            # A simultaneous duplicate detail request may win the unique insert.
            with self.sessions() as session:
                listing = session.get(Listing, listing_id)
                return listing.view_count if listing else None

    def prune_listing_view_deduplication(self, retention_days=90, now=None):
        cutoff = (now or utc_now()).date() - timedelta(days=max(1, retention_days))
        with self.sessions.begin() as session:
            return session.execute(delete(ListingView).where(ListingView.viewed_on < cutoff)).rowcount

    def list_listings(self, seller_id, limit=50, offset=0):
        with self.sessions() as session:
            listings = session.execute(
                select(Listing).where(Listing.seller_id == seller_id)
                .order_by(Listing.updated_at.desc())
                .limit(limit)
                .offset(offset)
            ).scalars().all()
            return [self._listing_dict(listing) for listing in listings]

    def repair_split_listings(self, seller_id):
        """Merge the brief 0.15.0 metadata/photo split without copying R2 data."""
        repaired = 0
        with self.sessions.begin() as session:
            metadata_listings = session.execute(
                select(Listing).where(
                    Listing.seller_id == seller_id,
                    Listing.scan_id.is_not(None),
                    Listing.photos_persisted.is_(False),
                )
            ).scalars().all()
            for metadata in metadata_listings:
                linked_id = metadata.scan_id
                if not linked_id or linked_id == metadata.id:
                    continue
                linked = session.get(Listing, linked_id)
                if (
                    linked is None
                    or linked.seller_id != seller_id
                    or not linked.photos_persisted
                    or (linked.title or "").strip()
                ):
                    continue
                if session.execute(
                    select(Order.id).where(Order.listing_id == linked.id).limit(1)
                ).first():
                    continue
                session.execute(
                    update(ListingImage)
                    .where(ListingImage.listing_id == linked.id)
                    .values(listing_id=metadata.id)
                )
                metadata.photos_persisted = True
                metadata.updated_at = utc_now()
                session.execute(delete(ScanJob).where(ScanJob.listing_id == linked.id))
                session.execute(delete(Listing).where(Listing.id == linked.id))
                repaired += 1
        return repaired

    @staticmethod
    def _order_dict(order, listing=None, buyer=None, seller=None):
        result = {
            "id": order.id, "listing_id": order.listing_id, "buyer_id": order.buyer_id,
            "seller_id": order.seller_id, "status": order.status,
            "item_cents": order.item_cents, "shipping_cents": order.shipping_cents,
            "commission_cents": order.commission_cents, "seller_amount_cents": order.seller_amount_cents,
            "currency": order.currency, "stripe_checkout_session_id": order.stripe_checkout_session_id,
            "stripe_payment_intent_id": order.stripe_payment_intent_id,
            "tracking_number": order.tracking_number,
            "tracking_carrier": order.tracking_carrier,
            "tracking_status": order.tracking_status,
            "shipping_name": order.shipping_name,
            "shipping_line1": order.shipping_line1,
            "shipping_line2": order.shipping_line2,
            "shipping_city": order.shipping_city,
            "shipping_state": order.shipping_state,
            "shipping_postal_code": order.shipping_postal_code,
            "shipping_country": order.shipping_country,
            "seller_shipping_name": order.seller_shipping_name,
            "seller_shipping_line1": order.seller_shipping_line1,
            "seller_shipping_line2": order.seller_shipping_line2,
            "seller_shipping_city": order.seller_shipping_city,
            "seller_shipping_state": order.seller_shipping_state,
            "seller_shipping_postal_code": order.seller_shipping_postal_code,
            "seller_shipping_country": order.seller_shipping_country,
            "delivered_at": order.delivered_at,
            "delivery_confirmation_due_at": order.delivery_confirmation_due_at,
            "hold_until": order.hold_until, "completed_at": order.completed_at,
            "payout_status": order.payout_status, "stripe_transfer_id": order.stripe_transfer_id,
            "stripe_refund_id": order.stripe_refund_id,
            "return_reason": order.return_reason,
            "return_notes": order.return_notes,
            "return_tracking_number": order.return_tracking_number,
            "return_tracking_carrier": order.return_tracking_carrier,
            "return_tracking_status": order.return_tracking_status,
            "return_requested_at": order.return_requested_at,
            "return_approved_at": order.return_approved_at,
            "return_received_at": order.return_received_at,
            "return_confirmation_due_at": order.return_confirmation_due_at,
            "paid_at": order.paid_at,
            "pii_redacted_at": order.pii_redacted_at,
            "created_at": order.created_at, "updated_at": order.updated_at,
        }
        if listing is not None:
            result.update(
                listing_title=listing.title,
                card_name=listing.card_name,
                set_name=listing.set_name,
                card_number=listing.card_number,
            )
        if buyer is not None:
            result["buyer_display_name"] = buyer.display_name
        if seller is not None:
            result["seller_display_name"] = seller.display_name
        return result

    @classmethod
    def _participant_order_dict(cls, order, requester_id, listing=None, buyer=None, seller=None):
        result = cls._order_dict(order, listing, buyer, seller)
        if requester_id == order.buyer_id and order.status not in {"return_approved", "return_shipped", "refund_pending", "refunded"}:
            for field in ("seller_shipping_name", "seller_shipping_line1", "seller_shipping_line2", "seller_shipping_city", "seller_shipping_state", "seller_shipping_postal_code", "seller_shipping_country"):
                result[field] = None
        return result

    @staticmethod
    def _audit(session, order_id, event_type, actor_user_id=None, details=None):
        session.add(OrderAuditEvent(order_id=order_id, event_type=event_type, actor_user_id=actor_user_id, details=details))

    @staticmethod
    def _decorate_admin_deadline(record, now=None):
        now = now or utc_now()
        mapping = {
            "shipped": ("delivery_confirmation", record.get("delivery_confirmation_due_at")),
            "protection_hold": ("seller_payout_hold", record.get("hold_until")),
            "return_shipped": ("return_confirmation", record.get("return_confirmation_due_at")),
        }
        kind, due = mapping.get(record.get("status"), (None, None))
        if due is not None and due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        record["active_deadline_type"] = kind
        record["active_deadline_at"] = due
        record["deadline_remaining_seconds"] = max(0, int((due - now).total_seconds())) if due else None
        return record

    def create_pending_order(self, order_id, listing_id, buyer_id, commission_percent, shipping_cents=0):
        """Create a checkout attempt without reserving the one-of-one listing."""
        with self.sessions.begin() as session:
            listing = session.get(Listing, listing_id, with_for_update=True)
            if listing is None or listing.status != "published" or not listing.publication_approved:
                raise ListingValidationError("This card is no longer available.")
            if listing.seller_id == buyer_id:
                raise ListingValidationError("You cannot buy your own listing.")
            seller = session.get(User, listing.seller_id)
            address = self._seller_address_dict(seller)
            if not address or not address["configured"]:
                raise ListingValidationError("The seller must add a return shipping address before this card can be purchased.")
            item_cents = int(listing.price_cents or 0)
            commission_cents = (item_cents * commission_percent + 99) // 100
            order = Order(
                id=order_id, listing_id=listing_id, buyer_id=buyer_id, seller_id=listing.seller_id,
                item_cents=item_cents, shipping_cents=shipping_cents, commission_cents=commission_cents,
                seller_amount_cents=item_cents + shipping_cents - commission_cents, currency=listing.currency,
                seller_shipping_name=address["name"], seller_shipping_line1=address["line1"],
                seller_shipping_line2=address["line2"] or None, seller_shipping_city=address["city"],
                seller_shipping_state=address["state"], seller_shipping_postal_code=address["postal_code"],
                seller_shipping_country=address["country"],
            )
            session.add(order)
            session.flush()
            self._audit(session, order.id, "checkout_created", buyer_id)
            return self._order_dict(order)

    # Kept for callers from the previous backend release. Pending checkouts no
    # longer reserve inventory; only a confirmed payment can claim a listing.
    reserve_order = create_pending_order

    def attach_checkout_session(self, order_id, session_id):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None:
                return None
            order.stripe_checkout_session_id = session_id
            order.updated_at = utc_now()
            return self._order_dict(order)

    def cancel_order(self, order_id):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order and order.status == "pending_payment":
                order.status = "canceled"
                order.updated_at = utc_now()
            return self._order_dict(order) if order else None

    def claim_order_payment(self, session_id, payment_intent_id=None):
        """Atomically let the first confirmed payment claim the listing."""
        with self.sessions.begin() as session:
            order = session.execute(select(Order).where(Order.stripe_checkout_session_id == session_id).with_for_update()).scalar_one_or_none()
            if order is None:
                return None
            if order.status in {"paid", "shipped", "delivered", "protection_hold", "completed"}:
                return {"won": True, "already_processed": True, "order": self._order_dict(order)}
            if order.status == "refunded":
                return {"won": False, "already_processed": True, "order": self._order_dict(order)}
            if order.status == "refund_pending":
                return {"won": False, "already_processed": False, "order": self._order_dict(order)}
            if order.status != "pending_payment":
                return {"won": False, "already_processed": True, "order": self._order_dict(order)}

            listing = session.get(Listing, order.listing_id, with_for_update=True)
            if listing and listing.status == "published" and listing.publication_approved:
                order.status = "paid"
                order.stripe_payment_intent_id = payment_intent_id
                order.paid_at = order.paid_at or utc_now()
                listing.status = "sold"
                listing.publication_approved = False
                listing.updated_at = utc_now()
                order.updated_at = utc_now()
                self._audit(session, order.id, "payment_confirmed", order.buyer_id)
                session.flush()
                return {"won": True, "already_processed": False, "order": self._order_dict(order)}

            order.status = "refund_pending"
            order.stripe_payment_intent_id = payment_intent_id
            order.payout_status = "not_applicable"
            order.updated_at = utc_now()
            session.flush()
            return {"won": False, "already_processed": False, "order": self._order_dict(order)}

    def mark_order_refunded(self, order_id, refund_id):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None:
                return None
            if order.status == "refund_pending":
                order.status = "refunded"
                order.stripe_refund_id = refund_id
                order.payout_status = "not_applicable"
                order.updated_at = utc_now()
                if order.return_reason:
                    listing = session.get(Listing, order.listing_id, with_for_update=True)
                    if listing is not None:
                        listing.status = "draft"
                        listing.publication_approved = False
                        listing.updated_at = utc_now()
                self._audit(session, order.id, "refund_issued", details={"refund_id": refund_id})
            return self._order_dict(order)

    def mark_order_paid(self, session_id, payment_intent_id=None):
        """Compatibility wrapper returning the claimed order, if any."""
        result = self.claim_order_payment(session_id, payment_intent_id)
        return result["order"] if result else None

    def get_order(self, order_id, user_id):
        with self.sessions() as session:
            order = session.get(Order, order_id)
            if order is None or user_id not in {order.buyer_id, order.seller_id}:
                return None
            return self._participant_order_dict(
                order,
                user_id,
                session.get(Listing, order.listing_id),
                session.get(User, order.buyer_id),
                session.get(User, order.seller_id),
            )

    def save_shipping_details(self, session_id, shipping):
        with self.sessions.begin() as session:
            order = session.execute(
                select(Order).where(Order.stripe_checkout_session_id == session_id).with_for_update()
            ).scalar_one_or_none()
            if order is None:
                return None
            address = shipping.get("address") or {}
            order.shipping_name = (shipping.get("name") or "").strip() or None
            order.shipping_line1 = (address.get("line1") or "").strip() or None
            order.shipping_line2 = (address.get("line2") or "").strip() or None
            order.shipping_city = (address.get("city") or "").strip() or None
            order.shipping_state = (address.get("state") or "").strip() or None
            order.shipping_postal_code = (address.get("postal_code") or "").strip() or None
            order.shipping_country = (address.get("country") or "").strip().upper()[:2] or None
            order.updated_at = utc_now()
            self._audit(session, order.id, "buyer_shipping_saved", order.buyer_id)
            return self._order_dict(order)

    def confirm_delivery(self, order_id, buyer_id, hold_days):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or order.buyer_id != buyer_id:
                return None
            if order.status not in {"paid", "shipped", "delivered"}:
                raise ListingValidationError("This order is not ready for delivery confirmation.")
            delivery_due = order.delivery_confirmation_due_at
            if delivery_due is not None and delivery_due.tzinfo is None:
                delivery_due = delivery_due.replace(tzinfo=timezone.utc)
            if order.status == "shipped" and delivery_due and delivery_due < utc_now():
                raise ListingValidationError("The delivery confirmation deadline ended; automatic completion is processing.")
            order.status = "protection_hold"
            order.delivered_at = order.delivered_at or utc_now()
            order.hold_until = order.hold_until or utc_now() + timedelta(days=hold_days)
            order.updated_at = utc_now()
            self._audit(session, order.id, "delivery_confirmed", buyer_id)
            return self._order_dict(order)

    def list_orders(self, user_id, limit=50, offset=0):
        with self.sessions() as session:
            orders = session.execute(
                select(Order).where((Order.buyer_id == user_id) | (Order.seller_id == user_id))
                .order_by(Order.created_at.desc()).limit(limit).offset(offset)
            ).scalars().all()
            return [
                self._participant_order_dict(
                    order,
                    user_id,
                    session.get(Listing, order.listing_id),
                    session.get(User, order.buyer_id),
                    session.get(User, order.seller_id),
                )
                for order in orders
            ]

    def mark_payout(self, order_id, status, transfer_id=None):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None:
                return None
            order.payout_status = status
            if transfer_id:
                order.stripe_transfer_id = transfer_id
            order.updated_at = utc_now()
            self._audit(session, order.id, "payout_status_changed", details={"status": status, "transfer_id": transfer_id})
            return self._order_dict(order)

    def set_tracking(self, order_id, seller_id, tracking_number, carrier=None, timeout_days=10):
        if isinstance(carrier, (int, float)):
            timeout_days, carrier = carrier, "Unverified"
        carrier = carrier or "Unverified"
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or order.seller_id != seller_id:
                return None
            if order.status not in {"paid", "shipped"}:
                raise ListingValidationError("Tracking can only be added to a paid order.")
            reused = session.execute(select(Order.id).where(
                Order.id != order_id,
                or_(Order.tracking_number == tracking_number, Order.return_tracking_number == tracking_number),
            )).scalar_one_or_none()
            if reused:
                raise ListingValidationError("This tracking number is already attached to another order.")
            first_shipment = order.status == "paid"
            order.tracking_number = tracking_number
            order.tracking_carrier = carrier
            order.tracking_status = "submitted"
            order.status = "shipped"
            if first_shipment or order.delivery_confirmation_due_at is None:
                order.delivery_confirmation_due_at = utc_now() + timedelta(days=timeout_days)
            order.updated_at = utc_now()
            self._audit(session, order.id, "outbound_tracking_submitted", seller_id, {"carrier": carrier})
            return self._order_dict(order)

    def request_return(self, order_id, buyer_id, reason, notes=""):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or order.buyer_id != buyer_id:
                return None
            now = utc_now()
            hold_until = order.hold_until
            if hold_until is not None and hold_until.tzinfo is None:
                hold_until = hold_until.replace(tzinfo=timezone.utc)
            delivery_due = order.delivery_confirmation_due_at
            if delivery_due is not None and delivery_due.tzinfo is None:
                delivery_due = delivery_due.replace(tzinfo=timezone.utc)
            protected_after_confirmation = order.status == "protection_hold" and hold_until and hold_until >= now
            protected_while_awaiting_confirmation = order.status == "shipped" and delivery_due and delivery_due >= now
            if not (protected_after_confirmation or protected_while_awaiting_confirmation):
                raise ListingValidationError("Returns must be requested during the buyer-protection period.")
            order.status = "return_requested"
            order.return_reason = reason
            order.return_notes = notes.strip() or None
            order.return_requested_at = utc_now()
            order.payout_status = "on_hold"
            order.updated_at = utc_now()
            self._audit(session, order.id, "return_requested", buyer_id, {"reason": reason})
            return self._order_dict(order)

    def review_return(self, order_id, seller_id, approved):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or order.seller_id != seller_id:
                return None
            if order.status != "return_requested":
                raise ListingValidationError("This return is no longer awaiting seller review.")
            order.status = "return_approved" if approved else "return_disputed"
            if approved:
                order.return_approved_at = utc_now()
            order.updated_at = utc_now()
            self._audit(session, order.id, "return_approved" if approved else "return_disputed", seller_id)
            return self._order_dict(order)

    def set_return_tracking(self, order_id, buyer_id, tracking_number, carrier=None, timeout_days=10):
        if isinstance(carrier, (int, float)):
            timeout_days, carrier = carrier, "Unverified"
        carrier = carrier or "Unverified"
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or order.buyer_id != buyer_id:
                return None
            if order.status not in {"return_approved", "return_shipped"}:
                raise ListingValidationError("This return is not ready for return shipping.")
            reused = session.execute(select(Order.id).where(
                Order.id != order_id,
                or_(Order.tracking_number == tracking_number, Order.return_tracking_number == tracking_number),
            )).scalar_one_or_none()
            if reused or order.tracking_number == tracking_number:
                raise ListingValidationError("This tracking number is already attached to an order.")
            first_return_shipment = order.status == "return_approved"
            order.return_tracking_number = tracking_number
            order.return_tracking_carrier = carrier
            order.return_tracking_status = "submitted"
            order.status = "return_shipped"
            if first_return_shipment or order.return_confirmation_due_at is None:
                order.return_confirmation_due_at = utc_now() + timedelta(days=timeout_days)
            order.updated_at = utc_now()
            self._audit(session, order.id, "return_tracking_submitted", buyer_id, {"carrier": carrier})
            return self._order_dict(order)

    def begin_return_refund(self, order_id, seller_id):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or order.seller_id != seller_id:
                return None
            if order.status not in {"return_shipped", "refund_pending"}:
                raise ListingValidationError("The returned card must be shipped before confirming receipt.")
            if order.status == "return_shipped":
                order.status = "refund_pending"
                order.return_received_at = utc_now()
                order.updated_at = utc_now()
                self._audit(session, order.id, "return_received", seller_id)
            return self._order_dict(order)

    def resolve_return_dispute(self, order_id, approved, timeout_days=10):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None:
                return None
            if order.status != "return_disputed":
                raise ListingValidationError("This order does not have an open return dispute.")
            if approved:
                order.status = "return_approved"
                order.return_approved_at = utc_now()
            else:
                order.status = "protection_hold"
                order.payout_status = "pending"
                if order.hold_until is None:
                    order.delivered_at = order.delivered_at or utc_now()
                    order.hold_until = order.delivery_confirmation_due_at or (
                        utc_now() + timedelta(days=timeout_days)
                    )
            order.updated_at = utc_now()
            self._audit(session, order.id, "return_dispute_approved" if approved else "return_dispute_denied")
            return self._order_dict(order)

    def backfill_confirmation_deadlines(self, timeout_days=10):
        """Give legacy in-flight shipments a durable deadline without resetting newer clocks."""
        with self.sessions.begin() as session:
            orders = session.execute(
                select(Order).where(Order.status.in_(("shipped", "return_shipped")))
            ).scalars().all()
            changed = 0
            for order in orders:
                base = order.updated_at or order.created_at or utc_now()
                if base.tzinfo is None:
                    base = base.replace(tzinfo=timezone.utc)
                if order.status == "shipped" and order.delivery_confirmation_due_at is None:
                    order.delivery_confirmation_due_at = base + timedelta(days=timeout_days)
                    changed += 1
                elif order.status == "return_shipped" and order.return_confirmation_due_at is None:
                    order.return_confirmation_due_at = base + timedelta(days=timeout_days)
                    changed += 1
            return changed

    def list_due_purchase_settlements(self, now=None, limit=100):
        now = now or utc_now()
        with self.sessions() as session:
            orders = session.execute(
                select(Order).where(
                    or_(
                        and_(Order.status == "shipped", Order.delivery_confirmation_due_at <= now),
                        and_(Order.status == "protection_hold", Order.hold_until <= now),
                        and_(Order.status == "completed", Order.payout_status.in_(("pending", "failed"))),
                    )
                ).order_by(Order.updated_at).limit(limit)
            ).scalars().all()
            return [self._order_dict(order) for order in orders]

    def list_due_return_settlements(self, now=None, limit=100):
        now = now or utc_now()
        with self.sessions() as session:
            orders = session.execute(
                select(Order).where(
                    or_(
                        and_(Order.status == "return_shipped", Order.return_confirmation_due_at <= now),
                        and_(Order.status == "refund_pending", Order.return_reason.is_not(None)),
                    )
                ).order_by(Order.updated_at).limit(limit)
            ).scalars().all()
            return [self._order_dict(order) for order in orders]

    @staticmethod
    def _complete_locked_order(session, order, now):
        order.status = "completed"
        order.delivered_at = order.delivered_at or now
        order.hold_until = order.hold_until or now
        order.completed_at = now
        order.updated_at = now
        session.add(OrderAuditEvent(order_id=order.id, event_type="order_completed"))
        buyer, seller = session.get(User, order.buyer_id), session.get(User, order.seller_id)
        buyer.completed_buys += 1
        seller.successful_sales += 1
        if order.item_cents >= 10_000:
            seller.successful_sales_over_100 += 1
        if seller.successful_sales_over_100 >= 5:
            seller.seller_tier, seller.max_listing_cents = 5, 100_000
        elif seller.completed_buys >= 5 and seller.successful_sales >= 5:
            seller.seller_tier, seller.max_listing_cents = 4, 25_000
        elif seller.completed_buys >= 3 and seller.successful_sales >= 3:
            seller.seller_tier, seller.max_listing_cents = 3, 20_000
        elif seller.completed_buys >= 1 or seller.successful_sales >= 1:
            seller.seller_tier, seller.max_listing_cents = 2, 10_000

    def complete_due_order(self, order_id, now=None):
        now = now or utc_now()
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None:
                return None
            if order.status == "completed":
                return self._order_dict(order)
            delivery_due = order.delivery_confirmation_due_at
            hold_until = order.hold_until
            if delivery_due is not None and delivery_due.tzinfo is None:
                delivery_due = delivery_due.replace(tzinfo=timezone.utc)
            if hold_until is not None and hold_until.tzinfo is None:
                hold_until = hold_until.replace(tzinfo=timezone.utc)
            shipment_expired = order.status == "shipped" and delivery_due and delivery_due <= now
            protection_expired = order.status == "protection_hold" and hold_until and hold_until <= now
            if not (shipment_expired or protection_expired):
                return None
            self._complete_locked_order(session, order, now)
            return self._order_dict(order)

    def begin_due_return_refund(self, order_id, now=None):
        now = now or utc_now()
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None:
                return None
            if order.status == "refund_pending" and order.return_reason:
                return self._order_dict(order)
            due = order.return_confirmation_due_at
            if due is not None and due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if order.status != "return_shipped" or not due or due > now:
                return None
            order.status = "refund_pending"
            order.return_received_at = order.return_received_at or now
            order.updated_at = now
            self._audit(session, order.id, "return_auto_confirmed")
            return self._order_dict(order)

    def list_return_disputes(self):
        with self.sessions() as session:
            orders = session.execute(
                select(Order).where(Order.status == "return_disputed").order_by(Order.updated_at)
            ).scalars().all()
            return [
                self._order_dict(
                    order,
                    session.get(Listing, order.listing_id),
                    session.get(User, order.buyer_id),
                    session.get(User, order.seller_id),
                )
                for order in orders
            ]

    def list_admin_sales(self, limit=100, offset=0, query="", period_start=None, period_end=None):
        with self.sessions() as session:
            buyer_alias = aliased(User)
            seller_alias = aliased(User)
            statement = select(Order, Listing, buyer_alias, seller_alias).join(
                Listing, Order.listing_id == Listing.id
            ).join(buyer_alias, Order.buyer_id == buyer_alias.id).join(seller_alias, Order.seller_id == seller_alias.id)
            statement = statement.where(Order.status.not_in(("pending_payment", "canceled")))
            if period_start is not None:
                statement = statement.where(func.coalesce(Order.paid_at, Order.created_at) >= period_start)
            if period_end is not None:
                statement = statement.where(func.coalesce(Order.paid_at, Order.created_at) < period_end)
            if query.strip():
                needle = f"%{query.strip().lower()}%"
                statement = statement.where(or_(
                    func.lower(Order.id).like(needle), func.lower(Listing.title).like(needle),
                    func.lower(buyer_alias.email).like(needle), func.lower(seller_alias.email).like(needle),
                ))
            rows = session.execute(statement.order_by(func.coalesce(Order.paid_at, Order.created_at).desc()).limit(limit).offset(offset)).all()
            results = []
            for order, listing, buyer, seller in rows:
                record = self._order_dict(order, listing, buyer, seller)
                record.update(buyer_email=buyer.email, seller_email=seller.email)
                results.append(self._decorate_admin_deadline(record))
            return results

    def get_admin_sale(self, order_id):
        with self.sessions() as session:
            order = session.get(Order, order_id)
            if order is None:
                return None
            result = self._order_dict(order, session.get(Listing, order.listing_id), session.get(User, order.buyer_id), session.get(User, order.seller_id))
            result.update(buyer_email=session.get(User, order.buyer_id).email, seller_email=session.get(User, order.seller_id).email)
            self._decorate_admin_deadline(result)
            events = session.execute(select(OrderAuditEvent).where(OrderAuditEvent.order_id == order_id).order_by(OrderAuditEvent.created_at)).scalars().all()
            result["audit_events"] = [{"id": event.id, "event_type": event.event_type, "actor_user_id": event.actor_user_id, "details": event.details, "created_at": event.created_at} for event in events]
            return result

    def admin_diagnostics(self, now=None):
        now = now or utc_now()
        soon = now + timedelta(hours=24)
        with self.sessions() as session:
            scalar = lambda condition: session.scalar(select(func.count()).select_from(Order).where(condition)) or 0
            last_report = session.execute(select(AdminReportRun).where(AdminReportRun.status == "sent").order_by(AdminReportRun.sent_at.desc()).limit(1)).scalar_one_or_none()
            return {
                "open_orders": scalar(Order.status.in_(("paid", "shipped", "protection_hold", "return_requested", "return_approved", "return_shipped", "return_disputed", "refund_pending"))),
                "failed_payouts": scalar(Order.payout_status == "failed"),
                "pending_refunds": scalar(Order.status == "refund_pending"),
                "return_disputes": scalar(Order.status == "return_disputed"),
                "deadlines_within_24h": scalar(or_(
                    and_(Order.delivery_confirmation_due_at > now, Order.delivery_confirmation_due_at <= soon),
                    and_(Order.hold_until > now, Order.hold_until <= soon),
                    and_(Order.return_confirmation_due_at > now, Order.return_confirmation_due_at <= soon),
                )),
                "queued_scans": session.scalar(select(func.count()).select_from(ScanJob).where(ScanJob.status.in_(("queued", "processing")))) or 0,
                "failed_scans": session.scalar(select(func.count()).select_from(ScanJob).where(ScanJob.status == "failed")) or 0,
                "last_report_sent_at": last_report.sent_at if last_report else None,
                "last_report_subject": last_report.subject if last_report else None,
                "tracking_verification": "format_and_reuse_guard",
            }

    def due_deadline_reminders(self, now=None):
        now = now or utc_now()
        candidates = []
        with self.sessions() as session:
            orders = session.execute(select(Order).where(Order.status.in_(("shipped", "protection_hold", "return_shipped")))).scalars().all()
            for order in orders:
                if order.status == "shipped":
                    due, prefix, recipient = order.delivery_confirmation_due_at, "delivery", order.buyer_id
                elif order.status == "protection_hold":
                    due, prefix, recipient = order.hold_until, "hold", order.seller_id
                else:
                    due, prefix, recipient = order.return_confirmation_due_at, "return", order.seller_id
                if not due:
                    continue
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                remaining = due - now
                for days in (3, 1):
                    field = f"{prefix}_reminder_{days}d_at"
                    if timedelta(0) < remaining <= timedelta(days=days) and getattr(order, field) is None:
                        candidates.append({"order_id": order.id, "user_id": recipient, "kind": prefix, "days": days, "due_at": due})
                        break
        return candidates

    def mark_deadline_reminder(self, order_id, kind, days, now=None):
        field = f"{kind}_reminder_{days}d_at"
        if field not in {"delivery_reminder_3d_at", "delivery_reminder_1d_at", "hold_reminder_3d_at", "hold_reminder_1d_at", "return_reminder_3d_at", "return_reminder_1d_at"}:
            return False
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or getattr(order, field) is not None:
                return False
            setattr(order, field, now or utc_now())
            self._audit(session, order_id, f"{kind}_deadline_reminder_{days}d")
            return True

    def report_already_sent(self, period_start, period_end):
        with self.sessions() as session:
            return session.execute(select(AdminReportRun.id).where(AdminReportRun.period_start == period_start, AdminReportRun.period_end == period_end, AdminReportRun.status == "sent")).scalar_one_or_none() is not None

    def record_report_sent(self, period_start, period_end, recipient, subject):
        with self.sessions.begin() as session:
            existing = session.execute(select(AdminReportRun).where(AdminReportRun.period_start == period_start, AdminReportRun.period_end == period_end)).scalar_one_or_none()
            if existing is None:
                session.add(AdminReportRun(period_start=period_start, period_end=period_end, recipient=recipient, status="sent", subject=subject, sent_at=utc_now()))
            else:
                existing.recipient, existing.status, existing.subject, existing.sent_at = recipient, "sent", subject, utc_now()

    def redact_expired_sale_addresses(self, retention_days=30, now=None):
        cutoff = (now or utc_now()) - timedelta(days=retention_days)
        fields = ("shipping_name", "shipping_line1", "shipping_line2", "shipping_city", "shipping_state", "shipping_postal_code", "shipping_country", "seller_shipping_name", "seller_shipping_line1", "seller_shipping_line2", "seller_shipping_city", "seller_shipping_state", "seller_shipping_postal_code", "seller_shipping_country")
        with self.sessions.begin() as session:
            orders = session.execute(select(Order).where(Order.pii_redacted_at.is_(None), func.coalesce(Order.paid_at, Order.created_at) < cutoff, Order.status.not_in(("pending_payment", "canceled")))).scalars().all()
            for order in orders:
                for field in fields:
                    setattr(order, field, None)
                order.pii_redacted_at = now or utc_now()
                self._audit(session, order.id, "shipping_pii_redacted", details={"retention_days": retention_days})
            return len(orders)

    def complete_order(self, order_id, user_id):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or user_id not in {order.buyer_id, order.seller_id}:
                return None
            if order.status == "completed":
                # Completion is safe to retry so a failed Stripe payout can be
                # attempted again without incrementing trust counters twice.
                return self._order_dict(order)
            hold_until = order.hold_until
            if hold_until is not None and hold_until.tzinfo is None:
                hold_until = hold_until.replace(tzinfo=timezone.utc)
            if order.status != "protection_hold" or not hold_until or hold_until > utc_now():
                raise ListingValidationError("The 10-day buyer-protection period has not ended.")
            self._complete_locked_order(session, order, utc_now())
            return self._order_dict(order)
