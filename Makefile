# Every target is a thin wrapper over a plain command, so Windows users
# without make can copy the command after the colon-tab.

PY ?= .venv/Scripts/python
ifeq ($(OS),)
PY = .venv/bin/python
endif

.DEFAULT_GOAL := help
.PHONY: help install seed demo eval eval-gemini serve graph test lint format

help:
	@echo "make install      create .venv and install with dev extras"
	@echo "make seed         migrate the database and seed the 4 scenarios (reset)"
	@echo "make demo         investigate the db_pool scenario with the offline mock"
	@echo "make eval         run all scenarios with the mock and score them"
	@echo "make eval-gemini  same, with Gemini (needs GOOGLE_API_KEY in .env)"
	@echo "make serve        start the API on http://127.0.0.1:8000"
	@echo "make graph        regenerate docs/graph.mmd from the compiled graph"
	@echo "make test         pytest"
	@echo "make lint         ruff check + format check"

install:
	python -m venv .venv
	$(PY) -m pip install -e ".[dev]"

seed:
	$(PY) -m investigator seed --reset

demo:
	$(PY) -m investigator run --scenario db_pool --provider mock

eval:
	$(PY) -m investigator eval --provider mock

eval-gemini:
	$(PY) -m investigator eval --provider gemini

serve:
	$(PY) -m investigator serve

graph:
	$(PY) -m investigator graph

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check . --fix
