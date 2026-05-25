"""FastAPI router for the FORGE Tool Execution Engine.

Exposes HTTP endpoints for GitHub, terminal, Docker, and deployment
operations used by the agent pipeline.  All endpoints delegate to the
underlying tool classes and return structured :class:`ToolExecutionResult`
payloads with full audit trails.
"""

from __future__ import annotations

import time
from typing import Any
import uuid

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from system.observability.logging.logger import get_logger
from system.shared.exceptions import ToolError
from system.tools.deployment.deployer import (
    DeploymentTool,
    DockerDeployConfig,
    RailwayDeployConfig,
    VercelDeployConfig,
)
from system.tools.docker.manager import DockerManager
from system.tools.github.client import GitHubClient
from system.tools.schemas import ToolExecutionResult
from system.tools.terminal.executor import TerminalExecutor

logger = get_logger(__name__)
router = APIRouter(prefix="/tools", tags=["tools"])


# --------------------------------------------------------------------------- #
# Shared dependency factories
# --------------------------------------------------------------------------- #


def get_github_client() -> GitHubClient:
    return GitHubClient()


def get_terminal_executor() -> TerminalExecutor:
    return TerminalExecutor()


def get_docker_manager() -> DockerManager:
    return DockerManager()


def get_deployment_tool() -> DeploymentTool:
    return DeploymentTool()


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class CreateRepoRequest(BaseModel):
    name: str = Field(..., description="Repository name.")
    description: str = Field(default="", description="Repository description.")
    private: bool = Field(default=True, description="Create as private repository.")
    org: str | None = Field(
        default=None, description="Organisation name (uses authenticated user if omitted)."
    )
    topics: list[str] | None = Field(default=None, description="Repository topic tags.")


class CreateBranchRequest(BaseModel):
    repo: str = Field(..., description="Full repo slug: 'owner/repo'.")
    branch_name: str = Field(..., description="Name for the new branch.")
    from_branch: str = Field(default="main", description="Source branch to branch from.")


class CommitFilesRequest(BaseModel):
    repo: str = Field(..., description="Full repo slug: 'owner/repo'.")
    branch: str = Field(..., description="Target branch name.")
    files: dict[str, str] = Field(..., description="Mapping of {file_path: content} to commit.")
    message: str = Field(..., description="Commit message.")
    author_name: str = Field(default="FORGE Bot", description="Git author name.")
    author_email: str = Field(default="forge-bot@forge.ai", description="Git author email.")


class CreatePRRequest(BaseModel):
    repo: str = Field(..., description="Full repo slug: 'owner/repo'.")
    title: str = Field(..., description="Pull request title.")
    body: str = Field(..., description="Pull request description (Markdown).")
    head: str = Field(..., description="Source branch name.")
    base: str = Field(default="main", description="Target branch name.")
    draft: bool = Field(default=False, description="Open as a draft PR.")


class MergePRRequest(BaseModel):
    repo: str = Field(..., description="Full repo slug: 'owner/repo'.")
    pr_number: int = Field(..., description="Pull request number.")
    merge_method: str = Field(
        default="squash", description="Merge method: 'merge' | 'squash' | 'rebase'."
    )
    commit_title: str | None = Field(default=None, description="Optional merge commit title.")


class RunCommandRequest(BaseModel):
    command: str = Field(..., description="Shell command to execute.")
    cwd: str = Field(default=".", description="Working directory.")
    timeout: int = Field(default=300, ge=1, le=3600, description="Execution timeout in seconds.")
    env: dict[str, str] | None = Field(
        default=None, description="Additional environment variables."
    )
    sandboxed: bool = Field(default=True, description="Enforce command sandbox allow-list.")


class RunTestsRequest(BaseModel):
    cwd: str = Field(default=".", description="Project directory.")
    test_path: str = Field(default="tests/", description="Test path or file.")
    extra_args: str = Field(default="", description="Additional pytest arguments.")
    timeout: int = Field(default=600, ge=1, le=3600, description="Execution timeout in seconds.")


