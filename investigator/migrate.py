"""Run Alembic migrations from code (used by the CLI, the API and tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["configure_logger"] = False
    if database_url:
        cfg.attributes["database_url"] = database_url
    return cfg


def upgrade(database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), "head")
