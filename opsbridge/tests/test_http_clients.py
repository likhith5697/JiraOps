"""Real HTTP clients against mocked Jira / Graph / Entra responses (httpx.MockTransport)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from app.core.errors import AmbiguousError, ConfigurationError, NotFoundError, PermanentError, TransientError
from app.integrations.jira_client import HttpJiraClient
from app.integrations.microsoft_graph import (
    ClientCredentialsTokenProvider,
    HttpGraphClient,
    MissingDelegatedTokenProvider,
    RefreshTokenProvider,
)

NO_SLEEP = {"sleep": lambda _s: None}


def jira(handler, **kw) -> HttpJiraClient:
    return HttpJiraClient(base_url="https://example.atlassian.net", email="bot@example.com", api_token="tok",
                          transport=httpx.MockTransport(handler), **NO_SLEEP, **kw)


# ---------------------------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------------------------
def test_jira_create_issue_success():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "10001", "key": "OPS-7", "self": "..."})

    ref = jira(handler).create_issue({"summary": "x"})
    assert ref.key == "OPS-7" and ref.url == "https://example.atlassian.net/browse/OPS-7"
    assert seen["path"] == "/rest/api/3/issue"
    assert seen["auth"].startswith("Basic ")
    assert seen["body"] == {"fields": {"summary": "x"}}


def test_jira_connect_error_is_retried_because_request_never_left():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("refused")
        return httpx.Response(201, json={"id": "1", "key": "OPS-1"})

    assert jira(handler).create_issue({}).key == "OPS-1"
    assert len(calls) == 3


def test_jira_429_honours_retry_after():
    sleeps, calls = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(201, json={"id": "1", "key": "OPS-1"})

    client = HttpJiraClient(base_url="https://x", email="e", api_token="t", transport=httpx.MockTransport(handler),
                            sleep=sleeps.append)
    client.create_issue({})
    assert sleeps and sleeps[0] >= 7


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped")])
def test_jira_create_timeout_is_ambiguous_and_not_retried(failure):
    calls = []

    def handler(request):
        calls.append(1)
        raise failure

    with pytest.raises(AmbiguousError):
        jira(handler).create_issue({})
    assert len(calls) == 1  # never blindly re-sent


@pytest.mark.parametrize("status", [500, 502, 504])
def test_jira_create_5xx_is_ambiguous(status):
    with pytest.raises(AmbiguousError) as exc:
        jira(lambda r: httpx.Response(status, text="upstream")).create_issue({})
    assert exc.value.status_code == status


def test_jira_400_is_permanent_with_field_errors():
    body = {"errorMessages": [], "errors": {"priority": "Specify a valid priority"}}
    with pytest.raises(PermanentError) as exc:
        jira(lambda r: httpx.Response(400, json=body)).create_issue({})
    assert exc.value.details["field_errors"] == {"priority": "Specify a valid priority"}
    assert "Specify a valid priority" in str(exc.value)


def test_jira_401_is_permanent():
    with pytest.raises(PermanentError):
        jira(lambda r: httpx.Response(401, json={"errorMessages": ["Unauthorized"]})).create_issue({})


def test_jira_search_by_label():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"issues": [{"id": "5", "key": "OPS-5"}]})

    ref = jira(handler).find_issue_by_label("OPS", "opsbridge-abc")
    assert ref.key == "OPS-5"
    assert seen["path"] == "/rest/api/3/search/jql"
    assert seen["body"]["jql"] == 'project = "OPS" AND labels = "opsbridge-abc" ORDER BY created ASC'


def test_jira_search_empty_and_retry_on_5xx():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"issues": []})

    assert jira(handler).find_issue_by_label("OPS", "x") is None
    assert len(calls) == 2  # search is idempotent -> 5xx retried in-call


# ---------------------------------------------------------------------------------------------
# Entra ID + Graph
# ---------------------------------------------------------------------------------------------
def graph_client(handler) -> tuple[HttpGraphClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(recording))
    app_tokens = ClientCredentialsTokenProvider(tenant_id="tenant", client_id="cid", client_secret="secret",
                                                login_base_url="https://login.example.com", http=http)
    delegated = RefreshTokenProvider(tenant_id="tenant", client_id="cid", refresh_token="rt-1",
                                     login_base_url="https://login.example.com", http=http)
    return HttpGraphClient(app_tokens=app_tokens, delegated_tokens=delegated, base_url="https://graph.example.com/v1.0",
                           http=http, **NO_SLEEP), requests


CHAT_MESSAGE = {
    "id": "1700000000000",
    "replyToId": None,
    "messageType": "message",
    "createdDateTime": "2026-01-01T10:00:00.000Z",
    "webUrl": "https://teams.example.com/l/message/1",
    "from": {"user": {"id": "user-1", "displayName": "Sample User"}, "application": None},
    "body": {"contentType": "html", "content": "<p>API Name: X</p><p>Priority: P1</p>"},
    "channelIdentity": {"teamId": "team-1", "channelId": "chan-1"},
}


def _graph_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "login.example.com":
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        token = "app-token" if form["grant_type"] == "client_credentials" else "user-token"
        return httpx.Response(200, json={"access_token": token, "expires_in": 3600, "refresh_token": "rt-2"})
    if request.method == "GET" and request.url.path.endswith("/messages/1700000000000"):
        return httpx.Response(200, json=CHAT_MESSAGE)
    if request.method == "POST" and request.url.path.endswith("/replies"):
        return httpx.Response(201, json={"id": "reply-1"})
    if request.url.path == "/v1.0/subscriptions":
        body = json.loads(request.content)
        return httpx.Response(201, json={"id": "sub-1", "resource": body["resource"],
                                         "expirationDateTime": body["expirationDateTime"]})
    if request.method == "PATCH":
        return httpx.Response(404, json={"error": {"code": "ResourceNotFound", "message": "gone"}})
    return httpx.Response(500)


def test_graph_get_message_maps_chat_message_and_caches_token():
    client, requests = graph_client(_graph_handler)
    m1 = client.get_channel_message("team-1", "chan-1", "1700000000000")
    client.get_channel_message("team-1", "chan-1", "1700000000000")

    assert m1.body_text == "API Name: X\n\nPriority: P1"
    assert m1.sender_id == "user-1" and m1.sender_name == "Sample User"
    assert m1.team_id == "team-1" and m1.channel_id == "chan-1"
    assert m1.created_at == datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    assert not m1.is_reply and not m1.sender_is_application

    token_requests = [r for r in requests if r.url.host == "login.example.com"]
    assert len(token_requests) == 1  # cached
    graph_requests = [r for r in requests if r.url.host == "graph.example.com"]
    assert all(r.headers["authorization"] == "Bearer app-token" for r in graph_requests)


def test_graph_reply_uses_delegated_token():
    client, requests = graph_client(_graph_handler)
    assert client.reply_to_message("team-1", "chan-1", "1700000000000", "<p>hi</p>") == "reply-1"
    reply = [r for r in requests if r.url.path.endswith("/replies")][0]
    assert reply.headers["authorization"] == "Bearer user-token"
    assert json.loads(reply.content) == {"body": {"contentType": "html", "content": "<p>hi</p>"}}
    token_form = [r for r in requests if b"refresh_token" in r.content][0].content.decode()
    assert "client_secret" not in token_form  # public-client refresh: no secret sent


def test_graph_reply_without_delegated_token_is_configuration_error():
    http = httpx.Client(transport=httpx.MockTransport(_graph_handler))
    app_tokens = ClientCredentialsTokenProvider(tenant_id="t", client_id="c", client_secret="s",
                                                login_base_url="https://login.example.com", http=http)
    client = HttpGraphClient(app_tokens=app_tokens, delegated_tokens=MissingDelegatedTokenProvider(),
                             base_url="https://graph.example.com/v1.0", http=http)
    with pytest.raises(ConfigurationError):
        client.reply_to_message("t", "c", "m", "x")


def test_graph_subscription_payload():
    client, requests = graph_client(_graph_handler)
    expiry = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
    info = client.create_subscription(resource="/teams/t/channels/c/messages", notification_url="https://x/webhooks/teams",
                                      lifecycle_notification_url="https://x/webhooks/teams/lifecycle",
                                      client_state="state", expiration_at=expiry)
    body = json.loads([r for r in requests if r.url.path == "/v1.0/subscriptions"][0].content)
    assert body["changeType"] == "created"
    assert body["clientState"] == "state"
    assert body["lifecycleNotificationUrl"] == "https://x/webhooks/teams/lifecycle"
    assert body["expirationDateTime"] == "2026-01-02T12:00:00Z"
    assert info.id == "sub-1" and info.expiration_at == expiry


def test_graph_renew_missing_subscription_raises_not_found():
    client, _ = graph_client(_graph_handler)
    with pytest.raises(NotFoundError):
        client.renew_subscription("sub-x", datetime.now(timezone.utc))


def test_graph_token_failure_is_reported_without_secret():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_client", "error_description": "AADSTS7000215: bad secret"})

    client, _ = graph_client(handler)
    with pytest.raises(PermanentError) as exc:
        client.get_channel_message("t", "c", "m")
    assert "invalid_client" in str(exc.value)
    assert "secret" not in str(exc.value).replace("bad secret", "")


def test_graph_throttling_is_transient():
    def handler(request):
        if request.url.host == "login.example.com":
            return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
        return httpx.Response(429, headers={"Retry-After": "1"})

    client, requests = graph_client(handler)
    with pytest.raises(TransientError):
        client.get_channel_message("t", "c", "m")
    assert len([r for r in requests if r.url.host == "graph.example.com"]) == 3


# ---------------------------------------------------------------------------------------------
# Jira workflow calls used by the `close` command
# ---------------------------------------------------------------------------------------------
def test_jira_status_transitions_transition_and_comment_requests():
    seen: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path + ("?" + request.url.query.decode() if request.url.query else ""), body))
        path = request.url.path
        if request.method == "GET" and path == "/rest/api/3/issue/OPS-1":
            return httpx.Response(200, json={"key": "OPS-1", "fields": {"status": {"name": "In Progress"}}})
        if request.method == "GET" and path.endswith("/transitions"):
            return httpx.Response(200, json={"transitions": [
                {"id": "21", "name": "Finish", "to": {"name": "Done"}},
                {"id": "11", "name": "Back", "to": {"name": "To Do"}},
            ]})
        if request.method == "POST" and path.endswith("/transitions"):
            return httpx.Response(204)
        if request.method == "POST" and path.endswith("/comment"):
            return httpx.Response(201, json={"id": "777"})
        if request.method == "GET" and path.endswith("/comment"):
            return httpx.Response(200, json={"comments": [{"id": "777", "body": body_doc}]})
        return httpx.Response(404)

    body_doc = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Closed from Microsoft Teams by A. Reason: x"}]}]}
    client = jira(handler)
    assert client.get_issue_status("OPS-1") == "In Progress"
    transitions = client.get_transitions("OPS-1")
    assert [(t.id, t.to_status) for t in transitions] == [("21", "Done"), ("11", "To Do")]
    client.transition_issue("OPS-1", "21")
    assert client.add_comment("OPS-1", body_doc) == "777"
    assert client.find_comment_containing("OPS-1", "by A. Reason: x") == "777"
    assert client.find_comment_containing("OPS-1", "something else") is None

    assert seen[0][1] == "/rest/api/3/issue/OPS-1?fields=status"
    assert seen[2] == ("POST", "/rest/api/3/issue/OPS-1/transitions", {"transition": {"id": "21"}})
    assert seen[3] == ("POST", "/rest/api/3/issue/OPS-1/comment", {"body": body_doc})


def test_jira_transition_timeout_is_ambiguous_and_429_is_retried():
    calls = []

    def timeout_handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(AmbiguousError):
        jira(timeout_handler).transition_issue("OPS-1", "21")
    assert len(calls) == 1

    attempts = []

    def throttled(request):
        attempts.append(1)
        return httpx.Response(429, headers={"Retry-After": "2"}) if len(attempts) == 1 else httpx.Response(204)

    sleeps: list[float] = []
    client = HttpJiraClient(base_url="https://x", email="e", api_token="t", transport=httpx.MockTransport(throttled),
                            sleep=sleeps.append)
    client.transition_issue("OPS-1", "21")
    assert len(attempts) == 2 and sleeps[0] >= 2


def test_graph_get_reply_path():
    def handler(request):
        if request.url.host == "login.example.com":
            return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
        assert request.url.path == "/v1.0/teams/t/channels/c/messages/parent-1/replies/reply-9"
        return httpx.Response(200, json={**CHAT_MESSAGE, "id": "reply-9", "replyToId": "parent-1",
                                         "body": {"contentType": "html", "content": "<p>close done</p>"}})

    client, _ = graph_client(handler)
    reply = client.get_reply("t", "c", "parent-1", "reply-9")
    assert reply.reply_to_id == "parent-1" and reply.body_text == "close done"


# ---------------------------------------------------------------------------------------------
# Token expiry across host sleep + 401 recovery
# ---------------------------------------------------------------------------------------------
def test_token_cache_uses_wall_clock_so_sleep_cannot_keep_an_expired_token(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(ClientCredentialsTokenProvider, "_clock", staticmethod(lambda: now[0]))
    issued = []

    def handler(request):
        issued.append(1)
        return httpx.Response(200, json={"access_token": f"tok-{len(issued)}", "expires_in": 3600})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ClientCredentialsTokenProvider(tenant_id="t", client_id="c", client_secret="s",
                                              login_base_url="https://login.example.com", http=http)
    assert provider.get_token() == "tok-1"
    now[0] += 1800  # 30 min later: still cached
    assert provider.get_token() == "tok-1"
    now[0] += 3 * 3600  # laptop slept for 3 hours: wall clock moved even though monotonic did not
    assert provider.get_token() == "tok-2"


def test_graph_401_refreshes_token_and_retries_once():
    tokens = iter(["stale-token", "fresh-token"])
    seen_auth = []

    def handler(request):
        if request.url.host == "login.example.com":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        seen_auth.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer stale-token":
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken",
                                                       "message": "Invalid token lifetime."}})
        return httpx.Response(200, json=CHAT_MESSAGE)

    client, _ = graph_client(handler)
    message = client.get_channel_message("team-1", "chan-1", "1700000000000")
    assert message.message_id == "1700000000000"
    assert seen_auth == ["Bearer stale-token", "Bearer fresh-token"]


def test_graph_persistent_401_is_permanent_after_one_refresh():
    calls = []

    def handler(request):
        if request.url.host == "login.example.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        calls.append(1)
        return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken", "message": "no"}})

    client, _ = graph_client(handler)
    with pytest.raises(PermanentError):
        client.get_channel_message("t", "c", "m")
    assert len(calls) == 2  # original + one retry with a fresh token, then give up


# ---------------------------------------------------------------------------------------------
# status / escalate HTTP calls
# ---------------------------------------------------------------------------------------------
def test_jira_issue_details_and_update_requests():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params),
                     json.loads(request.content) if request.content else None))
        if request.method == "GET":
            return httpx.Response(200, json={"fields": {
                "status": {"name": "In Progress"}, "assignee": {"displayName": "Jane Engineer"},
                "priority": {"name": "High"}, "updated": "2026-01-02T03:04:05.000+0000"}})
        return httpx.Response(204)

    client = jira(handler)
    d = client.get_issue_details("OPS-1")
    assert (d.status, d.assignee, d.priority) == ("In Progress", "Jane Engineer", "High")
    assert d.updated == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert seen[0][2] == {"fields": "status,assignee,priority,updated"}

    client.update_issue("OPS-1", fields={"priority": {"name": "Highest"}}, update={"labels": [{"add": "escalated"}]})
    assert seen[1] == ("PUT", "/rest/api/3/issue/OPS-1", {},
                       {"fields": {"priority": {"name": "Highest"}}, "update": {"labels": [{"add": "escalated"}]}})


def test_jira_update_is_idempotent_so_5xx_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502) if len(calls) == 1 else httpx.Response(204)

    jira(handler).update_issue("OPS-1", update={"labels": [{"add": "escalated"}]})
    assert len(calls) == 2


def test_jira_unassigned_issue_details():
    client = jira(lambda r: httpx.Response(200, json={"fields": {"status": {"name": "To Do"}, "assignee": None,
                                                                 "priority": None, "updated": None}}))
    d = client.get_issue_details("OPS-1")
    assert d.assignee is None and d.priority is None and d.updated is None


def test_graph_post_channel_message_uses_delegated_token():
    def handler(request):
        if request.url.host == "login.example.com":
            form = request.content.decode()
            return httpx.Response(200, json={"access_token": "user-token" if "refresh_token" in form else "app-token",
                                             "expires_in": 3600})
        if request.method == "POST":
            assert request.url.path == "/v1.0/teams/t/channels/esc/messages"
            assert request.headers["authorization"] == "Bearer user-token"
            assert json.loads(request.content) == {"body": {"contentType": "html", "content": "<p>alert</p>"}}
            return httpx.Response(201, json={"id": "alert-1"})
        return httpx.Response(200, json={"value": [{"id": "alert-1", "body": {"content": "x (OpsBridge escalation 7)"}}]})

    client, _ = graph_client(handler)
    assert client.post_channel_message("t", "esc", "<p>alert</p>") == "alert-1"
    assert client.find_channel_message_containing("t", "esc", "(OpsBridge escalation 7)") == "alert-1"
    assert client.find_channel_message_containing("t", "esc", "(OpsBridge escalation 8)") is None
