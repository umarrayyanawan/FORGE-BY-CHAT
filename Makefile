.PHONY: install dev test test-unit test-integration lint format migrate migrate-create \
        docker-build docker-up docker-down docker-logs clean setup seed help

PYTHON := python3.12
PIP := pip
PYTEST := pytest
RUFF := ruff
MYPY := mypy
ALEMBIC := alembic
DOCKER_COMPOSE := docker-compose
CELERY := celery

# Colors
RESET  := \033[0m
BOLD   := \033[1m
GREEN  := \033[32m
YELLOW := \033[33m
CYAN   := \033[36m
RED    := \033[31m

##@ General

help: ## Show this help message
	@awk 'BEGIN {FS = ":.*##"; printf "\n$(BOLD)FORGE Makefile$(RESET)\n\nUsage:\n  make $(CYAN)<target>$(RESET)\n"} \
	/^[a-zA-Z_0-9-]+:.*?##/ { printf "  $(CYAN)%-20s$(RESET) %s\n", $$1, $$2 } \
	/^##@/ { printf "\n$(BOLD)%s$(RESET)\n", substr($$0, 5) }' $(MAKEFILE_LIST)

##@ Development

install: ## Install package in editable mode with dev dependencies
	@echo "$(GREEN)Installing FORGE with dev dependencies...$(RESET)"
	$(PIP) install -e ".[dev]"
	@echo "$(GREEN)Installation complete.$(RESET)"

install-hooks: ## Install pre-commit hooks
	pre-commit install
	pre-commit install --hook-type commit-msg

dev: ## Start Docker services and run FastAPI in reload mode
	@echo "$(CYAN)Starting services...$(RESET)"
	$(DOCKER_COMPOSE) up -d forge-db forge-redis forge-neo4j forge-temporal forge-temporal-ui
	@echo "$(CYAN)Waiting for services to be healthy...$(RESET)"
	@sleep 5
	@echo "$(GREEN)Starting FastAPI dev server on port 8000...$(RESET)"
	uvicorn system.api.main:app --reload --port 8000 --host 0.0.0.0 \
		--log-level info \
		--reload-dir system/

dev-worker: ## Start Celery worker in development mode
	@echo "$(CYAN)Starting Celery worker...$(RESET)"
	$(CELERY) -A system.runtime.workers.celery_app worker \
		--loglevel=info \
		--concurrency=2 \
		--autoreload

dev-beat: ## Start Celery beat scheduler
	@echo "$(CYAN)Starting Celery beat...$(RESET)"
	$(CELERY) -A system.runtime.workers.celery_app beat \
		--loglevel=info \
		--scheduler celery.schedulers:DatabaseScheduler

dev-flower: ## Start Flower monitoring for Celery
	$(CELERY) -A system.runtime.workers.celery_app flower --port=5555

##@ Testing

test: ## Run all tests with coverage
	@echo "$(GREEN)Running all tests...$(RESET)"
	$(PYTEST) tests/ -v \
		--cov=system \
		--cov-report=html:htmlcov \
		--cov-report=term-missing \
		--cov-report=xml:coverage.xml \
		--cov-fail-under=70

test-unit: ## Run unit tests only
	@echo "$(GREEN)Running unit tests...$(RESET)"
	$(PYTEST) tests/unit/ -v \
		--cov=system \
		--cov-report=term-missing \
		-m "unit"

test-integration: ## Run integration tests (requires running services)
	@echo "$(YELLOW)Running integration tests (services must be running)...$(RESET)"
	$(PYTEST) tests/integration/ -v \
		--cov=system \
		--cov-report=term-missing \
		-m "integration"

test-api: ## Run API tests
	@echo "$(GREEN)Running API tests...$(RESET)"
	$(PYTEST) tests/ -v -m "api" --cov=system/api

test-watch: ## Run tests in watch mode
	$(PYTEST) tests/unit/ -v --tb=short -f

##@ Code Quality

lint: ## Run ruff linter and mypy type checker
	@echo "$(CYAN)Running ruff linter...$(RESET)"
	$(RUFF) check system/ tests/
	@echo "$(CYAN)Running mypy type checker...$(RESET)"
	$(MYPY) system/
	@echo "$(GREEN)Lint passed!$(RESET)"

lint-fix: ## Auto-fix lint issues
	$(RUFF) check --fix system/ tests/

format: ## Format code with ruff
	@echo "$(CYAN)Formatting code...$(RESET)"
	$(RUFF) format system/ tests/
	@echo "$(GREEN)Formatting complete.$(RESET)"

format-check: ## Check formatting without making changes
	$(RUFF) format --check system/ tests/

security: ## Run security checks
	@echo "$(CYAN)Running bandit security scan...$(RESET)"
	bandit -r system/ -ll
	@echo "$(CYAN)Running safety dependency check...$(RESET)"
	safety check

pre-commit: ## Run all pre-commit hooks
	pre-commit run --all-files

##@ Database

migrate: ## Apply all pending migrations
	@echo "$(CYAN)Running database migrations...$(RESET)"
	$(ALEMBIC) upgrade head
	@echo "$(GREEN)Migrations applied.$(RESET)"

migrate-create: ## Create a new migration (usage: make migrate-create m="description")
	@echo "$(CYAN)Creating migration: $(m)...$(RESET)"
	$(ALEMBIC) revision --autogenerate -m "$(m)"

migrate-down: ## Rollback last migration
	@echo "$(YELLOW)Rolling back last migration...$(RESET)"
	$(ALEMBIC) downgrade -1

migrate-down-all: ## Rollback all migrations
	@echo "$(RED)Rolling back ALL migrations...$(RESET)"
	$(ALEMBIC) downgrade base

migrate-show: ## Show current migration status
	$(ALEMBIC) current
	$(ALEMBIC) history --verbose

migrate-check: ## Check if migrations are up to date
	$(ALEMBIC) check

##@ Docker

docker-build: ## Build all Docker images
	@echo "$(CYAN)Building Docker images...$(RESET)"
	$(DOCKER_COMPOSE) build --no-cache
	@echo "$(GREEN)Build complete.$(RESET)"

docker-build-api: ## Build API Docker image only
	$(DOCKER_COMPOSE) build forge-api

docker-build-worker: ## Build worker Docker image only
	$(DOCKER_COMPOSE) build forge-worker

docker-up: ## Start all services in background
	@echo "$(CYAN)Starting all services...$(RESET)"
	$(DOCKER_COMPOSE) up -d
	@echo "$(GREEN)Services started. Run 'make docker-logs' to view logs.$(RESET)"

docker-up-infra: ## Start only infrastructure services (db, redis, neo4j, temporal)
	$(DOCKER_COMPOSE) up -d forge-db forge-redis forge-neo4j \
		forge-temporal forge-temporal-ui

docker-down: ## Stop all services
	@echo "$(YELLOW)Stopping all services...$(RESET)"
	$(DOCKER_COMPOSE) down
	@echo "$(GREEN)Services stopped.$(RESET)"

docker-down-volumes: ## Stop all services and remove volumes (DESTRUCTIVE)
	@echo "$(RED)Stopping services and removing volumes...$(RESET)"
	$(DOCKER_COMPOSE) down -v
	@echo "$(GREEN)Done.$(RESET)"

docker-logs: ## Follow logs from all services
	$(DOCKER_COMPOSE) logs -f

docker-logs-api: ## Follow API logs
	$(DOCKER_COMPOSE) logs -f forge-api

docker-logs-worker: ## Follow worker logs
	$(DOCKER_COMPOSE) logs -f forge-worker

docker-ps: ## Show running services
	$(DOCKER_COMPOSE) ps

docker-restart: ## Restart all services
	$(DOCKER_COMPOSE) restart

docker-exec-api: ## Open shell in API container
	$(DOCKER_COMPOSE) exec forge-api /bin/bash

docker-exec-db: ## Open psql in database container
	$(DOCKER_COMPOSE) exec forge-db psql -U forge -d forge

##@ Setup & Utilities

setup: ## Full project setup (run once after cloning)
	@echo "$(GREEN)Setting up FORGE...$(RESET)"
	@bash scripts/setup.sh

seed: ## Seed the database with initial data
	@echo "$(CYAN)Seeding database...$(RESET)"
	$(PYTHON) scripts/seed.py
	@echo "$(GREEN)Database seeded.$(RESET)"

clean: ## Remove build artifacts and cache files
	@echo "$(CYAN)Cleaning build artifacts...$(RESET)"
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.pyd" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ htmlcov/ coverage.xml .coverage
	@echo "$(GREEN)Clean complete.$(RESET)"

clean-all: clean ## Remove all generated files including venv
	rm -rf .venv/ node_modules/

check-env: ## Verify environment variables are set
	@$(PYTHON) -c "from system.core.config import settings; print('Config OK:', settings.APP_NAME)"

generate-secret: ## Generate a secure secret key
	@$(PYTHON) -c "import secrets; print(secrets.token_hex(32))"

##@ Monitoring

logs-tail: ## Tail application logs
	tail -f logs/*.log 2>/dev/null || echo "No log files found"

status: ## Show status of all components
	@echo "$(BOLD)Docker Services:$(RESET)"
	@$(DOCKER_COMPOSE) ps 2>/dev/null || echo "Docker not running"
	@echo ""
	@echo "$(BOLD)Database Migrations:$(RESET)"
	@$(ALEMBIC) current 2>/dev/null || echo "Cannot connect to database"
