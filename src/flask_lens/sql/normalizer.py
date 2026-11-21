"""Pure-Python SQL canonicalizer, operation classifier, and table extractor."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_SQL_TOKEN_RE = re.compile(
    r"""
    (?P<block_comment>/\*[\s\S]*?\*/) |
    (?P<line_comment>--[^\n]*) |
    (?P<single_str>'(?:''|\\'|[^'])*') |
    (?P<dollar_param>\$\d+) |
    (?P<pyformat_param>%\([A-Za-z_][A-Za-z0-9_]*\)s) |
    (?P<format_param>%s) |
    (?P<named_param>(?<![A-Za-z0-9_:]):[A-Za-z_][A-Za-z0-9_]*) |
    (?P<hex_num>\b0x[0-9a-fA-F]+\b) |
    (?P<num>(?<![A-Za-z0-9_])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![A-Za-z0-9_]))
    """,
    re.VERBOSE,
)

_IN_LIST_RE = re.compile(r"\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.IGNORECASE)
_VALUES_LIST_RE = re.compile(
    r"\bVALUES\s*(\(\s*\?(?:\s*,\s*\?)*\s*\))(?:\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))+",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")

_TABLE_RE = re.compile(
    r"""
    \b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+
    (?:ONLY\s+)?
    (?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_.]*))
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SQL_KEYWORDS_NOT_TABLES = frozenset(
    {
        "select",
        "where",
        "set",
        "values",
        "as",
        "on",
        "using",
        "inner",
        "left",
        "right",
        "outer",
        "full",
        "cross",
        "lateral",
        "unnest",
        "generate_series",
        "dual",
    }
)


@dataclass(frozen=True)
class NormalizedSQL:
    """Canonical representation of an SQL statement."""

    normalized: str
    fingerprint: str
    operation: str
    tables: list[str]


def _replace_token(match: re.Match[str]) -> str:
    if match.lastgroup in {"block_comment", "line_comment"}:
        return " "
    return "?"


def classify_operation(sql: str) -> str:
    """Classify SQL into SELECT, INSERT, UPDATE, DELETE, TRANSACTION, DDL, or OTHER."""
    stripped = _strip_leading_comments(sql).strip()
    if not stripped:
        return "OTHER"
    first = stripped.split(None, 1)[0].upper()
    if first == "WITH":
        upper = stripped.upper()
        for verb in ("INSERT", "UPDATE", "DELETE", "SELECT"):
            if re.search(rf"\)\s*{verb}\b", upper):
                return verb
        return "SELECT"
    if first in {"SELECT", "INSERT", "UPDATE", "DELETE"}:
        return first
    if first in {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
        return "TRANSACTION"
    if first in {"CREATE", "ALTER", "DROP", "TRUNCATE", "PRAGMA"}:
        return "DDL"
    return "OTHER"


def extract_tables(sql: str) -> list[str]:
    """Extract unique referenced table names in appearance order."""
    tables: list[str] = []
    seen: set[str] = set()
    cleaned = _strip_comments_and_strings(sql)
    for match in _TABLE_RE.finditer(cleaned):
        raw = next((group for group in match.groups() if group), "")
        if not raw:
            continue
        leaf = raw.split(".")[-1].lower()
        if leaf in _SQL_KEYWORDS_NOT_TABLES:
            continue
        if raw not in seen:
            seen.add(raw)
            tables.append(raw)
    return tables


def normalize_sql(sql: str) -> NormalizedSQL:
    """Normalize literals, bind parameters, and IN-lists into a stable fingerprint."""
    replaced = _SQL_TOKEN_RE.sub(_replace_token, sql)
    replaced = _IN_LIST_RE.sub("IN (?)", replaced)
    replaced = _VALUES_LIST_RE.sub(r"VALUES \1, (...)", replaced)
    normalized = _WHITESPACE_RE.sub(" ", replaced).strip()
    fingerprint = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    operation = classify_operation(sql)
    tables = extract_tables(sql)
    return NormalizedSQL(
        normalized=normalized,
        fingerprint=fingerprint,
        operation=operation,
        tables=tables,
    )


def _strip_leading_comments(sql: str) -> str:
    s = sql.lstrip()
    while True:
        if s.startswith("--"):
            nl = s.find("\n")
            s = "" if nl == -1 else s[nl + 1 :].lstrip()
        elif s.startswith("/*"):
            end = s.find("*/")
            s = "" if end == -1 else s[end + 2 :].lstrip()
        else:
            return s


def _strip_comments_and_strings(sql: str) -> str:
    def _blank(match: re.Match[str]) -> str:
        if match.lastgroup in {"block_comment", "line_comment", "single_str"}:
            return " "
        return match.group(0)

    return _SQL_TOKEN_RE.sub(_blank, sql)
