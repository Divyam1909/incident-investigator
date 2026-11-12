from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from investigator.db import Base, Incident, Span, make_engine, make_session_factory
from investigator.migrate import alembic_config, upgrade


def test_migrations_match_models_and_downgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    upgrade(url)
    engine = make_engine(url)
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    indexes = {i["name"] for i in inspect(engine).get_indexes("spans")}
    assert {
        "ix_spans_service_start",
        "ix_spans_service_name_start",
        "ix_spans_errors_service_start",
    } <= indexes
    engine.dispose()
    command.downgrade(alembic_config(url), "base")
    engine = make_engine(url)
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()


def test_datetimes_come_back_timezone_aware(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        span = session.scalars(select(Span).limit(1)).one()
        assert span.start_ts.tzinfo is not None


def test_naive_datetimes_are_rejected(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'n.db').as_posix()}"
    upgrade(url)
    f = make_session_factory(make_engine(url))
    with f() as session:
        session.add(Incident(title="t", alert_text="a", detected_at=datetime(2026, 1, 1)))
        with pytest.raises(Exception, match="naive datetime"):
            session.flush()
    with f() as session:
        session.add(
            Incident(title="t", alert_text="a", detected_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        session.flush()


def test_seed_is_deterministic_and_idempotent(factory: sessionmaker[Session]) -> None:
    from investigator.seed import seed

    with factory() as session:
        before = session.query(Span).count()
    ids = seed(factory)  # second call: every scenario already present
    with factory() as session:
        assert session.query(Span).count() == before
    assert sorted(ids) == ["bad_deploy", "cache_red_herring", "db_pool", "llm_timeout"]
