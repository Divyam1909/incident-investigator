"""The agent's tools: plain Python functions over the telemetry tables.

Design rules:

* Each tool returns a small JSON-serialisable dict. The LLM never sees raw
  rows, only aggregates and a few samples, which keeps token usage flat no
  matter how much traffic there is.
* Time is relative to the incident's ``detected_at`` ("now"), so replaying an
  incident sees exactly the data that existed when it was paged.
* Bad input (unknown service, bad window) returns ``{"error": ...}`` with a
  hint instead of raising, so the model can correct itself.
* Percentiles are computed in Python: SQLite has no percentile function and
  the per-service time-window slice is small. On Postgres this could be
  pushed down with ``percentile_cont``.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from investigator.db import Deploy, LogRecord, Service, Span

ToolResult = dict[str, Any]

_DURATION = re.compile(r"^(\d+)\s*([mhd])$")
_UNIT = {"m": "minutes", "h": "hours", "d": "days"}


class ToolInputError(ValueError):
    pass


def parse_duration(value: str) -> timedelta:
    match = _DURATION.match(value.strip().lower())
    if not match:
        raise ToolInputError(f"bad window {value!r}; use a duration like '15m', '1h' or '2h'")
    amount, unit = int(match[1]), match[2]
    return timedelta(**{_UNIT[unit]: amount})


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(p / 100 * len(ordered))  # 1-based nearest rank
    return round(ordered[max(0, min(len(ordered), rank) - 1)], 1)


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 2) if whole else 0.0


def _hhmm(ts: datetime) -> str:
    return ts.strftime("%H:%M")


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dependency_of(span: Span) -> tuple[str, str]:
    """(dependency name, type) for a CLIENT span."""
    attrs = span.attributes or {}
    if "gen_ai.system" in attrs:
        return attrs["gen_ai.system"], "llm"
    name = attrs.get("peer.service") or attrs.get("server.address") or "unknown"
    system = attrs.get("db.system")
    if system == "redis":
        return name, "cache"
    if system:
        return name, "database"
    return name, "http"


@dataclass
class Window:
    start: datetime
    end: datetime

    def as_dict(self) -> dict[str, str]:
        return {"start": _iso(self.start), "end": _iso(self.end)}


class Toolbox:
    """Tools bound to one database and one incident's notion of 'now'."""

    def __init__(self, factory: sessionmaker[Session], now: datetime):
        self.factory = factory
        self.now = now

    # ---- shared helpers --------------------------------------------------

    def _window(self, window: str, end: datetime | None = None) -> Window:
        end = end or self.now
        return Window(end - parse_duration(window), end)

    def _service_id(self, session: Session, service: str) -> int:
        sid = session.scalar(select(Service.id).where(Service.name == service))
        if sid is None:
            known = sorted(session.scalars(select(Service.name)))
            raise ToolInputError(f"unknown service {service!r}; known services: {known}")
        return sid

    def _spans(self, session: Session, service_id: int, w: Window, **filters: Any) -> list[Span]:
        stmt = select(Span).where(
            Span.service_id == service_id, Span.start_ts >= w.start, Span.start_ts < w.end
        )
        for column, value in filters.items():
            stmt = stmt.where(getattr(Span, column) == value)
        return list(session.scalars(stmt))

    # ---- tools -----------------------------------------------------------

    def query_error_spans(self, service: str, window: str = "15m") -> ToolResult:
        """Error spans for a service, grouped by span name and error message, with the
        service.version breakdown and the minute errors first appeared."""
        with self.factory() as session:
            sid = self._service_id(session, service)
            w = self._window(window)
            spans = self._spans(session, sid, w)
            errors = [s for s in spans if s.status == "ERROR"]
            server = [s for s in spans if s.kind == "SERVER"]
            server_errors = [s for s in server if s.status == "ERROR"]
            version_totals = Counter(s.attributes.get("service.version") for s in server)
            version_errors = Counter(s.attributes.get("service.version") for s in server_errors)

            groups: dict[tuple[str, str], list[Span]] = defaultdict(list)
            for s in errors:
                groups[(s.name, s.status_message or "")].append(s)
            top = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:5]
            return {
                "service": service,
                "window": w.as_dict(),
                "requests": len(server),
                "failed_requests": len(server_errors),
                "request_error_rate_pct": _pct(len(server_errors), len(server)),
                "failed_status_codes": [
                    {"status": code, "count": n}
                    for code, n in Counter(
                        s.attributes.get("http.response.status_code") for s in server_errors
                    ).most_common()
                ],
                "error_spans": len(errors),
                "first_error_at": _iso(min(s.start_ts for s in errors)) if errors else None,
                "error_rate_by_version": {
                    str(v): {"requests": n, "error_rate_pct": _pct(version_errors[v], n)}
                    for v, n in version_totals.most_common()
                },
                "top_errors": [
                    {
                        "span": name,
                        "kind": group[0].kind,
                        "message": message[:200],
                        "count": len(group),
                        "first_seen": _iso(min(s.start_ts for s in group)),
                        "dependency": _dependency_of(group[0])[0]
                        if group[0].kind == "CLIENT"
                        else None,
                        "sample_trace_id": group[0].trace_id,
                    }
                    for (name, message), group in top
                ],
            }

    def latency_percentiles(
        self, service: str, endpoint: str, window: str = "15m", baseline_window: str = "1h"
    ) -> ToolResult:
        """p50/p95/p99 of an endpoint's server spans now vs the baseline window that ends
        where the current window starts, plus the first minute p95 doubled (onset)."""
        with self.factory() as session:
            sid = self._service_id(session, service)
            w = self._window(window)
            b = self._window(baseline_window, end=w.start)
            now_spans = self._spans(session, sid, w, name=endpoint, kind="SERVER")
            base_spans = self._spans(session, sid, b, name=endpoint, kind="SERVER")
            if not now_spans and not base_spans:
                known = sorted(
                    set(
                        session.scalars(
                            select(Span.name)
                            .where(Span.service_id == sid, Span.kind == "SERVER")
                            .distinct()
                        )
                    )
                )
                return {
                    "error": f"no spans for endpoint {endpoint!r}; endpoints of {service}: {known}"
                }

            def stats(spans: list[Span]) -> dict[str, Any]:
                d = [s.duration_ms for s in spans]
                return {
                    "requests": len(d),
                    "p50_ms": percentile(d, 50),
                    "p95_ms": percentile(d, 95),
                    "p99_ms": percentile(d, 99),
                    "error_rate_pct": _pct(sum(s.status == "ERROR" for s in spans), len(spans)),
                }

            current, baseline = stats(now_spans), stats(base_spans)
            ratio = (
                round(current["p95_ms"] / baseline["p95_ms"], 2)
                if current["p95_ms"] and baseline["p95_ms"]
                else None
            )

            # Onset: the first of two consecutive minutes whose p95 is over 2x the
            # baseline p95, or whose error rate is 5+ points above baseline. Requiring
            # two minutes stops one slow outlier request from looking like the start.
            by_minute: dict[datetime, list[Span]] = defaultdict(list)
            for s in now_spans:
                by_minute[s.start_ts.replace(second=0, microsecond=0)].append(s)

            def anomalous(minute: datetime) -> bool:
                m = stats(by_minute.get(minute, []))
                if m["requests"] < 3:
                    return False
                slow = bool(
                    baseline["p95_ms"] and m["p95_ms"] and m["p95_ms"] > 2 * baseline["p95_ms"]
                )
                return slow or m["error_rate_pct"] > baseline["error_rate_pct"] + 5

            onset = next(
                (
                    m
                    for m in sorted(by_minute)
                    if anomalous(m) and anomalous(m + timedelta(minutes=1))
                ),
                None,
            )
            since_onset = (
                stats([s for s in now_spans if onset and s.start_ts >= onset]) if onset else None
            )
            return {
                "service": service,
                "endpoint": endpoint,
                "window": w.as_dict(),
                "baseline_window": b.as_dict(),
                "current": current,
                "baseline": baseline,
                "p95_ratio": ratio,
                "onset_at": _iso(onset) if onset else None,
                "since_onset": since_onset,
            }

    def recent_deploys(self, service: str, window: str = "2h") -> ToolResult:
        """Deploys in the window. Pass service='all' to see every service's deploys."""
        with self.factory() as session:
            w = self._window(window)
            stmt = (
                select(Deploy, Service.name)
                .join(Service)
                .where(Deploy.deployed_at >= w.start, Deploy.deployed_at <= w.end)
                .order_by(Deploy.deployed_at.desc())
            )
            if service != "all":
                stmt = stmt.where(Deploy.service_id == self._service_id(session, service))
            rows = session.execute(stmt).all()
            return {
                "service": service,
                "window": w.as_dict(),
                "deploys": [
                    {
                        "service": name,
                        "version": d.version,
                        "previous_version": d.previous_version,
                        "deployed_at": _iso(d.deployed_at),
                        "minutes_before_now": round(
                            (self.now - d.deployed_at).total_seconds() / 60, 1
                        ),
                        "deployed_by": d.deployed_by,
                        "commit_sha": d.commit_sha,
                        "change_summary": d.change_summary,
                    }
                    for d, name in rows
                ],
            }

    def search_logs(self, service: str, pattern: str, window: str = "15m") -> ToolResult:
        """Case-insensitive substring search over a service's logs."""
        if not pattern.strip():
            raise ToolInputError("pattern must not be empty")
        with self.factory() as session:
            sid = self._service_id(session, service)
            w = self._window(window)
            rows = list(
                session.scalars(
                    select(LogRecord)
                    .where(
                        LogRecord.service_id == sid,
                        LogRecord.ts >= w.start,
                        LogRecord.ts < w.end,
                        LogRecord.message.ilike(f"%{pattern}%"),
                    )
                    .order_by(LogRecord.ts)
                )
            )
            messages = Counter(r.message for r in rows)
            return {
                "service": service,
                "pattern": pattern,
                "window": w.as_dict(),
                "matches": len(rows),
                "by_level": dict(Counter(r.level for r in rows)),
                "first_seen": _iso(rows[0].ts) if rows else None,
                "last_seen": _iso(rows[-1].ts) if rows else None,
                "top_messages": [
                    {"message": m[:240], "count": n} for m, n in messages.most_common(5)
                ],
            }

    def dependency_health(
        self, service: str, window: str = "15m", baseline_window: str = "1h"
    ) -> ToolResult:
        """Error rate and latency of each downstream call (database, cache, HTTP, LLM)
        now vs baseline, plus which other services call the same dependency and how
        they are doing (blast radius)."""
        with self.factory() as session:
            sid = self._service_id(session, service)
            w = self._window(window)
            b = self._window(baseline_window, end=w.start)
            now_spans = self._spans(session, sid, w, kind="CLIENT")
            base_spans = self._spans(session, sid, b, kind="CLIENT")

            def grouped(spans: list[Span]) -> dict[tuple[str, str], list[Span]]:
                out: dict[tuple[str, str], list[Span]] = defaultdict(list)
                for s in spans:
                    out[_dependency_of(s)].append(s)
                return out

            now_g, base_g = grouped(now_spans), grouped(base_spans)

            # Other services calling the same dependencies (one query, all services).
            others: dict[str, dict[str, list[Span]]] = defaultdict(lambda: defaultdict(list))
            other_rows = session.execute(
                select(Span, Service.name)
                .join(Service)
                .where(
                    Span.kind == "CLIENT",
                    Span.service_id != sid,
                    Span.start_ts >= w.start,
                    Span.start_ts < w.end,
                )
            ).all()
            for s, name in other_rows:
                others[_dependency_of(s)[0]][name].append(s)

            deps = []
            for (dep, dep_type), spans in sorted(now_g.items()):
                base = base_g.get((dep, dep_type), [])
                errs = [s for s in spans if s.status == "ERROR"]
                waits = [
                    s.attributes["db.client.connection.wait_ms"]
                    for s in spans
                    if "db.client.connection.wait_ms" in s.attributes
                ]
                base_waits = [
                    s.attributes["db.client.connection.wait_ms"]
                    for s in base
                    if "db.client.connection.wait_ms" in s.attributes
                ]
                entry: dict[str, Any] = {
                    "dependency": dep,
                    "type": dep_type,
                    "calls": len(spans),
                    "error_rate_pct": _pct(len(errs), len(spans)),
                    "baseline_error_rate_pct": _pct(
                        sum(s.status == "ERROR" for s in base), len(base)
                    ),
                    "p95_ms": percentile([s.duration_ms for s in spans], 95),
                    "baseline_p95_ms": percentile([s.duration_ms for s in base], 95),
                    "first_error_at": _iso(min(s.start_ts for s in errs)) if errs else None,
                    "top_error": Counter(s.status_message for s in errs).most_common(1)[0][0][:200]
                    if errs
                    else None,
                }
                if waits:
                    entry["pool_wait_p95_ms"] = percentile(waits, 95)
                    entry["baseline_pool_wait_p95_ms"] = percentile(base_waits, 95)
                entry["other_callers"] = [
                    {
                        "service": name,
                        "calls": len(ss),
                        "error_rate_pct": _pct(sum(x.status == "ERROR" for x in ss), len(ss)),
                    }
                    for name, ss in sorted(others.get(dep, {}).items())
                ]
                deps.append(entry)
            return {
                "service": service,
                "window": w.as_dict(),
                "baseline_window": b.as_dict(),
                "dependencies": deps,
            }

    # ---- registry --------------------------------------------------------

    def registry(self) -> dict[str, Callable[..., ToolResult]]:
        return {
            "query_error_spans": self.query_error_spans,
            "latency_percentiles": self.latency_percentiles,
            "recent_deploys": self.recent_deploys,
            "search_logs": self.search_logs,
            "dependency_health": self.dependency_health,
        }

    def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Run a tool by name, turning bad input into an error result the model can read."""
        fn = self.registry().get(name)
        if fn is None:
            return {"error": f"unknown tool {name!r}; available: {sorted(self.registry())}"}
        try:
            return fn(**args)
        except (ToolInputError, TypeError) as exc:
            return {"error": str(exc)}


def summarize(name: str, result: ToolResult) -> str:
    """One-line human summary of a tool result, stored in investigation_steps."""
    if "error" in result:
        return f"error: {result['error']}"
    if name == "query_error_spans":
        top = result["top_errors"][0]["message"][:80] if result["top_errors"] else "none"
        return (
            f"{result['failed_requests']}/{result['requests']} requests failed "
            f"({result['request_error_rate_pct']}%); top error: {top}"
        )
    if name == "latency_percentiles":
        c, b = result["current"], result["baseline"]
        return (
            f"p95 {c['p95_ms']}ms vs baseline {b['p95_ms']}ms (x{result['p95_ratio']}); "
            f"onset {result['onset_at']}"
        )
    if name == "recent_deploys":
        ds = result["deploys"]
        return (
            "no deploys"
            if not ds
            else "; ".join(
                f"{d['service']} {d['version']} at {d['deployed_at'][11:16]}" for d in ds
            )
        )
    if name == "search_logs":
        return f"{result['matches']} matches for {result['pattern']!r} {result['by_level']}"
    if name == "dependency_health":
        return "; ".join(
            f"{d['dependency']} err {d['error_rate_pct']}% (base {d['baseline_error_rate_pct']}%) "
            f"p95 {d['p95_ms']}ms (base {d['baseline_p95_ms']}ms)"
            for d in result["dependencies"]
        )
    return str(result)[:200]
