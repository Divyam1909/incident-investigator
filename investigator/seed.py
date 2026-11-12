"""Synthetic telemetry generator.

Simulates a small shop (checkout, payments, catalog, an LLM support bot) for
about two hours around each scenario's alert, then injects that scenario's
fault. Everything is driven by a seeded RNG, so the same seed always produces
byte-identical data and the eval is reproducible.

Span shape follows OpenTelemetry (and LLM Observatory's spans table):
SERVER spans named "METHOD /route", CLIENT spans carrying ``peer.service``,
``db.system`` or ``gen_ai.*`` attributes, ``status`` UNSET/OK/ERROR.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from investigator.db import (
    Deploy,
    Incident,
    IncidentLabel,
    Investigation,
    InvestigationStep,
    LogRecord,
    Service,
    Span,
    session_scope,
)
from investigator.scenarios import SCENARIOS, Scenario

SERVICES = {
    "checkout-api": ("commerce", "tier-1"),
    "payments-service": ("payments", "tier-1"),
    "catalog-service": ("catalog", "tier-1"),
    "support-assistant": ("cx-ai", "tier-2"),
}
# Version each service runs before any scenario deploy.
BASE_VERSIONS = {
    "checkout-api": "2.13.4",
    "payments-service": "1.8.0",
    "catalog-service": "3.1.8",
    "support-assistant": "0.9.3",
}
# Requests per minute at scale=1.0.
RATES = {
    ("checkout-api", "POST /checkout"): 12,
    ("checkout-api", "GET /cart"): 15,
    ("catalog-service", "GET /products"): 25,
    ("catalog-service", "GET /products/{id}"): 15,
    ("support-assistant", "POST /chat"): 5,
}
LLM_MODEL = "gemini-flash-latest"
ROLLOUT = timedelta(seconds=90)  # rolling deploy: old and new versions overlap


@dataclass
class Batch:
    spans: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)


class Trace:
    """Builds the spans of one request with consistent ids and timing."""

    def __init__(self, sim: Simulator, start: datetime):
        self.sim = sim
        self.trace_id = f"{sim.rng.getrandbits(128):032x}"
        self.cursor = start

    def new_span_id(self) -> str:
        return f"{self.sim.rng.getrandbits(64):016x}"

    def span(
        self,
        service: str,
        name: str,
        kind: str,
        duration_ms: float,
        parent: str | None,
        attrs: dict[str, Any],
        *,
        start: datetime | None = None,
        error: str | None = None,
        span_id: str | None = None,
        advance: bool = True,
    ) -> str:
        sid = span_id or self.new_span_id()
        begin = start or self.cursor
        if error:
            attrs = {**attrs, "error.type": error.split(":")[0]}
        self.sim.batch.spans.append(
            {
                "trace_id": self.trace_id,
                "span_id": sid,
                "parent_span_id": parent,
                "service_id": self.sim.service_ids[service],
                "name": name,
                "kind": kind,
                "start_ts": begin,
                "duration_ms": round(duration_ms, 2),
                "status": "ERROR" if error else ("OK" if kind == "SERVER" else "UNSET"),
                "status_message": error,
                "attributes": attrs,
            }
        )
        if advance and start is None:
            self.cursor = begin + timedelta(milliseconds=duration_ms)
        return sid

    def log(self, service: str, level: str, message: str, span_id: str | None = None) -> None:
        self.sim.log(service, self.cursor, level, message, self.trace_id, span_id)


class Simulator:
    def __init__(self, scenario: Scenario, service_ids: dict[str, int], rng: random.Random):
        self.s = scenario
        self.service_ids = service_ids
        self.rng = rng
        self.batch = Batch()

    # ---- helpers ---------------------------------------------------------

    def fault(self, t: datetime) -> str | None:
        """The active fault at time t, or None during normal operation."""
        return self.s.key if t >= self.s.onset else None

    def version(self, service: str, t: datetime) -> str:
        version = BASE_VERSIONS[service]
        for d in sorted(self.s.deploys, key=lambda d: d.at):
            if d.service != service or t < d.at:
                continue
            if t < d.at + ROLLOUT:  # mid-rollout: some pods are still on the old version
                # Deterministic per timestamp so one request always sees one version.
                frac = (t - d.at) / ROLLOUT
                version = d.version if (t.microsecond % 997) / 997 < frac else version
            else:
                version = d.version
        return version

    def ms(self, lo: float, hi: float) -> float:
        """Right-skewed latency between lo and hi (most requests near lo)."""
        return lo + (hi - lo) * self.rng.betavariate(1.6, 4.0)

    def log(
        self,
        service: str,
        ts: datetime,
        level: str,
        message: str,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> None:
        self.batch.logs.append(
            {
                "service_id": self.service_ids[service],
                "ts": ts,
                "level": level,
                "message": message,
                "trace_id": trace_id,
                "span_id": span_id,
                "attributes": {},
            }
        )

    def server_attrs(self, service: str, route: str, status: int, t: datetime) -> dict[str, Any]:
        method, path = route.split(" ", 1)
        return {
            "http.request.method": method,
            "http.route": path,
            "http.response.status_code": status,
            "service.version": self.version(service, t),
            "deployment.environment": "production",
        }

    # ---- dependencies (CLIENT spans) -------------------------------------

    def redis_get(self, tr: Trace, service: str, parent: str, key: str) -> bool:
        """Returns True on a cache hit, False on miss or error."""
        attrs = {"peer.service": "redis", "db.system": "redis", "server.address": "redis-cache"}
        if self.fault(tr.cursor) == "cache_red_herring":
            err = "ConnectionError: Error 111 connecting to redis-cache:6379. Connection refused."
            sid = tr.span(service, f"redis GET {key}", "CLIENT", self.ms(1, 4), parent, attrs,
                          error=err)
            if self.rng.random() < 0.25:
                tr.log(service, "WARN", "Cache unavailable (redis-cache:6379), serving from "
                       "database", sid)
            return False
        tr.span(service, f"redis GET {key}", "CLIENT", self.ms(0.4, 3), parent, attrs)
        return self.rng.random() < 0.9

    def postgres(self, tr: Trace, service: str, parent: str, stmt: str,
                 lo: float = 3, hi: float = 12) -> str | None:
        """Runs one query. Returns an error string if the call failed."""
        attrs: dict[str, Any] = {
            "peer.service": "postgres",
            "db.system": "postgresql",
            "db.statement": stmt,
            "server.address": "orders-db",
        }
        query_ms = self.ms(lo, hi)
        wait_ms = self.ms(0, 1.5)
        if self.fault(tr.cursor) == "db_pool" and service == "checkout-api":
            wait_ms = min(self.rng.lognormvariate(6.2, 0.6), 3000.0)  # median ~500ms
            if wait_ms >= 3000.0 or self.rng.random() < 0.025:
                wait_ms = 3000.0
                err = ("TimeoutError: QueuePool limit of size 20 overflow 10 reached, "
                       "connection timed out, timeout 3.00")
                attrs["db.client.connection.wait_ms"] = wait_ms
                sid = tr.span(service, stmt.split(" ")[0] + " " + stmt.split(" ")[-1], "CLIENT",
                              wait_ms, parent, attrs, error=err)
                tr.log(service, "ERROR", err, sid)
                return err
            if wait_ms > 1000 and self.rng.random() < 0.3:
                in_use_waiting = self.rng.randint(25, 60)
                tr.log(service, "WARN", f"Slow connection checkout from pool: waited "
                       f"{wait_ms:.0f}ms (in_use=30/30, waiting={in_use_waiting})")
        attrs["db.client.connection.wait_ms"] = round(wait_ms, 1)
        name = stmt.split(" ")[0] + " " + stmt.split(" ")[-1]
        tr.span(service, name, "CLIENT", wait_ms + query_ms, parent, attrs)
        return None

    # ---- endpoints (SERVER spans) ----------------------------------------

    def checkout(self, t: datetime) -> None:
        svc, route = "checkout-api", "POST /checkout"
        tr = Trace(self, t)
        root = tr.new_span_id()
        tr.cursor += timedelta(milliseconds=self.ms(2, 6))
        error: str | None = None
        status = 200
        self.redis_get(tr, svc, root, "cart:{id}")
        error = self.postgres(tr, svc, root, "SELECT * FROM cart_items")
        version = self.version(svc, t)
        if not error and version == "2.14.0" and self.rng.random() < 0.3:
            error = ("TypeError: unsupported operand type(s) for -: 'NoneType' and 'Decimal' "
                     "(promotions.py:88 apply_promo_discount)")
            tr.cursor += timedelta(milliseconds=self.ms(3, 9))
            tr.log(svc, "ERROR", f"Unhandled exception in POST /checkout: {error}", root)
        if not error:
            tr.cursor += timedelta(milliseconds=self.ms(20, 70))  # pricing, tax, fraud rules
            error = self.charge(tr, root)
        if not error:
            error = self.postgres(tr, svc, root, "INSERT INTO orders")
        if error:
            status = 503 if "QueuePool" in error else 500
        tr.cursor += timedelta(milliseconds=self.ms(2, 8))
        duration = (tr.cursor - t) / timedelta(milliseconds=1)
        tr.span(svc, route, "SERVER", duration, None, self.server_attrs(svc, route, status, t),
                start=t, error=error, span_id=root)
        self.access_log(tr, svc, route, status, duration)

    def charge(self, tr: Trace, parent: str) -> str | None:
        """checkout-api -> payments-service -> stripe."""
        client_start = tr.cursor
        client = tr.new_span_id()
        svc = "payments-service"
        server = tr.new_span_id()
        server_start = tr.cursor + timedelta(milliseconds=self.ms(0.5, 2))
        tr.cursor = server_start + timedelta(milliseconds=self.ms(1, 4))
        error = None
        stripe_attrs = {"peer.service": "stripe", "server.address": "api.stripe.com",
                        "http.request.method": "POST"}
        if self.rng.random() < 0.002:  # rare, normal background failure
            error = "TimeoutError: api.stripe.com read timed out (10s)"
            tr.span(svc, "POST /v1/payment_intents", "CLIENT", 10000, server, stripe_attrs,
                    error=error)
        else:
            tr.span(svc, "POST /v1/payment_intents", "CLIENT", self.ms(45, 140), server,
                    stripe_attrs)
        tr.cursor += timedelta(milliseconds=self.ms(1, 3))
        if not error:
            self.postgres(tr, svc, server, "INSERT INTO charges", 2, 8)
        server_ms = (tr.cursor - server_start) / timedelta(milliseconds=1)
        status = 502 if error else 201
        tr.span(svc, "POST /charges", "SERVER", server_ms, client,
                self.server_attrs(svc, "POST /charges", status, server_start),
                start=server_start, error=error, span_id=server)
        if error:
            tr.log(svc, "ERROR", f"Charge failed: {error}", server)
        tr.cursor += timedelta(milliseconds=self.ms(0.5, 2))
        client_ms = (tr.cursor - client_start) / timedelta(milliseconds=1)
        tr.span("checkout-api", "POST /charges", "CLIENT", client_ms, parent,
                {"peer.service": "payments-service", "http.request.method": "POST",
                 "http.response.status_code": status},
                start=client_start, error=f"HTTPError: {status} from payments-service"
                if error else None, span_id=client)
        return f"PaymentError: payments-service returned {status}" if error else None

    def cart(self, t: datetime) -> None:
        svc, route = "checkout-api", "GET /cart"
        tr = Trace(self, t)
        root = tr.new_span_id()
        tr.cursor += timedelta(milliseconds=self.ms(1, 3))
        error = None
        if not self.redis_get(tr, svc, root, "cart:{id}"):
            error = self.postgres(tr, svc, root, "SELECT * FROM cart_items")
        tr.cursor += timedelta(milliseconds=self.ms(1, 5))
        status = 503 if error else 200
        duration = (tr.cursor - t) / timedelta(milliseconds=1)
        tr.span(svc, route, "SERVER", duration, None, self.server_attrs(svc, route, status, t),
                start=t, error=error, span_id=root)
        self.access_log(tr, svc, route, status, duration)

    def products(self, t: datetime, route: str) -> None:
        svc = "catalog-service"
        tr = Trace(self, t)
        root = tr.new_span_id()
        tr.cursor += timedelta(milliseconds=self.ms(2, 6))
        error = None
        outage = self.fault(t) == "cache_red_herring"
        if not self.redis_get(tr, svc, root, "products:{page}" if route == "GET /products"
                              else "product:{id}"):
            # Cache miss. During the outage every request misses at once, so the
            # database is slower too: a thundering herd, not a database fault.
            lo, hi = (350, 1800) if outage else (20, 70)
            stmt = "SELECT * FROM products" if route == "GET /products" else \
                "SELECT * FROM product_details"
            attrs = {"peer.service": "postgres", "db.system": "postgresql",
                     "db.statement": stmt, "server.address": "catalog-db",
                     "db.client.connection.wait_ms": round(self.ms(0, 2), 1)}
            q_ms = self.ms(lo, hi)
            if outage and q_ms > 1500:
                error = "QueryCanceled: canceling statement due to statement timeout"
                tr.span(svc, stmt.split(" ")[0] + " " + stmt.split(" ")[-1], "CLIENT", 1500,
                        root, attrs, error=error)
            else:
                tr.span(svc, stmt.split(" ")[0] + " " + stmt.split(" ")[-1], "CLIENT", q_ms,
                        root, attrs)
        tr.cursor += timedelta(milliseconds=self.ms(15, 70))  # ranking + serialisation
        status = 503 if error else 200
        duration = (tr.cursor - t) / timedelta(milliseconds=1)
        tr.span(svc, route, "SERVER", duration, None, self.server_attrs(svc, route, status, t),
                start=t, error=error, span_id=root)
        if self.version(svc, t) == "3.2.0" and self.rng.random() < 0.05:
            tr.log(svc, "WARN", "PydanticDeprecatedSince210: `min_items` is deprecated, use "
                   "`min_length` (models/listing.py:41)")
        self.access_log(tr, svc, route, status, duration)

    def chat(self, t: datetime) -> None:
        svc, route = "support-assistant", "POST /chat"
        tr = Trace(self, t)
        root = tr.new_span_id()
        tr.cursor += timedelta(milliseconds=self.ms(2, 5))
        self.postgres(tr, svc, root, "SELECT * FROM conversations", 2, 8)
        in_tok, out_tok = self.rng.randint(600, 2200), self.rng.randint(80, 450)
        attrs: dict[str, Any] = {
            "peer.service": "gemini",
            "server.address": "generativelanguage.googleapis.com",
            "gen_ai.system": "gemini",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": LLM_MODEL,
        }
        error = None
        degraded = self.fault(t) == "llm_timeout"
        if degraded and self.rng.random() < 0.4:
            error = "TimeoutError: Deadline exceeded after 30.0s waiting for gemini response"
            sid = tr.span(svc, f"chat {LLM_MODEL}", "CLIENT", 30000, root, attrs, error=error)
            tr.log(svc, "ERROR", f"LLM call timed out after 30000ms (provider=gemini, "
                   f"model={LLM_MODEL})", sid)
        else:
            lo, hi = (2500, 9000) if degraded else (700, 3200)
            attrs |= {"gen_ai.response.model": LLM_MODEL, "gen_ai.usage.input_tokens": in_tok,
                      "gen_ai.usage.output_tokens": out_tok}
            tr.span(svc, f"chat {LLM_MODEL}", "CLIENT", self.ms(lo, hi), root, attrs)
            self.postgres(tr, svc, root, "INSERT INTO messages", 2, 8)
        tr.cursor += timedelta(milliseconds=self.ms(2, 10))
        status = 504 if error else 200
        duration = (tr.cursor - t) / timedelta(milliseconds=1)
        tr.span(svc, route, "SERVER", duration, None, self.server_attrs(svc, route, status, t),
                start=t, error=error, span_id=root)
        self.access_log(tr, svc, route, status, duration)

    def access_log(self, tr: Trace, svc: str, route: str, status: int, ms: float) -> None:
        if status >= 500 or self.rng.random() < 0.1:  # sample successful requests
            level = "ERROR" if status >= 500 else "INFO"
            tr.log(svc, level, f"{route} {status} {ms:.0f}ms")

    # ---- driver ----------------------------------------------------------

    def run(self, scale: float) -> Batch:
        s = self.s
        t = s.traffic_start
        handlers = {
            ("checkout-api", "POST /checkout"): self.checkout,
            ("checkout-api", "GET /cart"): self.cart,
            ("catalog-service", "GET /products"): lambda ts: self.products(ts, "GET /products"),
            ("catalog-service", "GET /products/{id}"):
                lambda ts: self.products(ts, "GET /products/{id}"),
            ("support-assistant", "POST /chat"): self.chat,
        }
        while t < s.detected_at:
            for key, rate in RATES.items():
                mean = rate * scale
                if s.key == "db_pool" and key[0] == "checkout-api" and t >= s.onset:
                    mean *= 1.15  # retries from impatient clients
                n = max(0, round(self.rng.gauss(mean, mean**0.5)))
                for _ in range(n):
                    ts = t + timedelta(seconds=self.rng.uniform(0, 60))
                    if ts < s.detected_at:
                        handlers[key](ts)
            t += timedelta(minutes=1)
        self.background_logs()
        return self.batch

    def background_logs(self) -> None:
        s = self.s
        t = s.traffic_start
        while t < s.detected_at:
            for svc in SERVICES:
                self.log(svc, t + timedelta(seconds=self.rng.uniform(0, 60)), "INFO",
                         "health check ok")
            t += timedelta(minutes=5)
        for d in s.deploys:
            self.log(d.service, d.at, "INFO", f"Deployment started: {d.previous_version} -> "
                     f"{d.version} by {d.deployed_by}")
            self.log(d.service, d.at + ROLLOUT, "INFO", f"Deployment finished: {d.version} "
                     f"serving 100% of traffic")
        if s.key == "db_pool":
            self.log("checkout-api", s.onset - timedelta(minutes=2), "INFO",
                     "order-export: started full export of orders table (est. 18 min, "
                     "parallelism=12)")
        if s.key == "cache_red_herring":
            self.log("catalog-service", s.onset, "ERROR", "redis-cache:6379 health probe "
                     "failed: Connection refused")


# --------------------------------------------------------------------------


def _ensure_services(session: Session) -> dict[str, int]:
    existing = {s.name: s.id for s in session.scalars(select(Service))}
    for name, (team, tier) in SERVICES.items():
        if name not in existing:
            row = Service(name=name, team=team, tier=tier)
            session.add(row)
            session.flush()
            existing[name] = row.id
    return existing


def is_seeded(factory: sessionmaker[Session]) -> bool:
    with factory() as session:
        return bool(session.scalar(select(func.count()).select_from(IncidentLabel)))


def reset(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        for model in (InvestigationStep, Investigation, IncidentLabel, Incident, LogRecord,
                      Span, Deploy, Service):
            session.execute(delete(model))


def seed(factory: sessionmaker[Session], scale: float = 1.0, rng_seed: int = 42) -> dict[str, int]:
    """Seeds all scenarios. Returns {scenario key: incident id}. Idempotent per scenario."""
    incident_ids: dict[str, int] = {}
    with session_scope(factory) as session:
        service_ids = _ensure_services(session)
        done = {lbl.scenario: lbl.incident_id for lbl in session.scalars(select(IncidentLabel))}
        for i, scenario in enumerate(SCENARIOS.values()):
            if scenario.key in done:
                incident_ids[scenario.key] = done[scenario.key]
                continue
            batch = Simulator(scenario, service_ids, random.Random(rng_seed + i)).run(scale)
            session.execute(insert(Span), batch.spans)
            session.execute(insert(LogRecord), batch.logs)
            for d in scenario.deploys:
                session.add(Deploy(
                    service_id=service_ids[d.service], version=d.version,
                    previous_version=d.previous_version, deployed_by=d.deployed_by,
                    change_summary=d.change_summary, deployed_at=d.at,
                    commit_sha=f"{random.Random(d.version).getrandbits(40):010x}",
                ))
            incident = Incident(
                title=scenario.title, alert_text=scenario.alert_text,
                service_id=service_ids[scenario.service], endpoint=scenario.endpoint,
                severity=scenario.severity, detected_at=scenario.detected_at,
            )
            session.add(incident)
            session.flush()
            session.add(IncidentLabel(
                incident_id=incident.id, scenario=scenario.key,
                root_cause_category=scenario.root_cause_category,
                root_cause_component=scenario.root_cause_component,
                explanation=scenario.explanation,
            ))
            incident_ids[scenario.key] = incident.id
    return incident_ids
