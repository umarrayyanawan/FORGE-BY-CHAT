"""Base agent class and contract system for the FORGE Agent Runtime.

Every specialist agent (backend, frontend, infra, qa, security, docs, refactor,
architect) inherits from ``BaseAgent`` and overrides the two abstract methods:
  - ``build_contract()`` — produce an ``AgentContract`` scoped to the task.
  - ``build_system_prompt()`` — produce the full system prompt from the contract.

The ``execute()`` method orchestrates the full agent lifecycle:
  1. Build the structured user message from the scoped context.
  2. Check token budget.
  3. Call the LLM.
  4. Parse FILE blocks from the response.
  5. Validate all produced paths against the contract's allowed_files list.
  6. Return an ``AgentResult``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import fnmatch
import re
import time
from typing import Any

import tiktoken

from system.core.orchestration.task_schemas import TaskNode
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.constants import (
    DEFAULT_LLM_MODEL,
    MAX_TOKENS_PER_AGENT,
)
from system.shared.exceptions import AgentError
from system.shared.llm_client import LLMMessage, LLMResponse
from system.shared.models import AgentType

# ============================================================================ #
# AgentContract
# ============================================================================ #


@dataclass
class AgentContract:
    """Declarative scope contract handed to a specialist agent for one task.

    The contract is the single source of truth for what an agent is allowed
    to do.  The AgentRunner enforces every field programmatically before and
    after the LLM call.

    Attributes
    ----------
    identity:
        Unique name for this agent role (e.g. "backend_agent").
    objective:
        Plain-English statement of what the agent must accomplish.
    allowed_files:
        Glob patterns OR explicit relative paths the agent may read/write.
        The runner rejects any file produced by the agent that does not
        match at least one pattern in this list.
    constraints:
        Hard rules expressed as human-readable strings.  These are injected
        into the system prompt so the LLM is aware of them, and are also
        used by the runner for post-execution validation where possible.
    validation_rules:
        Checks that must pass after the agent completes.  Expressed as
        human-readable strings; the runner translates them to executable
        ValidationRule objects from the TaskNode where applicable.
    success_criteria:
        Measurable outcomes that define "done" for this task.  Used for
        post-execution review and included in the prompt so the LLM can
        self-check.
    max_tokens:
        Maximum completion tokens the LLM is allowed to use for this call.
    temperature:
        Sampling temperature.  Default 0.1 for deterministic code generation.
    model:
        Anthropic model identifier to use for this agent call.
    """

    identity: str
    objective: str
    allowed_files: list[str]
    constraints: list[str]
    validation_rules: list[str]
    success_criteria: list[str]
    max_tokens: int = MAX_TOKENS_PER_AGENT
    temperature: float = 0.1
    model: str = DEFAULT_LLM_MODEL


# ============================================================================ #
# AgentContext
# ============================================================================ #


@dataclass
class AgentContext:
    """All runtime context passed to an agent during execution.

    ``scoped_files`` is pre-filtered by the AgentRunner: only files that
    match the contract's ``allowed_files`` patterns are included, and the
    total count is capped at ``SCOPED_CONTEXT_MAX_FILES``.

    Attributes
    ----------
    task:
        The TaskNode this agent is executing.
    contract:
        The contract that defines the agent's scope.
    scoped_files:
        Mapping of relative path → file contents (strings).
    spec:
        Full ProjectSpec, if relevant to this task.
    arch_plan:
        Full ArchitecturePlan, if relevant to this task.
    additional_context:
        Arbitrary key/value pairs injected by the runner (e.g. test results,
        previous attempt error messages, dependency outputs).
    token_budget:
        Maximum tokens available for the context message + response.
    tokens_used:
        Running count of tokens already consumed (populated after execution).
    """

    task: TaskNode
    contract: AgentContract
    scoped_files: dict[str, str]
    spec: ProjectSpec | None = None
    arch_plan: ArchitecturePlan | None = None
    additional_context: dict[str, Any] = field(default_factory=dict)
    token_budget: int = MAX_TOKENS_PER_AGENT
    tokens_used: int = 0


# ============================================================================ #
# AgentResult
# ============================================================================ #


@dataclass
class AgentResult:
    """The structured output produced by an agent after executing a task.

    The AgentRunner uses this object to write files to disk, run validation
    rules, and publish events to the event bus.

    Attributes
    ----------
    agent_type:
        Which agent produced this result.
    task_id:
        The task_id from the originating TaskNode.
    success:
        True when the agent completed without fatal errors.
    generated_files:
        New files created by the agent: {relative_path: content}.
    modifications:
        Existing files modified by the agent: {relative_path: content}.
    deleted_files:
        Files the agent flagged for deletion (runner handles actual removal).
    validation_passed:
        True when all post-execution validation rules passed.
    errors:
        List of error messages (non-empty implies success=False or needs retry).
    warnings:
        Non-blocking issues the runner or review step should note.
    reasoning:
        The agent's own explanation of its decisions (extracted from the
        ### REASONING section of the LLM response).
    tokens_used:
        Total tokens consumed by the LLM call (prompt + completion).
    duration_ms:
        Wall-clock milliseconds the agent took to execute.
    """

    agent_type: AgentType
    task_id: str
    success: bool
    generated_files: dict[str, str]
    modifications: dict[str, str]
    deleted_files: list[str]
    validation_passed: bool
    errors: list[str]
    warnings: list[str]
    reasoning: str
    tokens_used: int
    duration_ms: float = 0.0


# ============================================================================ #
# BaseAgent
# ============================================================================ #


class BaseAgent(ABC):
    """Abstract base class for all FORGE specialist agents.

    Subclasses MUST implement:
      - ``build_contract(task, spec, arch)`` → AgentContract
      - ``build_system_prompt(contract)`` → str

    The ``execute()`` method provides the full agent lifecycle and should
    NOT be overridden unless there is a compelling agent-specific reason.

    Parameters
    ----------
    agent_type:
        The ``AgentType`` enum value for this agent.
    llm_client:
        A pre-initialised LLM client from ``get_llm_client()``.
    """

    # File block pattern: ### FILE: path/to/file.ext\n```lang\n...\n```
    _FILE_BLOCK_RE: re.Pattern = re.compile(
        r"^###\s+FILE:\s+(.+?)\s*\n```[a-zA-Z0-9_+-]*\n(.*?)^```",
        re.MULTILINE | re.DOTALL,
    )

    # Reasoning section pattern
    _REASONING_RE: re.Pattern = re.compile(
        r"###\s+REASONING\s*\n(.*?)(?=###\s+FILE:|$)",
        re.DOTALL | re.IGNORECASE,
    )

    def __init__(self, agent_type: AgentType, llm_client: Any) -> None:
        """Initialise the base agent with type and LLM client.

        Parameters
        ----------
        agent_type:
            Enum value identifying this agent's specialisation.
        llm_client:
            Async LLM client capable of ``complete(messages, ...)`` calls.
        """
        self.agent_type = agent_type
        self.llm_client = llm_client
        self.logger = get_logger(f"agent.{agent_type.value}")
        # tiktoken encoder for cl100k_base (GPT-4 / Claude approximate)
        self.encoder = tiktoken.get_encoding("cl100k_base")

    # ---------------------------------------------------------------------- #
    # Abstract methods — must be implemented by every subclass
    # ---------------------------------------------------------------------- #

    @abstractmethod
    def build_contract(
        self,
        task: TaskNode,
        spec: ProjectSpec,
        arch: ArchitecturePlan,
    ) -> AgentContract:
        """Build the agent's scoped contract for this specific task.

        Parameters
        ----------
        task:
            The TaskNode carrying task description, output_artifacts, context.
        spec:
            The full ProjectSpec for global context (schema, API contract, etc.).
        arch:
            The ArchitecturePlan for service topology and infra decisions.

        Returns
        -------
        AgentContract
            A fully populated contract scoped to exactly what this agent
            needs to accomplish the task.
        """

    @abstractmethod
    def build_system_prompt(self, contract: AgentContract) -> str:
        """Build the agent's system prompt from its contract.

        The system prompt must include:
          - FORGE_AGENT_PREAMBLE (from system.agents.prompts)
          - The agent's specific role description and standards.
          - The contract's objective, constraints, validation rules, and
            success criteria embedded literally.
          - FILE_OUTPUT_FORMAT and VALIDATION_INSTRUCTIONS.

        Parameters
        ----------
        contract:
            The AgentContract produced by ``build_contract()``.

        Returns
        -------
        str
            The complete system prompt string to pass to the LLM.
        """

    # ---------------------------------------------------------------------- #
    # Core execution lifecycle
    # ---------------------------------------------------------------------- #

    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute the agent within its contract boundaries.

        Full execution lifecycle:
          1. Build the user message from scoped context.
          2. Count tokens and enforce budget.
          3. Call the LLM.
          4. Parse the LLM response for reasoning and FILE blocks.
          5. Validate all produced file paths against allowed_files.
          6. Return a fully populated AgentResult.

        Parameters
        ----------
        context:
            The fully populated AgentContext from the runner.

        Returns
        -------
        AgentResult
            Structured result carrying generated files, reasoning, errors, etc.
        """
        start_ms = time.monotonic() * 1000
        self.logger.info(
            "agent_execution_start",
            task_id=context.task.task_id,
            agent=self.agent_type.value,
            scoped_files=len(context.scoped_files),
        )

        errors: list[str] = []
        warnings: list[str] = []
        generated_files: dict[str, str] = {}
        modifications: dict[str, str] = {}
        deleted_files: list[str] = []
        reasoning: str = ""
        tokens_used: int = 0

        try:
            # 1. Build messages
            system_prompt = self.build_system_prompt(context.contract)
            user_message = self.build_context_message(context)

            # 2. Token budget check
            system_tokens = self.count_tokens(system_prompt)
            user_tokens = self.count_tokens(user_message)
            total_input_tokens = system_tokens + user_tokens

            if total_input_tokens > context.token_budget:
                warnings.append(
                    f"Input tokens ({total_input_tokens}) exceed budget "
                    f"({context.token_budget}). Context may be truncated."
                )
                self.logger.warning(
                    "token_budget_exceeded",
                    input_tokens=total_input_tokens,
                    budget=context.token_budget,
                )

            # 3. LLM call
            messages: list[LLMMessage] = [
                LLMMessage(role="user", content=user_message),
            ]

            response: LLMResponse = await self.llm_client.complete(
                messages=messages,
                system=system_prompt,
                model=context.contract.model,
                max_tokens=context.contract.max_tokens,
                temperature=context.contract.temperature,
            )

            raw_text: str = response.content
            tokens_used = getattr(response, "tokens_used", total_input_tokens)
            context.tokens_used = tokens_used

            self.logger.info(
                "agent_llm_call_complete",
                task_id=context.task.task_id,
                tokens_used=tokens_used,
                response_length=len(raw_text),
            )

            # 4. Extract reasoning section
            reasoning = self._extract_reasoning(raw_text)

            # 5. Parse FILE blocks
            all_files = self._parse_llm_file_output(raw_text)

            if not all_files:
                errors.append(
                    "LLM response contained no valid FILE blocks. "
                    "The agent failed to produce any output files."
                )
                self.logger.error(
                    "no_file_blocks_in_response",
                    task_id=context.task.task_id,
                    response_preview=raw_text[:500],
                )

            # 6. Validate file paths against contract
            for path, content in all_files.items():
                if self._validate_file_access(path, context.contract):
                    # Classify as new or modified based on whether it was in scoped_files
                    if path in context.scoped_files:
                        modifications[path] = content
                    else:
                        generated_files[path] = content
                else:
                    errors.append(
                        f"Agent attempted to write file outside allowed_files scope: {path!r}. "
                        f"Allowed patterns: {context.contract.allowed_files}"
                    )
                    warnings.append(f"Discarded out-of-scope file: {path}")
                    self.logger.warning(
                        "out_of_scope_file_discarded",
                        path=path,
                        task_id=context.task.task_id,
                    )

            success = len(errors) == 0

            duration_ms = (time.monotonic() * 1000) - start_ms

            self.logger.info(
                "agent_execution_complete",
                task_id=context.task.task_id,
                success=success,
                generated=len(generated_files),
                modified=len(modifications),
                errors=len(errors),
                duration_ms=round(duration_ms, 1),
            )

            return AgentResult(
                agent_type=self.agent_type,
                task_id=context.task.task_id,
                success=success,
                generated_files=generated_files,
                modifications=modifications,
                deleted_files=deleted_files,
                validation_passed=False,  # set by runner after validation rules
                errors=errors,
                warnings=warnings,
                reasoning=reasoning,
                tokens_used=tokens_used,
                duration_ms=duration_ms,
            )

        except AgentError:
            raise
        except Exception as exc:
            duration_ms = (time.monotonic() * 1000) - start_ms
            self.logger.exception(
                "agent_execution_failed",
                task_id=context.task.task_id,
                error=str(exc),
            )
            raise AgentError(
                message=f"Agent {self.agent_type.value} failed on task "
                f"{context.task.task_id}: {exc}",
                details={"task_id": context.task.task_id, "original_error": str(exc)},
            ) from exc

    # ---------------------------------------------------------------------- #
    # Context message builder
    # ---------------------------------------------------------------------- #

    def build_context_message(self, context: AgentContext) -> str:
        """Build the user-turn message from the scoped context.

        Assembles the task description, spec summary, architecture summary, and
        all scoped file contents into a single, well-structured message that
        the LLM will use as its primary input.

        Parameters
        ----------
        context:
            Fully populated AgentContext.

        Returns
        -------
        str
            The complete user message string.
        """
        parts: list[str] = []

        # ── Task description ─────────────────────────────────────────────────
        parts.append("## TASK")
        parts.append(f"**Task ID:** `{context.task.task_id}`")
        parts.append(f"**Task Name:** {context.task.name}")
        parts.append(f"**Description:**\n{context.task.description}")
        parts.append(f"**Priority:** {context.task.priority}  **Phase:** {context.task.phase}")

        if context.task.output_artifacts:
            parts.append("\n**Expected Output Files:**")
            for artifact in context.task.output_artifacts:
                parts.append(f"  - `{artifact}`")

        # ── Contract summary ─────────────────────────────────────────────────
        parts.append("\n## CONTRACT")
        parts.append(f"**Objective:** {context.contract.objective}")
        parts.append("\n**Allowed Files (glob patterns):**")
        for pattern in context.contract.allowed_files:
            parts.append(f"  - `{pattern}`")

        parts.append("\n**Constraints (MUST obey):**")
        for constraint in context.contract.constraints:
            parts.append(f"  - {constraint}")

        parts.append("\n**Validation Rules (MUST satisfy):**")
        for rule in context.contract.validation_rules:
            parts.append(f"  - {rule}")

        parts.append("\n**Success Criteria:**")
        for criterion in context.contract.success_criteria:
            parts.append(f"  - {criterion}")

        # ── Project spec summary ─────────────────────────────────────────────
        if context.spec is not None:
            parts.append("\n## PROJECT SPECIFICATION SUMMARY")
            parts.append(
                "**Tech Stack:** "
                + ", ".join(f"{k}: {v}" for k, v in context.spec.tech_stack.items())
            )
            parts.append(f"**Complexity:** {context.spec.estimated_complexity}")

            # DB tables
            if context.spec.db_schema.tables:
                parts.append(
                    "\n**Database Tables:** "
                    + ", ".join(f"`{t.name}`" for t in context.spec.db_schema.tables)
                )

            # API summary
            ep_count = len(context.spec.api_contract.endpoints)
            parts.append(
                f"**API Endpoints:** {ep_count} endpoints on "
                f"`{context.spec.api_contract.base_path}`"
            )

        # ── Architecture plan summary ─────────────────────────────────────────
        if context.arch_plan is not None:
            parts.append("\n## ARCHITECTURE PLAN SUMMARY")
            if hasattr(context.arch_plan, "services"):
                svc_names = [getattr(s, "name", str(s)) for s in context.arch_plan.services]
                parts.append("**Services:** " + ", ".join(f"`{n}`" for n in svc_names))

        # ── Additional context from runner / previous tasks ──────────────────
        if context.additional_context:
            parts.append("\n## ADDITIONAL CONTEXT")
            for key, value in context.additional_context.items():
                parts.append(f"### {key}")
                if isinstance(value, str):
                    parts.append(value)
                else:
                    import json

                    parts.append(f"```json\n{json.dumps(value, indent=2, default=str)}\n```")

        # ── Scoped file contents ─────────────────────────────────────────────
        if context.scoped_files:
            parts.append(f"\n## EXISTING FILES IN SCOPE ({len(context.scoped_files)} files)")
            parts.append("_These are the current contents of files you may read or modify._\n")
            for path, content in context.scoped_files.items():
                # Infer language from extension for the code fence
                ext = path.rsplit(".", 1)[-1] if "." in path else "text"
                lang_map = {
                    "py": "python",
                    "ts": "typescript",
                    "tsx": "typescript",
                    "js": "javascript",
                    "jsx": "javascript",
                    "tf": "hcl",
                    "yaml": "yaml",
                    "yml": "yaml",
                    "json": "json",
                    "sh": "bash",
                    "md": "markdown",
                    "sql": "sql",
                    "toml": "toml",
                    "ini": "ini",
                    "env": "bash",
                }
                lang = lang_map.get(ext, ext)
                parts.append(f"### FILE: {path}")
                parts.append(f"```{lang}\n{content}\n```")
        else:
            parts.append("\n## EXISTING FILES IN SCOPE")
            parts.append("_No existing files are in scope — you are creating new files._")

        # ── Final instruction ─────────────────────────────────────────────────
        parts.append("\n## INSTRUCTIONS")
        parts.append(
            "Begin with a ### REASONING section (max 400 words), then emit "
            "your FILE blocks in the format specified in your system prompt. "
            "Produce complete file contents for every file. "
            "Do NOT produce any file outside your allowed_files patterns."
        )

        return "\n\n".join(parts)

    # ---------------------------------------------------------------------- #
    # Token counting
    # ---------------------------------------------------------------------- #

    def count_tokens(self, text: str) -> int:
        """Count the number of tokens in a text string using cl100k_base encoding.

        Parameters
        ----------
        text:
            The text to count tokens for.

        Returns
        -------
        int
            Approximate token count.
        """
        return len(self.encoder.encode(text))

    # ---------------------------------------------------------------------- #
    # LLM output parsing
    # ---------------------------------------------------------------------- #

    def _parse_llm_file_output(self, response: str) -> dict[str, str]:
        """Parse LLM response and extract all FILE blocks.

        Expects blocks in the format::

            ### FILE: path/to/file.ext
            ```language
            ...complete file contents...
            ```

        Parameters
        ----------
        response:
            The raw LLM response string.

        Returns
        -------
        Dict[str, str]
            Mapping of ``{relative_path: file_contents}``.  An empty dict is
            returned if no valid FILE blocks are found.
        """
        result: dict[str, str] = {}
        matches = self._FILE_BLOCK_RE.finditer(response)

        for match in matches:
            path = match.group(1).strip()
            content = match.group(2)

            # Normalise path: strip leading slashes and whitespace
            path = path.lstrip("/").strip()

            if not path:
                self.logger.warning("empty_file_path_in_response")
                continue

            # Remove trailing newline from content (the regex captures everything
            # up to the closing ```, which may include a trailing \n)
            if content.endswith("\n"):
                content = content[:-1]

            result[path] = content

        self.logger.debug(
            "parsed_file_blocks",
            count=len(result),
            paths=list(result.keys()),
        )
        return result

    def _extract_reasoning(self, response: str) -> str:
        """Extract the ### REASONING section from the LLM response.

        Parameters
        ----------
        response:
            Raw LLM response string.

        Returns
        -------
        str
            The extracted reasoning text, or an empty string if not found.
        """
        match = self._REASONING_RE.search(response)
        if match:
            return match.group(1).strip()
        # If no REASONING header, try to grab any text before the first FILE block
        first_file = response.find("### FILE:")
        if first_file > 0:
            return response[:first_file].strip()
        return ""

    # ---------------------------------------------------------------------- #
    # File access validation
    # ---------------------------------------------------------------------- #

    def _validate_file_access(self, path: str, contract: AgentContract) -> bool:
        """Check whether a file path is permitted by the contract.

        Uses ``fnmatch.fnmatch`` for glob pattern matching.  A path is
        allowed if it matches ANY pattern in ``contract.allowed_files``.

        Parameters
        ----------
        path:
            The relative file path to validate (e.g. ``"system/models/user.py"``).
        contract:
            The AgentContract whose ``allowed_files`` list to check against.

        Returns
        -------
        bool
            True if the path is permitted; False otherwise.
        """
        normalised = path.lstrip("/")
        for pattern in contract.allowed_files:
            normalised_pattern = pattern.lstrip("/")
            # Direct match
            if fnmatch.fnmatch(normalised, normalised_pattern):
                return True
            # Handle **/ glob: fnmatch doesn't support **, so we expand
            if "**" in normalised_pattern:
                # Replace ** with a pattern that matches any path segment(s)
                # We do this by trying fnmatch on every sub-path
                regex_pattern = _glob_to_regex(normalised_pattern)
                if re.fullmatch(regex_pattern, normalised):
                    return True
        return False


# ============================================================================ #
# Glob helper
# ============================================================================ #


def _glob_to_regex(glob_pattern: str) -> str:
    """Convert a glob pattern (with ** support) to a regex string.

    Standard ``fnmatch`` does not support ``**`` for matching across directory
    separators.  This helper converts ``**`` to a regex that matches any
    sequence of characters including ``/``.

    Parameters
    ----------
    glob_pattern:
        A glob pattern such as ``"system/**/*.py"`` or ``"infra/**"``.

    Returns
    -------
    str
        A regex pattern string usable with ``re.fullmatch()``.
    """
    parts = re.split(r"(\*\*)", glob_pattern)
    regex_parts: list[str] = []
    for part in parts:
        if part == "**":
            regex_parts.append(".*")
        else:
            # Escape regex special chars, then convert single * and ? to regex
            escaped = re.escape(part)
            escaped = escaped.replace(r"\*", "[^/]*")
            escaped = escaped.replace(r"\?", "[^/]")
            regex_parts.append(escaped)
    return "".join(regex_parts)
