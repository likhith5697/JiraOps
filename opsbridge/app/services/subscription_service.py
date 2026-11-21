"""Graph subscription lifecycle: create, renew, recover from removal."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from app.core.errors import NotFoundError
from app.core.time import utcnow
from app.database.session import Database
from app.domain.enums import SubscriptionStatus
from app.integrations.microsoft_graph import GraphClient
from app.models.graph_subscription import GraphSubscription
from app.repositories.subscription_repository import SubscriptionRepository

logger = logging.getLogger("opsbridge.subscriptions")


@dataclass(frozen=True)
class SubscriptionOutcome:
    subscription: GraphSubscription
    action: str  # created | existing | renewed | recreated


class SubscriptionService:
    def __init__(
        self,
        *,
        db: Database,
        graph: GraphClient,
        team_id: str,
        channel_id: str,
        base_url: str,
        client_state: str,
        lifetime_minutes: int,
        renewal_threshold_minutes: int,
    ) -> None:
        self.db = db
        self.graph = graph
        self.team_id = team_id
        self.channel_id = channel_id
        self.base_url = base_url.rstrip("/")
        self.client_state = client_state
        self.lifetime = timedelta(minutes=lifetime_minutes)
        self.threshold = timedelta(minutes=renewal_threshold_minutes)

    @property
    def resource(self) -> str:
        return f"/teams/{self.team_id}/channels/{self.channel_id}/messages"

    @property
    def notification_url(self) -> str:
        return f"{self.base_url}/webhooks/teams"

    @property
    def lifecycle_url(self) -> str:
        return f"{self.base_url}/webhooks/teams/lifecycle"

    def list(self) -> list[GraphSubscription]:
        with self.db.session() as session:
            return SubscriptionRepository(session).list()

    def create(self, *, force: bool = False) -> SubscriptionOutcome:
        with self.db.session() as session:
            existing = SubscriptionRepository(session).active_for_resource(self.resource)
        if existing and not force and existing.expiration_at > utcnow() + self.threshold:
            return SubscriptionOutcome(existing, "existing")
        return SubscriptionOutcome(self._create_new(), "created")

    def _create_new(self) -> GraphSubscription:
        if not self.base_url.startswith("https://") and not self.base_url.startswith("http://localhost"):
            logger.warning("APP_BASE_URL should be a public HTTPS URL for Graph to reach the webhook")
        info = self.graph.create_subscription(
            resource=self.resource,
            notification_url=self.notification_url,
            lifecycle_notification_url=self.lifecycle_url,
            client_state=self.client_state,
            expiration_at=utcnow() + self.lifetime,
        )
        with self.db.session() as session:
            sub = SubscriptionRepository(session).add(
                GraphSubscription(
                    subscription_id=info.id,
                    resource=info.resource or self.resource,
                    change_type="created",
                    notification_url=self.notification_url,
                    lifecycle_notification_url=self.lifecycle_url,
                    expiration_at=info.expiration_at,
                    status=SubscriptionStatus.ACTIVE,
                )
            )
        logger.info("created Graph subscription", extra={"subscription_id": info.id,
                                                          "expires": info.expiration_at.isoformat()})
        return sub

    def renew(self, subscription_id: str | None = None, *, only_expiring: bool = False) -> list[SubscriptionOutcome]:
        with self.db.session() as session:
            repo = SubscriptionRepository(session)
            if subscription_id:
                sub = repo.get_by_subscription_id(subscription_id)
                targets = [sub] if sub else []
            elif only_expiring:
                targets = repo.expiring_before(utcnow() + self.threshold)
            else:
                targets = repo.list(active_only=True)
        return [self._renew_one(sub) for sub in targets]

    def _renew_one(self, sub: GraphSubscription) -> SubscriptionOutcome:
        try:
            info = self.graph.renew_subscription(sub.subscription_id, utcnow() + self.lifetime)
        except NotFoundError:
            logger.warning("subscription no longer exists in Graph; recreating",
                           extra={"subscription_id": sub.subscription_id})
            self._set_status(sub.subscription_id, SubscriptionStatus.REMOVED, "not found on renewal")
            return SubscriptionOutcome(self._create_new(), "recreated")
        except Exception as exc:
            self._set_status(sub.subscription_id, None, f"renewal failed: {exc}"[:1000])
            raise
        with self.db.session() as session:
            row = SubscriptionRepository(session).get_by_subscription_id(sub.subscription_id)
            assert row is not None
            row.expiration_at = info.expiration_at
            row.last_renewed_at = utcnow()
            row.last_error = None
            row.status = SubscriptionStatus.ACTIVE
        logger.info("renewed Graph subscription", extra={"subscription_id": sub.subscription_id})
        with self.db.session() as session:
            return SubscriptionOutcome(SubscriptionRepository(session).get_by_subscription_id(sub.subscription_id),
                                       "renewed")  # type: ignore[arg-type]

    def ensure_active(self) -> SubscriptionOutcome | None:
        """Recreate the subscription when one existed before but none is active now (expired while
        the host was off, or a recreate failed). Never creates the first one: that stays an explicit
        admin action (POST /subscriptions/create)."""
        with self.db.session() as session:
            repo = SubscriptionRepository(session)
            if repo.active_for_resource(self.resource) is not None:
                return None
            had_one = any(s.resource == self.resource for s in repo.list())
        if not had_one:
            return None
        logger.warning("no active Teams subscription; recreating it")
        return SubscriptionOutcome(self._create_new(), "recreated")

    def handle_removed(self, subscription_id: str) -> None:
        self._set_status(subscription_id, SubscriptionStatus.REMOVED, "subscriptionRemoved lifecycle event")
        self._create_new()

    def _set_status(self, subscription_id: str, status: SubscriptionStatus | None, error: str | None) -> None:
        with self.db.session() as session:
            row = SubscriptionRepository(session).get_by_subscription_id(subscription_id)
            if row is None:
                return
            if status is not None:
                row.status = status
            row.last_error = error
