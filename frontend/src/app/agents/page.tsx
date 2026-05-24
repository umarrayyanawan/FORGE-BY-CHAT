"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { AgentType } from "@/types";

const AGENT_DESCRIPTIONS: Record<AgentType, string> = {
  architect: "Stack decisions, service topology, ADRs",
  backend: "FastAPI, SQLAlchemy, migrations, tests",
  frontend: "Next.js, TypeScript, Tailwind, Zustand",
  infra: "Docker, Kubernetes, Terraform, CI/CD",
  qa: "pytest, coverage, mocking strategies",
  security: "OWASP review, auth patterns, input validation",
  docs: "API docs, runbooks, architecture documentation",
  refactor: "Code quality, patterns, backward-compatible improvements",
};

const AGENTS: AgentType[] = [
  "architect", "backend", "frontend", "infra", "qa", "security", "docs", "refactor",
];

export default function AgentsPage() {
  const { data } = useQuery({
    queryKey: ["agents", "capabilities"],
    queryFn: () => api.get("/agents/capabilities").then((r) => r.data),
    retry: false,
  });

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <h1 className="mb-6 text-2xl font-bold text-forge-text">Agent Fleet</h1>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {AGENTS.map((agent) => (
          <Card key={agent}>
            <CardHeader>
              <CardTitle className="capitalize">{agent}</CardTitle>
              <Badge variant="info">active</Badge>
            </CardHeader>
            <CardContent>
              <p className="text-xs text-forge-muted">{AGENT_DESCRIPTIONS[agent]}</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
