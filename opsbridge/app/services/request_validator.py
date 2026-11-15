"""Business validation of parsed fields -> SupportRequestDraft. Parser-agnostic."""

from __future__ import annotations

import re

from app.domain.enums import Environment, Priority
from app.domain.messages import ParsedFields, SupportRequestDraft, ValidationIssue, ValidationResult
from app.services.intake_template import FIELD_SPECS, OPTIONAL_KEYS, SPECS_BY_KEY

_ENV_ALIASES = {
    "PROD": Environment.PROD,
    "PRODUCTION": Environment.PROD,
    "PRD": Environment.PROD,
    "UAT": Environment.UAT,
    "QA": Environment.QA,
}
_PRIORITY_RE = re.compile(r"^P\s*([1-9])\b", re.IGNORECASE)
_ALLOWED_ENVS = ", ".join(e.value for e in Environment)
_ALLOWED_PRIORITIES = ", ".join(p.value for p in Priority)


def normalise_environment(value: str) -> Environment | None:
    token = re.split(r"[\s(\-]", value.strip(), maxsplit=1)[0].upper() if value.strip() else ""
    return _ENV_ALIASES.get(token)


def normalise_priority(value: str) -> Priority | None:
    match = _PRIORITY_RE.match(value.strip())
    if not match:
        return None
    try:
        return Priority(f"P{match.group(1)}")
    except ValueError:
        return None


class RequestValidator:
    def validate(self, parsed: ParsedFields) -> ValidationResult:
        issues: list[ValidationIssue] = []
        values = parsed.fields

        for key in parsed.duplicate_keys:
            issues.append(ValidationIssue(key, f"{SPECS_BY_KEY[key].label} was provided more than once."))

        for spec in FIELD_SPECS:
            value = values.get(spec.key, "").strip()
            if spec.required and not value:
                issues.append(ValidationIssue(spec.key, f"Missing required field: {spec.label}."))
                continue
            if value and len(value) > spec.max_length:
                issues.append(
                    ValidationIssue(
                        spec.key,
                        f"{spec.label} must be at most {spec.max_length} characters (got {len(value)}).",
                    )
                )

        environment: Environment | None = None
        raw_env = values.get("environment", "").strip()
        if raw_env:
            environment = normalise_environment(raw_env)
            if environment is None:
                issues.append(
                    ValidationIssue(
                        "environment",
                        f"Environment '{raw_env[:32]}' is not supported. Allowed values: {_ALLOWED_ENVS}.",
                    )
                )

        priority: Priority | None = None
        raw_priority = values.get("priority", "").strip()
        if raw_priority:
            priority = normalise_priority(raw_priority)
            if priority is None:
                issues.append(
                    ValidationIssue(
                        "priority",
                        f"Priority '{raw_priority[:32]}' is not supported. Allowed values: {_ALLOWED_PRIORITIES}.",
                    )
                )

        if issues or environment is None or priority is None:
            return ValidationResult(draft=None, issues=issues)

        summary = " ".join(values["issue_summary"].split())  # Jira summaries are single-line
        draft = SupportRequestDraft(
            api_name=values["api_name"].strip(),
            environment=environment,
            issue_summary=summary,
            issue_description=values["issue_description"].strip(),
            correlation_id=values["correlation_id"].strip(),
            business_impact=values["business_impact"].strip(),
            priority=priority,
            optional_fields={k: values[k].strip() for k in OPTIONAL_KEYS if values.get(k, "").strip()},
        )
        return ValidationResult(draft=draft, issues=[])
