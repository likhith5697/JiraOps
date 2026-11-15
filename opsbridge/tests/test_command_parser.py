from __future__ import annotations

import pytest

from app.services.command_parser import Command, CommandParser

parser = CommandParser()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("close", Command("close", None)),
        ("/close", Command("close", None)),
        ("CLOSE reason text", Command("close", "reason text")),
        ("Close Fixed after redeploy", Command("close", "Fixed after redeploy")),
        ("   close   leading whitespace", Command("close", "leading whitespace")),
        ("\n/close\n", Command("close", None)),
        ("close: done now", Command("close", "done now")),
        ("close\nline one\nline two", Command("close", "line one line two")),
    ],
)
def test_close_is_recognised(text, expected):
    assert parser.parse(text) == expected


@pytest.mark.parametrize(
    "text",
    ["closed", "closer", "close-up", "please close", "/closed now", "", "   ", "thanks, looks good",
     "reopened", "statuses", "escalated to P1", "escalation", None],
)
def test_non_commands_do_not_match(text):
    assert parser.parse(text) is None


def test_reason_is_truncated():
    command = parser.parse("close " + "x" * 5000)
    assert command is not None and len(command.reason) == 1000


def test_registry_is_extensible_without_touching_matching_rules():
    extended = CommandParser(frozenset({"close", "reopen", "status"}))
    assert extended.parse("/reopen still broken") == Command("reopen", "still broken")
    assert extended.parse("status") == Command("status", None)
    assert extended.parse("reopened") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("status", Command("status", None)),
        ("/STATUS please", Command("status", None)),  # trailing text ignored
        ("reopen", Command("reopen", None)),
        ("/reopen still failing after deploy", Command("reopen", "still failing after deploy")),
        ("escalate P1", Command("escalate", None, {"priority": "P1"})),
        ("escalate p2 customer impact growing", Command("escalate", "customer impact growing", {"priority": "P2"})),
        ("/Escalate P1: outage", Command("escalate", "outage", {"priority": "P1"})),
    ],
)
def test_more_commands(text, expected):
    assert parser.parse(text) == expected


@pytest.mark.parametrize("text", ["escalate", "escalate P9", "escalate urgent please", "escalate high"])
def test_escalate_without_valid_priority_is_a_usage_error(text):
    command = parser.parse(text)
    assert command is not None and command.name == "escalate"
    assert command.args == {} and command.error and command.error.startswith("Usage: escalate P1|P2|P3")
