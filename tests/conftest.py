from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from investigator.bootstrap import open_db
from investigator.config import Settings
from investigator.scenarios import SCENARIOS, Scenario
from investigator.tools import Toolbox


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    db: Path = tmp_path_factory.mktemp("db") / "test.db"
    # Explicit values so a developer's .env (e.g. LLM_PROVIDER=gemini) never leaks in.
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{db.as_posix()}",
        llm_provider="mock",
        google_api_key="",
        max_steps=12,
        max_revisions=1,
    )


@pytest.fixture(scope="session")
def factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    """A migrated database seeded with all four scenarios (built once per test run)."""
    f = open_db(settings)
    yield f
    f.kw["bind"].dispose()


@pytest.fixture
def scenario() -> type[dict[str, Scenario]]:
    return SCENARIOS  # type: ignore[return-value]


def toolbox_for(factory: sessionmaker[Session], key: str) -> Toolbox:
    return Toolbox(factory, SCENARIOS[key].detected_at)
