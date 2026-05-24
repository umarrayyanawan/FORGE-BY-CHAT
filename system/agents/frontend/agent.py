"""Frontend Agent — Next.js 15 / TypeScript / Tailwind code generation.

Writes complete, production-grade frontend code for the Next.js 15 App Router:
React Server Components, typed client components, Zustand stores, TanStack
Query hooks, React Hook Form + Zod forms, and fully typed API client modules.
"""

from __future__ import annotations

from typing import Any, Optional

from system.agents.base import AgentContract, AgentContext, AgentResult, BaseAgent
from system.agents.prompts import (
    FILE_OUTPUT_FORMAT,
    FORGE_AGENT_PREAMBLE,
    FRONTEND_SYSTEM_PROMPT_TEMPLATE,
    VALIDATION_INSTRUCTIONS,
)
from system.core.orchestration.task_schemas import TaskNode
from system.core.planning.schemas import ArchitecturePlan
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL, MAX_TOKENS_PER_AGENT
from system.shared.models import AgentType

logger = get_logger(__name__)


class FrontendAgent(BaseAgent):
    """Specialist agent for Next.js 15 / TypeScript frontend code generation.

    Produces complete, production-grade TypeScript modules for a Next.js 15
    App Router application: React Server Components for data fetching, Client
    Components for interactivity, Zustand UI state stores, TanStack Query
    data hooks, React Hook Form + Zod validated forms, shadcn/ui-based
    component compositions, and typed API client wrappers.

    All code uses TypeScript strict mode — the ``any`` type is strictly
    forbidden, all props have explicit interfaces, all functions have typed
    return values, and Tailwind CSS utility classes replace all inline styles.

    Parameters
    ----------
    llm_client:
        Initialised async LLM client from ``get_llm_client()``.
    """

    def __init__(self, llm_client: Any) -> None:
        """Initialise the FrontendAgent.

        Parameters
        ----------
        llm_client:
            Async LLM client capable of ``complete(messages, ...)`` calls.
        """
        super().__init__(AgentType.FRONTEND, llm_client)

    # ---------------------------------------------------------------------- #
    # Contract
    # ---------------------------------------------------------------------- #

    def build_contract(
        self,
        task: TaskNode,
        spec: Optional[ProjectSpec],
        arch: Optional[ArchitecturePlan],
    ) -> AgentContract:
        """Build a scoped AgentContract for a frontend implementation task.

        Parameters
        ----------
        task:
            The TaskNode carrying the frontend implementation objective.
        spec:
            Project specification (API contract, tech stack).
        arch:
            Architecture plan (service topology for API base URLs).

        Returns
        -------
        AgentContract
            Contract scoped to frontend TypeScript and configuration files.
        """
        return AgentContract(
            identity="frontend_agent",
            objective=task.description,
            allowed_files=[
                "frontend/src/**/*.tsx",
                "frontend/src/**/*.ts",
                "frontend/public/**",
                "frontend/*.config.ts",
                "frontend/*.config.js",
                "frontend/*.config.mjs",
                "frontend/package.json",
                "frontend/tsconfig.json",
                "frontend/tailwind.config.ts",
                "frontend/tailwind.config.js",
                "frontend/postcss.config.js",
                "frontend/.eslintrc.json",
                "frontend/.eslintrc.js",
                "frontend/next.config.ts",
                "frontend/next.config.js",
                "frontend/next.config.mjs",
            ],
            constraints=[
                "NEVER use the TypeScript 'any' type — use 'unknown' and narrow, or define proper types.",
                "ALWAYS prefer React Server Components; only add 'use client' when browser APIs or hooks are strictly required.",
                "ALWAYS define a TypeScript interface named <ComponentName>Props for every component's props.",
                "ALWAYS use Tailwind CSS utility classes — zero inline styles (style={{...}} is forbidden).",
                "NEVER expose API keys, tokens, or secrets in any frontend file.",
                "ALWAYS handle loading states (skeleton UI or <Suspense>) and error states (error.tsx or ErrorBoundary).",
                "ALWAYS use next/image for images (with explicit width and height), next/link for navigation.",
                "NEVER use useEffect for data fetching — use React Server Components or TanStack Query.",
                "ALWAYS use React Hook Form with a Zod schema resolver for all form components.",
                "ALWAYS use the cn() utility (clsx + tailwind-merge) for conditional Tailwind class composition.",
                "NEVER hardcode API base URLs — read them from environment variables (process.env.NEXT_PUBLIC_API_URL).",
            ],
            validation_rules=[
                "No 'any' TypeScript type in any output file.",
                "No 'use client' directive on components that perform only data display without interactivity.",
                "All component files export a default function with a matching PascalCase name.",
                "All forms use React Hook Form with zodResolver — no uncontrolled native form handling.",
                "No inline styles (style={{...}}) — all styling via Tailwind classes.",
                "All images use next/image; all internal links use next/link.",
                "All API call functions are in frontend/src/lib/api/ and have TypeScript return types.",
            ],
            success_criteria=[
                "React components created with correct TypeScript props interfaces.",
                "Loading and error boundary states implemented for all data-fetching components.",
                "Forms implemented with React Hook Form + Zod validation and accessible error messages.",
                "API client modules written with typed request/response interfaces.",
                "Tailwind CSS applied consistently with responsive breakpoints and dark mode variants.",
                "TanStack Query hooks (useQuery/useMutation) used for all client-side data fetching.",
            ],
            max_tokens=MAX_TOKENS_PER_AGENT,
            temperature=0.1,
            model=DEFAULT_LLM_MODEL,
        )

    # ---------------------------------------------------------------------- #
    # System prompt
    # ---------------------------------------------------------------------- #

    def build_system_prompt(self, contract: AgentContract) -> str:
        """Build the Frontend Agent's system prompt from the contract.

        Composes the universal FORGE preamble, the frontend-specific Next.js
        technology standards from the template, the current task contract
        details, and canonical code examples for Server Components, Client
        Components, and typed API clients.

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

{FRONTEND_SYSTEM_PROMPT_TEMPLATE}

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
CANONICAL CODE PATTERNS — FOLLOW THESE EXACTLY
═══════════════════════════════════════════════════════════════════════════════

#### 1. React Server Component with data fetching (correct pattern)

```tsx
// frontend/src/app/users/page.tsx
// NO 'use client' — this is a Server Component
import {{ Suspense }} from "react";
import {{ UserList }} from "@/components/users/UserList";
import {{ UserListSkeleton }} from "@/components/users/UserListSkeleton";

export const metadata = {{ title: "Users" }};

export default async function UsersPage(): Promise<JSX.Element> {{
  return (
    <main className="container mx-auto px-4 py-8">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-6">
        Users
      </h1>
      <Suspense fallback={{<UserListSkeleton />}}>
        <UserList />
      </Suspense>
    </main>
  );
}}
```

#### 2. Client Component with TanStack Query (correct pattern)

```tsx
"use client";

import {{ useQuery }} from "@tanstack/react-query";
import {{ fetchUsers }} from "@/lib/api/users";
import type {{ User }} from "@/lib/api/users";

interface UserCardProps {{
  userId: string;
  className?: string;
}}

export function UserCard({{ userId, className }}: UserCardProps): JSX.Element {{
  const {{ data: user, isPending, isError }} = useQuery({{
    queryKey: ["users", userId],
    queryFn: () => fetchUsers(userId),
  }});

  if (isPending) return <div className="animate-pulse h-16 bg-gray-100 rounded-lg" />;
  if (isError) return <p className="text-red-600 text-sm">Failed to load user.</p>;

  return (
    <div className={{cn("rounded-lg border border-gray-200 p-4", className)}}>
      <p className="font-medium text-gray-900">{{user.email}}</p>
    </div>
  );
}}
```

#### 3. React Hook Form + Zod (correct pattern)

```tsx
"use client";

import {{ useForm }} from "react-hook-form";
import {{ zodResolver }} from "@hookform/resolvers/zod";
import {{ z }} from "zod";
import {{ useMutation }} from "@tanstack/react-query";

const createUserSchema = z.object({{
  email: z.string().email("Enter a valid email address"),
  password: z.string().min(8, "Password must be at least 8 characters"),
}});

type CreateUserFormValues = z.infer<typeof createUserSchema>;

export function CreateUserForm(): JSX.Element {{
  const {{
    register,
    handleSubmit,
    formState: {{ errors, isSubmitting }},
  }} = useForm<CreateUserFormValues>({{
    resolver: zodResolver(createUserSchema),
  }});

  const mutation = useMutation({{ mutationFn: createUser }});

  const onSubmit = async (values: CreateUserFormValues): Promise<void> => {{
    await mutation.mutateAsync(values);
  }};

  return (
    <form onSubmit={{handleSubmit(onSubmit)}} className="space-y-4">
      <div>
        <label htmlFor="email" className="block text-sm font-medium text-gray-700">
          Email
        </label>
        <input
          id="email"
          type="email"
          {{...register("email")}}
          className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm
                     focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
        {{errors.email && (
          <p className="mt-1 text-xs text-red-600">{{errors.email.message}}</p>
        )}}
      </div>
      <button
        type="submit"
        disabled={{isSubmitting}}
        className="w-full rounded-md bg-indigo-600 px-4 py-2 text-sm font-semibold text-white
                   hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {{isSubmitting ? "Creating..." : "Create User"}}
      </button>
    </form>
  );
}}
```

#### 4. Typed API client (correct pattern)

```ts
// frontend/src/lib/api/users.ts
const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

export interface User {{
  id: string;
  email: string;
  isActive: boolean;
  createdAt: string;
}}

export interface CreateUserPayload {{
  email: string;
  password: string;
}}

export async function fetchUsers(userId: string): Promise<User> {{
  const res = await fetch(`${{API_BASE}}/api/v1/users/${{userId}}`, {{
    cache: "no-store",
  }});
  if (!res.ok) {{
    throw new Error(`Failed to fetch user ${{userId}}: ${{res.status}} ${{res.statusText}}`);
  }}
  return res.json() as Promise<User>;
}}

export async function createUser(payload: CreateUserPayload): Promise<User> {{
  const res = await fetch(`${{API_BASE}}/api/v1/users`, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(payload),
  }});
  if (!res.ok) {{
    throw new Error(`Failed to create user: ${{res.status}} ${{res.statusText}}`);
  }}
  return res.json() as Promise<User>;
}}
```

{FILE_OUTPUT_FORMAT}

{VALIDATION_INSTRUCTIONS}
"""

    # ---------------------------------------------------------------------- #
    # Execution
    # ---------------------------------------------------------------------- #

    async def execute(self, context: AgentContext) -> AgentResult:
        """Execute the frontend agent against the given context.

        Delegates the full lifecycle (token counting, LLM call, file block
        parsing, scope validation, result assembly) to ``BaseAgent.execute()``.

        Parameters
        ----------
        context:
            Fully populated AgentContext from the runner.

        Returns
        -------
        AgentResult
            Structured result carrying generated TypeScript/TSX modules,
            reasoning, and any errors.
        """
        logger.info(
            "frontend_agent_execute",
            task_id=context.task.task_id,
            objective=context.contract.objective[:120],
        )
        return await super().execute(context)