class RunLintRequest(BaseModel):
    cwd: str = Field(default=".", description="Project directory.")
    format_output: str = Field(default="json", description="Output format: 'json' | 'text'.")


class DockerDeployRequest(BaseModel):
    compose_file: str = Field(
        default="docker-compose.yml", description="Path to docker-compose file."
    )
    services: list[str] = Field(
        default_factory=list, description="Services to start (all if empty)."
    )
    env_file: str = Field(default=".env", description="Path to .env file.")
    project_dir: str = Field(default=".", description="Project directory.")
    build: bool = Field(default=False, description="Build images before starting.")
    project_id: str = Field(..., description="FORGE project ID for audit trail.")


class VercelDeployRequest(BaseModel):
    project_dir: str = Field(default="frontend", description="Project directory.")
    token: str = Field(default="", description="Vercel authentication token.")
    production: bool = Field(default=False, description="Deploy to production.")
    project_id: str = Field(..., description="FORGE project ID for audit trail.")


class RailwayDeployRequest(BaseModel):
    service: str = Field(default="", description="Railway service name.")
    environment: str = Field(default="production", description="Railway environment.")
    project_dir: str = Field(default=".", description="Project directory.")
    project_id: str = Field(..., description="FORGE project ID for audit trail.")


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _build_result(
    tool_name: str,
    action: str,
    success: bool,
    output: Any,
    error: str | None,
    duration_ms: int,
    project_id: str = "unknown",
    task_id: str | None = None,
) -> ToolExecutionResult:
    """Assemble a :class:`ToolExecutionResult` with full audit trail."""
    return ToolExecutionResult(
        execution_id=str(uuid.uuid4()),
        tool_name=tool_name,
        action=action,
        success=success,
        output=output,
        error=error,
        duration_ms=duration_ms,
        audit_trail={
            "project_id": project_id,
            "task_id": task_id,
            "tool": tool_name,
            "action": action,
            "success": success,
        },
    )


def _handle_tool_error(exc: ToolError, tool_name: str, action: str) -> JSONResponse:
    """Convert a ToolError into a structured 400 JSON response."""
    logger.warning("Tool error", tool=tool_name, action=action, code=exc.code, message=exc.message)
    return JSONResponse(
        status_code=400,
        content={
            "error": exc.message,
            "code": exc.code,
            "details": exc.details,
        },
    )


# --------------------------------------------------------------------------- #
# GitHub endpoints
# --------------------------------------------------------------------------- #


@router.post(
    "/github/repo",
    summary="Create a GitHub repository",
    description="Create a new GitHub repository using the configured GitHub token.",
)
async def create_github_repo(
    request: CreateRepoRequest,
    client: GitHubClient = Depends(get_github_client),
) -> dict[str, Any]:
    """Create a new repository on GitHub and return the full API response."""
    start = time.monotonic()
    try:
        result = await client.create_repo(
            name=request.name,
            description=request.description,
            private=request.private,
            org=request.org,
            topics=request.topics,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "success": True,
            "repo": {
                "full_name": result.get("full_name"),
                "html_url": result.get("html_url"),
                "clone_url": result.get("clone_url"),
                "ssh_url": result.get("ssh_url"),
                "default_branch": result.get("default_branch"),
                "private": result.get("private"),
            },
            "duration_ms": duration_ms,
        }
    except ToolError as exc:
        return _handle_tool_error(exc, "github", "create_repo")
    finally:
        await client.close()


@router.post(
    "/github/branch",
    summary="Create a GitHub branch",
    description="Create a new branch from an existing branch in a repository.",
)
async def create_branch(
    request: CreateBranchRequest,
    client: GitHubClient = Depends(get_github_client),
) -> dict[str, Any]:
    """Create a new branch and return the git ref object."""
    start = time.monotonic()
    try:
        result = await client.create_branch(
            repo=request.repo,
            branch_name=request.branch_name,
            from_branch=request.from_branch,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "success": True,
            "branch": request.branch_name,
            "repo": request.repo,
            "ref": result.get("ref"),
            "sha": result.get("object", {}).get("sha"),
            "duration_ms": duration_ms,
        }
    except ToolError as exc:
        return _handle_tool_error(exc, "github", "create_branch")
    finally:
        await client.close()


