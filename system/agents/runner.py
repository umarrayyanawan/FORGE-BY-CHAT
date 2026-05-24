"""AgentRunner — orchestrates agent lifecycle for a single TaskNode.

The runner is responsible for:
  1. Resolving the correct specialist agent from the registry.
  2. Building the AgentContract by calling ``agent.build_contract()``.
  3. Loading scoped file context from disk (capped at SCOPED_CONTEXT_MAX_FILES).
  4. Assembling the AgentContext and invoking ``agent.execute()``.
  5. Writing generated and modified files back to disk.
  6. Deleting files flagged by the agent for removal.

It does NOT handle retry logic or event publishing — those are the
responsibility of the Orchestrator layer above this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from system.agents.base import AgentContext, AgentResult
from system.agents.registry import AgentRegistry
from system.core.orchestration.task_schemas import TaskNode
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.constants import SCOPED_CONTEXT_MAX_FILES
from system.shared.exceptions import AgentError

logger = get_logger(__name__)

# Extensions whose contents are safe/useful to inject into agent context.
_READABLE_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".ts", ".tsx", ".yaml", ".yml", ".json", ".md", ".toml", ".tf", ".sh", ".sql"}
)


class AgentRunner:
    """Orchestrates the execution of a specialist agent for one TaskNode.

    Parameters
    ----------
    registry:
        The AgentRegistry used to resolve agent instances.
    project_root:
        Path to the project root directory.  All relative file paths emitted
        by agents are resolved relative to this directory.
    """

    def __init__(self, registry: AgentRegistry, project_root: str = ".") -> None:
        """Initialise the runner with a registry and project root.

        Parameters
        ----------
        registry:
            AgentRegistry from which to retrieve agent instances.
        project_root:
            Filesystem path to the project root.  Defaults to the current
            working directory.
        """
        self.registry = registry
        self.project_root = Path(project_root).resolve()

    # ---------------------------------------------------------------------- #
    # Public interface
    # ---------------------------------------------------------------------- #

    async def run_task(
        self,
        task: TaskNode,
        spec: Optional[ProjectSpec] = None,
        arch: Optional[ArchitecturePlan] = None,
    ) -> AgentResult:
        """Execute the appropriate specialist agent for the given task.

        Full lifecycle:
          1. Retrieve the agent from the registry.
          2. Build the contract.
          3. Load scoped file context from disk.
          4. Build the AgentContext.
          5. Invoke ``agent.execute()``.
          6. Write outputs to disk on success.

        Parameters
        ----------
        task:
            The TaskNode carrying task description, expected artifacts, phase.
        spec:
            Optional ProjectSpec for global project context.
        arch:
            Optional ArchitecturePlan for service topology context.

        Returns
        -------
        AgentResult
            Structured result carrying generated files, reasoning, errors, etc.

        Raises
        ------
        AgentError
            If the agent type is not registered, or the agent raises during
            execution.
        """
        agent = self.registry.get(task.agent_type)
        contract = agent.build_contract(task, spec, arch)
        scoped_files = await self.load_scoped_context(contract, task)

        context = AgentContext(
            task=task,
            contract=contract,
            scoped_files=scoped_files,
            spec=spec,
            arch_plan=arch,
            token_budget=contract.max_tokens,
        )

        logger.info(
            "runner_executing_task",
            agent=task.agent_type,
            task_id=task.task_id,
            task_name=task.name,
            files_in_context=len(scoped_files),
            token_budget=contract.max_tokens,
        )

        result = await agent.execute(context)

        if result.success:
            await self.write_outputs(result)
            logger.info(
                "runner_task_complete",
                task_id=task.task_id,
                generated=len(result.generated_files),
                modified=len(result.modifications),
                deleted=len(result.deleted_files),
                tokens_used=result.tokens_used,
                duration_ms=result.duration_ms,
            )
        else:
            logger.warning(
                "runner_task_failed",
                task_id=task.task_id,
                errors=result.errors,
            )

        return result

    # ---------------------------------------------------------------------- #
    # Context loading
    # ---------------------------------------------------------------------- #

    async def load_scoped_context(
        self,
        contract: object,
        task: TaskNode,
    ) -> dict[str, str]:
        """Load file contents for all patterns in the contract's allowed_files.

        Uses ``Path.glob()`` to expand each pattern.  Only files with
        recognised code/config extensions are read.  The total count of
        loaded files is capped at ``SCOPED_CONTEXT_MAX_FILES``.

        Parameters
        ----------
        contract:
            The AgentContract whose ``allowed_files`` list to expand.
        task:
            The TaskNode (used for debug logging only).

        Returns
        -------
        dict[str, str]
            Mapping of ``{relative_path: file_contents}`` for all matched
            files that could be read successfully.
        """
        loaded: dict[str, str] = {}
        allowed_patterns: list[str] = getattr(contract, "allowed_files", [])

        for pattern in allowed_patterns:
            # Strip leading slash so Path.glob() works relative to project root
            normalised = pattern.lstrip("/")
            try:
                matched = sorted(self.project_root.glob(normalised))
            except (ValueError, OSError) as exc:
                logger.warning(
                    "glob_pattern_error",
                    pattern=pattern,
                    error=str(exc),
                )
                continue

            for path in matched:
                if len(loaded) >= SCOPED_CONTEXT_MAX_FILES:
                    logger.debug(
                        "scoped_context_cap_reached",
                        cap=SCOPED_CONTEXT_MAX_FILES,
                        task_id=task.task_id,
                    )
                    return loaded

                if not path.is_file():
                    continue

                if path.suffix not in _READABLE_EXTENSIONS:
                    continue

                rel_path = str(path.relative_to(self.project_root))

                if rel_path in loaded:
                    # Already loaded by an earlier overlapping pattern
                    continue

                try:
                    content = path.read_text(encoding="utf-8")
                    loaded[rel_path] = content
                except (OSError, UnicodeDecodeError) as exc:
                    logger.warning(
                        "file_read_error",
                        path=rel_path,
                        error=str(exc),
                    )

        logger.debug(
            "scoped_context_loaded",
            task_id=task.task_id,
            file_count=len(loaded),
            paths=list(loaded.keys()),
        )
        return loaded

    # ---------------------------------------------------------------------- #
    # Output writing
    # ---------------------------------------------------------------------- #

    async def write_outputs(self, result: AgentResult) -> None:
        """Persist the agent's output to disk.

        Writes all new files from ``result.generated_files``, overwrites
        existing files from ``result.modifications``, and removes files
        listed in ``result.deleted_files``.

        Parameters
        ----------
        result:
            The AgentResult produced by a successful agent execution.

        Raises
        ------
        AgentError
            If a file cannot be written due to an OS-level error.
        """
        # Write generated (new) files
        for rel_path, content in result.generated_files.items():
            full_path = self.project_root / rel_path
            try:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")
                logger.debug("file_written", path=rel_path)
            except OSError as exc:
                raise AgentError(
                    message=f"Failed to write generated file {rel_path!r}: {exc}",
                    details={"path": rel_path, "error": str(exc)},
                ) from exc

        # Write modified (overwritten) files
        for rel_path, content in result.modifications.items():
            full_path = self.project_root / rel_path
            if not full_path.exists():
                logger.warning(
                    "modification_target_missing",
                    path=rel_path,
                    action="creating_as_new",
                )
            try:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")
                logger.debug("file_modified", path=rel_path)
            except OSError as exc:
                raise AgentError(
                    message=f"Failed to write modified file {rel_path!r}: {exc}",
                    details={"path": rel_path, "error": str(exc)},
                ) from exc

        # Delete flagged files
        for rel_path in result.deleted_files:
            full_path = self.project_root / rel_path
            if full_path.exists():
                try:
                    full_path.unlink()
                    logger.info("file_deleted", path=rel_path)
                except OSError as exc:
                    logger.warning(
                        "file_delete_error",
                        path=rel_path,
                        error=str(exc),
                    )
            else:
                logger.debug("file_delete_skipped_not_found", path=rel_path)
