"""Parse a Teams thread reply into a Command.

Grammar (case-insensitive, leading whitespace ignored):

    [/]<command>[:]?[ <arguments / reason ...>]

The command word must be followed by end-of-text, whitespace or ':' so that `closed`,
`reopened` or `close-up` are NOT treated as commands.

Supported today:

    close    [reason]
    reopen   [reason]
    status
    escalate <P1|P2|P3> [reason]
    ask      <question>
    helpful | not helpful            (feedback on an `ask` answer)

Adding a command = one entry in ARGUMENT_PARSERS (+ an executor in CommandHandler). The
processor routes every parsed command to the handler, so it never changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from app.services.request_validator import normalise_priority

MAX_REASON_LENGTH = 1000
ESCALATE_USAGE = "Usage: escalate P1|P2|P3 [reason], e.g. `escalate P1 Customer-facing outage`"

_COMMAND_RE = re.compile(r"^\s*/?(?P<name>[a-z]+)(?=$|[\s:!.,?])[\s:!.,]*(?P<rest>.*)$",
                         re.IGNORECASE | re.DOTALL)
_NOT_HELPFUL_RE = re.compile(r"^\s*/?(?:not[\s-]*helpful|unhelpful)(?=$|[\s!.,?])", re.IGNORECASE)
ASK_USAGE = "Usage: ask <your question>, e.g. `ask why does CustomerOrderAPI return HTTP 500 in QA?`"


@dataclass(frozen=True)
class Command:
    name: str
    reason: str | None
    args: dict[str, str] = field(default_factory=dict)
    error: str | None = None  # set when the command is recognised but its arguments are invalid


# An argument parser turns the text after the command word into (args, reason, error).
ArgumentParser = Callable[[str], tuple[dict[str, str], str, str | None]]


def _reason_only(rest: str) -> tuple[dict[str, str], str, str | None]:
    return {}, rest, None


def _no_arguments(rest: str) -> tuple[dict[str, str], str, str | None]:
    return {}, "", None  # `status` ignores trailing text


def _ask(rest: str) -> tuple[dict[str, str], str, str | None]:
    return ({}, rest, None) if rest.strip() else ({}, "", ASK_USAGE)


def _escalate(rest: str) -> tuple[dict[str, str], str, str | None]:
    first, _, remainder = rest.partition(" ")
    priority = normalise_priority(first) if first else None
    if priority is None:
        return {}, rest, ESCALATE_USAGE
    return {"priority": priority.value}, remainder, None


ARGUMENT_PARSERS: dict[str, ArgumentParser] = {
    "close": _reason_only,
    "reopen": _reason_only,
    "status": _no_arguments,
    "escalate": _escalate,
    "ask": _ask,
    "helpful": _no_arguments,
    "not_helpful": _no_arguments,
}
SUPPORTED_COMMANDS: frozenset[str] = frozenset(ARGUMENT_PARSERS)


class CommandParser:
    def __init__(self, supported: frozenset[str] = SUPPORTED_COMMANDS) -> None:
        self.supported = frozenset(c.lower() for c in supported)

    def parse(self, text: str | None) -> Command | None:
        """Return a Command for supported commands, otherwise None (ordinary chatter)."""
        if not text:
            return None
        if "not_helpful" in self.supported and _NOT_HELPFUL_RE.match(text):
            return Command(name="not_helpful", reason=None)
        match = _COMMAND_RE.match(text)
        if not match:
            return None
        name = match.group("name").lower()
        if name not in self.supported:
            return None
        rest = " ".join(match.group("rest").split())  # collapse newlines/extra spaces
        args, reason, error = ARGUMENT_PARSERS.get(name, _reason_only)(rest)
        limit = 2000 if name == "ask" else MAX_REASON_LENGTH
        return Command(name=name, reason=reason[:limit] or None, args=args, error=error)
