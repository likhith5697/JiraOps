"""Jira Cloud REST API v3 client (real HTTP + in-memory mock sharing one Protocol)."""

from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol
from urllib.parse import quote

import httpx

from app.core.errors import AmbiguousError, NotFoundError, PermanentError, TransientError
from app.integrations.http import send_with_retries

logger = logging.getLogger("opsbridge.jira")
SERVICE = "jira"


@dataclass(frozen=True)
class JiraIssueRef:
    id: str
    key: str
    url: str


@dataclass(frozen=True)
class JiraIssueDetails:
    status: str
    assignee: str | None       # display name; None = unassigned
    priority: str | None       # Jira priority name
    updated: datetime | None
    assignee_id: str | None = None  # accountId (stable identity; names can change)


@dataclass(frozen=True)
class JiraIssueState:
    """Current status/assignee of an issue, as returned by search or a webhook."""

    key: str
    status: str
    assignee_id: str | None
    assignee_name: str | None
    updated: datetime | None


def issue_state_from_json(issue: dict[str, Any]) -> JiraIssueState:
    f = issue.get("fields") or {}
    assignee = f.get("assignee") or {}
    return JiraIssueState(
        key=str(issue.get("key", "")),
        status=str((f.get("status") or {}).get("name", "")),
        assignee_id=assignee.get("accountId"),
        assignee_name=assignee.get("displayName"),
        updated=_parse_jira_datetime(f.get("updated")),
    )


@dataclass(frozen=True)
class JiraTransition:
    id: str
    name: str
    to_status: str


class JiraClient(Protocol):
    def create_issue(self, fields: dict[str, Any]) -> JiraIssueRef: ...

    def find_issue_by_label(self, project_key: str, label: str) -> JiraIssueRef | None: ...

    def get_issue_status(self, key: str) -> str: ...

    def get_issue_details(self, key: str) -> JiraIssueDetails: ...

    def update_issue(self, key: str, *, fields: dict[str, Any] | None = None,
                     update: dict[str, Any] | None = None) -> None: ...

    def search_recently_updated(self, project_key: str, minutes: int) -> list[JiraIssueState]: ...

    def get_transitions(self, key: str) -> list[JiraTransition]: ...

    def transition_issue(self, key: str, transition_id: str) -> None: ...

    def add_comment(self, key: str, body: dict[str, Any]) -> str: ...

    def find_comment_containing(self, key: str, text: str) -> str | None: ...

    def close(self) -> None: ...


