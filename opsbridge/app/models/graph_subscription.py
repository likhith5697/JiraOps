from __future__ import annotations

from datetime import datetime

from sqlalchemy import Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.database.base import Base, UTCDateTime
from app.domain.enums import SubscriptionStatus


class GraphSubscription(Base):
    __tablename__ = "graph_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    resource: Mapped[str] = mapped_column(String(512), nullable=False)
    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    notification_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    lifecycle_notification_url: Mapped[str | None] = mapped_column(String(1024))
    expiration_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, native_enum=False, length=16, validate_strings=True),
        default=SubscriptionStatus.ACTIVE,
        nullable=False,
    )
    last_renewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )
