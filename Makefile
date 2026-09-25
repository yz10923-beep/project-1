.PHONY: sync lint type test verify live evals-selftest

sync:
	uv sync

lint:
	uv run ruff check src tests evals
	uv run ruff format --check src tests evals

type:
	uv run mypy src evals/harness.py evals/run_evals.py

test:
	uv run pytest -v

verify: lint type test

live:
	uv run pytest -m live -v tests/live

evals-selftest:
	uv run python -m evals.run_evals selftest