@router.post(
    "/github/commit",
    summary="Commit files to a branch",
    description="Commit one or more files to a repository branch in a single atomic commit using the Git Data API.",
)
async def commit_files(
    request: CommitFilesRequest,
    client: GitHubClient = Depends(get_github_client),
) -> dict[str, Any]:
    """Commit files and return the new commit object."""
    start = time.monotonic()
    try:
        result = await client.commit_files(
            repo=request.repo,
            branch=request.branch,
            files=request.files,
            message=request.message,
            author_name=request.author_name,
            author_email=request.author_email,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "success": True,
            "commit": {
                "sha": result.get("sha"),
                "message": result.get("message"),
                "url": result.get("html_url"),
                "author": result.get("author"),
            },
            "files_committed": len(request.files),
            "duration_ms": duration_ms,
        }
    except ToolError as exc:
        return _handle_tool_error(exc, "github", "commit_files")
    finally:
        await client.close()


@router.post(
    "/github/pr",
    summary="Create a pull request",
    description="Open a pull request in a GitHub repository.",
)
async def create_pr(
    request: CreatePRRequest,
    client: GitHubClient = Depends(get_github_client),
) -> dict[str, Any]:
    """Create a pull request and return the PR metadata."""
    start = time.monotonic()
    try:
        result = await client.create_pull_request(
            repo=request.repo,
            title=request.title,
            body=request.body,
            head=request.head,
            base=request.base,
            draft=request.draft,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "success": True,
            "pull_request": {
                "number": result.get("number"),
                "title": result.get("title"),
                "html_url": result.get("html_url"),
                "state": result.get("state"),
                "head": result.get("head", {}).get("ref"),
                "base": result.get("base", {}).get("ref"),
                "draft": result.get("draft"),
            },
            "duration_ms": duration_ms,
        }
    except ToolError as exc:
        return _handle_tool_error(exc, "github", "create_pull_request")
    finally:
        await client.close()


@router.post(
    "/github/pr/merge",
    summary="Merge a pull request",
    description="Merge a pull request using the specified merge method.",
)
async def merge_pr(
    request: MergePRRequest,
    client: GitHubClient = Depends(get_github_client),
) -> dict[str, Any]:
    """Merge a pull request."""
    start = time.monotonic()
    try:
        result = await client.merge_pull_request(
            repo=request.repo,
            pr_number=request.pr_number,
            merge_method=request.merge_method,
            commit_title=request.commit_title,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "success": result.get("merged", False),
            "sha": result.get("sha"),
            "message": result.get("message"),
            "duration_ms": duration_ms,
        }
    except ToolError as exc:
        return _handle_tool_error(exc, "github", "merge_pull_request")
    finally:
        await client.close()


@router.get(
    "/github/branches",
    summary="List repository branches",
    description="Return all branch names for a GitHub repository.",
)
async def list_branches(
    repo: str = Query(..., description="Full repo slug: 'owner/repo'."),
    client: GitHubClient = Depends(get_github_client),
) -> dict[str, Any]:
    """List all branches in a repository."""
    start = time.monotonic()
    try:
        branches = await client.list_branches(repo=repo)
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "success": True,
            "repo": repo,
            "branches": branches,
            "count": len(branches),
            "duration_ms": duration_ms,
        }
    except ToolError as exc:
        return _handle_tool_error(exc, "github", "list_branches")
    finally:
        await client.close()


