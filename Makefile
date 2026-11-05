.DEFAULT_GOAL := help
.PHONY: help install test lint frontend frontend-build api demo demo-local demo-down clean

help: ## Show this help message
	@echo "FinSight — make targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install backend package with dev extras
	pip install -e ".[dev]"

test: ## Run the backend test suite
	pytest tests/ -v

lint: ## Lint the frontend (next lint / ESLint)
	cd frontend && npm run lint

frontend: ## Install deps and run the frontend dev server (http://localhost:3002)
	cd frontend && npm install && npm run dev

frontend-build: ## Build the frontend for production
	cd frontend && npm install && npm run build

api: ## Run the FastAPI server (http://localhost:8001)
	uvicorn finsight.api.app:app --reload --port 8001

demo: ## One command: full stack via Docker (API :8001 + frontend :3002)
	docker compose up --build

demo-down: ## Stop and remove the docker-compose demo stack
	docker compose down

demo-local: ## Full stack without Docker: demo API (:8001) + frontend dev (:3002)
	@echo "Starting FinSight demo (backend :8001 + frontend :3002)…"
	@echo "Backend runs in FINSIGHT_DEMO mode — no network or API keys needed."
	FINSIGHT_DEMO=1 uvicorn finsight.api.app:app --port 8001 & \
	  echo $$! > .demo_api.pid; \
	  cd frontend && NEXT_PUBLIC_API_URL=http://localhost:8001 npm install && \
	  NEXT_PUBLIC_API_URL=http://localhost:8001 npm run dev; \
	  kill `cat ../.demo_api.pid` 2>/dev/null; rm -f ../.demo_api.pid

clean: ## Remove Python build/cache artifacts
	rm -rf build dist *.egg-info .pytest_cache .coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
