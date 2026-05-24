"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

const PHASES = [
  "Intent",
  "Clarification",
  "Specification",
  "Architecture",
  "Task Graph",
  "Assignment",
  "Execution",
  "Verification",
  "Deployment",
  "Monitoring",
  "Iteration",
];

export default function HomePage() {
  const [description, setDescription] = useState("");

  const handleStart = () => {
    if (!description.trim()) return;
    const projectId = `proj_${Date.now()}`;
    window.location.href = `/pipeline?project_id=${projectId}&description=${encodeURIComponent(description)}`;
  };

  return (
    <div className="mx-auto max-w-4xl px-6 py-16">
      <div className="mb-12 text-center">
        <h1 className="mb-4 text-4xl font-bold tracking-tight text-forge-text">
          Build anything with <span className="text-forge-accent">FORGE</span>
        </h1>
        <p className="text-lg text-forge-muted">
          Describe your project in plain English. FORGE handles the rest — architecture, code,
          tests, deployment.
        </p>
      </div>

      <Card className="mb-8">
        <CardHeader>
          <CardTitle>New Project</CardTitle>
        </CardHeader>
        <CardContent>
          <textarea
            className="mb-4 w-full resize-none rounded-lg border border-forge-border bg-forge-bg px-4 py-3 text-sm text-forge-text placeholder-forge-muted focus:border-forge-accent focus:outline-none focus:ring-1 focus:ring-forge-accent"
            rows={5}
            placeholder="Describe your project... e.g. 'Build a SaaS platform for managing e-commerce ad campaigns with AI-powered budget optimization, real-time analytics, and multi-channel support.'"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
          <Button onClick={handleStart} disabled={!description.trim()}>
            Start Building
          </Button>
        </CardContent>
      </Card>

      <div className="grid grid-cols-3 gap-4 sm:grid-cols-4 lg:grid-cols-6">
        {PHASES.map((phase, i) => (
          <div
            key={phase}
            className="flex flex-col items-center rounded-lg border border-forge-border bg-forge-surface p-3 text-center"
          >
            <span className="mb-1 text-xs font-semibold text-forge-accent">{i + 1}</span>
            <span className="text-xs text-forge-muted">{phase}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
