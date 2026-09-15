from datetime import datetime, timezone

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
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class DatabaseConfigurationError(RuntimeError):
    pass


class DatabaseOperationError(RuntimeError):
    pass


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


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scan_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False, index=True)
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

    def ping(self):
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    @staticmethod
    def _listing_dict(listing):
        return {
            "id": listing.id,
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

    def replace_images(self, listing_id, stored_images):
        try:
            with self.sessions.begin() as session:
                listing = session.get(Listing, listing_id)
                if listing is None:
                    listing = Listing(id=listing_id)
                    session.add(listing)
                    session.flush()

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
                listing.updated_at = utc_now()
                session.flush()
                session.expire(listing, ["images"])
                result = self._listing_dict(listing)
            return result, stale_keys
        except Exception as exc:
            raise DatabaseOperationError(f"Could not save image records: {exc}") from exc

    def upsert_listing(self, listing_id, values):
        try:
            with self.sessions.begin() as session:
                listing = session.get(Listing, listing_id)
                if listing is None:
                    listing = Listing(id=listing_id)
                    session.add(listing)
                for key, value in values.items():
                    setattr(listing, key, value)
                listing.updated_at = utc_now()
                session.flush()
                session.refresh(listing)
                return self._listing_dict(listing)
        except Exception as exc:
            raise DatabaseOperationError(f"Could not save listing: {exc}") from exc

    def get_listing(self, listing_id):
        with self.sessions() as session:
            listing = session.execute(
                select(Listing).where(Listing.id == listing_id)
            ).scalar_one_or_none()
            return self._listing_dict(listing) if listing else None

    def list_listings(self, limit=50, offset=0):
        with self.sessions() as session:
            listings = session.execute(
                select(Listing)
                .order_by(Listing.updated_at.desc())
                .limit(limit)
                .offset(offset)
            ).scalars().all()
            return [self._listing_dict(listing) for listing in listings]
