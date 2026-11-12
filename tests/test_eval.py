from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from investigator.config import Settings
from investigator.evaluate import is_correct, main
from investigator.scenarios import SCENARIOS


def test_scoring_needs_category_and_component() -> None:
    s = SCENARIOS["cache_red_herring"]
    assert is_correct(s, "cache_outage", "redis-cache")
    assert not is_correct(s, "cache_outage", "postgres")
    assert not is_correct(s, "bad_deploy", "catalog-service@3.2.0")  # the red herring
    assert is_correct(SCENARIOS["bad_deploy"], "bad_deploy", "checkout-api@2.14.0")


def test_eval_writes_results(
    tmp_path: Path, settings: Settings, factory: sessionmaker[Session]
) -> None:
    rows = main(provider="mock", out_dir=tmp_path, settings=settings)
    assert len(rows) == 4 and all(r.correct for r in rows)
    md = (tmp_path / "results-mock.md").read_text(encoding="utf-8")
    assert "Accuracy: 4/4" in md
    assert (tmp_path / "results-mock.json").exists()
