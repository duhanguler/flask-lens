"""Pure-Python SQL canonicalizer, operation classifier, and table extractor."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_SQL_TOKEN_RE = re.compile(
    r"""
    (?P<block_comment>/\*[\s\S]*?\*/) |
    (?P<line_comment>--[^\n]*) |
    (?P<pg_escape_str>[eE]'(?:''|\\.|[^'\\])*') |
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

_IN_LIST_RE = re.compile(
    r"\bIN\s*\(\s*(?:\?|NULL)(?:\s*,\s*(?:\?|NULL))*\s*\)",
    re.IGNORECASE,
)
_VALUES_LIST_RE = re.compile(
    r"\bVALUES\s*(\(\s*(?:\?|NULL)(?:\s*,\s*(?:\?|NULL))*\s*\))(?:\s*,\s*\(\s*(?:\?|NULL)(?:\s*,\s*(?:\?|NULL))*\s*\))+",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")

_IDENT_PART = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)'
_TABLE_RE = re.compile(
    rf"""
    \b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+
    (?:ONLY\s+)?
    (?P<table>{_IDENT_PART}(?:\.{_IDENT_PART})?)
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


def strip_comments_and_strings(sql: str) -> str:
    """Replace SQL comments and string literals with whitespace for safe structural inspection."""

    def _blank(match: re.Match[str]) -> str:
        if match.lastgroup in {"block_comment", "line_comment", "single_str", "pg_escape_str"}:
            return " "
        return match.group(0)

    return _SQL_TOKEN_RE.sub(_blank, sql)


def classify_operation(sql: str) -> str:
    """Classify SQL into SELECT, INSERT, UPDATE, DELETE, TRANSACTION, DDL, or OTHER."""
    cleaned = strip_comments_and_strings(sql).strip()
    if not cleaned:
        return "OTHER"
    first = cleaned.split(None, 1)[0].upper()
    if first == "WITH":
        upper = cleaned.upper()
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
    """Extract unique referenced table names (including schema.table) in appearance order."""
    tables: list[str] = []
    seen: set[str] = set()
    cleaned = strip_comments_and_strings(sql)
    for match in _TABLE_RE.finditer(cleaned):
        raw_expr = match.group("table")
        if not raw_expr:
            continue
        parts = [_unquote_ident(p) for p in _split_qualified_ident(raw_expr)]
        normalized_table = ".".join(p for p in parts if p)
        if not normalized_table:
            continue
        leaf = parts[-1].lower()
        if leaf in _SQL_KEYWORDS_NOT_TABLES:
            continue
        if normalized_table not in seen:
            seen.add(normalized_table)
            tables.append(normalized_table)
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


def _split_qualified_ident(expr: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in expr:
        if quote is not None:
            buf.append(ch)
            if (quote == "[" and ch == "]") or (quote in {'"', "`"} and ch == quote):
                quote = None
        elif ch in {'"', "`", "["}:
            quote = ch
            buf.append(ch)
        elif ch == ".":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _unquote_ident(ident: str) -> str:
    if len(ident) >= 2 and (
        (ident[0] == '"' and ident[-1] == '"')
        or (ident[0] == "`" and ident[-1] == "`")
        or (ident[0] == "[" and ident[-1] == "]")
    ):
        return ident[1:-1]
    return ident
