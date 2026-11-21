"""Redact secrets and personal data BEFORE text is sent to any LLM or stored as knowledge.

Deliberately conservative about what it keeps: Jira keys (OPS-12), correlation IDs (CORR-123456),
HTTP status codes, dates and UUIDs survive, because they are useful for search and are not secrets.
"""

from __future__ import annotations

import re

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_RULES: list[tuple[re.Pattern[str], str]] = [
    # Authorization headers / bearer & basic tokens
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]{8,}=*"), r"\1 [REDACTED]"),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"), "[REDACTED_JWT]"),
    # key=value / key: value secrets (password=..., api_key: ..., "token": "...")
    (re.compile(
        r"(?i)\b((?:pass(?:word|wd)?|pwd|secret|client[_-]?secret|token|access[_-]?token|refresh[_-]?token|"
        r"api[_-]?key|apikey|access[_-]?key|private[_-]?key|auth)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}]+"),
     r"\1[REDACTED]"),
    # Well-known key prefixes (Anthropic, OpenAI, Atlassian, GitHub, Slack, AWS)
    (re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9_-]{20,}|ATATT[A-Za-z0-9_=-]{20,}|"
                r"gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"), "[REDACTED_KEY]"),
    # Email addresses
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    # Phone numbers: +44 20 7946 0958, (555) 123-4567, 555-123-4567, 555.123.4567
    (re.compile(r"(?<![\w-])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?![\w-])"), "[PHONE]"),
    (re.compile(r"(?<![\w-])(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w-])"), "[PHONE]"),
]

_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_=-]{32,}(?![A-Za-z0-9+/_=-])")


def _redact_long_token(match: re.Match[str]) -> str:
    token = match.group(0)
    if _UUID.match(token):
        return token  # identifiers, not secrets
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    if has_digit and has_alpha:  # long hex / base64 strings: keys, hashes, signatures
        return "[REDACTED_TOKEN]"
    return token


def redact_text(text: str | None) -> str:
    if not text:
        return ""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return _LONG_TOKEN.sub(_redact_long_token, text)
