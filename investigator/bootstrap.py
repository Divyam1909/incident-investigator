"""Shared setup for the CLI, API and eval: open the database, migrate, seed."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from investigator import migrate
from investigator.config import Settings
from investigator.db import IncidentLabel, make_engine, make_session_factory
from investigator.seed import is_seeded, seed


def open_db(settings: Settings, *, auto_seed: bool = True) -> sessionmaker[Session]:
    """Apply migrations and, on an empty database, seed the demo scenarios."""
    migrate.upgrade(settings.database_url)
    factory = make_session_factory(make_engine(settings.database_url))
    if auto_seed and not is_seeded(factory):
        seed(factory)
    return factory


def scenario_incident_id(factory: sessionmaker[Session], scenario: str) -> int:
    with factory() as session:
        incident_id = session.scalar(
            select(IncidentLabel.incident_id).where(IncidentLabel.scenario == scenario)
        )
    if incident_id is None:
        raise LookupError(f"scenario {scenario!r} is not seeded; run `python -m investigator seed`")
    return incident_id
