SKILLS_DIR := $(CURDIR)/skills
COMMANDS_DIR := $(HOME)/.claude/commands

.PHONY: install install-hooks lint fmt format type-check test check build clean link-skills unlink-skills

install:
	uv sync --all-groups

install-hooks:
	uv run pre-commit install

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

link-skills:
	@mkdir -p $(COMMANDS_DIR)
	@for dir in $(SKILLS_DIR)/*/; do \
		name=$$(basename $$dir); \
		src=$$dir/SKILL.md; \
		dest=$(COMMANDS_DIR)/$$name.md; \
		if [ -f "$$src" ]; then \
			ln -sf "$$src" "$$dest"; \
			echo "  linked $$name"; \
		fi; \
	done

unlink-skills:
	@for dir in $(SKILLS_DIR)/*/; do \
		name=$$(basename $$dir); \
		src=$$dir/SKILL.md; \
		dest=$(COMMANDS_DIR)/$$name.md; \
		if [ -L "$$dest" ]; then \
			target=$$(readlink "$$dest"); \
			if [ "$$target" = "$$src" ]; then \
				rm "$$dest"; \
				echo "  unlinked $$name"; \
			else \
				echo "  skipped $$name (symlink points elsewhere: $$target)"; \
			fi; \
		fi; \
	done