@router.get(
    "/github/workflows/{repo:path}/{workflow_id}/runs",
    summary="Get workflow runs",
    description="List GitHub Actions workflow runs for a specific workflow file.",
)
async def get_workflow_runs(
    repo: str,
    workflow_id: str,
    branch: str | None = Query(default=None),
    per_page: int = Query(default=10, ge=1, le=100),
    client: GitHubClient = Depends(get_github_client),
) -> dict[str, Any]:
    """Fetch recent workflow run records."""
    start = time.monotonic()
    try:
        runs = await client.get_workflow_runs(
            repo=repo,
            workflow_id=workflow_id,
            branch=branch,
            per_page=per_page,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "success": True,
            "repo": repo,
            "workflow_id": workflow_id,
            "runs": [
                {
                    "id": r.get("id"),
                    "status": r.get("status"),
                    "conclusion": r.get("conclusion"),
                    "created_at": r.get("created_at"),
                    "html_url": r.get("html_url"),
                    "head_branch": r.get("head_branch"),
                    "head_sha": r.get("head_sha", "")[:7],
                }
                for r in runs
            ],
            "duration_ms": duration_ms,
        }
    except ToolError as exc:
        return _handle_tool_error(exc, "github", "get_workflow_runs")
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# Terminal endpoints
# --------------------------------------------------------------------------- #


@router.post(
    "/terminal/run",
    summary="Run a shell command",
    description="Execute a shell command in a project workspace. Set sandboxed=true to enforce the command allow-list.",
)
async def run_command(
    request: RunCommandRequest,
    executor: TerminalExecutor = Depends(get_terminal_executor),
) -> dict[str, Any]:
    """Execute a shell command and return its output."""
    if request.sandboxed:
        result = await executor.run_in_sandbox(
            command=request.command,
            cwd=request.cwd,
            timeout=request.timeout,
            env=request.env,
        )
    else:
        result = await executor.run(
            command=request.command,
            cwd=request.cwd,
            timeout=request.timeout,
            env=request.env,
        )
    return {
        "success": result.succeeded,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated": result.truncated,
        "command": result.command,
    }


@router.post(
    "/terminal/test",
    summary="Run tests",
    description="Run the pytest test suite in a project directory.",
)
async def run_tests(
    request: RunTestsRequest,
    executor: TerminalExecutor = Depends(get_terminal_executor),
) -> dict[str, Any]:
    """Execute the test suite and return results."""
    result = await executor.run_tests(
        cwd=request.cwd,
        test_path=request.test_path,
        extra_args=request.extra_args,
        timeout=request.timeout,
    )
    return {
        "success": result.succeeded,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated": result.truncated,
    }


@router.post(
    "/terminal/lint",
    summary="Run linter",
    description="Run ruff lint against a project directory.",
)
async def run_lint(
    request: RunLintRequest,
    executor: TerminalExecutor = Depends(get_terminal_executor),
) -> dict[str, Any]:
    """Execute the linter and return findings."""
    result = await executor.run_lint(cwd=request.cwd, format_output=request.format_output)
    return {
        "success": result.succeeded,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated": result.truncated,
    }


@router.post(
    "/terminal/type-check",
    summary="Run mypy type checking",
    description="Run mypy type checking against a project directory.",
)
async def run_type_check(
    cwd: str = Body(default="."),
    target: str = Body(default="."),
    executor: TerminalExecutor = Depends(get_terminal_executor),
) -> dict[str, Any]:
    """Execute mypy and return type error findings."""
    result = await executor.run_type_check(cwd=cwd, target=target)
    return {
        "success": result.succeeded,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
    }


@router.post(
    "/terminal/migrate",
    summary="Run database migrations",
    description="Execute Alembic database migrations in a project directory.",
)
async def run_migrations(
    cwd: str = Body(default="."),
    target: str = Body(default="head"),
    executor: TerminalExecutor = Depends(get_terminal_executor),
) -> dict[str, Any]:
    """Execute Alembic migrations."""
    result = await executor.run_migrations(cwd=cwd, target=target)
    return {
        "success": result.succeeded,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
    }


@router.post(
    "/terminal/npm-install",
    summary="Install Node.js dependencies",
    description="Run npm install in a frontend project directory.",
)
async def npm_install(
    cwd: str = Body(default="frontend"),
    ci: bool = Body(default=False),
    executor: TerminalExecutor = Depends(get_terminal_executor),
) -> dict[str, Any]:
    """Install npm dependencies."""
    result = await executor.npm_install(cwd=cwd, ci=ci)
    return {
        "success": result.succeeded,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
    }


