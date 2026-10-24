.PHONY: help install test lint format typecheck check clean packages-check

VENV := .venv/bin

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ``uv pip``, not ``$(VENV)/pip``: a uv-made venv (``uv venv`` / ``uv
# sync``, which is how this repo's own .venv is provisioned) seeds no
# pip at all, so the old recipe died with "make: ... Error 127" —
# the documented one-command setup failing on the documented toolchain.
# ``$(VENV)/python -m pip`` is no fix either; the module isn't there.
# Extras are the four pyproject actually declares. ``knowledge`` is not
# one of them and never has been; an unknown extra only warns, so it
# installed nothing and said so in a line nobody read.
install: ## Install package in dev mode with all extras
	uv pip install --python $(VENV)/python -e ".[dev,mcp,gemini,web]"

test: ## Run tests
	$(VENV)/python -m pytest tests/ -q

test-v: ## Run tests with verbose output
	$(VENV)/python -m pytest tests/ -v

test-cov: ## Run tests with coverage
	$(VENV)/python -m pytest tests/ --cov=ember_code --cov-report=term-missing

lint: ## Run ruff linter
	$(VENV)/ruff check src/ tests/

format: ## Format code with ruff
	$(VENV)/ruff format src/ tests/

format-check: ## Check formatting without making changes
	$(VENV)/ruff format --check src/ tests/

typecheck: ## Run mypy type checking
	$(VENV)/mypy src/ember_code/

check: lint format-check typecheck test ## Run all checks (lint + format + typecheck + test)

fix: ## Auto-fix linting and formatting issues
	$(VENV)/ruff check --fix src/ tests/
	$(VENV)/ruff format src/ tests/

# The Python SDKs under packages/ — igni-service for a service,
# igni-extension for an extension — are packages of their own, with
# their own dependencies (FastAPI, for one, which igni itself does not
# use). Each is checked in its own environment, which uv makes from its
# pyproject on first run, rather than by adding their dependencies to
# igni's.
PY_PACKAGES := packages/igni-service packages/igni-extension

packages-check: ## Lint, type-check and test the Python SDKs under packages/
	@for pkg in $(PY_PACKAGES); do \
		echo "== $$pkg"; \
		uv run --directory $$pkg ruff check src tests || exit 1; \
		uv run --directory $$pkg ruff format --check src tests || exit 1; \
		uv run --directory $$pkg mypy || exit 1; \
		uv run --directory $$pkg pytest -q || exit 1; \
	done

clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
