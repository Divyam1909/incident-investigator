from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from investigator.api import create_app
from investigator.config import Settings


@pytest.fixture(scope="module")
def client(settings: Settings, factory: sessionmaker[Session]) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


def test_create_incident_runs_investigation(client: TestClient) -> None:
    # TestClient runs the background task before returning, so the run is finished.
    r = client.post("/incidents", json={"scenario": "cache_red_herring"})
    assert r.status_code == 202
    inv_id = r.json()["investigation_id"]

    inv = client.get(f"/investigations/{inv_id}").json()
    assert inv["status"] == "completed"
    assert inv["root_cause_category"] == "cache_outage"
    assert inv["root_cause_component"] == "redis"
    assert inv["steps"] == 5
    assert inv["input_tokens"] > 0
    assert [s["node"] for s in inv["step_log"]][:2] == ["triage", "hypothesize"]
    assert inv["step_log"][-1]["node"] == "report"
    assert "## Evidence" in inv["report_markdown"]
    tool_steps = [s for s in inv["step_log"] if s["tool_name"]]
    assert [s["evidence_id"] for s in tool_steps] == ["E1", "E2", "E3", "E4", "E5"]


def test_polling_returns_steps_after_cursor(client: TestClient) -> None:
    inv_id = client.post("/incidents", json={"scenario": "db_pool"}).json()["investigation_id"]
    first = client.get(f"/investigations/{inv_id}/steps", params={"after": 0}).json()
    assert first["status"] == "completed"
    cursor = first["steps"][2]["seq"]
    rest = client.get(f"/investigations/{inv_id}/steps", params={"after": cursor}).json()
    assert [s["seq"] for s in rest["steps"]] == [s["seq"] for s in first["steps"][3:]]


def test_sse_stream_replays_steps_then_done(client: TestClient) -> None:
    inv_id = client.post("/incidents", json={"scenario": "llm_timeout"}).json()["investigation_id"]
    with client.stream("GET", f"/investigations/{inv_id}/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = [block for block in body.strip().split("\n\n") if block]
    assert events[0].startswith("event: step")
    assert events[-1] == 'event: done\ndata: {"status": "completed"}'
    steps = [json.loads(e.split("data: ", 1)[1]) for e in events if e.startswith("event: step")]
    assert steps[-1]["node"] == "report"


def test_free_form_alert_against_seeded_data(client: TestClient) -> None:
    r = client.post(
        "/incidents",
        json={
            "alert_text": "POST /checkout throwing 500s",
            "service": "checkout-api",
            "endpoint": "POST /checkout",
            "detected_at": "2026-11-02T14:12:00Z",
        },
    )
    inv = client.get(f"/investigations/{r.json()['investigation_id']}").json()
    assert inv["root_cause_category"] == "bad_deploy"


def test_validation_errors(client: TestClient) -> None:
    assert client.post("/incidents", json={}).status_code == 422
    assert client.post("/incidents", json={"scenario": "nope"}).status_code == 422
    r = client.post("/incidents", json={"alert_text": "slow", "service": "nope"})
    assert r.status_code == 422
    r = client.post("/incidents", json={"alert_text": "x", "detected_at": "2026-11-02T14:12:00"})
    assert r.status_code == 422  # naive datetime
    assert client.get("/investigations/999999").status_code == 404
    assert client.get("/investigations/999999/events").status_code == 404


def test_scenarios_listing_hides_ground_truth(client: TestClient) -> None:
    rows = client.get("/scenarios").json()
    assert {r["scenario"] for r in rows} == {
        "bad_deploy",
        "db_pool",
        "llm_timeout",
        "cache_red_herring",
    }
    assert all(set(r) == {"scenario", "incident_id", "title"} for r in rows)
