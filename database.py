from datetime import datetime, timedelta, timezone
import hashlib
import time

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


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
    required = {"required_front_straight", "required_front_slight_left", "required_front_slight_right", "required_back"}
    if not listing.photos_persisted or not required.issubset({i.label for i in listing.images if i.size_bytes > 0 and i.object_key}):
        raise ListingValidationError("Upload all four required card photos before publishing.")


def utc_now():
    return datetime.now(timezone.utc)


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
    shipping_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    shipping_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    shipping_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    shipping_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shipping_state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shipping_postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shipping_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payout_status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    stripe_transfer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stripe_refund_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
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
            for name, definition in (("email_verified", "BOOLEAN NOT NULL DEFAULT false"), ("session_version", "INTEGER NOT NULL DEFAULT 0")):
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
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_listings_seller_id ON listings (seller_id)")
            )
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
            ):
                if name not in order_columns:
                    connection.execute(text(f"ALTER TABLE orders ADD COLUMN {name} {definition}"))

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
            statement = select(ScanJob).where(ScanJob.status == "queued").order_by(ScanJob.created_at).limit(1)
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            job = session.execute(statement).scalar_one_or_none()
            if job is None:
                return None
            job.status = "processing"
            job.attempts += 1
            job.updated_at = utc_now()
            session.flush()
            return self._scan_job_dict(job)

    def finish_scan_job(self, job_id, result=None, error=None):
        with self.sessions.begin() as session:
            job = session.get(ScanJob, job_id, with_for_update=True)
            if job is None:
                return None
            job.status = "complete" if error is None else "failed"
            job.result = result if error is None else None
            job.error = error
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

    def marketplace(self, limit=20, offset=0, query="", listing_id=None):
        with self.sessions() as session:
            statement = select(Listing, User).join(User, Listing.seller_id == User.id).where(Listing.status == "published", Listing.publication_approved.is_(True))
            if listing_id is not None:
                statement = statement.where(Listing.id == listing_id)
            if query.strip():
                statement = statement.where(Listing.title.icontains(query.strip(), autoescape=True))
            rows = session.execute(statement.order_by(Listing.updated_at.desc(), Listing.id).limit(limit).offset(offset)).all()
            results = []
            for listing, seller in rows:
                record = self._listing_dict(listing)
                # Explicit public allowlist: no email, scan payload, local notes or credentials.
                public = {key: record[key] for key in ("id", "title", "description", "price_cents", "currency", "card_name", "set_name", "card_number", "estimated_condition", "updated_at", "images")}
                public["seller"] = {"display_name": seller.display_name, "tier": seller.seller_tier}
                results.append(public)
            return results

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
            "shipping_name": order.shipping_name,
            "shipping_line1": order.shipping_line1,
            "shipping_line2": order.shipping_line2,
            "shipping_city": order.shipping_city,
            "shipping_state": order.shipping_state,
            "shipping_postal_code": order.shipping_postal_code,
            "shipping_country": order.shipping_country,
            "delivered_at": order.delivered_at,
            "hold_until": order.hold_until, "completed_at": order.completed_at,
            "payout_status": order.payout_status, "stripe_transfer_id": order.stripe_transfer_id,
            "stripe_refund_id": order.stripe_refund_id,
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

    def create_pending_order(self, order_id, listing_id, buyer_id, commission_percent, shipping_cents=0):
        """Create a checkout attempt without reserving the one-of-one listing."""
        with self.sessions.begin() as session:
            listing = session.get(Listing, listing_id, with_for_update=True)
            if listing is None or listing.status != "published" or not listing.publication_approved:
                raise ListingValidationError("This card is no longer available.")
            if listing.seller_id == buyer_id:
                raise ListingValidationError("You cannot buy your own listing.")
            item_cents = int(listing.price_cents or 0)
            commission_cents = (item_cents * commission_percent + 99) // 100
            order = Order(id=order_id, listing_id=listing_id, buyer_id=buyer_id, seller_id=listing.seller_id, item_cents=item_cents, shipping_cents=shipping_cents, commission_cents=commission_cents, seller_amount_cents=item_cents + shipping_cents - commission_cents, currency=listing.currency)
            session.add(order)
            session.flush()
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
                listing.status = "sold"
                listing.publication_approved = False
                listing.updated_at = utc_now()
                order.updated_at = utc_now()
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
                order.updated_at = utc_now()
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
            return self._order_dict(
                order,
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
            return self._order_dict(order)

    def confirm_delivery(self, order_id, buyer_id, hold_days):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or order.buyer_id != buyer_id:
                return None
            if order.status not in {"paid", "shipped", "delivered"}:
                raise ListingValidationError("This order is not ready for delivery confirmation.")
            order.status = "protection_hold"
            order.delivered_at = order.delivered_at or utc_now()
            order.hold_until = order.hold_until or utc_now() + timedelta(days=hold_days)
            order.updated_at = utc_now()
            return self._order_dict(order)

    def list_orders(self, user_id, limit=50, offset=0):
        with self.sessions() as session:
            orders = session.execute(
                select(Order).where((Order.buyer_id == user_id) | (Order.seller_id == user_id))
                .order_by(Order.created_at.desc()).limit(limit).offset(offset)
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

    def mark_payout(self, order_id, status, transfer_id=None):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None:
                return None
            order.payout_status = status
            if transfer_id:
                order.stripe_transfer_id = transfer_id
            order.updated_at = utc_now()
            return self._order_dict(order)

    def set_tracking(self, order_id, seller_id, tracking_number):
        with self.sessions.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or order.seller_id != seller_id:
                return None
            if order.status not in {"paid", "shipped"}:
                raise ListingValidationError("Tracking can only be added to a paid order.")
            order.tracking_number = tracking_number
            order.status = "shipped"
            order.updated_at = utc_now()
            return self._order_dict(order)

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
            order.status = "completed"
            order.completed_at = utc_now()
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
            return self._order_dict(order)
