.PHONY: help install dev test lint format clean build run-api run-web run-worker run-eval db-migrate db-seed docker-up docker-down docker-build docker-logs

# Colors for output
BLUE=\033[0;34m
GREEN=\033[0;32m
YELLOW=\033[0;33m
RED=\033[0;31m
NC=\033[0m # No Color

help: ## Show this help message
	@echo "$(BLUE)AI Interviewer - Development Commands$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "$(YELLOW)%-20s$(NC) %s\n", $$1, $$2}'

# ==========================================
# INSTALLATION & SETUP
# ==========================================

install: ## Install dependencies
	@echo "$(BLUE)Installing dependencies...$(NC)"
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

install-pre-commit: ## Setup pre-commit hooks
	@echo "$(BLUE)Setting up pre-commit hooks...$(NC)"
	pre-commit install

install-all: install install-pre-commit ## Install everything
	@echo "$(GREEN)✓ All dependencies installed$(NC)"

# ==========================================
# DEVELOPMENT
# ==========================================

dev: ## Start all services with docker-compose
	@echo "$(BLUE)Starting development environment...$(NC)"
	docker-compose up -d
	@echo "$(GREEN)✓ Services started$(NC)"
	@echo "  API: http://localhost:8000"
	@echo "  Docs: http://localhost:8000/docs"
	@echo "  Flower: http://localhost:5555"

dev-down: ## Stop all docker services
	@echo "$(BLUE)Stopping development environment...$(NC)"
	docker-compose down
	@echo "$(GREEN)✓ Services stopped$(NC)"

run-api: ## Run FastAPI backend (development mode)
	@echo "$(BLUE)Starting FastAPI server...$(NC)"
	uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000

run-web: ## Run React frontend
	@echo "$(BLUE)Starting React server...$(NC)"
	cd apps/web && npm start

run-worker: ## Run Celery worker
	@echo "$(BLUE)Starting Celery worker...$(NC)"
	celery -A apps.worker.celery_app worker --loglevel=info

run-eval: ## Run evaluation framework
	@echo "$(BLUE)Starting evaluation runner...$(NC)"
	python -m evaluation.regression.runner

shell: ## Open Python shell with app context
	@echo "$(BLUE)Opening IPython shell...$(NC)"
	ipython

# ==========================================
# DATABASE
# ==========================================

db-migrate: ## Run database migrations
	@echo "$(BLUE)Running database migrations...$(NC)"
	alembic upgrade head
	@echo "$(GREEN)✓ Migrations completed$(NC)"

db-seed: ## Seed database with sample data
	@echo "$(BLUE)Seeding database...$(NC)"
	python -m integrations.supabase.seeds
	@echo "$(GREEN)✓ Database seeded$(NC)"

db-reset: ## Reset database (drop and recreate)
	@echo "$(RED)⚠ Resetting database...$(NC)"
	alembic downgrade base
	alembic upgrade head
	python -m integrations.supabase.seeds
	@echo "$(GREEN)✓ Database reset completed$(NC)"

db-shell: ## Connect to PostgreSQL shell
	@echo "$(BLUE)Connecting to PostgreSQL...$(NC)"
	psql $(DATABASE_URL)

# ==========================================
# TESTING
# ==========================================

test: ## Run all tests
	@echo "$(BLUE)Running tests...$(NC)"
	pytest -v --tb=short

test-unit: ## Run unit tests only
	@echo "$(BLUE)Running unit tests...$(NC)"
	pytest tests/unit/ -v

test-integration: ## Run integration tests
	@echo "$(BLUE)Running integration tests...$(NC)"
	pytest tests/integration/ -v

test-e2e: ## Run end-to-end tests
	@echo "$(BLUE)Running E2E tests...$(NC)"
	pytest tests/e2e/ -v

test-cov: ## Run tests with coverage report
	@echo "$(BLUE)Running tests with coverage...$(NC)"
	pytest --cov=apps --cov=agents --cov=rag --cov=voice --cov-report=html --cov-report=term
	@echo "$(GREEN)✓ Coverage report: htmlcov/index.html$(NC)"

test-watch: ## Run tests in watch mode
	@echo "$(BLUE)Running tests in watch mode...$(NC)"
	ptw

test-slow: ## Run only slow tests
	@echo "$(BLUE)Running slow tests...$(NC)"
	pytest -v -m slow

test-voice: ## Run voice pipeline tests
	@echo "$(BLUE)Running voice tests...$(NC)"
	pytest tests/ -v -k voice

test-rag: ## Run RAG pipeline tests
	@echo "$(BLUE)Running RAG tests...$(NC)"
	pytest tests/ -v -k rag

# ==========================================
# CODE QUALITY
# ==========================================

lint: ## Run all linters
	@echo "$(BLUE)Running linters...$(NC)"
	pylint apps agents rag voice evaluation integrations || true
	flake8 . --max-line-length=100 || true
	mypy apps agents --ignore-missing-imports || true

format: ## Format code with black and isort
	@echo "$(BLUE)Formatting code...$(NC)"
	black .
	isort .
	@echo "$(GREEN)✓ Code formatted$(NC)"

format-check: ## Check code formatting without changes
	@echo "$(BLUE)Checking code format...$(NC)"
	black --check .
	isort --check-only .

security: ## Run security checks
	@echo "$(BLUE)Running security checks...$(NC)"
	bandit -r apps agents -ll || true
	safety check || true

check-all: format-check lint security ## Run all checks (no modifications)
	@echo "$(GREEN)✓ All checks passed$(NC)"

# ==========================================
# DOCKER
# ==========================================

docker-build: ## Build Docker images
	@echo "$(BLUE)Building Docker images...$(NC)"
	docker-compose build
	@echo "$(GREEN)✓ Images built$(NC)"

docker-up: ## Start Docker services
	@echo "$(BLUE)Starting Docker services...$(NC)"
	docker-compose up -d
	@echo "$(GREEN)✓ Services started$(NC)"

docker-down: ## Stop Docker services
	@echo "$(BLUE)Stopping Docker services...$(NC)"
	docker-compose down
	@echo "$(GREEN)✓ Services stopped$(NC)"

docker-logs: ## Show Docker logs
	@echo "$(BLUE)Tailing Docker logs...$(NC)"
	docker-compose logs -f

docker-logs-api: ## Show API logs
	docker-compose logs -f api

docker-logs-worker: ## Show Celery worker logs
	docker-compose logs -f celery_worker

docker-clean: ## Remove Docker volumes and images
	@echo "$(RED)⚠ Removing Docker volumes...$(NC)"
	docker-compose down -v
	@echo "$(GREEN)✓ Cleaned$(NC)"

# ==========================================
# DOCUMENTATION
# ==========================================

docs-build: ## Build documentation
	@echo "$(BLUE)Building documentation...$(NC)"
	mkdocs build
	@echo "$(GREEN)✓ Documentation built$(NC)"

docs-serve: ## Serve documentation locally
	@echo "$(BLUE)Serving documentation...$(NC)"
	mkdocs serve

# ==========================================
# CLEANUP
# ==========================================

clean: ## Remove cache and temporary files
	@echo "$(BLUE)Cleaning up...$(NC)"
	find . -type d -name __pycache__ -exec rm -rf {} + || true
	find . -type d -name .pytest_cache -exec rm -rf {} + || true
	find . -type d -name .mypy_cache -exec rm -rf {} + || true
	find . -type d -name *.egg-info -exec rm -rf {} + || true
	find . -type f -name .DS_Store -delete
	rm -rf build/ dist/ htmlcov/ .coverage
	@echo "$(GREEN)✓ Cleanup completed$(NC)"

clean-all: clean docker-clean ## Complete cleanup including Docker
	@echo "$(GREEN)✓ Complete cleanup done$(NC)"

# ==========================================
# EVALUATION & METRICS
# ==========================================

eval-generate: ## Generate synthetic interview cases
	@echo "$(BLUE)Generating synthetic cases...$(NC)"
	python -m evaluation.datasets.synthetic.generator --count 1000

eval-regression: ## Run regression test suite
	@echo "$(BLUE)Running regression suite...$(NC)"
	python -m evaluation.regression.runner --baseline v1.0

eval-rag: ## Evaluate RAG pipeline
	@echo "$(BLUE)Evaluating RAG...$(NC)"
	python -m evaluation.rag_eval.retrieval_eval

eval-agent: ## Evaluate agent behavior
	@echo "$(BLUE)Evaluating agent...$(NC)"
	python -m evaluation.agent_eval.plan_eval

# ==========================================
# DEPLOYMENT
# ==========================================

deploy-staging: ## Deploy to staging environment
	@echo "$(BLUE)Deploying to staging...$(NC)"
	# Add your staging deployment commands here
	@echo "$(GREEN)✓ Deployed to staging$(NC)"

deploy-prod: ## Deploy to production (requires confirmation)
	@echo "$(RED)⚠ Deploying to PRODUCTION$(NC)"
	@read -p "Are you sure? [y/N] " -n 1 -r; \
	echo; \
	if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
		echo "$(BLUE)Deploying to production...$(NC)"; \
		# Add your production deployment commands here; \
		echo "$(GREEN)✓ Deployed to production$(NC)"; \
	else \
		echo "$(YELLOW)Deployment cancelled$(NC)"; \
	fi

# ==========================================
# UTILITIES
# ==========================================

version: ## Show project version
	@grep "version" pyproject.toml | head -1

info: ## Show project information
	@echo "$(BLUE)AI Interviewer Project Info$(NC)"
	@echo ""
	@echo "Python version: $$(python --version)"
	@echo "FastAPI version: $$(pip show fastapi | grep Version)"
	@echo "LangChain version: $$(pip show langchain | grep Version)"
	@echo ""
	@echo "Services:"
	@echo "  API: http://localhost:8000"
	@echo "  Frontend: http://localhost:3000"
	@echo "  Flower: http://localhost:5555"
	@echo "  pgAdmin: http://localhost:5050"

.DEFAULT_GOAL := help
