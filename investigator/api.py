"""HTTP API.

    POST /incidents                     create an incident and start investigating it
    GET  /investigations/{id}           status, steps, tokens, cost and the report
    GET  /investigations/{id}/steps     polling: steps after a given seq
    GET  /investigations/{id}/events    Server-Sent Events stream of steps
    GET  /scenarios                     the seeded demo incidents

Investigations run in a background task; progress is read back from the
database, so clients can poll or stream from any API process.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from investigator.agent.llm import build_llm
from investigator.bootstrap import open_db
from investigator.config import Settings, get_settings
from investigator.db import Incident, IncidentLabel, Investigation, InvestigationStep, Service
from investigator.db import session_scope as scope
from investigator.db.models import utcnow
from investigator.runner import create_investigation, run_investigation
from investigator.scenarios import SCENARIOS

TERMINAL = {"completed", "failed"}


class IncidentIn(BaseModel):
    """Either replay a seeded scenario, or describe a new alert."""

    scenario: str | None = Field(default=None, description="Replay a seeded scenario's alert")
    title: str | None = None
    alert_text: str | None = None
    service: str | None = None
    endpoint: str | None = None
    severity: Literal["sev1", "sev2", "sev3"] = "sev2"
    detected_at: datetime | None = Field(default=None, description="Defaults to now (UTC)")
    provider: Literal["mock", "gemini"] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=40)

    @model_validator(mode="after")
    def _one_source(self) -> IncidentIn:
        if not self.scenario and not self.alert_text:
            raise ValueError("give either 'scenario' or 'alert_text'")
        if self.scenario and self.scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario; choose from {sorted(SCENARIOS)}")
        if self.detected_at and self.detected_at.tzinfo is None:
            raise ValueError("detected_at must include a timezone, e.g. 2026-11-03T14:12:00Z")
        return self


class StepOut(BaseModel):
    seq: int
    node: str
    tool_name: str | None
    evidence_id: str | None
    args: dict[str, Any] | None
    summary: str
    latency_ms: float | None
    created_at: datetime


class InvestigationOut(BaseModel):
    id: int
    incident_id: int
    status: str
    llm_provider: str
    llm_model: str
    steps: int
    max_steps: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float | None
    root_cause_category: str | None
    root_cause_component: str | None
    confidence: str | None
    error: str | None
    step_log: list[StepOut]
    report: dict[str, Any] | None
    report_markdown: str | None


def _step_out(s: InvestigationStep) -> StepOut:
    return StepOut(
        seq=s.seq,
        node=s.node,
        tool_name=s.tool_name,
        evidence_id=s.evidence_id,
        args=s.args,
        summary=s.result_summary,
        latency_ms=s.latency_ms,
        created_at=s.created_at,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.factory = open_db(settings)
        yield

    app = FastAPI(title="Incident Investigator", version="0.1.0", lifespan=lifespan)

    def factory(request: Request) -> sessionmaker[Session]:
        return request.app.state.factory

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/scenarios")
    def scenarios(request: Request) -> list[dict[str, Any]]:
        with factory(request)() as session:
            rows = session.execute(
                select(IncidentLabel.scenario, Incident.id, Incident.title).join(Incident)
            ).all()
        return [{"scenario": s, "incident_id": i, "title": t} for s, i, t in rows]

    @app.post("/incidents", status_code=202)
    def create_incident(
        body: IncidentIn, request: Request, background: BackgroundTasks
    ) -> dict[str, Any]:
        f = factory(request)
        if body.scenario:
            s = SCENARIOS[body.scenario]
            title, alert, svc, endpoint = s.title, s.alert_text, s.service, s.endpoint
            severity, detected_at = s.severity, s.detected_at
        else:
            title = body.title or (body.alert_text or "")[:120]
            alert, svc, endpoint = body.alert_text or "", body.service, body.endpoint
            severity, detected_at = body.severity, body.detected_at or utcnow()
        with scope(f) as session:
            service_id = None
            if svc:
                service_id = session.scalar(select(Service.id).where(Service.name == svc))
                if service_id is None:
                    raise HTTPException(422, f"unknown service {svc!r}")
            incident = Incident(
                title=title,
                alert_text=alert,
                service_id=service_id,
                endpoint=endpoint,
                severity=severity,
                detected_at=detected_at,
            )
            session.add(incident)
            session.flush()
            incident_id = incident.id

        llm, info = build_llm(settings, body.provider)
        inv_id = create_investigation(f, incident_id, info, body.max_steps or settings.max_steps)
        background.add_task(run_investigation, f, inv_id, llm, info, settings)
        return {
            "incident_id": incident_id,
            "investigation_id": inv_id,
            "status": "queued",
            "links": {
                "self": f"/investigations/{inv_id}",
                "steps": f"/investigations/{inv_id}/steps",
                "events": f"/investigations/{inv_id}/events",
            },
        }

    @app.get("/investigations/{investigation_id}")
    def get_investigation(investigation_id: int, request: Request) -> InvestigationOut:
        with factory(request)() as session:
            inv = session.get(Investigation, investigation_id)
            if inv is None:
                raise HTTPException(404, "investigation not found")
            return InvestigationOut(
                id=inv.id,
                incident_id=inv.incident_id,
                status=inv.status,
                llm_provider=inv.llm_provider,
                llm_model=inv.llm_model,
                steps=inv.steps,
                max_steps=inv.max_steps,
                input_tokens=inv.input_tokens,
                output_tokens=inv.output_tokens,
                cost_usd=inv.cost_usd,
                latency_ms=inv.latency_ms,
                root_cause_category=inv.root_cause_category,
                root_cause_component=inv.root_cause_component,
                confidence=inv.confidence,
                error=inv.error,
                step_log=[_step_out(s) for s in inv.step_rows],
                report=inv.report,
                report_markdown=inv.report_markdown,
            )

    def _steps_after(
        f: sessionmaker[Session], inv_id: int, after: int
    ) -> tuple[str, list[StepOut]]:
        with f() as session:
            status = session.scalar(select(Investigation.status).where(Investigation.id == inv_id))
            if status is None:
                raise HTTPException(404, "investigation not found")
            rows = session.scalars(
                select(InvestigationStep)
                .where(InvestigationStep.investigation_id == inv_id, InvestigationStep.seq > after)
                .order_by(InvestigationStep.seq)
            ).all()
            return status, [_step_out(s) for s in rows]

    @app.get("/investigations/{investigation_id}/steps")
    def poll_steps(
        investigation_id: int, request: Request, after: int = Query(0, ge=0)
    ) -> dict[str, Any]:
        status, steps = _steps_after(factory(request), investigation_id, after)
        return {"status": status, "steps": steps, "next_after": steps[-1].seq if steps else after}

    @app.get("/investigations/{investigation_id}/events")
    def stream_events(investigation_id: int, request: Request) -> StreamingResponse:
        f = factory(request)
        _steps_after(f, investigation_id, 0)  # 404 before the stream starts

        def events() -> Iterator[str]:
            after, deadline = 0, time.monotonic() + 600
            while time.monotonic() < deadline:
                status, steps = _steps_after(f, investigation_id, after)
                for s in steps:
                    after = s.seq
                    yield f"event: step\ndata: {s.model_dump_json()}\n\n"
                if status in TERMINAL:
                    yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                    return
                time.sleep(0.5)
            yield "event: timeout\ndata: {}\n\n"

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    return app


app = create_app()
