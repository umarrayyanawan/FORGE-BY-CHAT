"""Refactor Agent — structural code improvement without behavioural changes.

Improves the internal quality of existing code without altering its observable
behaviour.  Works exclusively on files specified in the task's input context
(``task.input_context["target_files"]``).  All existing public API signatures,
module import paths, database schema, and environment variable names are
preserved exactly — only internal structure is improved.
"""

from __future__ import annotations

from typing import Any, Optional

from system.agents.base import AgentContract, AgentContext, AgentResult, BaseAgent
from system.agents.prompts import (
    FILE_OUTPUT_FORMAT,
    FORGE_AGENT_PREAMBLE,
    REFACTOR_SYSTEM_PROMPT_TEMPLATE,
    VALIDATION_INSTRUCTIONS,
)
from system.core.orchestration.task_schemas import TaskNode
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL, MAX_TOKENS_PER_AGENT
from system.shared.models import AgentType

logger = get_logger(__name__)


class RefactorAgent(BaseAgent):
    """Specialist agent for structural code quality improvement.

    Improves the internal quality of existing code without changing observable
    behaviour.  The agent applies the following refactoring patterns:

    - Extract method: breaks functions >40 lines into cohesive private helpers.
    - Guard clauses: converts nested if/else into early returns.
    - Magic value elimination: replaces magic strings/numbers with named constants.
    - Dead code removal: eliminates unreachable branches, unused imports, unused vars.
    - Duplicate logic consolidation: extracts copy-pasted logic into shared helpers.
    - Improved error messages: replaces vague errors with descriptive ones.
    - Type narrowing: replaces ``Any`` annotations with specific types or TypeVar.
    - Async correctness: ensures ``await`` is used for all coroutines.
    - Docstring completion: adds or improves docstrings on all public symbols.
    - Type hint completion: adds missing PEP 484 annotations to all signatures.

    The agent operates ONLY on files listed in
    ``task.input_context.get("target_files", [])``.  It derives its
    ``allowed_files`` list dynamically from this field so that the scope
    is always task-specific and minimal.

    Parameters
    ----------
    llm_client:
        Initialised async LLM client from ``get_llm_client()``.
    """

    def __init__(self, llm_client: Any) -> None:
        """Initialise the RefactorAgent.

        Parameters
        ----------
        llm_client:
            Async LLM client capable of ``complete(messages, ...)`` calls.
        """
        super().__init__(AgentType.REFACTOR, llm_client)

    # ---------------------------------------------------------------------- #
    # Contract
    # ---------------------------------------------------------------------- #

    def build_contract(
        self,
        task: TaskNode,
        spec: Optional[ProjectSpec],
        arch: Optional[ArchitecturePlan],
    ) -> AgentContract:
        """Build a scoped AgentContract for a refactoring task.

        Derives the ``allowed_files`` list from
        ``task.input_context.get("target_files", [])``.  If no target files
        are specified the scope falls back to ``system/**/*.py`` to avoid
        blocking the pipeline while providing an appropriately broad default.

        Parameters
        ----------
        task:
            The TaskNode carrying the refactoring objective and target files.
        spec:
            Project specification (used for context; not directly modified).
        arch:
            Architecture plan (used for context; not directly modified).

        Returns
        -------
        AgentContract
            Contract scoped to the specific target files for this refactoring
            task plus the associated test files.
        """
        # Derive target files from task input context
        target_files: list[str] = []
        if hasattr(task, "input_context") and isinstance(task.input_context, dict):
            raw_targets = task.input_context.get("target_files", [])
            if isinstance(raw_targets, list):
                target_files = [str(f) for f in raw_targets]

        # If the task specifies target files, also allow their test counterparts
        if target_files:
            test_counterparts: list[str] = []
            for f in target_files:
                # e.g. system/services/user_service.py -> tests/**/test_user_service.py
                filename = f.split("/")[-1]
                module_name = filename.replace(".py", "")
                test_counterparts.append(f"tests/**/test_{module_name}.py")

            allowed_files = target_files + test_counterparts
        else:
            # Broad fallback: all Python source and test files
            allowed_files = ["system/**/*.py", "tests/**/*.py"]
            logger.warning(
                "refactor_agent_no_target_files",
                task_id=task.task_id,
                fallback="system/**/*.py + tests/**/*.py",
            )

        return AgentContract(
            identity="refactor_agent",
            objective=task.description,
            allowed_files=allowed_files,
            constraints=[
                "NEVER change public function or method signatures — parameter names, types, order, and return types must remain identical.",
                "NEVER add new public functions, classes, or methods — only restructure existing ones.",
                "NEVER introduce behavioural changes — if you are not certain the logic is equivalent, do not change it.",
                "ALL existing tests must still pass after your refactoring — never break a passing test.",
                "NEVER add new features, business logic, or validation rules during refactoring.",
                "ALWAYS maintain backward compatibility — no imports from other modules should break.",
                "NEVER change module-level variable names, constants, or class names used by external code.",
                "NEVER alter database schema, Alembic migrations, or ORM model column definitions.",
                "NEVER change environment variable names or configuration key names.",
                "ALWAYS preserve the existing module docstring and update it only if factually incorrect.",
            ],
            validation_rules=[
                "No new public symbols (functions, classes, methods) introduced — only existing ones restructured.",
                "All existing function signatures preserved exactly (name, parameters, return type).",
                "No new imports that are not in the original file (except for typing helpers: TypeVar, Protocol, etc.).",
                "Import structure unchanged — no module-level names renamed that could break external imports.",
                "All functions and classes have complete docstrings after refactoring.",
                "All function and method signatures have complete PEP 484 type annotations after refactoring.",
                "No TODO, FIXME, or HACK comments left in the output (address them or remove them).",
            ],
            success_criteria=[
                "Target files refactored with improved internal structure and no behavioural changes.",
                "All functions >40 lines extracted into focused, well-named private helpers.",
                "All magic strings and numbers replaced with named module-level constants.",
                "All missing type annotations added; all 'Any' types narrowed to specific types.",
                "All docstrings written or updated to accurately reflect current behaviour.",
                "Dead code (unreachable branches, unused imports, unused variables) removed.",
                "Nested conditionals converted to guard clause (early return) pattern where clearer.",
            ],
            max_tokens=MAX_TOKENS_PER_AGENT,
            temperature=0.05,  # Very low temperature for deterministic structural changes
            model=DEFAULT_LLM_MODEL,
        )

    # ---------------------------------------------------------------------- #
    # System prompt
    # ---------------------------------------------------------------------- #

    def build_system_prompt(self, contract: AgentContract) -> str:
        """Build the Refactor Agent's system prompt from the contract.

        Composes the universal FORGE preamble, the refactoring-specific
        quality standards from the template, the current task contract
        details, and a catalogue of specific refactoring patterns to apply
        with before/after examples.

        Parameters
        ----------
        contract:
            The AgentContract produced by ``build_contract()``.

        Returns
        -------
        str
            Complete system prompt string ready for the LLM.
        """
        constraints_text = "\n".join(f"  - {c}" for c in contract.constraints)
        validation_text = "\n".join(f"  - {v}" for v in contract.validation_rules)
        success_text = "\n".join(f"  - {s}" for s in contract.success_criteria)

        allowed_text = "\n".join(f"  - {f}" for f in contract.allowed_files)

        return f"""{FORGE_AGENT_PREAMBLE}

{REFACTOR_SYSTEM_PROMPT_TEMPLATE}

═══════════════════════════════════════════════════════════════════════════════
CURRENT TASK CONTRACT
═══════════════════════════════════════════════════════════════════════════════

### Objective
{contract.objective}

### Target Files (ONLY these files may be modified)
{allowed_text}

### Hard Constraints (NEVER violate these)
{constraints_text}

### Validation Rules (your output MUST satisfy ALL of these)
{validation_text}

### Success Criteria (define "done" for this task)
{success_text}

═══════════════════════════════════════════════════════════════════════════════
REFACTORING PATTERN CATALOGUE — APPLY THESE SYSTEMATICALLY
═══════════════════════════════════════════════════════════════════════════════

Work through each pattern below in order. For every pattern, scan the target
files, apply it where applicable, and document in your REASONING section which
patterns you applied and why.

#### Pattern 1: Extract Method
BEFORE (function too long, mixed concerns):
```python
async def process_payment(order_id: str, amount: float) -> dict:
    # 60 lines of validation, API call, DB write, email all mixed together
    ...
```
AFTER (single-responsibility private helpers):
```python
async def process_payment(order_id: str, amount: float) -> PaymentResult:
    \"\"\"Orchestrate payment processing for an order.\"\"\"
    order = await _fetch_and_validate_order(order_id, amount)
    charge = await _charge_payment_provider(order, amount)
    result = await _record_payment(order, charge)
    await _notify_customer(order, result)
    return result
```

#### Pattern 2: Guard Clauses (Early Return)
BEFORE (deep nesting):
```python
def get_user_display_name(user):
    if user is not None:
        if user.is_active:
            if user.display_name:
                return user.display_name
            else:
                return user.email
        else:
            return "Deactivated User"
    else:
        return "Unknown User"
```
AFTER (flat with guard clauses):
```python
def get_user_display_name(user: Optional[User]) -> str:
    \"\"\"Return the best display name for a user, with fallbacks.\"\"\"
    if user is None:
        return "Unknown User"
    if not user.is_active:
        return "Deactivated User"
    return user.display_name or user.email
```

#### Pattern 3: Magic Value Elimination
BEFORE:
```python
if user.role == 3:  # What is 3?
    if status_code == 429:
        time.sleep(0.5)  # Why 0.5?
```
AFTER:
```python
_ADMIN_ROLE_ID: int = 3
_HTTP_RATE_LIMITED: int = 429
_RATE_LIMIT_BACKOFF_SECONDS: float = 0.5

if user.role == _ADMIN_ROLE_ID:
    if status_code == _HTTP_RATE_LIMITED:
        time.sleep(_RATE_LIMIT_BACKOFF_SECONDS)
```

#### Pattern 4: Improved Error Messages
BEFORE:
```python
raise ValueError("Invalid input")
raise KeyError("Not found")
```
AFTER:
```python
raise ValueError(
    f"Expected a positive integer for 'amount', got {{amount!r}}."
)
raise KeyError(
    f"User with id={{user_id!r}} not found in the database."
)
```

#### Pattern 5: Type Narrowing
BEFORE:
```python
def process(data: Any) -> Any:
    result = some_fn(data)
    return result
```
AFTER:
```python
def process(data: UserPayload) -> ProcessedResult:
    \"\"\"Process a user payload and return the structured result.\"\"\"
    result: ProcessedResult = some_fn(data)
    return result
```

{FILE_OUTPUT_FORMAT}

{VALIDATION_INSTRUCTIONS}
"""

    # ---------------------------------------------------------------------- #
    # Execution
    # ---------------------------------------------------------------------- #

    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute the refactor agent against the given context.

        Delegates the full lifecycle (token counting, LLM call, file block
        parsing, scope validation, result assembly) to ``BaseAgent.execute()``.

        Parameters
        ----------
        context:
            Fully populated AgentContext from the runner.

        Returns
        -------
        AgentResult
            Structured result carrying refactored source files (as
            modifications), reasoning documenting which patterns were applied,
            and any errors.
        """
        logger.info(
            "refactor_agent_execute",
            task_id=context.task.task_id,
            objective=context.contract.objective[:120],
            target_files=context.contract.allowed_files,
        )
        return await super().execute(context)