def _parse_jira_datetime(value: str | None) -> datetime | None:
    """Jira returns e.g. 2026-01-01T10:00:00.000+0000 (no colon in the offset)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


def adf_plain_text(node: Any) -> str:
    """Flatten an Atlassian Document Format node to plain text (for matching comments)."""
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [adf_plain_text(child) for child in node.get("content", [])]
        sep = "\n" if node.get("type") in ("doc",) else ""
        return sep.join(parts)
    if isinstance(node, list):
        return "".join(adf_plain_text(n) for n in node)
    return str(node or "")


def _quote_jql(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class HttpJiraClient:
    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        timeout: float = 10.0,
        max_attempts: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._max_attempts = max_attempts
        self._sleep_kwargs = {"sleep": sleep} if sleep else {}
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=httpx.BasicAuth(email, api_token),
            timeout=httpx.Timeout(timeout),
            headers={"Accept": "application/json", "User-Agent": "OpsBridge/1.0"},
            transport=transport,
        )

    def browse_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{key}"

    def create_issue(self, fields: dict[str, Any]) -> JiraIssueRef:
        # Non-idempotent: a timeout / 5xx surfaces as AmbiguousError and is NOT retried here.
        response = send_with_retries(
            self._client,
            "POST",
            "/rest/api/3/issue",
            service=SERVICE,
            idempotent=False,
            max_attempts=self._max_attempts,
            json={"fields": fields},
            **self._sleep_kwargs,
        )
        body = response.json()
        return JiraIssueRef(id=str(body["id"]), key=body["key"], url=self.browse_url(body["key"]))

    def find_issue_by_label(self, project_key: str, label: str) -> JiraIssueRef | None:
        jql = f"project = {_quote_jql(project_key)} AND labels = {_quote_jql(label)} ORDER BY created ASC"
        response = send_with_retries(
            self._client,
            "POST",
            "/rest/api/3/search/jql",
            service=SERVICE,
            idempotent=True,  # search is read-only
            max_attempts=self._max_attempts,
            json={"jql": jql, "fields": ["summary"], "maxResults": 5},
            **self._sleep_kwargs,
        )
        issues = response.json().get("issues") or []
        if not issues:
            return None
        if len(issues) > 1:
            logger.error(
                "multiple Jira issues share an idempotency label; using the oldest",
                extra={"label": label, "keys": [i.get("key") for i in issues]},
            )
        first = issues[0]
        return JiraIssueRef(id=str(first["id"]), key=first["key"], url=self.browse_url(first["key"]))


    # ---- issue workflow (close-from-Teams) --------------------------------------------------
    def _issue_path(self, key: str, suffix: str = "") -> str:
        return f"/rest/api/3/issue/{quote(key, safe='')}{suffix}"

    def _send(self, method: str, path: str, *, idempotent: bool, **kwargs: Any) -> httpx.Response:
        return send_with_retries(self._client, method, path, service=SERVICE, idempotent=idempotent,
                                 max_attempts=self._max_attempts, **self._sleep_kwargs, **kwargs)

    def get_issue_status(self, key: str) -> str:
        body = self._send("GET", self._issue_path(key), idempotent=True, params={"fields": "status"}).json()
        return str(((body.get("fields") or {}).get("status") or {}).get("name", ""))

    def get_issue_details(self, key: str) -> JiraIssueDetails:
        body = self._send("GET", self._issue_path(key), idempotent=True,
                          params={"fields": "status,assignee,priority,updated"}).json()
        f = body.get("fields") or {}
        return JiraIssueDetails(
            status=str((f.get("status") or {}).get("name", "")),
            assignee=(f.get("assignee") or {}).get("displayName"),
            priority=(f.get("priority") or {}).get("name"),
            updated=_parse_jira_datetime(f.get("updated")),
            assignee_id=(f.get("assignee") or {}).get("accountId"),
        )

    def search_recently_updated(self, project_key: str, minutes: int, *, max_issues: int = 500) -> list[JiraIssueState]:
        """Issues in the project updated in the last `minutes` (paginated, oldest change first)."""
        jql = f"project = {_quote_jql(project_key)} AND updated >= \"-{int(minutes)}m\" ORDER BY updated ASC"
        states: list[JiraIssueState] = []
        token: str | None = None
        while len(states) < max_issues:
            body: dict[str, Any] = {"jql": jql, "fields": ["status", "assignee", "updated"], "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            data = self._send("POST", "/rest/api/3/search/jql", idempotent=True, json=body).json()
            states += [issue_state_from_json(i) for i in data.get("issues", [])]
            token = data.get("nextPageToken")
            if not token or data.get("isLast", True):
                break
        return states[:max_issues]

    def update_issue(self, key: str, *, fields: dict[str, Any] | None = None,
                     update: dict[str, Any] | None = None) -> None:
        # PUT edit is idempotent (setting a field / adding an existing label twice is a no-op).
        payload: dict[str, Any] = {}
        if fields:
            payload["fields"] = fields
        if update:
            payload["update"] = update
        self._send("PUT", self._issue_path(key), idempotent=True, json=payload)

    def get_transitions(self, key: str) -> list[JiraTransition]:
        body = self._send("GET", self._issue_path(key, "/transitions"), idempotent=True).json()
        return [
            JiraTransition(id=str(t["id"]), name=str(t.get("name", "")), to_status=str((t.get("to") or {}).get("name", "")))
            for t in body.get("transitions", [])
        ]

    def transition_issue(self, key: str, transition_id: str) -> None:
        # Non-idempotent: a timeout surfaces as AmbiguousError; the caller re-checks the status.
        self._send("POST", self._issue_path(key, "/transitions"), idempotent=False,
                   json={"transition": {"id": transition_id}})

    def add_comment(self, key: str, body: dict[str, Any]) -> str:
        data = self._send("POST", self._issue_path(key, "/comment"), idempotent=False, json={"body": body}).json()
        return str(data.get("id", ""))

    def find_comment_containing(self, key: str, text: str) -> str | None:
        data = self._send("GET", self._issue_path(key, "/comment"), idempotent=True,
                          params={"orderBy": "-created", "maxResults": 50}).json()
        for comment in data.get("comments", []):
            if text in adf_plain_text(comment.get("body")):
                return str(comment.get("id"))
        return None

    def close(self) -> None:
        self._client.close()


# ----------------------------------------------------------------------------------------
# Mock
# ----------------------------------------------------------------------------------------
@dataclass
class MockJiraClient:
    """Thread-safe in-memory Jira. Failure modes can be queued to exercise error handling.

    Modes for `fail_next`:
      transient        - raise TransientError, nothing created
      ambiguous_lost   - raise AmbiguousError, nothing created (request lost before Jira)
      ambiguous_after  - create the issue, then raise AmbiguousError (response lost)
      permanent        - raise PermanentError (HTTP 400)
      search_transient - next search raises TransientError
    Workflow ops (close-from-Teams), each consumed only by its own operation:
      status_transient, transitions_transient, transition_transient, transition_ambiguous_after,
      transition_permanent, comment_transient, comment_ambiguous_after, details_transient,
      update_transient
    """

    base_url: str = "https://mock-jira.example.com"
    project_key: str = "OPS"
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    create_calls: int = 0
    search_calls: int = 0
    _failures: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(10001))
    # Simple workflow: every transition is available except the one to the current status.
    workflow: list[JiraTransition] = field(default_factory=lambda: [
        JiraTransition("11", "Start Progress", "In Progress"),
        JiraTransition("31", "Done", "Done"),
        JiraTransition("41", "Reopen", "To Do"),
    ])
    transition_calls: int = 0
    comment_calls: int = 0
    update_calls: int = 0
    reject_priority_updates: bool = False  # simulate a project whose priority field is not editable

    def fail_next(self, *modes: str) -> None:
        with self._lock:
            self._failures.extend(modes)

    def _pop_failure(self, allowed: set[str]) -> str | None:
        if self._failures and self._failures[0] in allowed:
            return self._failures.pop(0)
        return None

    def create_issue(self, fields: dict[str, Any]) -> JiraIssueRef:
        with self._lock:
            self.create_calls += 1
            mode = self._pop_failure({"transient", "ambiguous_lost", "ambiguous_after", "permanent"})
            if mode == "transient":
                raise TransientError("mock connection refused", service=SERVICE)
            if mode == "ambiguous_lost":
                raise AmbiguousError("mock read timeout", service=SERVICE)
            if mode == "permanent":
                raise PermanentError("mock: field 'summary' is required", service=SERVICE, status_code=400)
            issue_id = str(next(self._ids))
            key = f"{fields.get('project', {}).get('key', self.project_key)}-{len(self.issues) + 1}"
            self.issues[key] = {"id": issue_id, "key": key, "fields": fields, "status": "To Do", "comments": [],
                                "assignee": None, "updated": datetime.now().astimezone()}
            ref = JiraIssueRef(id=issue_id, key=key, url=f"{self.base_url}/browse/{key}")
            if mode == "ambiguous_after":
                raise AmbiguousError("mock read timeout after commit", service=SERVICE)
            return ref

    def find_issue_by_label(self, project_key: str, label: str) -> JiraIssueRef | None:
        with self._lock:
            self.search_calls += 1
            if self._pop_failure({"search_transient"}):
                raise TransientError("mock search unavailable", service=SERVICE)
            for key, issue in self.issues.items():
                if label in issue["fields"].get("labels", []) and key.startswith(f"{project_key}-"):
                    return JiraIssueRef(id=issue["id"], key=key, url=f"{self.base_url}/browse/{key}")
            return None

    def _issue(self, key: str) -> dict[str, Any]:
        if key not in self.issues:
            raise NotFoundError(f"issue {key} does not exist", service=SERVICE, status_code=404)
        return self.issues[key]

    def get_issue_status(self, key: str) -> str:
        with self._lock:
            if self._pop_failure({"status_transient"}):
                raise TransientError("mock Jira 503", service=SERVICE, status_code=503)
            return self._issue(key)["status"]

    def get_issue_details(self, key: str) -> JiraIssueDetails:
        with self._lock:
            if self._pop_failure({"details_transient"}):
                raise TransientError("mock Jira 503", service=SERVICE, status_code=503)
            issue = self._issue(key)
            return JiraIssueDetails(status=issue["status"], assignee=issue.get("assignee"),
                                    priority=(issue["fields"].get("priority") or {}).get("name"),
                                    updated=issue.get("updated"), assignee_id=self._assignee_id(issue))

    @staticmethod
    def _assignee_id(issue: dict[str, Any]) -> str | None:
        return issue.get("assignee_id") or (f"acct-{issue['assignee']}" if issue.get("assignee") else None)

    def _touch(self, issue: dict[str, Any]) -> None:
        # strictly increasing `updated`, like Jira
        now = datetime.now().astimezone()
        previous = issue.get("updated")
        issue["updated"] = now if previous is None or now > previous else previous + timedelta(milliseconds=1)

    def change_in_jira(self, key: str, *, status: str | None = None, assignee: str | None | object = ...) -> None:
        """Test/demo helper: simulate a person changing the issue directly in Jira."""
        with self._lock:
            issue = self._issue(key)
            if status is not None:
                issue["status"] = status
            if assignee is not ...:
                issue["assignee"] = assignee
                issue.pop("assignee_id", None)
            self._touch(issue)

    def search_recently_updated(self, project_key: str, minutes: int) -> list[JiraIssueState]:
        with self._lock:
            if self._pop_failure({"search_transient"}):
                raise TransientError("mock search unavailable", service=SERVICE)
            cutoff = datetime.now().astimezone() - timedelta(minutes=minutes)
            found = [i for k, i in self.issues.items()
                     if k.startswith(f"{project_key}-") and i.get("updated") and i["updated"] >= cutoff]
            found.sort(key=lambda i: i["updated"])
            return [JiraIssueState(key=i["key"], status=i["status"], assignee_id=self._assignee_id(i),
                                   assignee_name=i.get("assignee"), updated=i["updated"]) for i in found]

    def update_issue(self, key: str, *, fields: dict[str, Any] | None = None,
                     update: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.update_calls += 1
            if self._pop_failure({"update_transient"}):
                raise TransientError("mock Jira 503", service=SERVICE, status_code=503)
            issue = self._issue(key)
            if fields and "priority" in fields and self.reject_priority_updates:
                raise PermanentError("priority: Field 'priority' cannot be set.", service=SERVICE, status_code=400,
                                     details={"field_errors": {"priority": "Field 'priority' cannot be set."}})
            issue["fields"].update(fields or {})
            for op in (update or {}).get("labels", []):
                labels = issue["fields"].setdefault("labels", [])
                if "add" in op and op["add"] not in labels:
                    labels.append(op["add"])
            self._touch(issue)

    def get_transitions(self, key: str) -> list[JiraTransition]:
        with self._lock:
            if self._pop_failure({"transitions_transient"}):
                raise TransientError("mock Jira 503", service=SERVICE, status_code=503)
            current = self._issue(key)["status"].lower()
            return [t for t in self.workflow if t.to_status.lower() != current]

    def transition_issue(self, key: str, transition_id: str) -> None:
        with self._lock:
            self.transition_calls += 1
            mode = self._pop_failure({"transition_transient", "transition_ambiguous_after", "transition_permanent"})
            if mode == "transition_transient":
                raise TransientError("mock Jira 429", service=SERVICE, status_code=429, retry_after=1)
            if mode == "transition_permanent":
                raise PermanentError("mock: transition is not valid", service=SERVICE, status_code=400)
            issue = self._issue(key)
            match = [t for t in self.workflow if t.id == transition_id]
            if not match or match[0].to_status.lower() == issue["status"].lower():
                raise PermanentError("mock: transition is not valid for current status", service=SERVICE,
                                     status_code=400)
            issue["status"] = match[0].to_status
            self._touch(issue)
            if mode == "transition_ambiguous_after":
                raise AmbiguousError("mock read timeout after transition", service=SERVICE)

    def add_comment(self, key: str, body: dict[str, Any]) -> str:
        with self._lock:
            self.comment_calls += 1
            mode = self._pop_failure({"comment_transient", "comment_ambiguous_after"})
            if mode == "comment_transient":
                raise TransientError("mock Jira 503", service=SERVICE, status_code=503)
            issue = self._issue(key)
            comment_id = str(next(self._ids))
            issue["comments"].append({"id": comment_id, "body": body})
            self._touch(issue)
            if mode == "comment_ambiguous_after":
                raise AmbiguousError("mock read timeout after comment", service=SERVICE)
            return comment_id

    def find_comment_containing(self, key: str, text: str) -> str | None:
        with self._lock:
            for comment in self._issue(key)["comments"]:
                if text in adf_plain_text(comment["body"]):
                    return comment["id"]
            return None

    def close(self) -> None:
        pass
