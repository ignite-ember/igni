.PHONY: help install test lint format typecheck check clean packages-check host-sdks

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

# The host object model (docs/HOST_OBJECT_MODEL.md) is described once,
# in ember_code.extensions.host.definition, and both SDKs' `host` objects
# are generated from it. tests/test_the_host_sdks_are_generated.py fails
# when what is checked in is stale; this regenerates it.
host-sdks: ## Regenerate the SDKs' host objects from the host schema
	$(VENV)/python scripts/generate_host_sdks.py

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

# ── Publishing the SDKs ─────────────────────────────────────────────────
#
# Five packages, one version — docs/PUBLISHING_THE_SDKS.md. Python:
# igni-service, igni-extension. npm: @igni/extension-bridge,
# @igni/ui-react, and the stylesheet @igni/ui-css (clients/ui), which
# @igni/ui-react's README tells an extension to install beside it. They
# are released together by pushing the tag `sdk-v<version>`
# (.github/workflows/sdk-release.yml), which runs the targets below.
SDK_PY_PACKAGES := $(PY_PACKAGES)
SDK_NPM_PACKAGES := packages/extension-bridge packages/ui-react clients/ui
SDK_DIST := dist/sdk

# Every manifest's version, checked to be one version. The npm form is
# the canonical spelling (`0.2.0`, `0.2.0-rc.1`) and the tag's; a
# pyproject may carry the same version normalised the PEP 440 way
# (`0.2.0rc1`), which is what `uv version` writes.
define SDK_VERSIONS_PY
import json, pathlib, re, sys, tomllib
want, py, npm = sys.argv[1], sys.argv[2].split(), sys.argv[3].split()
SEMVER = re.compile(r"\d+\.\d+\.\d+(-(alpha|beta|rc)\.\d+)?")
def pep440(v):
    return re.sub(r"-(alpha|beta|rc)\.(\d+)", lambda m: dict(alpha="a", beta="b", rc="rc")[m[1]] + m[2], v)
found = {p + "/pyproject.toml": tomllib.loads(pathlib.Path(p, "pyproject.toml").read_text())["project"]["version"] for p in py}
found.update({p + "/package.json": json.loads(pathlib.Path(p, "package.json").read_text())["version"] for p in npm})
for path, version in found.items():
    print(f"  {version:<16}{path}")
npm_versions = {found[p + "/package.json"] for p in npm}
if len(npm_versions) != 1 or {pep440(v) for v in found.values()} != {pep440(v) for v in npm_versions}:
    sys.exit("The SDK versions disagree. They are released in lockstep: make sdk-version VERSION=<x.y.z>")
version = npm_versions.pop()
if not SEMVER.fullmatch(version):
    sys.exit(f"{version} is not x.y.z or x.y.z-(alpha|beta|rc).N, the forms both npm and PEP 440 read the same way")
if want and want != version:
    sys.exit(f"The SDK version is {version}, not {want}")
print(f"SDK version {version}")
endef
export SDK_VERSIONS_PY

.PHONY: sdk-version sdk-version-check sdk-build sdk-publish

sdk-version-check: ## Check the five SDK packages carry one version (and VERSION=, when given)
	@uv run --no-project --python 3.12 python -c "$$SDK_VERSIONS_PY" "$(VERSION)" "$(SDK_PY_PACKAGES)" "$(SDK_NPM_PACKAGES)"

sdk-version: ## Set the five SDK packages' version: make sdk-version VERSION=0.2.0
	@test -n "$(VERSION)" || { echo "VERSION=<x.y.z> is required"; exit 1; }
	@for pkg in $(SDK_PY_PACKAGES); do uv version --directory $$pkg --no-sync "$(VERSION)" || exit 1; done
	@for pkg in $(SDK_NPM_PACKAGES); do \
		(cd $$pkg && npm version --no-git-tag-version --allow-same-version "$(VERSION)" >/dev/null) || exit 1; \
	done
	@$(MAKE) --no-print-directory sdk-version-check VERSION="$(VERSION)"

sdk-build: sdk-version-check ## Build the five SDK packages into dist/sdk/ (wheels, sdists, npm tarballs)
	rm -rf $(SDK_DIST)
	@for pkg in $(SDK_PY_PACKAGES); do \
		uv build --no-sources --out-dir $(SDK_DIST)/python $$pkg || exit 1; \
	done
	@mkdir -p $(SDK_DIST)/npm
	@for pkg in $(SDK_NPM_PACKAGES); do \
		(cd $$pkg && npm pack --silent --pack-destination $(abspath $(SDK_DIST))/npm) || exit 1; \
	done
	@ls -1 $(SDK_DIST)/python $(SDK_DIST)/npm

# Uploads what `sdk-build` made. The release workflow runs this; a person
# should not need to. Needs SDK_INDEX_TOKEN — an OAuth access token for an
# account with write access to both repositories — and the repositories'
# URLs (`terraform output sdk_python_repository_url sdk_npm_registry_url`
# in igni_iac). Safe to re-run: a file or version already there is skipped.
SDK_PYTHON_URL ?=
SDK_NPM_URL ?=
NPM_DIST_TAG ?= latest

sdk-publish: ## Upload dist/sdk/ to the SDK index (CI): SDK_PYTHON_URL=… SDK_NPM_URL=… SDK_INDEX_TOKEN=…
	@test -n "$(SDK_PYTHON_URL)" -a -n "$(SDK_NPM_URL)" || { echo "SDK_PYTHON_URL and SDK_NPM_URL are required"; exit 1; }
	@test -n "$$SDK_INDEX_TOKEN" || { echo "SDK_INDEX_TOKEN is required (gcloud auth print-access-token)"; exit 1; }
	@set -eu; \
	scratch=$$(mktemp -d); trap 'rm -rf "$$scratch"' EXIT; \
	py_url="$(patsubst %/,%,$(SDK_PYTHON_URL))/"; npm_url="$(patsubst %/,%,$(SDK_NPM_URL))/"; \
	py_host=$$(echo "$$py_url" | sed -E 's#^[a-z]+://([^/:]+).*#\1#'); \
	printf 'machine %s\nlogin oauth2accesstoken\npassword %s\n' "$$py_host" "$$SDK_INDEX_TOKEN" > "$$scratch/netrc"; \
	NETRC="$$scratch/netrc" UV_PUBLISH_USERNAME=oauth2accesstoken UV_PUBLISH_PASSWORD="$$SDK_INDEX_TOKEN" \
		uv publish --publish-url "$$py_url" --check-url "$${py_url}simple/" $(SDK_DIST)/python/*; \
	printf '@igni:registry=%s\n//%s:_authToken=%s\n' "$$npm_url" "$${npm_url#*://}" "$$SDK_INDEX_TOKEN" > "$$scratch/npmrc"; \
	for pkg in $(SDK_NPM_PACKAGES); do \
		name=$$(node -p "require('./$$pkg/package.json').name"); \
		version=$$(node -p "require('./$$pkg/package.json').version"); \
		tarball="$(SDK_DIST)/npm/$$(echo "$$name" | sed 's#^@##; s#/#-#')-$$version.tgz"; \
		if npm view "$$name@$$version" version --userconfig "$$scratch/npmrc" >/dev/null 2>&1; then \
			echo "$$name@$$version is already published"; \
		else \
			npm publish "$$tarball" --tag "$(NPM_DIST_TAG)" --userconfig "$$scratch/npmrc"; \
		fi; \
	done
