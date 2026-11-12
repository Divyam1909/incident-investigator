from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from investigator.scenarios import SCENARIOS
from investigator.tools import Toolbox, parse_duration, percentile, summarize
from tests.conftest import toolbox_for


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 99) == 99
    assert percentile([], 95) is None


def test_parse_duration() -> None:
    assert parse_duration("15m") == timedelta(minutes=15)
    assert parse_duration("2h") == timedelta(hours=2)


def test_latency_percentiles_finds_spike_and_onset(factory: sessionmaker[Session]) -> None:
    r = toolbox_for(factory, "db_pool").latency_percentiles("checkout-api", "POST /checkout")
    assert r["p95_ratio"] > 5
    assert 140 < r["baseline"]["p95_ms"] < 220  # the alert's "180ms" baseline
    assert r["onset_at"].endswith("T14:05:00Z")


def test_errors_concentrated_on_new_version_after_bad_deploy(
    factory: sessionmaker[Session],
) -> None:
    r = toolbox_for(factory, "bad_deploy").query_error_spans("checkout-api")
    by_version = r["error_rate_by_version"]
    assert by_version["2.14.0"]["error_rate_pct"] > 5
    assert by_version["2.13.4"]["error_rate_pct"] == 0
    assert "TypeError" in r["top_errors"][0]["message"]
    assert {"status": 500, "count": r["failed_requests"]} in r["failed_status_codes"]


def test_dependency_health_shows_pool_wait(factory: sessionmaker[Session]) -> None:
    deps = {
        d["dependency"]: d
        for d in toolbox_for(factory, "db_pool").dependency_health("checkout-api")["dependencies"]
    }
    pg = deps["postgres"]
    assert pg["type"] == "database"
    assert pg["pool_wait_p95_ms"] > 10 * pg["baseline_pool_wait_p95_ms"]
    assert deps["redis"]["error_rate_pct"] == 0


def test_dependency_health_reports_blast_radius(factory: sessionmaker[Session]) -> None:
    r = toolbox_for(factory, "cache_red_herring").dependency_health("catalog-service")
    redis = next(d for d in r["dependencies"] if d["dependency"] == "redis")
    assert redis["type"] == "cache"
    assert "Connection refused" in redis["top_error"]
    checkout = next(c for c in redis["other_callers"] if c["service"] == "checkout-api")
    assert checkout["error_rate_pct"] > 20  # same cache fails for a service with no deploy


def test_llm_dependency_is_identified_by_gen_ai_system(factory: sessionmaker[Session]) -> None:
    r = toolbox_for(factory, "llm_timeout").dependency_health("support-assistant")
    gemini = next(d for d in r["dependencies"] if d["dependency"] == "gemini")
    assert gemini["type"] == "llm"
    assert gemini["p95_ms"] >= 29000


def test_recent_deploys_for_all_services(factory: sessionmaker[Session]) -> None:
    r = toolbox_for(factory, "cache_red_herring").recent_deploys("all", "2h")
    assert [(d["service"], d["version"]) for d in r["deploys"]] == [("catalog-service", "3.2.0")]
    assert r["deploys"][0]["minutes_before_now"] == 11.0


def test_search_logs_is_case_insensitive(factory: sessionmaker[Session]) -> None:
    r = toolbox_for(factory, "db_pool").search_logs("checkout-api", "QUEUEPOOL")
    assert r["matches"] > 0
    assert "QueuePool" in r["top_messages"][0]["message"]


def test_tools_cannot_see_past_now(factory: sessionmaker[Session]) -> None:
    """Replaying from before the fault must show a healthy system."""
    s = SCENARIOS["bad_deploy"]
    r = Toolbox(factory, s.onset - timedelta(minutes=10)).query_error_spans("checkout-api")
    assert r["failed_requests"] <= 1


def test_bad_input_returns_error_hint_not_exception(factory: sessionmaker[Session]) -> None:
    tb = toolbox_for(factory, "db_pool")
    r = tb.call("query_error_spans", {"service": "checkout"})
    assert "unknown service" in r["error"] and "checkout-api" in r["error"]
    assert (
        "bad window"
        in tb.call("search_logs", {"service": "checkout-api", "pattern": "x", "window": "soon"})[
            "error"
        ]
    )
    assert "unknown tool" in tb.call("drop_tables", {})["error"]
    r = tb.call("latency_percentiles", {"service": "checkout-api", "endpoint": "/checkout"})
    assert "POST /checkout" in r["error"]
    assert tb.call("query_error_spans", {"service": "checkout-api", "bogus": 1})["error"]


def test_summaries_are_one_line(factory: sessionmaker[Session]) -> None:
    tb = toolbox_for(factory, "db_pool")
    for name, fn in tb.registry().items():
        args = {"service": "checkout-api"}
        if name == "latency_percentiles":
            args["endpoint"] = "POST /checkout"
        if name == "search_logs":
            args["pattern"] = "pool"
        assert "\n" not in summarize(name, fn(**args))
