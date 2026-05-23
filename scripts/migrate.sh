#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# FORGE Database Migration Script
# Runs Alembic migrations with validation and status reporting.
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# ─────────────────────────────────────────────
# Colors
# ─────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ALEMBIC_INI="${REPO_ROOT}/alembic.ini"
MAX_RETRIES=5
RETRY_DELAY=3

# ─────────────────────────────────────────────
# Parse arguments
# ─────────────────────────────────────────────
COMMAND="${1:-upgrade}"
REVISION="${2:-head}"
CREATE_MSG="${3:-}"

usage() {
    echo -e "\n${BOLD}FORGE Migration Script${RESET}"
    echo -e "\nUsage:"
    echo -e "  ${CYAN}$0 upgrade [revision]${RESET}      - Apply migrations (default: head)"
    echo -e "  ${CYAN}$0 downgrade [revision]${RESET}    - Rollback migrations (default: -1)"
    echo -e "  ${CYAN}$0 create <message>${RESET}        - Create new migration"
    echo -e "  ${CYAN}$0 status${RESET}                  - Show migration status"
    echo -e "  ${CYAN}$0 history${RESET}                 - Show migration history"
    echo -e "  ${CYAN}$0 check${RESET}                   - Check if migrations are up to date"
    echo -e "\nExamples:"
    echo -e "  $0 upgrade"
    echo -e "  $0 upgrade abc123"
    echo -e "  $0 downgrade -1"
    echo -e "  $0 create 'add user preferences table'"
    echo ""
}

