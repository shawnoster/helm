.PHONY: install lint fmt format type-check test check build clean

install:
	uv sync --all-groups

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

type-check:
	uv run mypy src

test:
	uv run pytest

check: lint type-check test

build:
	uv build

clean:
	rm -rf dist/ .mypy_cache/ .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