@router.post(
    "/terminal/build-frontend",
    summary="Build frontend project",
    description="Run npm run build in a frontend project directory.",
)
async def build_frontend(
    cwd: str = Body(default="frontend"),
    script: str = Body(default="build"),
    executor: TerminalExecutor = Depends(get_terminal_executor),
) -> dict[str, Any]:
    """Build the frontend project."""
    result = await executor.build_frontend(cwd=cwd, script=script)
    return {
        "success": result.succeeded,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
    }


# --------------------------------------------------------------------------- #
# Deployment endpoints
# --------------------------------------------------------------------------- #


@router.post(
    "/deploy/docker",
    summary="Deploy with Docker Compose",
    description="Start services using Docker Compose.",
)
async def deploy_docker(
    request: DockerDeployRequest,
    deployer: DeploymentTool = Depends(get_deployment_tool),
) -> dict[str, Any]:
    """Deploy services via Docker Compose and return the deployment result."""
    config = DockerDeployConfig(
        compose_file=request.compose_file,
        services=request.services,
        env_file=request.env_file,
        project_dir=request.project_dir,
        build=request.build,
    )
    result = await deployer.deploy_docker(config)
    return {
        "success": result.succeeded,
        "deployment_id": result.deployment_id,
        "status": result.status,
        "health_url": result.health_url,
        "rollback_available": result.rollback_available,
        "logs": result.logs[-2000:],  # Return last 2000 chars to avoid huge payloads.
        "error": result.error,
    }


@router.post(
    "/deploy/vercel",
    summary="Deploy to Vercel",
    description="Deploy a frontend project to Vercel.",
)
async def deploy_vercel(
    request: VercelDeployRequest,
    deployer: DeploymentTool = Depends(get_deployment_tool),
) -> dict[str, Any]:
    """Deploy to Vercel and return the deployment URL."""
    config = VercelDeployConfig(
        project_dir=request.project_dir,
        token=request.token,
        production=request.production,
    )
    result = await deployer.deploy_vercel(config)
    return {
        "success": result.succeeded,
        "deployment_id": result.deployment_id,
        "url": result.url,
        "status": result.status,
        "rollback_available": result.rollback_available,
        "logs": result.logs[-2000:],
        "error": result.error,
    }


@router.post(
    "/deploy/railway",
    summary="Deploy to Railway",
    description="Deploy a project to Railway PaaS.",
)
async def deploy_railway(
    request: RailwayDeployRequest,
    deployer: DeploymentTool = Depends(get_deployment_tool),
) -> dict[str, Any]:
    """Deploy to Railway and return the deployment result."""
    config = RailwayDeployConfig(
        service=request.service,
        environment=request.environment,
        project_dir=request.project_dir,
    )
    result = await deployer.deploy_railway(config)
    return {
        "success": result.succeeded,
        "deployment_id": result.deployment_id,
        "url": result.url,
        "status": result.status,
        "rollback_available": result.rollback_available,
        "logs": result.logs[-2000:],
        "error": result.error,
    }


# --------------------------------------------------------------------------- #
# Docker management endpoints
# --------------------------------------------------------------------------- #


@router.get(
    "/docker/containers",
    summary="List Docker containers",
    description="List all running Docker containers managed by FORGE.",
)
async def list_containers(
    all_containers: bool = Query(default=False, description="Include stopped containers."),
    filter_label: str | None = Query(default=None, description="Filter by label key=value."),
    manager: DockerManager = Depends(get_docker_manager),
) -> dict[str, Any]:
    """Return a list of Docker containers."""
    try:
        containers = manager.list_containers(
            all_containers=all_containers,
            filter_label=filter_label,
        )
        return {
            "success": True,
            "containers": [
                {
                    "id": c.container_id[:12],
                    "name": c.name,
                    "status": c.status,
                    "image": c.image,
                }
                for c in containers
            ],
            "count": len(containers),
        }
    except ToolError as exc:
        return _handle_tool_error(exc, "docker", "list_containers")


