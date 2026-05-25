"use client";

import { Suspense, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { usePipelineStatus, useStartPipeline } from "@/hooks/use-pipeline";
import { usePipelineWebSocket } from "@/hooks/use-websocket";
import { usePipelineStore } from "@/store/pipeline";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatDate } from "@/lib/utils";

function PipelineContent() {
  const params = useSearchParams();
  const projectId = params.get("project_id");
  const description = params.get("description");

  const { data: statusData } = usePipelineStatus(projectId);
  const startPipeline = useStartPipeline();
  const events = usePipelineStore((s) => s.events);
  const isConnected = usePipelineStore((s) => s.isConnected);
  const pipelineState = usePipelineStore((s) => s.pipelineState);

  usePipelineWebSocket(projectId);

  useEffect(() => {
    if (projectId && description && !statusData) {
      startPipeline.mutate({ projectId, description });
    }
  }, [projectId, description]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!projectId) {
    return (
      <div className="mx-auto max-w-2xl px-6 py-10 text-forge-muted">
        No project selected.{" "}
        <a href="/" className="text-forge-accent underline">
          Start a new project
        </a>
        .
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-forge-text">Pipeline</h1>
          <p className="font-mono text-xs text-forge-muted">{projectId}</p>
        </div>
        <div className="flex items-center gap-3">
          <div
            className={`h-2 w-2 rounded-full ${isConnected ? "bg-green-400" : "bg-red-400"}`}
          />
          <span className="text-xs text-forge-muted">
            {isConnected ? "Live" : "Disconnected"}
          </span>
        </div>
      </div>

      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Current Phase</CardTitle>
          </CardHeader>
          <CardContent>
            <span className="font-semibold text-forge-accent">
              {pipelineState?.current_phase ?? "—"}
            </span>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Tasks</CardTitle>
          </CardHeader>
          <CardContent>
            <span className="text-forge-text">
              {pipelineState?.tasks_completed ?? 0} /{" "}
              {pipelineState?.tasks_total ?? 0}
            </span>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Status</CardTitle>
          </CardHeader>
          <CardContent>
            <Badge variant={pipelineState?.is_running ? "info" : "default"}>
              {pipelineState?.is_running ? "Running" : "Idle"}
            </Badge>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Event Stream</CardTitle>
        </CardHeader>
        <CardContent>
          {events.length === 0 ? (
            <p className="text-sm text-forge-muted">Waiting for events...</p>
          ) : (
            <div className="max-h-80 space-y-2 overflow-y-auto">
              {events.map((e) => (
                <div
                  key={e.event_id}
                  className="flex items-start gap-3 rounded-lg bg-forge-bg p-2 text-xs"
                >
                  <Badge variant="info">{e.event_type}</Badge>
                  <span className="font-mono text-forge-muted">
                    {formatDate(e.timestamp)}
                  </span>
                  <span className="truncate text-forge-text">
                    {JSON.stringify(e.payload).slice(0, 80)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export default function PipelinePage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-5xl px-6 py-10 text-forge-muted">
          Loading pipeline...
        </div>
      }
    >
      <PipelineContent />
    </Suspense>
  );
}
