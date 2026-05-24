# FORGE — Autonomous Software Production System

## Overview
FORGE is an AI-native autonomous engineering infrastructure platform. It converts natural language project descriptions into production-grade software through a structured 13-phase execution pipeline.

## Repository Structure
```
system/           Python backend (FastAPI + Celery)
├── core/         Core engines (intent, spec, planning, orchestration, etc.)
├── agents/       Specialized AI agents (8 types)
├── runtime/      Task queues, workers, scheduler
├── repo_intelligence/  AST parsing, embeddings, graph
├── tools/        GitHub, terminal, Docker, K8s tools
├── execution/    Sandboxes, containers, isolation
├── observability/ Logging, tracing, metrics, alerts
├── api/          FastAPI gateway
└── shared/       Models, schemas, database, Redis, Neo4j

frontend/         Next.js 15 + TypeScript + Tailwind dashboard
infra/            Docker, Kubernetes, Terraform
tests/            pytest test suite
alembic/          Database migrations
scripts/          Setup and utility scripts
```

## Development Setup
```bash
cp .env.example .env
# Edit .env with your API keys
make setup          # Install deps + start Docker services
make migrate        # Run database migrations
make dev            # Start API server (port 8000)
# In another terminal:
cd frontend && npm install && npm run dev  # Start frontend (port 3000)
```

## Key Commands
```bash
make test           # Run full test suite with coverage
make lint           # ruff + mypy
make format         # ruff format
make docker-up      # Start all Docker services
make migrate        # Run Alembic migrations
make migrate-create m="description"  # Create new migration
```

## Execution Pipeline
```
1. Intent       — Parse natural language → structured ProjectIntent
2. Clarification — Ask targeted questions to fill gaps
3. Specification — Generate PRD, DB schema, API contracts, UI structure
4. Architecture  — Stack selection, service topology, infra planning
5. Task Graph   — Convert spec into executable DAG of agent tasks
6. Assignment   — Assign tasks to specialized agents with scoped context
7. Execution    — Agents generate production code (isolated, token-budgeted)
8. Verification — Static lint, type checks, tests, architecture validation
9. Deployment   — Provision infra, deploy, health check, create rollback point
10. Monitoring  — Health monitoring, metrics collection
11. Iteration   — Detect improvements, plan patches, regression-safe evolution
```

## Agents
Each agent is isolated with a strict contract (allowed files, constraints, validation rules):
- **architect** — Stack decisions, service topology, ADRs
- **backend** — FastAPI, SQLAlchemy, migrations, tests
- **frontend** — Next.js, TypeScript, Tailwind, Zustand
- **infra** — Docker, Kubernetes, Terraform, CI/CD
- **qa** — pytest, coverage, mocking strategies
- **security** — OWASP review, auth patterns, input validation
- **docs** — API docs, runbooks, architecture documentation
- **refactor** — Code quality, patterns, backward-compatible improvements

## Tech Stack
- **Backend**: Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic
- **Databases**: PostgreSQL 16 + pgvector, Redis 7, Neo4j 5
- **Agent Runtime**: LangGraph, Temporal, Celery
- **LLM**: Anthropic Claude (claude-sonnet-4-6 default)
- **Infra**: Docker, Kubernetes, Terraform
- **Frontend**: Next.js 15, TypeScript, Tailwind CSS, Zustand, TanStack Query
- **Observability**: structlog, OpenTelemetry, Prometheus

## Environment Variables
See `.env.example` for all required variables. Critical ones:
- `ANTHROPIC_API_KEY` — Required for all LLM operations
- `DATABASE_URL` — Async PostgreSQL connection string
- `SECRET_KEY` — JWT signing key (min 32 chars)

## Architecture Rules (enforced by agents)
1. Never inject full repo context to agents — use scoped retrieval
2. All outputs must pass static validation before merge
3. Never deploy without health check passing
4. All schema changes require Alembic migrations
5. Never hardcode secrets — use SecretsManager
