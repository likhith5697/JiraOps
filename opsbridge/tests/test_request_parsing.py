"""Scenarios 2-4: field extraction, missing fields, invalid priority, invalid environment."""

from __future__ import annotations

import pytest

from app.core.text import html_to_text
from app.domain.enums import Environment, Priority
from app.domain.messages import InboundMessage
from app.services.request_parser import RequestParser, TemplateRequestParser
from app.services.request_validator import RequestValidator
from tests.conftest import VALID_MESSAGE


def validate(text: str):
    message = InboundMessage(message_id="m1", team_id="t", channel_id="c", body_text=text)
    return RequestValidator().validate(TemplateRequestParser().parse(message))


def messages_of(result) -> list[str]:
    return [i.message for i in result.issues]


def test_template_parser_satisfies_parser_protocol():
    assert isinstance(TemplateRequestParser(), RequestParser)


def test_valid_template_is_extracted():
    result = validate(VALID_MESSAGE)
    assert result.is_valid
    d = result.draft
    assert d.api_name == "CustomerOrderAPI"
    assert d.environment is Environment.QA
    assert d.priority is Priority.P2
    assert d.issue_summary == "Order creation is failing"
    assert d.issue_description == (
        "The API returns HTTP 500 when creating an order.\nThis started after the latest deployment."
    )
    assert d.correlation_id == "CORR-123456"
    assert d.business_impact == "Order processing is blocked in QA."
    assert d.optional_fields == {}


def test_teams_html_body_is_supported():
    html = (
        "<p><strong>API Name</strong>: CustomerOrderAPI</p><p>Environment: qa</p>"
        "<p>Issue Summary: Order creation &amp; payment failing</p>"
        "<div>Issue Description:<br>Line one<br>Note: not a template label</div>"
        "<p>Correlation ID: CORR-1</p><p>Business Impact: Blocked</p><p>Priority:&nbsp;p1</p>"
    )
    result = validate(html_to_text(html))
    assert result.is_valid, messages_of(result)
    assert result.draft.issue_summary == "Order creation & payment failing"
    assert result.draft.issue_description == "Line one\nNote: not a template label"
    assert result.draft.priority is Priority.P1
    assert result.draft.environment is Environment.QA


def test_optional_fields_are_captured():
    result = validate(VALID_MESSAGE + "\nSteps to Reproduce:\n1. POST /orders\n2. Observe 500")
    assert result.is_valid
    assert result.draft.optional_fields == {"steps_to_reproduce": "1. POST /orders\n2. Observe 500"}


def test_missing_required_fields_are_all_reported():
    result = validate("API Name: CustomerOrderAPI\nPriority: P2")
    assert not result.is_valid
    assert result.draft is None
    missing = messages_of(result)
    for label in ("Environment", "Issue Summary", "Issue Description", "Correlation ID", "Business Impact"):
        assert f"Missing required field: {label}." in missing
    assert not any("API Name" in m for m in missing)


def test_blank_value_counts_as_missing():
    result = validate(VALID_MESSAGE.replace("Correlation ID: CORR-123456", "Correlation ID:   "))
    assert messages_of(result) == ["Missing required field: Correlation ID."]


@pytest.mark.parametrize("value", ["P4", "P0", "High", "urgent"])
def test_invalid_priority(value: str):
    result = validate(VALID_MESSAGE.replace("Priority: P2", f"Priority: {value}"))
    assert not result.is_valid
    assert messages_of(result) == [f"Priority '{value}' is not supported. Allowed values: P1, P2, P3."]


@pytest.mark.parametrize("value", ["DEV", "staging", "Prod2"])
def test_invalid_environment(value: str):
    result = validate(VALID_MESSAGE.replace("Environment: QA", f"Environment: {value}"))
    assert not result.is_valid
    assert messages_of(result) == [f"Environment '{value}' is not supported. Allowed values: PROD, UAT, QA."]


@pytest.mark.parametrize(
    ("raw", "expected"), [("prod", Environment.PROD), ("Production", Environment.PROD), ("uat", Environment.UAT)]
)
def test_environment_normalisation(raw: str, expected: Environment):
    result = validate(VALID_MESSAGE.replace("Environment: QA", f"Environment: {raw}"))
    assert result.draft.environment is expected


@pytest.mark.parametrize(("raw", "expected"), [("p3", Priority.P3), ("P1 - Critical", Priority.P1)])
def test_priority_normalisation(raw: str, expected: Priority):
    result = validate(VALID_MESSAGE.replace("Priority: P2", f"Priority: {raw}"))
    assert result.draft.priority is expected


def test_duplicate_label_is_rejected():
    result = validate(VALID_MESSAGE + "\nPriority: P1")
    assert messages_of(result) == ["Priority was provided more than once."]


def test_summary_length_limit():
    result = validate(VALID_MESSAGE.replace("Order creation is failing", "x" * 300))
    assert messages_of(result) == ["Issue Summary must be at most 255 characters (got 300)."]


def test_free_text_message_is_invalid():
    result = validate("Hi team, the order API looks broken, can someone help?")
    assert not result.is_valid
    assert len(result.issues) == 7
