"""CI/CD pipeline builder — generates GitHub Actions workflows for FORGE projects."""

from __future__ import annotations

from typing import Any, Dict, Optional

from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.models import DeployTarget

logger = get_logger(__name__)


class CICDBuilder:
    """Generates and optionally pushes GitHub Actions CI/CD workflows.

    Args:
        github_client: Optional GitHub API wrapper with a
            ``commit_files(repo, branch, files, message)`` coroutine.
            When omitted, ``setup_github_actions`` returns the YAML strings
            without pushing them (dry-run behaviour useful for testing).
    """

    def __init__(self, github_client: Optional[Any] = None) -> None:
        self.github = github_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def setup_github_actions(
        self,
        repo: str,
        project_spec: ProjectSpec,
        deploy_target: DeployTarget = DeployTarget.DOCKER,
    ) -> Dict[str, str]:
        """Generate and (if a client is available) push GitHub Actions workflows.

        Returns a mapping of file path → YAML content.
        """
        ci_yaml = self._generate_ci_workflow(project_spec)
        deploy_yaml = self._generate_deploy_workflow(project_spec, deploy_target)
        dependabot_yaml = self._generate_dependabot_config()

        files: Dict[str, str] = {
            ".github/workflows/ci.yml": ci_yaml,
            ".github/workflows/deploy.yml": deploy_yaml,
            ".github/dependabot.yml": dependabot_yaml,
        }

        if self.github:
            await self.github.commit_files(
                repo,
                "main",
                files,
                "ci: add GitHub Actions CI/CD and Dependabot workflows",
            )
            logger.info(
                "GitHub Actions workflows pushed",
                repo=repo,
                files=list(files.keys()),
            )
        else:
            logger.info(
                "CICDBuilder has no github_client — workflows generated but not pushed",
                file_count=len(files),
            )

        return files

    # ------------------------------------------------------------------
    # CI workflow
    # ------------------------------------------------------------------

    def _generate_ci_workflow(self, spec: ProjectSpec) -> str:
        return """name: CI

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main, develop]

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint:
    name: Lint & type-check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"
      - name: Install dev dependencies
        run: pip install -e ".[dev]"
      - name: Ruff lint
        run: ruff check . --output-format=github
      - name: Ruff format check
        run: ruff format --check .
      - name: Mypy type-check
        run: mypy system/ --ignore-missing-imports --show-error-codes

  test:
    name: Unit & integration tests
    runs-on: ubuntu-latest
    needs: lint
    services:
      postgres:
        image: ankane/pgvector:latest
        env:
          POSTGRES_USER: forge
          POSTGRES_PASSWORD: forge_test_pw
          POSTGRES_DB: forge_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"
      - name: Install dependencies
        run: pip install -e ".[dev]"
      - name: Run Alembic migrations
        env:
          DATABASE_URL: postgresql+asyncpg://forge:forge_test_pw@localhost:5432/forge_test
          DATABASE_SYNC_URL: postgresql+psycopg2://forge:forge_test_pw@localhost:5432/forge_test
          REDIS_URL: redis://localhost:6379/0
          SECRET_KEY: test-secret-key-32-chars-minimum-len
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY || 'test-key' }}
          NEO4J_PASSWORD: test
        run: alembic upgrade head
      - name: Pytest
        env:
          DATABASE_URL: postgresql+asyncpg://forge:forge_test_pw@localhost:5432/forge_test
          DATABASE_SYNC_URL: postgresql+psycopg2://forge:forge_test_pw@localhost:5432/forge_test
          REDIS_URL: redis://localhost:6379/0
          SECRET_KEY: test-secret-key-32-chars-minimum-len
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY || 'test-key' }}
          NEO4J_PASSWORD: test
        run: |
          pytest tests/ -v \\
            --cov=system \\
            --cov-report=xml \\
            --cov-report=term-missing \\
            --tb=short \\
            -q
      - name: Upload coverage
        uses: codecov/codecov-action@v4
        if: always()
        with:
          token: ${{ secrets.CODECOV_TOKEN }}
          files: ./coverage.xml
          fail_ci_if_error: false

  security:
    name: Security scan
    runs-on: ubuntu-latest
    needs: lint
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"
      - name: Install bandit
        run: pip install bandit[toml]
      - name: Bandit security scan
        run: bandit -r system/ -c pyproject.toml -ll
"""

    # ------------------------------------------------------------------
    # Deploy workflow
    # ------------------------------------------------------------------

    def _generate_deploy_workflow(self, spec: ProjectSpec, target: DeployTarget) -> str:
        if target == DeployTarget.KUBERNETES:
            return self._k8s_deploy_workflow(spec)
        if target == DeployTarget.VERCEL:
            return self._vercel_deploy_workflow(spec)
        if target == DeployTarget.RAILWAY:
            return self._railway_deploy_workflow(spec)
        # Default: Docker / generic
        return self._docker_deploy_workflow(spec)

    def _docker_deploy_workflow(self, spec: ProjectSpec) -> str:
        return """name: Deploy (Docker)

on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      environment:
        description: "Target environment"
        required: true
        default: "staging"
        type: choice
        options: [staging, production]

concurrency:
  group: deploy-${{ github.ref }}
  cancel-in-progress: false

jobs:
  build-and-push:
    name: Build & push Docker images
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    outputs:
      image_tag: ${{ steps.meta.outputs.version }}
    steps:
      - uses: actions/checkout@v4
      - name: Docker meta
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/${{ github.repository }}
          tags: |
            type=sha,prefix=,suffix=,format=short
            type=ref,event=branch
            type=semver,pattern={{version}}
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3
      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Build and push API image
        uses: docker/build-push-action@v5
        with:
          context: .
          file: infra/docker/Dockerfile.api
          push: true
          tags: ${{ steps.meta.outputs.tags }}-api
          cache-from: type=gha
          cache-to: type=gha,mode=max
      - name: Build and push Worker image
        uses: docker/build-push-action@v5
        with:
          context: .
          file: infra/docker/Dockerfile.worker
          push: true
          tags: ${{ steps.meta.outputs.tags }}-worker
          cache-from: type=gha
          cache-to: type=gha,mode=max

  deploy-staging:
    name: Deploy to Staging
    runs-on: ubuntu-latest
    needs: build-and-push
    environment: staging
    steps:
      - uses: actions/checkout@v4
      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.STAGING_HOST }}
          username: ${{ secrets.STAGING_USER }}
          key: ${{ secrets.STAGING_SSH_KEY }}
          script: |
            export IMAGE_TAG=${{ needs.build-and-push.outputs.image_tag }}
            cd /opt/forge
            docker-compose pull
            docker-compose up -d --remove-orphans
            docker-compose exec -T api alembic upgrade head
      - name: Smoke test
        run: |
          sleep 10
          curl -f ${{ secrets.STAGING_URL }}/health || exit 1

  deploy-production:
    name: Deploy to Production
    runs-on: ubuntu-latest
    needs: [build-and-push, deploy-staging]
    environment: production
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    steps:
      - uses: actions/checkout@v4
      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.PROD_HOST }}
          username: ${{ secrets.PROD_USER }}
          key: ${{ secrets.PROD_SSH_KEY }}
          script: |
            export IMAGE_TAG=${{ needs.build-and-push.outputs.image_tag }}
            cd /opt/forge
            docker-compose pull
            docker-compose up -d --no-deps --remove-orphans api worker
            docker-compose exec -T api alembic upgrade head
      - name: Smoke test production
        run: |
          sleep 15
          curl -f ${{ secrets.PROD_URL }}/health || exit 1
"""

    def _k8s_deploy_workflow(self, spec: ProjectSpec) -> str:
        return """name: Deploy (Kubernetes)

on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  deploy:
    name: Deploy to Kubernetes
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - name: Configure kubectl
        uses: azure/k8s-set-context@v3
        with:
          method: kubeconfig
          kubeconfig: ${{ secrets.KUBECONFIG }}
      - name: Set image tag
        id: tag
        run: echo "TAG=$(git rev-parse --short HEAD)" >> $GITHUB_OUTPUT
      - name: Apply manifests
        run: |
          kubectl apply -f infra/k8s/
          kubectl set image deployment/forge-api \\
            app=ghcr.io/${{ github.repository }}:${{ steps.tag.outputs.TAG }}-api \\
            -n forge-system
          kubectl set image deployment/forge-worker \\
            app=ghcr.io/${{ github.repository }}:${{ steps.tag.outputs.TAG }}-worker \\
            -n forge-system
          kubectl rollout status deployment/forge-api -n forge-system --timeout=5m
          kubectl rollout status deployment/forge-worker -n forge-system --timeout=5m
"""

    def _vercel_deploy_workflow(self, spec: ProjectSpec) -> str:
        return """name: Deploy (Vercel)

on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  deploy-vercel:
    name: Deploy frontend to Vercel
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: "npm"
          cache-dependency-path: frontend/package-lock.json
      - name: Install dependencies
        run: npm ci
        working-directory: frontend
      - name: Deploy to Vercel
        run: npx vercel --prod --yes --token ${{ secrets.VERCEL_TOKEN }}
        working-directory: frontend
        env:
          VERCEL_ORG_ID: ${{ secrets.VERCEL_ORG_ID }}
          VERCEL_PROJECT_ID: ${{ secrets.VERCEL_PROJECT_ID }}
"""

    def _railway_deploy_workflow(self, spec: ProjectSpec) -> str:
        return """name: Deploy (Railway)

on:
  push:
    branches: [main]

jobs:
  deploy-railway:
    name: Deploy to Railway
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - name: Install Railway CLI
        run: npm install -g @railway/cli
      - name: Deploy
        run: railway up --service api
        env:
          RAILWAY_TOKEN: ${{ secrets.RAILWAY_TOKEN }}
"""

    # ------------------------------------------------------------------
    # Dependabot config
    # ------------------------------------------------------------------

    def _generate_dependabot_config(self) -> str:
        return """version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
    groups:
      python-deps:
        patterns: ["*"]

  - package-ecosystem: "npm"
    directory: "/frontend"
    schedule:
      interval: "weekly"
      day: "monday"
    groups:
      npm-deps:
        patterns: ["*"]

  - package-ecosystem: "docker"
    directory: "/infra/docker"
    schedule:
      interval: "weekly"

  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
"""