@router.post(
    "/docker/containers/{container_id}/stop",
    summary="Stop a Docker container",
    description="Gracefully stop a running Docker container.",
)
async def stop_container(
    container_id: str,
    timeout: int = Query(default=10, ge=1, le=60),
    manager: DockerManager = Depends(get_docker_manager),
) -> dict[str, Any]:
    """Stop a Docker container by ID."""
    try:
        manager.stop_container(container_id=container_id, timeout=timeout)
        return {"success": True, "container_id": container_id, "action": "stopped"}
    except ToolError as exc:
        return _handle_tool_error(exc, "docker", "stop_container")


@router.delete(
    "/docker/containers/{container_id}",
    summary="Remove a Docker container",
    description="Remove a stopped Docker container.",
)
async def remove_container(
    container_id: str,
    force: bool = Query(default=False, description="Force remove a running container."),
    manager: DockerManager = Depends(get_docker_manager),
) -> dict[str, Any]:
    """Remove a Docker container."""
    try:
        manager.remove_container(container_id=container_id, force=force)
        return {"success": True, "container_id": container_id, "action": "removed"}
    except ToolError as exc:
        return _handle_tool_error(exc, "docker", "remove_container")


@router.get(
    "/docker/containers/{container_id}/logs",
    summary="Get container logs",
    description="Fetch log output from a running or stopped Docker container.",
)
async def get_container_logs(
    container_id: str,
    tail: int = Query(default=100, ge=1, le=10000),
    manager: DockerManager = Depends(get_docker_manager),
) -> dict[str, Any]:
    """Return recent log output from a Docker container."""
    try:
        logs = manager.get_container_logs(container_id=container_id, tail=tail)
        return {"success": True, "container_id": container_id, "logs": logs}
    except ToolError as exc:
        return _handle_tool_error(exc, "docker", "get_container_logs")


# --------------------------------------------------------------------------- #
# Audit trail endpoint
# --------------------------------------------------------------------------- #


@router.get(
    "/audit",
    summary="Get tool execution audit trail",
    description="Return recent tool execution records for a project.",
)
async def get_audit_trail(
    project_id: str = Query(..., description="FORGE project ID."),
    limit: int = Query(
        default=50, ge=1, le=500, description="Maximum number of records to return."
    ),
) -> dict[str, Any]:
    """Return the audit trail for tool executions in a project.

    In the current implementation, audit records are stored in-process
    (not persisted to a database).  A production implementation would
    query a PostgreSQL ``tool_executions`` table or a time-series store.
    """
    # NOTE: Real implementation queries the database using SQLAlchemy.
    # The schema would be:
    #   SELECT * FROM tool_executions
    #   WHERE project_id = :project_id
    #   ORDER BY created_at DESC
    #   LIMIT :limit
    #
    # Returning an empty list with a message so the endpoint is functional
    # and can be integrated with real storage when the ORM model is added.
    return {
        "project_id": project_id,
        "limit": limit,
        "records": [],
        "message": (
            "Audit trail persistence requires a 'tool_executions' ORM model "
            "to be added to the database schema.  Execution results are "
            "currently logged via the structured logging pipeline."
        ),
    }


# --------------------------------------------------------------------------- #
# Health check for the tools subsystem
# --------------------------------------------------------------------------- #


@router.get(
    "/health",
    summary="Tools subsystem health",
    description="Check availability of all underlying tool backends.",
)
async def tools_health() -> dict[str, Any]:
    """Return the health status of each tool backend."""
    docker_manager = DockerManager()
    docker_available = docker_manager.ping()

    github_client = GitHubClient()
    github_ok = False
    try:
        rate_limit = await github_client.get_rate_limit()
        core = rate_limit.get("rate", rate_limit.get("resources", {}).get("core", {}))
        github_ok = core.get("remaining", 0) > 0
    except Exception:
        pass
    finally:
        await github_client.close()

    return {
        "status": "ok" if github_ok or docker_available else "degraded",
        "backends": {
            "github": "ok" if github_ok else "unavailable",
            "docker": "ok" if docker_available else "unavailable",
            "terminal": "ok",  # Always available (subprocess-based)
        },
    }
