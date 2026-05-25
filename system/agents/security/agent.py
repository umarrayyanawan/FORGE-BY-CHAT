"""Security Agent — OWASP / CWE-25 security review and remediation.

Reviews all code produced by other agents against OWASP Top 10 (2021),
CWE/SANS Top 25, and ASVS Level 2 controls.  Produces a structured security
assessment report in Markdown and remediated versions of all affected files.
"""

from __future__ import annotations

from typing import Any

from system.agents.base import AgentContext, AgentContract, AgentResult, BaseAgent
from system.agents.prompts import (
    FILE_OUTPUT_FORMAT,
    FORGE_AGENT_PREAMBLE,
    SECURITY_SYSTEM_PROMPT_TEMPLATE,
    VALIDATION_INSTRUCTIONS,
)
from system.core.orchestration.task_schemas import TaskNode
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL, MAX_TOKENS_PER_AGENT
from system.shared.models import AgentType

logger = get_logger(__name__)


class SecurityAgent(BaseAgent):
    """Specialist agent for security review and remediation.

    Performs a thorough security review of backend, frontend, and
    infrastructure code against:

    - OWASP Top 10 (2021): injection, broken auth, sensitive data exposure,
      XXE, broken access control, security misconfiguration, XSS, insecure
      deserialisation, known vulnerabilities, insufficient logging.
    - CWE/SANS Top 25: buffer overflow, SQL injection, OS command injection,
      path traversal, XSS, use-after-free, improper access control.
    - ASVS Level 2: authentication, session management, access control,
      input validation, cryptography, error handling, logging.
    - CIS Benchmarks for Docker and Kubernetes.

    Produces:
      1. A Markdown security assessment report with severity-rated findings.
      2. Remediated versions of all affected source files.
      3. Security-hardening additions (rate limiting, CORS policies, CSP headers).

    Parameters
    ----------
    llm_client:
        Initialised async LLM client from ``get_llm_client()``.
    """

    def __init__(self, llm_client: Any) -> None:
        """Initialise the SecurityAgent.

        Parameters
        ----------
        llm_client:
            Async LLM client capable of ``complete(messages, ...)`` calls.
        """
        super().__init__(AgentType.SECURITY, llm_client)

    # ---------------------------------------------------------------------- #
    # Contract
    # ---------------------------------------------------------------------- #

    def build_contract(
        self,
        task: TaskNode,
        spec: ProjectSpec | None,
        arch: ArchitecturePlan | None,
    ) -> AgentContract:
        """Build a scoped AgentContract for a security review task.

        The Security agent has broad read access to review all code, but
        its write scope is constrained to security assessment reports in
        ``docs/security/`` and remediated source files.

        Parameters
        ----------
        task:
            The TaskNode carrying the security review objective.
        spec:
            Project specification (API contract, auth mechanism).
        arch:
            Architecture plan (service topology, network boundaries).

        Returns
        -------
        AgentContract
            Contract scoped to security review artefacts and source files.
        """
        return AgentContract(
            identity="security_agent",
            objective=task.description,
            allowed_files=[
                "system/**/*.py",
                "infra/**",
                ".github/workflows/**",
                "frontend/src/**/*.ts",
                "frontend/src/**/*.tsx",
                "docs/security/**/*.md",
                "docs/adr/**/*.md",
            ],
            constraints=[
                "NEVER output actual secret values, tokens, or credentials — not even as examples.",
                "ALWAYS flag SQL string concatenation or f-string SQL as CRITICAL severity.",
                "ALWAYS flag missing input validation at API boundaries as HIGH severity.",
                "ALWAYS check for all OWASP Top 10 (2021) vulnerability categories.",
                "NEVER suggest disabling SSL/TLS certificate verification under any circumstances.",
                "ALWAYS verify that password hashing uses bcrypt (cost ≥12) or Argon2id — flag MD5/SHA-1/plain text as CRITICAL.",
                "ALWAYS check that JWT tokens have an expiry claim (exp) — flag missing exp as HIGH.",
                "ALWAYS check for missing rate limiting on authentication endpoints — flag as HIGH.",
                "NEVER recommend storing secrets in environment variables with hardcoded defaults in code.",
                "ALWAYS check Kubernetes manifests for privileged containers and host path mounts.",
            ],
            validation_rules=[
                "Security assessment report follows the required section structure (Executive Summary, Critical, High, Medium, Low).",
                "Every finding has: severity, OWASP/CWE reference, file and line reference, description, and remediation.",
                "No actual secret values appear in any output file.",
                "All Critical and High findings have a corresponding remediated file in the output.",
                "Remediated files do not introduce new vulnerabilities while fixing existing ones.",
                "The report includes a 'Remediation Applied' section listing every changed file and reason.",
            ],
            success_criteria=[
                "Security assessment report generated at docs/security/security-assessment.md.",
                "All OWASP Top 10 categories checked and findings documented.",
                "Critical and High findings remediated in output files.",
                "Security-hardening additions applied: rate limiting, secure headers, CORS policy.",
                "CIS Docker and Kubernetes benchmark checks completed for all infra files.",
                "Authentication and authorisation flows verified for JWT expiry, bcrypt hashing, RBAC enforcement.",
            ],
            max_tokens=MAX_TOKENS_PER_AGENT,
            temperature=0.1,
            model=DEFAULT_LLM_MODEL,
        )

    # ---------------------------------------------------------------------- #
    # System prompt
    # ---------------------------------------------------------------------- #

    def build_system_prompt(self, contract: AgentContract) -> str:
        """Build the Security Agent's system prompt from the contract.

        Composes the universal FORGE preamble, the security-specific review
        standards from the template, the current task contract details, and
        the required security report structure with severity rating criteria.

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

        return f"""{FORGE_AGENT_PREAMBLE}

