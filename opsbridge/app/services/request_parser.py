"""Support-request parsers.

`RequestParser` is the extension point for Phase 3 (LLM-based natural-language intake):
a parser only *extracts* raw field values; `RequestValidator` applies business rules.
Swapping parsers therefore never touches validation, Jira or Teams logic.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from app.domain.messages import InboundMessage, ParsedFields
from app.services.intake_template import FIELD_SPECS


@runtime_checkable
class RequestParser(Protocol):
    name: str

    def parse(self, message: InboundMessage) -> ParsedFields: ...


# --------------------------------------------------------------------------------------
# Template parser
# --------------------------------------------------------------------------------------
_LABEL_LINE = re.compile(
    r"^\s*[*_~]*\s*(?P<label>[A-Za-z][A-Za-z0-9 /_\-]{0,40}?)\s*[*_~]*\s*[:：]\s*[*_~]*\s*(?P<value>.*)$"
)


def _normalise_label(label: str) -> str:
    return re.sub(r"[\s_\-/]+", " ", label.strip().lower())


_ALIASES: dict[str, str] = {
    _normalise_label(alias): spec.key for spec in FIELD_SPECS for alias in (spec.label, *spec.aliases)
}


class TemplateRequestParser:
    """Parses `Label: value` lines. Known labels start a field; other lines continue the current one.

    Multi-line values (e.g. Issue Description) are supported: every line until the next known
    label belongs to the current field. Lines such as `Note: ...` inside a description are kept
    because `Note` is not a template label.
    """

    name = "template-v1"

    def parse(self, message: InboundMessage) -> ParsedFields:
        values: dict[str, list[str]] = {}
        duplicates: list[str] = []
        unknown: list[str] = []
        current: str | None = None

        for line in message.body_text.split("\n"):
            match = _LABEL_LINE.match(line)
            key = _ALIASES.get(_normalise_label(match.group("label"))) if match else None
            if match and key:
                if key in values:
                    duplicates.append(key)
                    current = None  # ignore the repeated block entirely
                    continue
                current = key
                values[key] = [match.group("value").strip()]
            elif current is not None:
                values[current].append(line.strip())
            elif line.strip():
                unknown.append(line.strip())

        fields = {key: "\n".join(lines).strip() for key, lines in values.items()}
        return ParsedFields(
            fields=fields,
            parser_name=self.name,
            unknown_content="\n".join(unknown),
            duplicate_keys=tuple(dict.fromkeys(duplicates)),
        )
