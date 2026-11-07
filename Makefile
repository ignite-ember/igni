.PHONY: help install check typecheck test e2e tauri-test version

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# igni's desktop app (clients/tauri) and the web UI it loads, which is also
# the web client (clients/web). The SDKs it is built with come from a
# checkout of igni-sdk beside this one (../igni-sdk), and the backend from
# igni-core (../igni-core, or IGNI_CORE_DIR): see CONTRIBUTING.md.
WEB := clients/web
TAURI := clients/tauri/src-tauri

install: ## Install the web client's dependencies
	cd $(WEB) && npm ci

check: typecheck test tauri-test ## Everything CI runs, but the browser suite

typecheck: ## Type-check the web client
	cd $(WEB) && npx tsc -b --noEmit

test: ## The web client's unit tests, with the coverage floor
	cd $(WEB) && npx vitest run --coverage

e2e: ## The browser suite (spawns igni-core's backend when its venv exists)
	cd $(WEB) && npx playwright test

tauri-test: ## The desktop shell's Rust tests
	cd $(TAURI) && cargo test --no-default-features

version: ## Copy igni-core.version into the Tauri shell's files
	node scripts/sync-version.mjs
