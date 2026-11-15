from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import SubscriptionStatus
from app.models.graph_subscription import GraphSubscription


class SubscriptionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_subscription_id(self, subscription_id: str) -> GraphSubscription | None:
        return self.session.scalar(
            select(GraphSubscription).where(GraphSubscription.subscription_id == subscription_id)
        )

    def list(self, *, active_only: bool = False) -> list[GraphSubscription]:
        query = select(GraphSubscription).order_by(GraphSubscription.id.desc())
        if active_only:
            query = query.where(GraphSubscription.status == SubscriptionStatus.ACTIVE)
        return list(self.session.scalars(query).all())

    def active_for_resource(self, resource: str) -> GraphSubscription | None:
        return self.session.scalar(
            select(GraphSubscription)
            .where(
                GraphSubscription.resource == resource,
                GraphSubscription.status == SubscriptionStatus.ACTIVE,
            )
            .order_by(GraphSubscription.expiration_at.desc())
        )

    def expiring_before(self, cutoff: datetime) -> list[GraphSubscription]:
        return list(
            self.session.scalars(
                select(GraphSubscription).where(
                    GraphSubscription.status == SubscriptionStatus.ACTIVE,
                    GraphSubscription.expiration_at <= cutoff,
                )
            ).all()
        )

    def add(self, subscription: GraphSubscription) -> GraphSubscription:
        self.session.add(subscription)
        self.session.flush()
        return subscription
