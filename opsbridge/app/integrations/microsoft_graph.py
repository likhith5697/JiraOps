"""Microsoft Graph integration: Entra ID tokens, channel messages, replies, subscriptions.

Identity model
--------------
* App-only token (client credentials, `.default`): read channel messages, manage subscriptions.
  Requires admin-consented application permission `ChannelMessage.Read.All` (or RSC).
* Delegated token for a service account (refresh-token grant, `ChannelMessage.Send`): post replies.
  Graph does not support application permissions for sending channel messages.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

import httpx

from app.core.errors import AmbiguousError, ConfigurationError, NotFoundError, PermanentError, TransientError
from app.core.time import utcnow
from app.domain.messages import InboundMessage
from app.integrations.http import send_with_retries
from app.core.text import html_to_text

logger = logging.getLogger("opsbridge.graph")
SERVICE = "microsoft_graph"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_SEND_SCOPE = "https://graph.microsoft.com/ChannelMessage.Send offline_access"


# ---------------------------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------------------------
class TokenProvider(Protocol):
    def get_token(self) -> str: ...

    def invalidate(self) -> None: ...


class _CachedTokenProvider:
    """Caches an access token until shortly before it expires.

    Expiry is tracked with the WALL CLOCK (time.time), not time.monotonic: on a laptop or VM
    that suspends (e.g. Docker Desktop while the host sleeps), the monotonic clock stops, so a
    monotonic deadline would keep an expired token "valid" after resume (Graph: 401
    "Invalid token lifetime"). Callers also `invalidate()` on a 401 as a second line of defence.
    """

    EXPIRY_SKEW_SECONDS = 300
    _clock = staticmethod(time.time)  # overridable in tests

    def __init__(self, *, token_url: str, http: httpx.Client, max_attempts: int = 3) -> None:
        self._token_url = token_url
        self._http = http
        self._max_attempts = max_attempts
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def _request_body(self) -> dict[str, str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _on_token_response(self, body: dict[str, Any]) -> None:
        pass

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def get_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires_at - self.EXPIRY_SKEW_SECONDS:
                return self._token
            response = send_with_retries(
                self._http,
                "POST",
                self._token_url,
                service="entra_id",
                idempotent=True,
                max_attempts=self._max_attempts,
                data=self._request_body(),
            )
            body = response.json()
            self._on_token_response(body)
            self._token = body["access_token"]
            self._expires_at = self._clock() + float(body.get("expires_in", 3600))
            logger.info("acquired Entra ID access token", extra={"provider": type(self).__name__})
            return self._token


class ClientCredentialsTokenProvider(_CachedTokenProvider):
    def __init__(self, *, tenant_id: str, client_id: str, client_secret: str, login_base_url: str,
                 http: httpx.Client, max_attempts: int = 3) -> None:
        super().__init__(token_url=f"{login_base_url}/{tenant_id}/oauth2/v2.0/token", http=http,
                         max_attempts=max_attempts)
        self._client_id = client_id
        self._client_secret = client_secret

    def _request_body(self) -> dict[str, str]:
        return {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": GRAPH_DEFAULT_SCOPE,
        }


class RefreshTokenProvider(_CachedTokenProvider):
    """Delegated token for the service account. The refresh token comes from the device-code flow
    (scripts/get_delegated_token.py, a *public client* flow, so no client secret is sent).
    Entra rotates refresh tokens; the newest one is kept in memory for the process lifetime.
    """

    def __init__(self, *, tenant_id: str, client_id: str, refresh_token: str, login_base_url: str,
                 http: httpx.Client, max_attempts: int = 3) -> None:
        super().__init__(token_url=f"{login_base_url}/{tenant_id}/oauth2/v2.0/token", http=http,
                         max_attempts=max_attempts)
        self._client_id = client_id
        self._refresh_token = refresh_token

    def _request_body(self) -> dict[str, str]:
        return {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
            "scope": GRAPH_SEND_SCOPE,
        }

    def _on_token_response(self, body: dict[str, Any]) -> None:
        if body.get("refresh_token"):
            self._refresh_token = body["refresh_token"]


class MissingDelegatedTokenProvider:
    def invalidate(self) -> None:
        pass

    def get_token(self) -> str:
        raise ConfigurationError(
            "MICROSOFT_DELEGATED_REFRESH_TOKEN is not configured; Teams replies require a delegated "
            "service-account token (see docs/microsoft-setup.md)"
        )


# ---------------------------------------------------------------------------------------------
# Domain mapping
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SubscriptionInfo:
    id: str
    resource: str
    expiration_at: datetime


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def chat_message_to_inbound(data: dict[str, Any], *, team_id: str, channel_id: str) -> InboundMessage:
    """Normalise a Graph `chatMessage` resource."""
    sender = data.get("from") or {}
    user = sender.get("user") or {}
    application = sender.get("application")
    body = data.get("body") or {}
    content = body.get("content") or ""
    text = html_to_text(content) if (body.get("contentType") or "html").lower() == "html" else content.strip()
    identity = data.get("channelIdentity") or {}
    return InboundMessage(
        message_id=str(data["id"]),
        team_id=identity.get("teamId") or team_id,
        channel_id=identity.get("channelId") or channel_id,
        body_text=text,
        created_at=_parse_dt(data.get("createdDateTime")),
        sender_id=user.get("id"),
        sender_name=user.get("displayName") or (application or {}).get("displayName"),
        sender_is_application=bool(application) and not user,
        reply_to_id=data.get("replyToId"),
        message_type=data.get("messageType") or "message",
        web_url=data.get("webUrl"),
        deleted=bool(data.get("deletedDateTime")),
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _messages_path(team_id: str, channel_id: str) -> str:
    return f"/teams/{team_id}/channels/{channel_id}/messages"


# ---------------------------------------------------------------------------------------------
# Client protocol + HTTP implementation
# ---------------------------------------------------------------------------------------------
class GraphClient(Protocol):
    def get_channel_message(self, team_id: str, channel_id: str, message_id: str) -> InboundMessage: ...

    def get_reply(self, team_id: str, channel_id: str, parent_id: str, reply_id: str) -> InboundMessage: ...

    def list_recent_channel_messages(self, team_id: str, channel_id: str, top: int = 20) -> list[InboundMessage]: ...

    def reply_to_message(self, team_id: str, channel_id: str, message_id: str, html: str) -> str: ...

    def find_reply_containing(self, team_id: str, channel_id: str, message_id: str, marker: str) -> str | None: ...

    def post_channel_message(self, team_id: str, channel_id: str, html: str) -> str: ...

    def find_channel_message_containing(self, team_id: str, channel_id: str, marker: str) -> str | None: ...

    def create_subscription(self, *, resource: str, notification_url: str, lifecycle_notification_url: str | None,
                            client_state: str, expiration_at: datetime) -> SubscriptionInfo: ...

    def renew_subscription(self, subscription_id: str, expiration_at: datetime) -> SubscriptionInfo: ...

    def close(self) -> None: ...


class HttpGraphClient:
    def __init__(
        self,
        *,
        app_tokens: TokenProvider,
        delegated_tokens: TokenProvider,
        base_url: str = "https://graph.microsoft.com/v1.0",
        timeout: float = 10.0,
        max_attempts: int = 3,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._app_tokens = app_tokens
        self._delegated_tokens = delegated_tokens
        self._base_url = base_url.rstrip("/")
        self._max_attempts = max_attempts
        self._extra = {"sleep": sleep} if sleep else {}
        self._http = http or httpx.Client(timeout=httpx.Timeout(timeout), headers={"User-Agent": "OpsBridge/1.0"})

    def _call(self, method: str, path: str, *, delegated: bool = False, idempotent: bool = True,
              **kwargs: Any) -> httpx.Response:
        provider = self._delegated_tokens if delegated else self._app_tokens
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {provider.get_token()}", "Accept": "application/json"}
            try:
                return send_with_retries(
                    self._http, method, f"{self._base_url}{path}", service=SERVICE, idempotent=idempotent,
                    max_attempts=self._max_attempts, headers=headers, **self._extra, **kwargs,
                )
            except PermanentError as exc:
                # 401 = token rejected (expired / revoked). The request was NOT applied, so even a
                # non-idempotent call is safe to resend once with a freshly acquired token.
                if exc.status_code != 401 or attempt == 2:
                    raise
                logger.warning("Graph rejected the access token; refreshing and retrying once",
                               extra={"delegated": delegated})
                provider.invalidate()
        raise AssertionError("unreachable")  # pragma: no cover

    def get_channel_message(self, team_id: str, channel_id: str, message_id: str) -> InboundMessage:
        data = self._call("GET", f"{_messages_path(team_id, channel_id)}/{message_id}").json()
        return chat_message_to_inbound(data, team_id=team_id, channel_id=channel_id)

    def get_reply(self, team_id: str, channel_id: str, parent_id: str, reply_id: str) -> InboundMessage:
        data = self._call("GET", f"{_messages_path(team_id, channel_id)}/{parent_id}/replies/{reply_id}").json()
        return chat_message_to_inbound(data, team_id=team_id, channel_id=channel_id)

    def list_recent_channel_messages(self, team_id: str, channel_id: str, top: int = 20) -> list[InboundMessage]:
        data = self._call("GET", _messages_path(team_id, channel_id), params={"$top": min(top, 50)}).json()
        return [chat_message_to_inbound(m, team_id=team_id, channel_id=channel_id) for m in data.get("value", [])]

    def reply_to_message(self, team_id: str, channel_id: str, message_id: str, html: str) -> str:
        data = self._call(
            "POST", f"{_messages_path(team_id, channel_id)}/{message_id}/replies", delegated=True,
            idempotent=False, json={"body": {"contentType": "html", "content": html}},
        ).json()
        return str(data.get("id", ""))

    def find_reply_containing(self, team_id: str, channel_id: str, message_id: str, marker: str) -> str | None:
        data = self._call("GET", f"{_messages_path(team_id, channel_id)}/{message_id}/replies",
                          params={"$top": 50}).json()
        for reply in data.get("value", []):
            if marker in ((reply.get("body") or {}).get("content") or ""):
                return str(reply.get("id"))
        return None

    def post_channel_message(self, team_id: str, channel_id: str, html: str) -> str:
        """New top-level channel message (escalation alerts), posted as the service account."""
        data = self._call("POST", _messages_path(team_id, channel_id), delegated=True, idempotent=False,
                          json={"body": {"contentType": "html", "content": html}}).json()
        return str(data.get("id", ""))

    def find_channel_message_containing(self, team_id: str, channel_id: str, marker: str) -> str | None:
        data = self._call("GET", _messages_path(team_id, channel_id), params={"$top": 20}).json()
        for message in data.get("value", []):
            if marker in ((message.get("body") or {}).get("content") or ""):
                return str(message.get("id"))
        return None

    def create_subscription(self, *, resource: str, notification_url: str, lifecycle_notification_url: str | None,
                            client_state: str, expiration_at: datetime) -> SubscriptionInfo:
        payload: dict[str, Any] = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": resource,
            "expirationDateTime": _iso(expiration_at),
            "clientState": client_state,
            "latestSupportedTlsVersion": "v1_2",
        }
        if lifecycle_notification_url:
            payload["lifecycleNotificationUrl"] = lifecycle_notification_url
        # Creating twice yields two subscriptions (harmless duplicates are de-duplicated by the
        # message-id unique key), so a transient retry is acceptable here.
        data = self._call("POST", "/subscriptions", json=payload, idempotent=False).json()
        return SubscriptionInfo(data["id"], data["resource"], _parse_dt(data["expirationDateTime"]) or expiration_at)

    def renew_subscription(self, subscription_id: str, expiration_at: datetime) -> SubscriptionInfo:
        data = self._call("PATCH", f"/subscriptions/{subscription_id}",
                          json={"expirationDateTime": _iso(expiration_at)}).json()
        return SubscriptionInfo(data["id"], data["resource"], _parse_dt(data["expirationDateTime"]) or expiration_at)

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------------------------
# Mock implementation (stores Graph-shaped JSON so the real mapping code is exercised)
# ---------------------------------------------------------------------------------------------
@dataclass
class MockGraphClient:
    """In-memory Graph. Failure modes for `fail_next`:
    get_transient, get_not_found (apply to messages and replies), reply_transient,
    reply_ambiguous_after, reply_permanent, post_transient, post_ambiguous_after (channel alerts).
    """

    service_account_user_id: str = "mock-opsbridge-service-account"
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    replies: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    subscriptions: dict[str, SubscriptionInfo] = field(default_factory=dict)
    get_calls: int = 0
    reply_calls: int = 0
    _failures: list[str] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def fail_next(self, *modes: str) -> None:
        with self._lock:
            self._failures.extend(modes)

    def _pop(self, prefix: str) -> str | None:
        if self._failures and self._failures[0].startswith(prefix):
            return self._failures.pop(0)
        return None

    def _new_id(self) -> str:
        return str(int(time.time() * 1000) * 1000 + next(self._ids))

    def add_message(self, *, team_id: str, channel_id: str, content: str, content_type: str = "html",
                    sender_id: str = "mock-user-1", sender_name: str = "Sample User",
                    application: bool = False, reply_to_id: str | None = None,
                    message_type: str = "message") -> dict[str, Any]:
        with self._lock:
            message_id = self._new_id()
            data = {
                "id": message_id,
                "replyToId": reply_to_id,
                "messageType": message_type,
                "createdDateTime": utcnow().isoformat().replace("+00:00", "Z"),
                "deletedDateTime": None,
                "webUrl": f"https://teams.example.com/l/message/{channel_id}/{message_id}",
                "from": (
                    {"application": {"id": "mock-bot", "displayName": "Some Bot"}, "user": None}
                    if application else {"user": {"id": sender_id, "displayName": sender_name}, "application": None}
                ),
                "body": {"contentType": content_type, "content": content},
                "channelIdentity": {"teamId": team_id, "channelId": channel_id},
            }
            self.messages[message_id] = data
            return data

    def get_channel_message(self, team_id: str, channel_id: str, message_id: str) -> InboundMessage:
        with self._lock:
            self.get_calls += 1
            mode = self._pop("get_")
            if mode == "get_transient":
                raise TransientError("mock Graph 503", service=SERVICE, status_code=503)
            if mode == "get_not_found" or message_id not in self.messages:
                raise NotFoundError("mock message not found", service=SERVICE, status_code=404)
            return chat_message_to_inbound(self.messages[message_id], team_id=team_id, channel_id=channel_id)

    def get_reply(self, team_id: str, channel_id: str, parent_id: str, reply_id: str) -> InboundMessage:
        with self._lock:
            self.get_calls += 1
            mode = self._pop("get_")
            if mode == "get_transient":
                raise TransientError("mock Graph 503", service=SERVICE, status_code=503)
            data = self.messages.get(reply_id)
            if data is None:  # replies posted by OpsBridge itself live in self.replies
                data = next((r for r in self.replies.get(parent_id, []) if r["id"] == reply_id), None)
            if mode == "get_not_found" or data is None or data.get("replyToId") != parent_id:
                raise NotFoundError("mock reply not found", service=SERVICE, status_code=404)
            return chat_message_to_inbound(data, team_id=team_id, channel_id=channel_id)

    def list_recent_channel_messages(self, team_id: str, channel_id: str, top: int = 20) -> list[InboundMessage]:
        with self._lock:
            items = [m for m in self.messages.values()
                     if not m.get("replyToId") and m["channelIdentity"]["channelId"] == channel_id][-top:]
            return [chat_message_to_inbound(m, team_id=team_id, channel_id=channel_id) for m in items]

    def reply_to_message(self, team_id: str, channel_id: str, message_id: str, html: str) -> str:
        with self._lock:
            self.reply_calls += 1
            mode = self._pop("reply_")
            if mode == "reply_transient":
                raise TransientError("mock Graph 429", service=SERVICE, status_code=429)
            if mode == "reply_permanent":
                raise PermanentError("mock Graph 403 Forbidden", service=SERVICE, status_code=403)
            reply_id = self._new_id()
            self.replies.setdefault(message_id, []).append(
                {"id": reply_id, "replyToId": message_id, "body": {"contentType": "html", "content": html},
                 "from": {"user": {"id": self.service_account_user_id, "displayName": "OpsBridge"}}}
            )
            if mode == "reply_ambiguous_after":
                raise AmbiguousError("mock Graph timeout after reply", service=SERVICE)
            return reply_id

    def find_reply_containing(self, team_id: str, channel_id: str, message_id: str, marker: str) -> str | None:
        with self._lock:
            for reply in self.replies.get(message_id, []):
                if marker in reply["body"]["content"]:
                    return reply["id"]
            return None

    def post_channel_message(self, team_id: str, channel_id: str, html: str) -> str:
        with self._lock:
            mode = self._pop("post_")
            if mode == "post_transient":
                raise TransientError("mock Graph 503", service=SERVICE, status_code=503)
            data = self.add_message(team_id=team_id, channel_id=channel_id, content=html,
                                    sender_id=self.service_account_user_id, sender_name="OpsBridge")
            if mode == "post_ambiguous_after":
                raise AmbiguousError("mock Graph timeout after post", service=SERVICE)
            return data["id"]

    def find_channel_message_containing(self, team_id: str, channel_id: str, marker: str) -> str | None:
        with self._lock:
            for m in self.messages.values():
                if (m["channelIdentity"]["channelId"] == channel_id and not m.get("replyToId")
                        and marker in m["body"]["content"]):
                    return m["id"]
            return None

    def create_subscription(self, *, resource: str, notification_url: str, lifecycle_notification_url: str | None,
                            client_state: str, expiration_at: datetime) -> SubscriptionInfo:
        with self._lock:
            info = SubscriptionInfo(f"mock-sub-{next(self._ids)}", resource, expiration_at)
            self.subscriptions[info.id] = info
            return info

    def renew_subscription(self, subscription_id: str, expiration_at: datetime) -> SubscriptionInfo:
        with self._lock:
            if subscription_id not in self.subscriptions:
                raise NotFoundError("subscription not found", service=SERVICE, status_code=404)
            info = SubscriptionInfo(subscription_id, self.subscriptions[subscription_id].resource, expiration_at)
            self.subscriptions[subscription_id] = info
            return info

    def close(self) -> None:
        pass
