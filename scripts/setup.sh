#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# FORGE Bootstrap Setup Script
# Run once after cloning to set up the development environment.
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# ─────────────────────────────────────────────
# Colors & formatting
# ─────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
step()    { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }
die()     { error "$*"; exit 1; }

# ─────────────────────────────────────────────
# Check prerequisites
# ─────────────────────────────────────────────
check_command() {
    local cmd="$1"
    local install_hint="${2:-}"
    if ! command -v "$cmd" &>/dev/null; then
        error "Required command '$cmd' not found."
        if [[ -n "$install_hint" ]]; then
            error "Install it: $install_hint"
        fi
        return 1
    fi
    success "$cmd found: $(command -v "$cmd")"
    return 0
}

check_version() {
    local cmd="$1"
    local min_version="$2"
    local actual_version
    actual_version="$("$cmd" --version 2>&1 | head -1)"
    info "$cmd version: $actual_version"
}

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo -e "\n${BOLD}${GREEN}"
echo "  ███████╗ ██████╗ ██████╗  ██████╗ ███████╗"
echo "  ██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝"
echo "  █████╗  ██║   ██║██████╔╝██║  ███╗█████╗  "
echo "  ██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝  "
echo "  ██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗"
echo "  ╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝"
echo -e "${RESET}"
echo -e "${BOLD}Autonomous Software Production System - Setup${RESET}"
echo -e "Repository: ${CYAN}${REPO_ROOT}${RESET}\n"

# ─────────────────────────────────────────────
step "Checking prerequisites"
# ─────────────────────────────────────────────
MISSING=0
check_command python3 "https://python.org" || MISSING=$((MISSING + 1))
check_command pip "https://pip.pypa.io" || MISSING=$((MISSING + 1))
check_command docker "https://docs.docker.com/get-docker/" || MISSING=$((MISSING + 1))
check_command docker-compose "https://docs.docker.com/compose/install/" || MISSING=$((MISSING + 1))
check_command git "https://git-scm.com" || MISSING=$((MISSING + 1))

if [[ $MISSING -gt 0 ]]; then
    die "Missing $MISSING required prerequisites. Please install them and retry."
fi

# Check Python version
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
REQUIRED_MAJOR=3
REQUIRED_MINOR=12
ACTUAL_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
ACTUAL_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ $ACTUAL_MAJOR -lt $REQUIRED_MAJOR ]] || [[ $ACTUAL_MAJOR -eq $REQUIRED_MAJOR && $ACTUAL_MINOR -lt $REQUIRED_MINOR ]]; then
    die "Python $REQUIRED_MAJOR.$REQUIRED_MINOR+ is required. Found: $PYTHON_VERSION"
fi
success "Python $PYTHON_VERSION OK"

# ─────────────────────────────────────────────
step "Setting up environment configuration"
# ─────────────────────────────────────────────
cd "$REPO_ROOT"

if [[ -f ".env" ]]; then
    warn ".env file already exists. Skipping creation."
    warn "To reset: rm .env && make setup"
else
    info "Creating .env from .env.example..."
    cp .env.example .env

    # Generate a secure secret key
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    if [[ "$(uname)" == "Darwin" ]]; then
        sed -i '' "s|change-me-in-production-32-chars-min|${SECRET_KEY}|g" .env
    else
        sed -i "s|change-me-in-production-32-chars-min|${SECRET_KEY}|g" .env
    fi

    success ".env created with generated secret key"
    warn "Please review .env and fill in API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)"
fi

# ─────────────────────────────────────────────
step "Installing Python dependencies"
# ─────────────────────────────────────────────
info "Installing FORGE with development dependencies..."
pip install -e ".[dev]" --quiet
success "Python dependencies installed"

# ─────────────────────────────────────────────
step "Setting up pre-commit hooks"
# ─────────────────────────────────────────────
if command -v pre-commit &>/dev/null; then
    info "Installing pre-commit hooks..."
    pre-commit install
    pre-commit install --hook-type commit-msg
    success "Pre-commit hooks installed"
else
    warn "pre-commit not found, skipping hook installation"
fi

# ─────────────────────────────────────────────
step "Starting Docker services"
# ─────────────────────────────────────────────
info "Pulling Docker images (this may take a few minutes)..."
docker-compose pull --quiet 2>/dev/null || warn "Some images could not be pulled, they will be built locally"

info "Starting infrastructure services..."
docker-compose up -d forge-db forge-redis forge-neo4j

# ─────────────────────────────────────────────
step "Waiting for services to be healthy"
# ─────────────────────────────────────────────
wait_for_service() {
    local service="$1"
    local max_attempts=30
    local attempt=0

    info "Waiting for $service to be healthy..."
    while [[ $attempt -lt $max_attempts ]]; do
        if docker-compose ps "$service" 2>/dev/null | grep -q "healthy"; then
            success "$service is healthy"
            return 0
        fi
        attempt=$((attempt + 1))
        if [[ $attempt -eq $max_attempts ]]; then
            warn "$service did not become healthy after ${max_attempts}s"
            return 1
        fi
        sleep 2
    done
}

wait_for_service forge-db || true
wait_for_service forge-redis || true

# ─────────────────────────────────────────────
step "Running database migrations"
# ─────────────────────────────────────────────
source .env 2>/dev/null || true

if [[ -f "alembic.ini" ]]; then
    info "Running Alembic migrations..."
    if alembic upgrade head 2>&1; then
        success "Database migrations applied"
    else
        warn "Migration failed - database may not be ready yet"
        warn "Run 'make migrate' manually after services start"
    fi
else
    warn "alembic.ini not found, skipping migrations"
    warn "Run 'make migrate' after the database models are created"
fi

# ─────────────────────────────────────────────
step "Starting remaining services"
# ─────────────────────────────────────────────
info "Starting Temporal and remaining services..."
docker-compose up -d forge-temporal forge-temporal-ui 2>/dev/null || warn "Temporal may need more time to start"

# ─────────────────────────────────────────────
step "Setup complete!"
# ─────────────────────────────────────────────
echo -e "\n${GREEN}${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║        FORGE is ready to forge!          ║${RESET}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════╝${RESET}\n"

echo -e "${BOLD}Quick start:${RESET}"
echo -e "  ${CYAN}make dev${RESET}          - Start FastAPI dev server"
echo -e "  ${CYAN}make dev-worker${RESET}   - Start Celery worker"
echo -e "  ${CYAN}make docker-up${RESET}    - Start all services"
echo -e "  ${CYAN}make test${RESET}         - Run tests"
echo -e "  ${CYAN}make lint${RESET}         - Lint & type check"

echo -e "\n${BOLD}Service URLs:${RESET}"
echo -e "  ${CYAN}API${RESET}:         http://localhost:8000"
echo -e "  ${CYAN}API Docs${RESET}:    http://localhost:8000/docs"
echo -e "  ${CYAN}Frontend${RESET}:    http://localhost:3000"
echo -e "  ${CYAN}Neo4j${RESET}:       http://localhost:7474"
echo -e "  ${CYAN}Temporal UI${RESET}: http://localhost:8088"

echo -e "\n${YELLOW}${BOLD}Next steps:${RESET}"
echo -e "  1. Edit ${CYAN}.env${RESET} and add your API keys"
echo -e "  2. Run ${CYAN}make migrate${RESET} after models are defined"
echo -e "  3. Run ${CYAN}make seed${RESET} to populate initial data"
echo -e "  4. Run ${CYAN}make dev${RESET} to start developing\n"
