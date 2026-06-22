"""Shared fuzzy and regex matching helpers for read/query tools."""
from __future__ import annotations

import json
import re
from typing import Any


def search_blob(*values: Any) -> str:
    parts: list[str] = []

    def add(value: Any) -> None:
        if value in (None, "", [], {}):
            return
        if isinstance(value, dict):
            for key in sorted(value.keys(), key=str):
                add(key)
                add(value.get(key))
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        try:
            parts.append(json.dumps(value, ensure_ascii=False, default=str))
        except TypeError:
            parts.append(str(value))

    for value in values:
        add(value)
    return "\n".join(parts)


def normalize_regex_patterns(*values: Any) -> list[str]:
    patterns: list[str] = []

    def add(value: Any) -> None:
        if value in (None, "", [], {}):
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        text = str(value).strip()
        if not text:
            return
        for line in text.splitlines():
            pattern = line.strip()
            if pattern:
                patterns.append(pattern)

    for value in values:
        add(value)
    return patterns


def match_text(
    blob: str,
    *,
    query: str | None = None,
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    raw_blob = str(blob or "")
    query_text = str(query or "").strip()
    regex_patterns = normalize_regex_patterns(regex, pattern)
    if not query_text and not regex_patterns:
        return {
            "matched": True,
            "mode": "all",
            "matched_terms": [],
            "matched_patterns": [],
            "invalid_patterns": [],
        }

    flags = 0 if case_sensitive else re.IGNORECASE
    invalid_patterns: list[dict[str, str]] = []
    matched_patterns: list[str] = []
    for regex_pattern in regex_patterns:
        try:
            compiled = re.compile(regex_pattern, flags)
        except re.error as exc:
            invalid_patterns.append({"pattern": regex_pattern, "error": str(exc)})
            continue
        if compiled.search(raw_blob):
            matched_patterns.append(regex_pattern)

    matched_terms: list[str] = []
    if query_text:
        compare_blob = raw_blob if case_sensitive else raw_blob.lower()
        compare_query = query_text if case_sensitive else query_text.lower()
        terms = [term for term in re.split(r"\s+", compare_query) if term]
        if compare_query and compare_query in compare_blob:
            matched_terms = [query_text]
        elif terms and all(term in compare_blob for term in terms):
            matched_terms = terms

    matched = bool(matched_terms or matched_patterns)
    mode = "none"
    if matched_terms and matched_patterns:
        mode = "query+regex"
    elif matched_patterns:
        mode = "regex"
    elif matched_terms:
        mode = "query"

    return {
        "matched": matched,
        "mode": mode,
        "matched_terms": matched_terms,
        "matched_patterns": matched_patterns,
        "invalid_patterns": invalid_patterns,
    }


def invalid_regex_response(regex: Any = None, pattern: Any = None) -> dict[str, Any] | None:
    match = match_text("", regex=regex, pattern=pattern)
    invalid = match.get("invalid_patterns") or []
    if not invalid:
        return None
    return {
        "ok": False,
        "error": "Invalid regex pattern",
        "error_kind": "invalid_regex",
        "invalid_patterns": invalid,
    }
