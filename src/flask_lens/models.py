"""Strongly-typed domain models for captured requests, SQL, profiling, and analytics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class StackFrame:
    """Single source frame pointing to user or framework code."""

    filename: str
    lineno: int
    function: str
    code_line: str = ""
    context_lines: list[tuple[int, str]] = field(default_factory=list)
    is_user_code: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "lineno": self.lineno,
            "function": self.function,
            "code_line": self.code_line,
            "context_lines": [list(item) for item in self.context_lines],
            "is_user_code": self.is_user_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StackFrame:
        return cls(
            filename=str(data.get("filename", "")),
            lineno=int(data.get("lineno", 0)),
            function=str(data.get("function", "")),
            code_line=str(data.get("code_line", "")),
            context_lines=[(int(line[0]), str(line[1])) for line in data.get("context_lines", [])],
            is_user_code=bool(data.get("is_user_code", False)),
        )


@dataclass
class SQLQuery:
    """Single captured SQL query executed during a request."""

    id: str
    sql: str
    normalized_sql: str
    fingerprint: str
    operation: str  # SELECT, INSERT, UPDATE, DELETE, DDL, OTHER
    tables: list[str]
    params: Any
    duration_ms: float
    offset_ms: float  # ms since request start
    rowcount: int | None = None
    engine_name: str = "default"
    dialect: str = "sqlite"
    is_slow: bool = False
    is_duplicate: bool = False
    is_n_plus_one: bool = False
    n_plus_one_group: str | None = None
    stack: list[StackFrame] = field(default_factory=list)
    call_site: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stack"] = [frame.to_dict() for frame in self.stack]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SQLQuery:
        return cls(
            id=str(data["id"]),
            sql=str(data["sql"]),
            normalized_sql=str(data["normalized_sql"]),
            fingerprint=str(data["fingerprint"]),
            operation=str(data.get("operation", "OTHER")),
            tables=list(data.get("tables", [])),
            params=data.get("params"),
            duration_ms=float(data["duration_ms"]),
            offset_ms=float(data.get("offset_ms", 0.0)),
            rowcount=data.get("rowcount"),
            engine_name=str(data.get("engine_name", "default")),
            dialect=str(data.get("dialect", "sqlite")),
            is_slow=bool(data.get("is_slow", False)),
            is_duplicate=bool(data.get("is_duplicate", False)),
            is_n_plus_one=bool(data.get("is_n_plus_one", False)),
            n_plus_one_group=data.get("n_plus_one_group"),
            stack=[StackFrame.from_dict(f) for f in data.get("stack", [])],
            call_site=str(data.get("call_site", "")),
        )


@dataclass
class NPlusOneGroup:
    """Detected N+1 query pattern within a request."""

    fingerprint: str
    normalized_sql: str
    count: int
    total_ms: float
    call_site: str
    query_ids: list[str]
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NPlusOneGroup:
        return cls(
            fingerprint=str(data["fingerprint"]),
            normalized_sql=str(data["normalized_sql"]),
            count=int(data["count"]),
            total_ms=float(data["total_ms"]),
            call_site=str(data.get("call_site", "")),
            query_ids=list(data.get("query_ids", [])),
            suggestion=str(data.get("suggestion", "")),
        )


@dataclass
class OutgoingHTTP:
    """Captured outgoing HTTP call (requests/urllib3/httpx)."""

    id: str
    method: str
    url: str
    host: str
    status_code: int | None
    duration_ms: float
    offset_ms: float
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    library: str = "urllib3"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutgoingHTTP:
        return cls(
            id=str(data["id"]),
            method=str(data["method"]),
            url=str(data["url"]),
            host=str(data.get("host", "")),
            status_code=data.get("status_code"),
            duration_ms=float(data["duration_ms"]),
            offset_ms=float(data.get("offset_ms", 0.0)),
            request_headers=dict(data.get("request_headers", {})),
            response_headers=dict(data.get("response_headers", {})),
            error=data.get("error"),
            library=str(data.get("library", "urllib3")),
        )


@dataclass
class LogEntry:
    """Python logging record emitted during a request."""

    level: str
    levelno: int
    logger: str
    message: str
    offset_ms: float
    filename: str = ""
    lineno: int = 0
    function: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LogEntry:
        return cls(
            level=str(data["level"]),
            levelno=int(data.get("levelno", 20)),
            logger=str(data.get("logger", "")),
            message=str(data["message"]),
            offset_ms=float(data.get("offset_ms", 0.0)),
            filename=str(data.get("filename", "")),
            lineno=int(data.get("lineno", 0)),
            function=str(data.get("function", "")),
        )


@dataclass
class CustomSpan:
    """User-defined span or event recorded via ``lens.span(...)`` / ``lens.event(...)``."""

    name: str
    kind: str  # "span" or "event" or "template"
    offset_ms: float
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CustomSpan:
        return cls(
            name=str(data["name"]),
            kind=str(data.get("kind", "event")),
            offset_ms=float(data.get("offset_ms", 0.0)),
            duration_ms=float(data.get("duration_ms", 0.0)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class ExceptionFrame:
    """Single traceback frame with source snippet and redacted locals."""

    filename: str
    lineno: int
    function: str
    code_line: str
    context_lines: list[tuple[int, str]]
    locals_preview: dict[str, str]
    is_user_code: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "lineno": self.lineno,
            "function": self.function,
            "code_line": self.code_line,
            "context_lines": [list(item) for item in self.context_lines],
            "locals_preview": self.locals_preview,
            "is_user_code": self.is_user_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExceptionFrame:
        return cls(
            filename=str(data["filename"]),
            lineno=int(data["lineno"]),
            function=str(data["function"]),
            code_line=str(data.get("code_line", "")),
            context_lines=[(int(line[0]), str(line[1])) for line in data.get("context_lines", [])],
            locals_preview=dict(data.get("locals_preview", {})),
            is_user_code=bool(data.get("is_user_code", False)),
        )


@dataclass
class ExceptionInfo:
    """Captured unhandled exception with structured frames and formatted traceback."""

    exc_type: str
    message: str
    traceback_text: str
    frames: list[ExceptionFrame] = field(default_factory=list)
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exc_type": self.exc_type,
            "message": self.message,
            "traceback_text": self.traceback_text,
            "frames": [f.to_dict() for f in self.frames],
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExceptionInfo:
        return cls(
            exc_type=str(data["exc_type"]),
            message=str(data.get("message", "")),
            traceback_text=str(data.get("traceback_text", "")),
            frames=[ExceptionFrame.from_dict(f) for f in data.get("frames", [])],
            fingerprint=str(data.get("fingerprint", "")),
        )


@dataclass
class ProfileFunction:
    """Single function row from cProfile."""

    function: str
    filename: str
    lineno: int
    calls: int
    primitive_calls: int
    self_ms: float
    cum_ms: float
    pct_total: float
    category: str  # "user", "framework", "stdlib", "third_party"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileFunction:
        return cls(
            function=str(data["function"]),
            filename=str(data["filename"]),
            lineno=int(data.get("lineno", 0)),
            calls=int(data.get("calls", 0)),
            primitive_calls=int(data.get("primitive_calls", 0)),
            self_ms=float(data.get("self_ms", 0.0)),
            cum_ms=float(data.get("cum_ms", 0.0)),
            pct_total=float(data.get("pct_total", 0.0)),
            category=str(data.get("category", "third_party")),
        )


@dataclass
class ProfileData:
    """Structured cProfile payload containing flamegraph tree + flat stats."""

    total_ms: float
    total_calls: int
    primitive_calls: int
    functions: list[ProfileFunction] = field(default_factory=list)
    flamegraph: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_ms": self.total_ms,
            "total_calls": self.total_calls,
            "primitive_calls": self.primitive_calls,
            "functions": [fn.to_dict() for fn in self.functions],
            "flamegraph": self.flamegraph,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileData:
        return cls(
            total_ms=float(data.get("total_ms", 0.0)),
            total_calls=int(data.get("total_calls", 0)),
            primitive_calls=int(data.get("primitive_calls", 0)),
            functions=[ProfileFunction.from_dict(f) for f in data.get("functions", [])],
            flamegraph=data.get("flamegraph"),
        )


@dataclass
class CapturedRequest:
    """Complete record of a captured Flask HTTP request."""

    id: str
    timestamp: float  # epoch seconds
    method: str
    path: str
    full_path: str
    query_string: str
    endpoint: str | None
    rule: str | None
    status_code: int
    duration_ms: float
    remote_addr: str
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: str | None = None
    request_content_type: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: str | None = None
    response_content_type: str | None = None
    response_size: int = 0

    sql_queries: list[SQLQuery] = field(default_factory=list)
    n_plus_one_groups: list[NPlusOneGroup] = field(default_factory=list)
    sql_count: int = 0
    sql_duration_ms: float = 0.0
    has_n_plus_one: bool = False
    has_slow_sql: bool = False
    duplicate_sql_count: int = 0

    outgoing_http: list[OutgoingHTTP] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    spans: list[CustomSpan] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    exception: ExceptionInfo | None = None
    profile: ProfileData | None = None

    is_replay: bool = False
    replayed_from: str | None = None

    def summary_dict(self) -> dict[str, Any]:
        """Lightweight summary used by list endpoints and SSE live streams."""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "method": self.method,
            "path": self.path,
            "full_path": self.full_path,
            "endpoint": self.endpoint,
            "rule": self.rule,
            "status_code": self.status_code,
            "duration_ms": round(self.duration_ms, 3),
            "sql_count": self.sql_count,
            "sql_duration_ms": round(self.sql_duration_ms, 3),
            "has_n_plus_one": self.has_n_plus_one,
            "n_plus_one_count": len(self.n_plus_one_groups),
            "has_slow_sql": self.has_slow_sql,
            "duplicate_sql_count": self.duplicate_sql_count,
            "outgoing_http_count": len(self.outgoing_http),
            "log_count": len(self.logs),
            "has_exception": self.exception is not None,
            "exception_type": self.exception.exc_type if self.exception else None,
            "has_profile": self.profile is not None,
            "response_size": self.response_size,
            "response_content_type": self.response_content_type,
            "is_replay": self.is_replay,
            "replayed_from": self.replayed_from,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "method": self.method,
            "path": self.path,
            "full_path": self.full_path,
            "query_string": self.query_string,
            "endpoint": self.endpoint,
            "rule": self.rule,
            "status_code": self.status_code,
            "duration_ms": round(self.duration_ms, 3),
            "remote_addr": self.remote_addr,
            "request_headers": self.request_headers,
            "request_body": self.request_body,
            "request_content_type": self.request_content_type,
            "response_headers": self.response_headers,
            "response_body": self.response_body,
            "response_content_type": self.response_content_type,
            "response_size": self.response_size,
            "sql_queries": [q.to_dict() for q in self.sql_queries],
            "n_plus_one_groups": [g.to_dict() for g in self.n_plus_one_groups],
            "sql_count": self.sql_count,
            "sql_duration_ms": round(self.sql_duration_ms, 3),
            "has_n_plus_one": self.has_n_plus_one,
            "has_slow_sql": self.has_slow_sql,
            "duplicate_sql_count": self.duplicate_sql_count,
            "outgoing_http": [h.to_dict() for h in self.outgoing_http],
            "logs": [entry.to_dict() for entry in self.logs],
            "spans": [s.to_dict() for s in self.spans],
            "templates": self.templates,
            "exception": self.exception.to_dict() if self.exception else None,
            "profile": self.profile.to_dict() if self.profile else None,
            "is_replay": self.is_replay,
            "replayed_from": self.replayed_from,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapturedRequest:
        return cls(
            id=str(data["id"]),
            timestamp=float(data["timestamp"]),
            method=str(data["method"]),
            path=str(data["path"]),
            full_path=str(data.get("full_path", data["path"])),
            query_string=str(data.get("query_string", "")),
            endpoint=data.get("endpoint"),
            rule=data.get("rule"),
            status_code=int(data["status_code"]),
            duration_ms=float(data["duration_ms"]),
            remote_addr=str(data.get("remote_addr", "")),
            request_headers=dict(data.get("request_headers", {})),
            request_body=data.get("request_body"),
            request_content_type=data.get("request_content_type"),
            response_headers=dict(data.get("response_headers", {})),
            response_body=data.get("response_body"),
            response_content_type=data.get("response_content_type"),
            response_size=int(data.get("response_size", 0)),
            sql_queries=[SQLQuery.from_dict(q) for q in data.get("sql_queries", [])],
            n_plus_one_groups=[
                NPlusOneGroup.from_dict(g) for g in data.get("n_plus_one_groups", [])
            ],
            sql_count=int(data.get("sql_count", 0)),
            sql_duration_ms=float(data.get("sql_duration_ms", 0.0)),
            has_n_plus_one=bool(data.get("has_n_plus_one", False)),
            has_slow_sql=bool(data.get("has_slow_sql", False)),
            duplicate_sql_count=int(data.get("duplicate_sql_count", 0)),
            outgoing_http=[OutgoingHTTP.from_dict(h) for h in data.get("outgoing_http", [])],
            logs=[LogEntry.from_dict(entry) for entry in data.get("logs", [])],
            spans=[CustomSpan.from_dict(s) for s in data.get("spans", [])],
            templates=list(data.get("templates", [])),
            exception=(
                ExceptionInfo.from_dict(data["exception"]) if data.get("exception") else None
            ),
            profile=ProfileData.from_dict(data["profile"]) if data.get("profile") else None,
            is_replay=bool(data.get("is_replay", False)),
            replayed_from=data.get("replayed_from"),
        )
