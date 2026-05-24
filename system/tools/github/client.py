"""GitHub REST API client for FORGE using httpx (not PyGitHub).

Provides full repository, branch, file-commit, pull-request, and
Actions workflow operations needed by the autonomous agent pipeline.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import httpx

from system.config.settings import settings
from system.observability.logging.logger import get_logger
from system.shared.exceptions import ToolError

logger = get_logger(__name__)


class GitHubClient:
    """Async GitHub API client backed by httpx.

    All methods raise ``ToolError`` on API failures so callers can
    catch a single exception type regardless of the underlying HTTP
    status code.

    Args:
        token: Personal access token or GitHub App installation token.
               Defaults to ``settings.github_token``.
    """

    BASE_URL = "https://api.github.com"

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token or settings.github_token
        self.headers: Dict[str, str] = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers=self.headers,
            timeout=30.0,
        )

    # ---------------------------------------------------------------------- #
    # Internal request helper
    # ---------------------------------------------------------------------- #

    async def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        """Execute an authenticated HTTP request and return parsed JSON.

        Raises:
            ToolError: On HTTP 4xx / 5xx responses or connection failures.
        """
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise ToolError(
                f"GitHub network error: {exc}",
                "GITHUB_NETWORK_ERROR",
                {"path": path, "method": method},
            ) from exc

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            raise ToolError(
                f"GitHub rate limit hit. Retry after {retry_after}s",
                "RATE_LIMIT",
                {"retry_after": retry_after},
            )

        if response.status_code == 404:
            raise ToolError(
                f"GitHub resource not found: {path}",
                "GITHUB_NOT_FOUND",
                {"path": path},
            )

        if response.status_code == 422:
            body = response.text[:500]
            raise ToolError(
                f"GitHub validation failed: {body}",
                "GITHUB_VALIDATION_ERROR",
                {"path": path, "body": body},
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ToolError(
                f"GitHub API error: {exc.response.status_code} {exc.response.text[:300]}",
                "GITHUB_ERROR",
                {"status_code": exc.response.status_code, "path": path},
            ) from exc

        if not response.content:
            return {}
        try:
            return response.json()
        except Exception as exc:
            raise ToolError(
                f"GitHub returned non-JSON response: {response.text[:200]}",
                "GITHUB_PARSE_ERROR",
            ) from exc

    # ---------------------------------------------------------------------- #
    # Repository operations
    # ---------------------------------------------------------------------- #

    async def create_repo(
        self,
        name: str,
        description: str = "",
        private: bool = True,
        org: Optional[str] = None,
        auto_init: bool = True,
        default_branch: str = "main",
        topics: Optional[List[str]] = None,
        homepage: str = "",
        has_issues: bool = True,
        has_wiki: bool = False,
        has_projects: bool = False,
        allow_squash_merge: bool = True,
        allow_merge_commit: bool = False,
        allow_rebase_merge: bool = False,
        delete_branch_on_merge: bool = True,
    ) -> Dict[str, Any]:
        """Create a new GitHub repository.

        Args:
            name: Repository name (must be unique within the owner/org).
            description: Short description shown on GitHub.
            private: Whether the repo is private.
            org: Organisation to create under; if None uses authenticated user.
            auto_init: Initialise with a default README.
            default_branch: Default branch name.
            topics: List of topic tags to apply.
            homepage: Project URL.
            has_issues / has_wiki / has_projects: Feature flags.
            allow_squash_merge / allow_merge_commit / allow_rebase_merge:
                Permitted merge strategies.
            delete_branch_on_merge: Auto-delete head branch after merge.

        Returns:
            Full GitHub API repo object.
        """
        endpoint = f"/orgs/{org}/repos" if org else "/user/repos"
        payload: Dict[str, Any] = {
            "name": name,
            "description": description,
            "private": private,
            "auto_init": auto_init,
            "default_branch": default_branch,
            "homepage": homepage,
            "has_issues": has_issues,
            "has_wiki": has_wiki,
            "has_projects": has_projects,
            "allow_squash_merge": allow_squash_merge,
            "allow_merge_commit": allow_merge_commit,
            "allow_rebase_merge": allow_rebase_merge,
            "delete_branch_on_merge": delete_branch_on_merge,
        }
        result = await self._request("POST", endpoint, json=payload)
        if topics:
            await self.replace_topics(result["full_name"], topics)
        logger.info("GitHub repo created", repo=result.get("full_name"), private=private)
        return result

    async def get_repo(self, repo: str) -> Dict[str, Any]:
        """Fetch repository metadata.

        Args:
            repo: Full repo slug, e.g. ``"owner/repo-name"``.
        """
        return await self._request("GET", f"/repos/{repo}")

    async def delete_repo(self, repo: str) -> None:
        """Delete a repository (irreversible).

        Args:
            repo: Full repo slug.
        """
        await self._request("DELETE", f"/repos/{repo}")
        logger.warning("GitHub repo deleted", repo=repo)

    async def replace_topics(self, repo: str, topics: List[str]) -> Dict[str, Any]:
        """Replace all topics on a repository."""
        return await self._request(
            "PUT",
            f"/repos/{repo}/topics",
            json={"names": topics},
        )

    # ---------------------------------------------------------------------- #
    # Branch operations
    # ---------------------------------------------------------------------- #

    async def create_branch(
        self,
        repo: str,
        branch_name: str,
        from_branch: str = "main",
    ) -> Dict[str, Any]:
        """Create a new branch from an existing branch's tip.

        Args:
            repo: Full repo slug.
            branch_name: Name for the new branch.
            from_branch: Source branch to branch from.

        Returns:
            GitHub API git ref object.
        """
        ref_data = await self._request(
            "GET",
            f"/repos/{repo}/git/ref/heads/{from_branch}",
        )
        sha = ref_data["object"]["sha"]
        result = await self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": sha},
        )
        logger.info("Branch created", repo=repo, branch=branch_name, from_branch=from_branch)
        return result

    async def delete_branch(self, repo: str, branch_name: str) -> None:
        """Delete a branch.

        Args:
            repo: Full repo slug.
            branch_name: Branch to delete.
        """
        await self._request("DELETE", f"/repos/{repo}/git/refs/heads/{branch_name}")
        logger.info("Branch deleted", repo=repo, branch=branch_name)

    async def list_branches(self, repo: str) -> List[str]:
        """Return a list of branch names for the repository.

        Args:
            repo: Full repo slug.
        """
        data = await self._request("GET", f"/repos/{repo}/branches", params={"per_page": 100})
        return [b["name"] for b in data]

    async def get_branch(self, repo: str, branch: str) -> Dict[str, Any]:
        """Fetch branch details including protection status and latest commit."""
        return await self._request("GET", f"/repos/{repo}/branches/{branch}")

    # ---------------------------------------------------------------------- #
    # File & commit operations
    # ---------------------------------------------------------------------- #

    async def commit_files(
        self,
        repo: str,
        branch: str,
        files: Dict[str, str],
        message: str,
        author_name: str = "FORGE Bot",
        author_email: str = "forge-bot@forge.ai",
    ) -> Dict[str, Any]:
        """Commit multiple files to a branch in a single atomic commit.

        Uses the Git Data API to avoid any local git operations.

        Args:
            repo: Full repo slug.
            branch: Target branch name.
            files: Mapping of ``{file_path: file_content}`` to write.
            message: Commit message.
            author_name: Git author name shown in the commit.
            author_email: Git author email shown in the commit.

        Returns:
            GitHub API commit object.
        """
        # 1. Resolve the current HEAD SHA for the branch.
        ref = await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        base_sha: str = ref["object"]["sha"]

        # 2. Fetch the base tree SHA from the parent commit.
        commit = await self._request("GET", f"/repos/{repo}/git/commits/{base_sha}")
        base_tree_sha: str = commit["tree"]["sha"]

        # 3. Create a blob for each file.
        tree_items: List[Dict[str, Any]] = []
        for path, content in files.items():
            blob = await self._request(
                "POST",
                f"/repos/{repo}/git/blobs",
                json={
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                },
            )
            tree_items.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        # 4. Create a new tree referencing the base.
        tree = await self._request(
            "POST",
            f"/repos/{repo}/git/trees",
            json={"base_tree": base_tree_sha, "tree": tree_items},
        )

        # 5. Create the commit object.
        new_commit = await self._request(
            "POST",
            f"/repos/{repo}/git/commits",
            json={
                "message": message,
                "tree": tree["sha"],
                "parents": [base_sha],
                "author": {"name": author_name, "email": author_email},
                "committer": {"name": author_name, "email": author_email},
            },
        )

        # 6. Move the branch ref to point at the new commit.
        await self._request(
            "PATCH",
            f"/repos/{repo}/git/refs/heads/{branch}",
            json={"sha": new_commit["sha"]},
        )

        logger.info(
            "Files committed",
            repo=repo,
            branch=branch,
            commit_sha=new_commit["sha"][:7],
            num_files=len(files),
        )
        return new_commit

    async def get_file_content(
        self,
        repo: str,
        path: str,
        ref: str = "main",
    ) -> str:
        """Fetch and decode a single file from the repository.

        Args:
            repo: Full repo slug.
            path: File path within the repository.
            ref: Branch, tag, or commit SHA.

        Returns:
            UTF-8 decoded file content as a string.
        """
        data = await self._request(
            "GET",
            f"/repos/{repo}/contents/{path}",
            params={"ref": ref},
        )
        if isinstance(data, list):
            raise ToolError(f"Path '{path}' is a directory, not a file", "NOT_A_FILE")
        encoding = data.get("encoding", "base64")
        if encoding != "base64":
            raise ToolError(f"Unexpected encoding '{encoding}' for {path}", "UNKNOWN_ENCODING")
        return base64.b64decode(data["content"]).decode("utf-8")

    async def update_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str = "main",
        sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or update a single file using the Contents API.

        Args:
            repo: Full repo slug.
            path: File path within the repository.
            content: New file content (UTF-8 string).
            message: Commit message.
            branch: Target branch.
            sha: Current blob SHA (required for updates; omit for new files).

        Returns:
            GitHub API response containing commit and content objects.
        """
        if sha is None:
            try:
                existing = await self._request(
                    "GET", f"/repos/{repo}/contents/{path}", params={"ref": branch}
                )
                sha = existing.get("sha")
            except ToolError:
                pass  # File doesn't exist yet — create it

        payload: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        return await self._request("PUT", f"/repos/{repo}/contents/{path}", json=payload)

    async def delete_file(
        self,
        repo: str,
        path: str,
        message: str,
        branch: str = "main",
        sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Delete a file from the repository.

        Args:
            repo: Full repo slug.
            path: File path within the repository.
            message: Commit message for the deletion.
            branch: Target branch.
            sha: Current blob SHA (fetched automatically if not provided).
        """
        if sha is None:
            existing = await self._request(
                "GET", f"/repos/{repo}/contents/{path}", params={"ref": branch}
            )
            sha = existing["sha"]

        return await self._request(
            "DELETE",
            f"/repos/{repo}/contents/{path}",
            json={"message": message, "sha": sha, "branch": branch},
        )

    # ---------------------------------------------------------------------- #
    # Pull request operations
    # ---------------------------------------------------------------------- #

    async def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool = False,
        maintainer_can_modify: bool = True,
    ) -> Dict[str, Any]:
        """Open a pull request.

        Args:
            repo: Full repo slug.
            title: PR title.
            body: PR description (Markdown).
            head: Source branch name (or ``owner:branch`` for forks).
            base: Target branch name.
            draft: Open as a draft PR if True.
            maintainer_can_modify: Allow maintainers to push to the head branch.

        Returns:
            GitHub API pull request object.
        """
        result = await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "draft": draft,
                "maintainer_can_modify": maintainer_can_modify,
            },
        )
        logger.info("Pull request created", repo=repo, pr_number=result.get("number"), head=head, base=base)
        return result

    async def get_pull_request(self, repo: str, pr_number: int) -> Dict[str, Any]:
        """Fetch pull request details."""
        return await self._request("GET", f"/repos/{repo}/pulls/{pr_number}")

    async def list_pull_requests(
        self,
        repo: str,
        state: str = "open",
        base: Optional[str] = None,
        per_page: int = 30,
    ) -> List[Dict[str, Any]]:
        """List pull requests for a repository.

        Args:
            repo: Full repo slug.
            state: ``"open"`` | ``"closed"`` | ``"all"``.
            base: Filter by base branch name.
            per_page: Number of results per page (max 100).
        """
        params: Dict[str, Any] = {"state": state, "per_page": per_page}
        if base:
            params["base"] = base
        return await self._request("GET", f"/repos/{repo}/pulls", params=params)

    async def merge_pull_request(
        self,
        repo: str,
        pr_number: int,
        merge_method: str = "squash",
        commit_title: Optional[str] = None,
        commit_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Merge a pull request.

        Args:
            repo: Full repo slug.
            pr_number: PR number.
            merge_method: ``"merge"`` | ``"squash"`` | ``"rebase"``.
            commit_title: Optional merge commit title (squash/merge only).
            commit_message: Optional merge commit message.

        Returns:
            GitHub API merge result object.
        """
        payload: Dict[str, Any] = {"merge_method": merge_method}
        if commit_title:
            payload["commit_title"] = commit_title
        if commit_message:
            payload["commit_message"] = commit_message

        result = await self._request(
            "PUT",
            f"/repos/{repo}/pulls/{pr_number}/merge",
            json=payload,
        )
        logger.info("Pull request merged", repo=repo, pr_number=pr_number, method=merge_method)
        return result

    async def add_pr_labels(self, repo: str, pr_number: int, labels: List[str]) -> List[Dict[str, Any]]:
        """Add labels to a pull request."""
        return await self._request(
            "POST",
            f"/repos/{repo}/issues/{pr_number}/labels",
            json={"labels": labels},
        )

    async def add_pr_reviewers(
        self,
        repo: str,
        pr_number: int,
        reviewers: List[str],
        team_reviewers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Request reviewers for a pull request."""
        return await self._request(
            "POST",
            f"/repos/{repo}/pulls/{pr_number}/requested_reviewers",
            json={
                "reviewers": reviewers,
                "team_reviewers": team_reviewers or [],
            },
        )

    async def create_pr_comment(self, repo: str, pr_number: int, body: str) -> Dict[str, Any]:
        """Post a comment on a pull request."""
        return await self._request(
            "POST",
            f"/repos/{repo}/issues/{pr_number}/comments",
            json={"body": body},
        )

    # ---------------------------------------------------------------------- #
    # Issues
    # ---------------------------------------------------------------------- #

    async def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
        assignees: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create a GitHub issue."""
        return await self._request(
            "POST",
            f"/repos/{repo}/issues",
            json={
                "title": title,
                "body": body,
                "labels": labels or [],
                "assignees": assignees or [],
            },
        )

    async def close_issue(self, repo: str, issue_number: int) -> Dict[str, Any]:
        """Close an issue."""
        return await self._request(
            "PATCH",
            f"/repos/{repo}/issues/{issue_number}",
            json={"state": "closed"},
        )

    # ---------------------------------------------------------------------- #
    # GitHub Actions
    # ---------------------------------------------------------------------- #

    async def get_workflow_runs(
        self,
        repo: str,
        workflow_id: str,
        branch: Optional[str] = None,
        status: Optional[str] = None,
        per_page: int = 10,
    ) -> List[Dict[str, Any]]:
        """List workflow runs for a specific workflow file or ID.

        Args:
            repo: Full repo slug.
            workflow_id: Workflow file name (e.g. ``"ci.yml"``) or numeric ID.
            branch: Filter by branch name.
            status: Filter by status: ``"queued"`` | ``"in_progress"`` | ``"completed"``.
            per_page: Number of results to return.
        """
        params: Dict[str, Any] = {"per_page": per_page}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status

        data = await self._request(
            "GET",
            f"/repos/{repo}/actions/workflows/{workflow_id}/runs",
            params=params,
        )
        return data.get("workflow_runs", [])

    async def get_latest_workflow_run(
        self,
        repo: str,
        workflow_id: str,
        branch: str = "main",
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent workflow run for the given workflow and branch."""
        runs = await self.get_workflow_runs(repo, workflow_id, branch=branch, per_page=1)
        return runs[0] if runs else None

    async def trigger_workflow(
        self,
        repo: str,
        workflow_id: str,
        ref: str = "main",
        inputs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Trigger a workflow_dispatch event.

        Args:
            repo: Full repo slug.
            workflow_id: Workflow file name or numeric ID.
            ref: Branch, tag, or commit SHA to run the workflow from.
            inputs: Optional workflow_dispatch input values.
        """
        await self._request(
            "POST",
            f"/repos/{repo}/actions/workflows/{workflow_id}/dispatches",
            json={"ref": ref, "inputs": inputs or {}},
        )
        logger.info("Workflow triggered", repo=repo, workflow=workflow_id, ref=ref)

    async def list_workflow_run_jobs(self, repo: str, run_id: int) -> List[Dict[str, Any]]:
        """List jobs for a workflow run."""
        data = await self._request("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs")
        return data.get("jobs", [])

    async def cancel_workflow_run(self, repo: str, run_id: int) -> None:
        """Cancel a running workflow."""
        await self._request("POST", f"/repos/{repo}/actions/runs/{run_id}/cancel")

    # ---------------------------------------------------------------------- #
    # Repository secrets
    # ---------------------------------------------------------------------- #

    async def list_repo_secrets(self, repo: str) -> List[Dict[str, Any]]:
        """List repository Actions secrets (names only, not values)."""
        data = await self._request("GET", f"/repos/{repo}/actions/secrets")
        return data.get("secrets", [])

    # ---------------------------------------------------------------------- #
    # Commit & diff operations
    # ---------------------------------------------------------------------- #

    async def get_commit(self, repo: str, sha: str) -> Dict[str, Any]:
        """Fetch full details of a commit."""
        return await self._request("GET", f"/repos/{repo}/commits/{sha}")

    async def compare_commits(self, repo: str, base: str, head: str) -> Dict[str, Any]:
        """Compare two commits or branches and return the diff."""
        return await self._request("GET", f"/repos/{repo}/compare/{base}...{head}")

    async def list_commits(
        self,
        repo: str,
        branch: str = "main",
        per_page: int = 30,
    ) -> List[Dict[str, Any]]:
        """List commits on a branch."""
        return await self._request(
            "GET",
            f"/repos/{repo}/commits",
            params={"sha": branch, "per_page": per_page},
        )

    # ---------------------------------------------------------------------- #
    # Releases & tags
    # ---------------------------------------------------------------------- #

    async def create_release(
        self,
        repo: str,
        tag_name: str,
        name: str,
        body: str,
        draft: bool = False,
        prerelease: bool = False,
        target_commitish: str = "main",
    ) -> Dict[str, Any]:
        """Create a GitHub release.

        Args:
            repo: Full repo slug.
            tag_name: Tag to create (e.g. ``"v1.2.3"``).
            name: Release title.
            body: Release notes (Markdown).
            draft: Create as a draft release.
            prerelease: Mark as a pre-release.
            target_commitish: Branch or commit SHA the release is based on.
        """
        return await self._request(
            "POST",
            f"/repos/{repo}/releases",
            json={
                "tag_name": tag_name,
                "name": name,
                "body": body,
                "draft": draft,
                "prerelease": prerelease,
                "target_commitish": target_commitish,
            },
        )

    async def get_latest_release(self, repo: str) -> Dict[str, Any]:
        """Return the latest non-draft, non-prerelease release."""
        return await self._request("GET", f"/repos/{repo}/releases/latest")

    # ---------------------------------------------------------------------- #
    # Repository labels
    # ---------------------------------------------------------------------- #

    async def create_label(
        self,
        repo: str,
        name: str,
        color: str,
        description: str = "",
    ) -> Dict[str, Any]:
        """Create a label on the repository."""
        return await self._request(
            "POST",
            f"/repos/{repo}/labels",
            json={"name": name, "color": color, "description": description},
        )

    # ---------------------------------------------------------------------- #
    # Branch protection
    # ---------------------------------------------------------------------- #

    async def protect_branch(
        self,
        repo: str,
        branch: str,
        required_status_checks: Optional[List[str]] = None,
        require_pr_reviews: bool = True,
        dismiss_stale_reviews: bool = True,
        require_code_owner_reviews: bool = False,
        required_approving_review_count: int = 1,
        enforce_admins: bool = False,
    ) -> Dict[str, Any]:
        """Apply branch protection rules.

        Args:
            repo: Full repo slug.
            branch: Branch to protect.
            required_status_checks: List of required CI check names.
            require_pr_reviews: Require approved pull request reviews.
            dismiss_stale_reviews: Auto-dismiss approvals on new commits.
            require_code_owner_reviews: Require approval from code owners.
            required_approving_review_count: Minimum number of approvals.
            enforce_admins: Apply rules to repository administrators too.
        """
        payload: Dict[str, Any] = {
            "required_status_checks": (
                {"strict": True, "contexts": required_status_checks}
                if required_status_checks
                else None
            ),
            "enforce_admins": enforce_admins,
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": dismiss_stale_reviews,
                "require_code_owner_reviews": require_code_owner_reviews,
                "required_approving_review_count": required_approving_review_count,
            }
            if require_pr_reviews
            else None,
            "restrictions": None,
        }
        return await self._request(
            "PUT",
            f"/repos/{repo}/branches/{branch}/protection",
            json=payload,
        )

    # ---------------------------------------------------------------------- #
    # Webhooks
    # ---------------------------------------------------------------------- #

    async def create_webhook(
        self,
        repo: str,
        url: str,
        events: List[str],
        secret: Optional[str] = None,
        content_type: str = "json",
        active: bool = True,
    ) -> Dict[str, Any]:
        """Register a webhook on the repository.

        Args:
            repo: Full repo slug.
            url: Payload URL.
            events: List of event types, e.g. ``["push", "pull_request"]``.
            secret: Optional shared secret for HMAC signature validation.
            content_type: ``"json"`` or ``"form"``.
            active: Whether the webhook is active.
        """
        config: Dict[str, Any] = {"url": url, "content_type": content_type}
        if secret:
            config["secret"] = secret

        return await self._request(
            "POST",
            f"/repos/{repo}/hooks",
            json={"name": "web", "active": active, "events": events, "config": config},
        )

    # ---------------------------------------------------------------------- #
    # Authenticated user
    # ---------------------------------------------------------------------- #

    async def get_authenticated_user(self) -> Dict[str, Any]:
        """Return the currently authenticated GitHub user."""
        return await self._request("GET", "/user")

    async def get_rate_limit(self) -> Dict[str, Any]:
        """Return the current rate limit status for all API categories."""
        return await self._request("GET", "/rate_limit")

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def close(self) -> None:
        """Gracefully close the underlying httpx client."""
        await self.client.aclose()

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
