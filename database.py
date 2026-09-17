from datetime import datetime, timezone
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


class Database:
    def __init__(self, database_url):
        url = normalize_database_url(database_url)
        engine_options = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False}
        else:
            engine_options["pool_recycle"] = 300
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