# ─────────────────────────────────────────────
# Validate environment
# ─────────────────────────────────────────────
validate_env() {
    # Load .env if it exists
    if [[ -f "${REPO_ROOT}/.env" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "${REPO_ROOT}/.env"
        set +a
        info "Loaded environment from .env"
    fi

    # Check DATABASE_URL or DATABASE_SYNC_URL
    local db_url="${DATABASE_SYNC_URL:-${DATABASE_URL:-}}"
    if [[ -z "$db_url" ]]; then
        die "DATABASE_URL or DATABASE_SYNC_URL is not set. Cannot run migrations."
    fi

    # Mask the URL for display
    local masked_url
    masked_url=$(echo "$db_url" | sed 's|://[^:]*:[^@]*@|://****:****@|')
    info "Database: ${masked_url}"

    # Check alembic.ini exists
    if [[ ! -f "$ALEMBIC_INI" ]]; then
        die "alembic.ini not found at: $ALEMBIC_INI"
    fi

    # Check alembic is available
    if ! command -v alembic &>/dev/null; then
        die "alembic not found. Run: pip install -e '.[dev]'"
    fi
}

# ─────────────────────────────────────────────
# Wait for database connectivity
# ─────────────────────────────────────────────
wait_for_db() {
    local attempt=0
    info "Checking database connectivity..."

    while [[ $attempt -lt $MAX_RETRIES ]]; do
        if python3 -c "
import sys
import os

# Try to import and connect
try:
    import psycopg2
    db_url = os.environ.get('DATABASE_SYNC_URL', os.environ.get('DATABASE_URL', ''))
    # Convert asyncpg URL to psycopg2 format
    db_url = db_url.replace('postgresql+asyncpg://', 'postgresql://')
    conn = psycopg2.connect(db_url, connect_timeout=5)
    conn.close()
    sys.exit(0)
except Exception as e:
    sys.exit(1)
" 2>/dev/null; then
            success "Database is reachable"
            return 0
        fi

        attempt=$((attempt + 1))
        if [[ $attempt -lt $MAX_RETRIES ]]; then
            warn "Database not ready (attempt $attempt/$MAX_RETRIES). Retrying in ${RETRY_DELAY}s..."
            sleep "$RETRY_DELAY"
        fi
    done

    warn "Could not verify database connectivity. Proceeding anyway..."
}

# ─────────────────────────────────────────────
# Show current status
# ─────────────────────────────────────────────
show_status() {
    echo -e "\n${BOLD}Current Migration Status:${RESET}"
    cd "$REPO_ROOT"
    alembic current --verbose 2>/dev/null || warn "Could not determine current revision"
}

# ─────────────────────────────────────────────
# Run upgrade
# ─────────────────────────────────────────────
run_upgrade() {
    local target="${1:-head}"
    echo -e "\n${BOLD}${CYAN}━━━ Applying Migrations (target: ${target}) ━━━${RESET}"

    cd "$REPO_ROOT"

    # Show pre-migration status
    info "Current state:"
    alembic current 2>/dev/null || true

    info "Running: alembic upgrade ${target}"
    if alembic upgrade "$target"; then
        echo ""
        success "Migrations applied successfully"
        info "Current state after migration:"
        alembic current --verbose
    else
        die "Migration failed! Check the output above for details."
    fi
}

# ─────────────────────────────────────────────
# Run downgrade
# ─────────────────────────────────────────────
run_downgrade() {
    local target="${1:--1}"
    echo -e "\n${BOLD}${YELLOW}━━━ Rolling Back Migrations (target: ${target}) ━━━${RESET}"

    cd "$REPO_ROOT"

    info "Current state:"
    alembic current 2>/dev/null || true

    # Confirm destructive operation
    if [[ "${CI:-false}" != "true" && "${FORCE:-false}" != "true" ]]; then
        warn "This will rollback database migrations. Data may be lost."
        read -r -p "Are you sure? (type 'yes' to confirm): " confirm
        if [[ "$confirm" != "yes" ]]; then
            info "Downgrade cancelled."
            exit 0
        fi
    fi

    info "Running: alembic downgrade ${target}"
    if alembic downgrade "$target"; then
        echo ""
        success "Rollback completed"
        info "Current state after rollback:"
        alembic current --verbose
    else
        die "Rollback failed! Check the output above for details."
    fi
}

# ─────────────────────────────────────────────
# Create migration
# ─────────────────────────────────────────────
run_create() {
    local message="${1:-}"
    if [[ -z "$message" ]]; then
        die "Migration message is required. Usage: $0 create 'your migration message'"
    fi

    echo -e "\n${BOLD}${CYAN}━━━ Creating Migration: ${message} ━━━${RESET}"

    cd "$REPO_ROOT"
    info "Running: alembic revision --autogenerate -m '${message}'"

    if alembic revision --autogenerate -m "$message"; then
        echo ""
        success "Migration file created"
        info "Review the generated file in alembic/versions/ before applying"
        info "Run '$0 upgrade' to apply"
    else
        die "Failed to create migration"
    fi
}

# ─────────────────────────────────────────────
# Check migrations
# ─────────────────────────────────────────────
run_check() {
    echo -e "\n${BOLD}${CYAN}━━━ Checking Migration Status ━━━${RESET}"
    cd "$REPO_ROOT"

    local current
    current=$(alembic current 2>/dev/null | grep -c "head" || true)

    if [[ $current -gt 0 ]]; then
        success "Database is up to date (at head revision)"
        alembic current --verbose
        exit 0
    else
        warn "Database may not be at head revision"
        alembic current --verbose
        info "Pending migrations:"
        alembic history -r "current:head" 2>/dev/null || true
        exit 1
    fi
}

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
echo -e "\n${BOLD}FORGE - Database Migration Tool${RESET}"
echo -e "Timestamp: $(date -u '+%Y-%m-%d %H:%M:%S UTC')\n"

validate_env
wait_for_db

case "$COMMAND" in
    upgrade)
        run_upgrade "$REVISION"
        ;;
    downgrade)
        run_downgrade "${2:--1}"
        ;;
    create)
        run_create "$CREATE_MSG"
        ;;
    status|current)
        show_status
        ;;
    history)
        cd "$REPO_ROOT"
        alembic history --verbose
        ;;
    check)
        run_check
        ;;
    help|--help|-h)
        usage
        exit 0
        ;;
    *)
        error "Unknown command: $COMMAND"
        usage
        exit 1
        ;;
esac

echo -e "\n${GREEN}Migration script completed successfully.${RESET}\n"
