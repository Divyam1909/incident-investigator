"""The four labelled incident scenarios and their ground truth.

Each scenario lives on its own day, so all four coexist in one database and
an investigation only ever sees the traffic around its own alert. The alert
text is what an on-call engineer would actually be paged with: a symptom,
never a cause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# Root-cause categories the agent must choose from. Kept small and SRE-shaped
# so accuracy can be scored exactly instead of by an LLM judge.
ROOT_CAUSE_CATEGORIES = (
    "bad_deploy",
    "db_pool_exhaustion",
    "dependency_timeout",
    "cache_outage",
    "traffic_surge",
    "unknown",
)


@dataclass(frozen=True)
class DeploySpec:
    service: str
    version: str
    previous_version: str
    at: datetime
    change_summary: str
    deployed_by: str = "ci-bot"


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    alert_text: str
    service: str
    endpoint: str
    severity: str
    day: datetime  # 00:00 UTC of the scenario's day
    root_cause_category: str
    root_cause_component: str
    # Strings that count as naming the right component when scoring.
    component_aliases: tuple[str, ...]
    explanation: str
    deploys: tuple[DeploySpec, ...] = field(default_factory=tuple)

    @property
    def onset(self) -> datetime:
        return self.day + timedelta(hours=14, minutes=5)

    @property
    def detected_at(self) -> datetime:
        """When the alert fired. Tools treat this as 'now'."""
        return self.day + timedelta(hours=14, minutes=12)

    @property
    def traffic_start(self) -> datetime:
        return self.day + timedelta(hours=12)


def _day(d: int) -> datetime:
    return datetime(2026, 11, d, tzinfo=UTC)


def _at(day: datetime, hh: int, mm: int) -> datetime:
    return day + timedelta(hours=hh, minutes=mm)


_D1, _D2, _D3, _D4 = _day(2), _day(3), _day(4), _day(5)

SCENARIOS: dict[str, Scenario] = {
    s.key: s
    for s in [
        Scenario(
            key="bad_deploy",
            title="5xx surge on POST /checkout",
            alert_text=(
                "5xx error rate on POST /checkout (checkout-api) rose from 0.2% to ~30% "
                "starting 14:05 UTC."
            ),
            service="checkout-api",
            endpoint="POST /checkout",
            severity="sev1",
            day=_D1,
            root_cause_category="bad_deploy",
            root_cause_component="checkout-api",
            component_aliases=("checkout-api", "2.14.0"),
            explanation=(
                "checkout-api v2.14.0 (deployed 14:03) refactored promo-code discounts and "
                "raises TypeError when a cart has no promo code. Only v2.14.0 spans fail; "
                "all downstream dependencies are healthy."
            ),
            deploys=(
                DeploySpec(
                    "payments-service", "1.8.1", "1.8.0", _at(_D1, 12, 40),
                    "Add idempotency-key logging",
                ),
                DeploySpec(
                    "checkout-api", "2.14.0", "2.13.4", _at(_D1, 14, 3),
                    "Refactor promo-code discount calculation (PR #4821)",
                    deployed_by="a.sharma",
                ),
            ),
        ),
        Scenario(
            key="db_pool",
            title="Latency spike on POST /checkout",
            alert_text="p95 latency on POST /checkout jumped from 180ms to 2.4s at 14:05 UTC.",
            service="checkout-api",
            endpoint="POST /checkout",
            severity="sev2",
            day=_D2,
            root_cause_category="db_pool_exhaustion",
            root_cause_component="postgres",
            component_aliases=("postgres", "postgresql", "connection pool", "db pool"),
            explanation=(
                "A full order-export job started at 14:03 holds 12 of checkout-api's 30 "
                "Postgres connections. Requests queue for a pooled connection (waits of "
                "1-3s, some QueuePool timeouts) while the queries themselves stay fast."
            ),
            deploys=(
                DeploySpec(
                    "payments-service", "1.8.2", "1.8.1", _at(_D2, 13, 50),
                    "Bump stripe SDK to 11.2",
                ),
            ),
        ),
        Scenario(
            key="llm_timeout",
            title="504s and slow responses on POST /chat",
            alert_text=(
                "support-assistant POST /chat: 504 rate above 35% and p95 latency near 30s "
                "since 14:05 UTC."
            ),
            service="support-assistant",
            endpoint="POST /chat",
            severity="sev2",
            day=_D3,
            root_cause_category="dependency_timeout",
            root_cause_component="gemini",
            component_aliases=("gemini", "llm provider", "generativelanguage", "google"),
            explanation=(
                "The upstream LLM provider (Gemini) degraded at 14:05: ~40% of chat "
                "completions hit the 30s client deadline and the endpoint returns 504."
            ),
            deploys=(
                DeploySpec(
                    "catalog-service", "3.1.9", "3.1.8", _at(_D3, 13, 20),
                    "Fix pagination off-by-one",
                ),
            ),
        ),
        Scenario(
            key="cache_red_herring",
            title="Latency spike on GET /products",
            alert_text="p95 latency on GET /products (catalog-service) rose from ~90ms to over 1s at 14:05 UTC.",
            service="catalog-service",
            endpoint="GET /products",
            severity="sev2",
            day=_D4,
            root_cause_category="cache_outage",
            root_cause_component="redis",
            component_aliases=("redis", "cache"),
            explanation=(
                "catalog-service v3.2.0 shipped at 14:01, but the real cause is the shared "
                "redis-cache refusing connections from 14:05. Every catalog read falls back "
                "to Postgres. checkout-api (no deploy) sees the same redis errors, and "
                "v3.2.0 served normally for four minutes before the outage."
            ),
            deploys=(
                DeploySpec(
                    "catalog-service", "3.2.0", "3.1.9", _at(_D4, 14, 1),
                    "New sort options for product listing; upgrade pydantic 2.9 -> 2.10",
                    deployed_by="r.iyer",
                ),
            ),
        ),
    ]
}


def get_scenario(key: str) -> Scenario:
    try:
        return SCENARIOS[key]
    except KeyError:
        raise KeyError(f"unknown scenario {key!r}; choose from {sorted(SCENARIOS)}") from None
