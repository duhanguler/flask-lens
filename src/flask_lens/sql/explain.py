"""Safe read-only EXPLAIN / EXPLAIN QUERY PLAN runner with index-scan diagnostics."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from flask_lens.sql.normalizer import classify_operation, strip_comments_and_strings

_MUTATING_KEYWORD_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|PRAGMA|ATTACH|DETACH|VACUUM|CALL|COPY)\b",
    re.IGNORECASE,
)


@dataclass
class ExplainResult:
    """Structured query execution plan plus automated performance hints."""

    supported: bool
    dialect: str
    statement: str
    plan_lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_safe_explainable_sql(sql: str) -> tuple[bool, str | None]:
    """Validate that *sql* is a single read-only SELECT query safe for EXPLAIN."""
    stripped = sql.strip()
    if not stripped:
        return False, "Empty SQL statement."
    # Reject unclosed block comments so malicious trailing payloads can't hide
    if _has_unclosed_block_comment(stripped):
        return False, "Unclosed SQL block comment is not permitted for EXPLAIN."

    # Strip comments & string literals before structural checks
    structural = strip_comments_and_strings(stripped).strip()
    if structural.endswith(";"):
        structural = structural[:-1].strip()
    if not structural:
        return False, "Empty SQL statement."
    if ";" in structural:
        return False, "Multi-statement SQL is not permitted for EXPLAIN."

    op = classify_operation(stripped)
    if op != "SELECT":
        return False, f"EXPLAIN is restricted to read-only SELECT queries (got {op})."

    # Also reject data-modifying CTEs like: WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d
    if _MUTATING_KEYWORD_RE.search(structural):
        return False, "Data-modifying or DDL statements inside CTEs are not permitted for EXPLAIN."

    return True, None


def run_explain(
    engine: Any,
    sql: str,
    params: Any = None,
) -> ExplainResult:
    """Run read-only EXPLAIN against *engine* for a captured SELECT query."""
    dialect = getattr(getattr(engine, "dialect", None), "name", "unknown")
    safe, reason = is_safe_explainable_sql(sql)
    if not safe:
        return ExplainResult(
            supported=False,
            dialect=dialect,
            statement=sql,
            error=reason,
        )

    clean_sql = sql.strip().rstrip(";")
    if dialect == "sqlite":
        explain_sql = f"EXPLAIN QUERY PLAN {clean_sql}"
    elif dialect in {"postgresql", "postgres"}:
        explain_sql = f"EXPLAIN (FORMAT TEXT) {clean_sql}"
    else:
        explain_sql = f"EXPLAIN {clean_sql}"

    plan_lines: list[str] = []
    try:
        with engine.connect() as conn:
            raw_conn = conn.connection
            cursor = raw_conn.cursor()
            try:
                if params is None or params in ((), [], {}):
                    cursor.execute(explain_sql)
                else:
                    cursor.execute(explain_sql, params)
                rows = cursor.fetchall()
            finally:
                cursor.close()
    except Exception as exc:
        return ExplainResult(
            supported=True,
            dialect=dialect,
            statement=explain_sql,
            error=f"EXPLAIN failed: {exc}",
        )

    for row in rows:
        if dialect == "sqlite" and len(row) >= 4:
            plan_lines.append(str(row[3]))
        elif len(row) == 1:
            plan_lines.append(str(row[0]))
        else:
            plan_lines.append(" | ".join(str(cell) for cell in row))

    warnings = _extract_plan_warnings(dialect, plan_lines)
    return ExplainResult(
        supported=True,
        dialect=dialect,
        statement=explain_sql,
        plan_lines=plan_lines,
        warnings=warnings,
    )


def _extract_plan_warnings(dialect: str, lines: list[str]) -> list[str]:
    warnings: list[str] = []
    joined = "\n".join(lines).upper()
    if dialect == "sqlite":
        for line in lines:
            upper = line.upper()
            if (
                upper.startswith("SCAN ")
                and "USING COVERING INDEX" not in upper
                and "USING INDEX" not in upper
            ):
                warnings.append(f"Full table scan detected: {line}")
            if "USE TEMP B-TREE" in upper:
                warnings.append(f"Temporary B-Tree sort/group needed: {line}")
    elif dialect in {"postgresql", "postgres"}:
        if "SEQ SCAN" in joined:
            warnings.append(
                "PostgreSQL Seq Scan detected — check missing indexes on WHERE/JOIN columns."
            )
        if "SORT METHOD: EXTERNAL" in joined:
            warnings.append("External disk sort detected — consider indexing ORDER BY columns.")
    elif dialect in {"mysql", "mariadb"}:
        if "ALL" in joined:
            warnings.append("Full table scan (type=ALL) detected.")
        if "USING FILESORT" in joined:
            warnings.append("MySQL Using filesort detected.")
        if "USING TEMPORARY" in joined:
            warnings.append("MySQL Using temporary table detected.")
    return warnings


def _has_unclosed_block_comment(sql: str) -> bool:
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
        elif in_single:
            if ch == "'" and nxt == "'":
                i += 2
                continue
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        else:
            if ch == "-" and nxt == "-":
                in_line_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
        i += 1
    return in_block_comment
