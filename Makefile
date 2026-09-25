.PHONY: sync lint type test verify

sync:
	uv sync

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

type:
	uv run mypy src

test:
	uv run pytest -v

verify: lint type test
