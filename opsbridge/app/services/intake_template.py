"""Single source of truth for the structured intake template.

Used by the parser (label aliases), the validator (required/limits) and the notification
service (the template shown to users when a submission is incomplete).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    required: bool
    max_length: int
    aliases: tuple[str, ...] = ()
    multiline: bool = False
    example: str = ""


FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("api_name", "API Name", True, 128, ("api name", "api"), example="CustomerOrderAPI"),
    FieldSpec("environment", "Environment", True, 16, ("environment", "env"), example="QA"),
    FieldSpec("issue_summary", "Issue Summary", True, 255, ("issue summary", "summary"),
              example="Order creation is failing"),
    FieldSpec("issue_description", "Issue Description", True, 20_000,
              ("issue description", "description"), multiline=True,
              example="The API returns HTTP 500 when creating an order."),
    FieldSpec("correlation_id", "Correlation ID", True, 128,
              ("correlation id", "correlationid", "correlation"), example="CORR-123456"),
    FieldSpec("business_impact", "Business Impact", True, 5_000, ("business impact", "impact"),
              multiline=True, example="Order processing is blocked in QA."),
    FieldSpec("priority", "Priority", True, 16, ("priority",), example="P2"),
    # Optional fields
    FieldSpec("steps_to_reproduce", "Steps to Reproduce", False, 10_000,
              ("steps to reproduce", "steps"), multiline=True),
    FieldSpec("additional_notes", "Additional Notes", False, 10_000,
              ("additional notes", "notes"), multiline=True),
)

SPECS_BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in FIELD_SPECS}
REQUIRED_KEYS: tuple[str, ...] = tuple(s.key for s in FIELD_SPECS if s.required)
OPTIONAL_KEYS: tuple[str, ...] = tuple(s.key for s in FIELD_SPECS if not s.required)


def template_text() -> str:
    lines = [f"{spec.label}: {spec.example or '<value>'}" for spec in FIELD_SPECS if spec.required]
    lines.append("")
    lines.append("Optional: " + ", ".join(s.label for s in FIELD_SPECS if not s.required))
    return "\n".join(lines)