{SECURITY_SYSTEM_PROMPT_TEMPLATE}

═══════════════════════════════════════════════════════════════════════════════
CURRENT TASK CONTRACT
═══════════════════════════════════════════════════════════════════════════════

### Objective
{contract.objective}

### Hard Constraints (NEVER violate these)
{constraints_text}

### Validation Rules (your output MUST satisfy ALL of these)
{validation_text}

### Success Criteria (define "done" for this task)
{success_text}

═══════════════════════════════════════════════════════════════════════════════
SEVERITY RATING CRITERIA
═══════════════════════════════════════════════════════════════════════════════

Use these exact criteria to classify every finding:

  CRITICAL — Blocks deployment immediately.
    • Direct SQL/command injection via user input.
    • Authentication bypass — unauthenticated access to protected resources.
    • Plaintext password storage (MD5, SHA-1, unsalted, or no hashing).
    • Remote code execution (eval/exec with user input).
    • Secrets committed to source code or container images.
    • Missing authentication on privileged endpoints.

  HIGH — Must be fixed before next release.
    • JWT with no expiry claim (exp).
    • CORS configured with allow_origins=["*"] in production.
    • Missing input validation on user-controlled fields (XSS, injection risk).
    • Sensitive data logged (passwords, tokens, PII).
    • Broken object-level authorisation (accessing other users' resources).
    • Missing rate limiting on login/registration endpoints.
    • Kubernetes containers running as root without explicit justification.
    • Privileged Docker containers or host path mounts.

  MEDIUM — Fix within the current sprint.
    • Missing security headers (X-Content-Type-Options, X-Frame-Options, CSP).
    • Debug mode left enabled in a production configuration path.
    • Overly broad exception handling that swallows security-relevant errors.
    • Verbose error messages leaking internal path or version info.
    • Weak password policy (length <8, no complexity enforcement).
    • Missing CSRF protection on state-changing form endpoints.

  LOW / INFORMATIONAL — Note for future improvement.
    • Missing security-related log entries (successful logins, failed attempts).
    • Unused dependencies that may contain CVEs.
    • Hardcoded non-secret configuration values that should be env vars.
    • Missing Content-Security-Policy report-uri directive.

═══════════════════════════════════════════════════════════════════════════════
REQUIRED FINDING FORMAT
═══════════════════════════════════════════════════════════════════════════════

For every finding in the report, use this exact format:

  ### FINDING-NNN: <Short Title>
  - **Severity:** CRITICAL | HIGH | MEDIUM | LOW
  - **OWASP Reference:** A01:2021 – Broken Access Control (or applicable category)
  - **CWE Reference:** CWE-89 SQL Injection (or applicable CWE)
  - **File:** `path/to/file.py`, line NNN
  - **Description:** <What is the vulnerability and why is it dangerous?>
  - **Vulnerable Code:**
    ```python
    <the vulnerable snippet — anonymise any real secret values>
    ```
  - **Remediation:** <How to fix it — be specific with code where helpful>
  - **Status:** Remediated in this report | Requires manual action

═══════════════════════════════════════════════════════════════════════════════
PYTHON SECURITY PATTERNS TO CHECK
═══════════════════════════════════════════════════════════════════════════════

  DANGEROUS (always flag):
    - f"SELECT ... {{user_input}}" — SQL injection
    - os.system(user_input) — command injection
    - eval(user_input) / exec(user_input) — RCE
    - pickle.loads(user_data) — deserialisation attack
    - hashlib.md5(password) / hashlib.sha1(password) — weak hashing
    - logging.info(f"password: {{password}}") — credential logging
    - verify=False in requests/httpx calls — TLS bypass

  SAFE (expected patterns):
    - db.execute(select(Model).where(Model.field == bound_param)) — ORM with bound params
    - text("SELECT ... WHERE id = :id").bindparams(id=user_id) — parameterised
    - passlib.context.CryptContext(schemes=["bcrypt"], deprecated="auto") — correct hashing
    - JWT with exp, iss, sub claims and HS256/RS256 algorithm

{FILE_OUTPUT_FORMAT}

{VALIDATION_INSTRUCTIONS}
"""

    # ---------------------------------------------------------------------- #
    # Execution
    # ---------------------------------------------------------------------- #

    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute the security agent against the given context.

        Delegates the full lifecycle (token counting, LLM call, file block
        parsing, scope validation, result assembly) to ``BaseAgent.execute()``.

        Parameters
        ----------
        context:
            Fully populated AgentContext from the runner.

        Returns
        -------
        AgentResult
            Structured result carrying the security assessment report,
            remediated source files, reasoning, and any errors.
        """
        logger.info(
            "security_agent_execute",
            task_id=context.task.task_id,
            objective=context.contract.objective[:120],
        )
        return await super().execute(context)
