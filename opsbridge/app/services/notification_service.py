"""Teams acknowledgement replies (success + rejection). All user-provided values are HTML-escaped."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from app.integrations.microsoft_graph import GraphClient
from app.services.intake_template import template_text

logger = logging.getLogger("opsbridge.notifications")


@dataclass(frozen=True)
class ReplyTarget:
    request_id: int
    team_id: str
    channel_id: str
    message_id: str


def reference_marker(request_id: int) -> str:
    """Visible footer that lets us detect an already-posted reply after an ambiguous timeout."""
    return f"OpsBridge ref #{request_id}"


def _lines_to_html(lines: list[str]) -> str:
    return "".join(f"<p>{line}</p>" if line else "<p>&nbsp;</p>" for line in lines)


def render_success(*, request_id: int, jira_key: str, jira_url: str, priority: str, environment: str) -> str:
    e = html.escape
    return _lines_to_html(
        [
            "Your support request has been successfully registered.",
            "",
            f'<b>Jira Ticket:</b> <a href="{e(jira_url, quote=True)}">{e(jira_key)}</a>',
            f"<b>Priority:</b> {e(priority)}",
            f"<b>Environment:</b> {e(environment)}",
            "<b>Status:</b> Created",
            "",
            "Our engineering team can now track this request.",
            "",
            "Thank you for using OpsBridge.",
            f"<i>{e(reference_marker(request_id))}</i>",
        ]
    )


def render_rejection(*, request_id: int, problems: list[str]) -> str:
    e = html.escape
    template = "<br>".join(e(line) for line in template_text().split("\n"))
    items = "".join(f"<li>{e(p)}</li>" for p in problems)
    return (
        "<p>Your support request could not be registered because the submission is incomplete "
        "or invalid. No Jira ticket was created.</p>"
        f"<p><b>Please fix the following:</b></p><ul>{items}</ul>"
        "<p><b>Expected template</b> (post a new message in this channel):</p>"
        f"<pre>{template}</pre>"
        f"<p><i>{e(reference_marker(request_id))}</i></p>"
    )


# --- thread-command replies ---------------------------------------------------------------------
def command_marker(command_id: int) -> str:
    """Closing ')' prevents 'cmd 1' from matching 'cmd 12' when a thread has several commands."""
    return f"(OpsBridge cmd {command_id})"


def _with_marker(lines: list[str], command_id: int) -> str:
    return _lines_to_html([*lines, f"<i>{html.escape(command_marker(command_id))}</i>"])


def render_close_success(*, command_id: int, jira_key: str, jira_url: str, sender_name: str,
                         reason: str | None) -> str:
    e = html.escape
    return _with_marker([
        f"✅ {e(jira_key)} has been closed by {e(sender_name)}.",
        f"Reason: {e(reason or 'Not provided')}",
        f'Jira: <a href="{e(jira_url, quote=True)}">{e(jira_url)}</a>',
    ], command_id)


def render_close_already(*, command_id: int, jira_key: str) -> str:
    return _with_marker([f"{html.escape(jira_key)} is already closed."], command_id)


def render_unauthorized(*, command_id: int, jira_key: str, verb: str) -> str:
    return _with_marker([
        f"You're not allowed to {verb} {html.escape(jira_key)}. Only the requester or a support admin can."
    ], command_id)


def render_no_transition(*, command_id: int, jira_key: str, target_status: str, verb: str) -> str:
    e = html.escape
    return _with_marker([
        f"Could not {verb} {e(jira_key)}: the Jira workflow has no transition to '{e(target_status)}' "
        f"from its current status. Please {verb} it in Jira or ask a Jira admin to check the workflow."
    ], command_id)


def render_reopen_success(*, command_id: int, jira_key: str, jira_url: str, sender_name: str,
                          reason: str | None) -> str:
    e = html.escape
    return _with_marker([
        f"🔄 {e(jira_key)} has been reopened by {e(sender_name)}.",
        f"Reason: {e(reason or 'Not provided')}",
        f'Jira: <a href="{e(jira_url, quote=True)}">{e(jira_url)}</a>',
    ], command_id)


def render_not_closed(*, command_id: int, jira_key: str, current_status: str) -> str:
    e = html.escape
    return _with_marker([
        f"{e(jira_key)} is not closed (current status: {e(current_status)}), so there is nothing to reopen."
    ], command_id)


def render_status(*, command_id: int, jira_key: str, jira_url: str, status: str, assignee: str | None,
                  priority: str | None, updated: str | None) -> str:
    e = html.escape
    return _with_marker([
        f"📋 <b>{e(jira_key)}</b> status: {e(status or 'Unknown')}",
        f"Assignee: {e(assignee or 'Unassigned')}",
        f"Priority: {e(priority or 'Not set')}",
        f"Last updated: {e(updated or 'Unknown')}",
        f'Jira: <a href="{e(jira_url, quote=True)}">{e(jira_url)}</a>',
    ], command_id)


def render_escalated(*, command_id: int, jira_key: str, jira_url: str, sender_name: str, from_priority: str,
                     to_priority: str, reason: str | None, alerted: bool, jira_priority_set: bool,
                     label: str) -> str:
    e = html.escape
    lines = [
        f"🚨 {e(jira_key)} has been escalated from {e(from_priority)} to {e(to_priority)} by {e(sender_name)}.",
        f"Reason: {e(reason or 'Not provided')}",
    ]
    if alerted:
        lines.append("The escalation channel has been notified.")
    if not jira_priority_set:
        lines.append(f"Note: Jira did not accept a priority change for this project; the issue was labelled "
                     f"'{e(label)}' instead.")
    lines.append(f'Jira: <a href="{e(jira_url, quote=True)}">{e(jira_url)}</a>')
    return _with_marker(lines, command_id)


def render_not_higher(*, command_id: int, jira_key: str, current: str, requested: str) -> str:
    e = html.escape
    return _with_marker([
        f"{e(jira_key)} is already {e(current)}. `escalate` can only raise the priority "
        f"({e(requested)} is not higher than {e(current)})."
    ], command_id)


def render_ticket_closed(*, command_id: int, jira_key: str) -> str:
    return _with_marker([
        f"{html.escape(jira_key)} is closed. Reply `reopen` first if the problem is back, then escalate."
    ], command_id)


def render_invalid_args(*, command_id: int, message: str) -> str:
    return _with_marker([html.escape(message)], command_id)


def update_marker(notification_id: int) -> str:
    return f"(OpsBridge update {notification_id})"


def render_jira_update(*, notification_id: int, jira_key: str, jira_url: str | None, status_changed: bool,
                       old_status: str | None, new_status: str | None, assignee_changed: bool,
                       old_assignee: str | None, new_assignee: str | None, actor: str | None = None,
                       source: str | None = None) -> str:
    """Jira -> Teams update, e.g. 'Status: To Do → In Progress'. `source` is stored for audit only."""
    e = html.escape
    lines = [f"🔔 <b>{e(jira_key)}</b> was updated in Jira"]
    if status_changed:
        lines.append(f"Status: {e(old_status or 'Unknown')} → <b>{e(new_status or 'Unknown')}</b>")
    if assignee_changed:
        lines.append(f"Assignee: {e(old_assignee or 'Unassigned')} → <b>{e(new_assignee or 'Unassigned')}</b>")
    if actor:
        lines.append(f"Changed by: {e(actor)}")
    if jira_url:
        lines.append(f'Jira: <a href="{e(jira_url, quote=True)}">{e(jira_url)}</a>')
    lines.append(f"<i>{e(update_marker(notification_id))}</i>")
    return _lines_to_html(lines)


def escalation_alert_marker(command_id: int) -> str:
    return f"(OpsBridge escalation {command_id})"


def render_escalation_alert(*, command_id: int, jira_key: str, jira_url: str, summary: str, from_priority: str,
                            to_priority: str, sender_name: str, reason: str | None,
                            teams_message_url: str | None) -> str:
    e = html.escape
    lines = [
        f"🚨 <b>Escalation: {e(jira_key)} raised from {e(from_priority)} to {e(to_priority)}</b>",
        e(summary),
        f"Escalated by {e(sender_name)}. Reason: {e(reason or 'Not provided')}",
        f'Jira: <a href="{e(jira_url, quote=True)}">{e(jira_key)}</a>',
    ]
    if teams_message_url:
        lines.append(f'Original request: <a href="{e(teams_message_url, quote=True)}">Teams thread</a>')
    lines.append(f"<i>{e(escalation_alert_marker(command_id))}</i>")
    return _lines_to_html(lines)


class NotificationService:
    def __init__(self, graph: GraphClient) -> None:
        self.graph = graph

    def already_replied(self, target: ReplyTarget, marker: str | None = None) -> str | None:
        return self.graph.find_reply_containing(
            target.team_id, target.channel_id, target.message_id, marker or reference_marker(target.request_id)
        )

    def send(self, target: ReplyTarget, body_html: str) -> str:
        reply_id = self.graph.reply_to_message(target.team_id, target.channel_id, target.message_id, body_html)
        logger.info("posted Teams reply", extra={"reply_id": reply_id})
        return reply_id
