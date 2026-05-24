"use client";

import { useEffect, useRef } from "react";
import { PipelineWebSocket } from "@/lib/websocket";
import { usePipelineStore } from "@/store/pipeline";
import type { ForgeEvent, AgentExecution } from "@/types";

export function usePipelineWebSocket(projectId: string | null) {
  const wsRef = useRef<PipelineWebSocket | null>(null);
  const appendEvent = usePipelineStore((s) => s.appendEvent);
  const upsertExecution = usePipelineStore((s) => s.upsertExecution);
  const setPipelineState = usePipelineStore((s) => s.setPipelineState);
  const setConnected = usePipelineStore((s) => s.setConnected);

  useEffect(() => {
    if (!projectId) return;

    const ws = new PipelineWebSocket(projectId);
    wsRef.current = ws;

    const unsub = ws.on((event: ForgeEvent) => {
      appendEvent(event);

      if (event.event_type === "task_completed" || event.event_type === "task_failed") {
        const exec = event.payload as unknown as AgentExecution;
        if (exec?.task_id) upsertExecution(exec);
      }

      if (event.event_type === "pipeline_state_update" && event.payload.state) {
        setPipelineState(event.payload.state as never);
      }
    });

    ws.connect();
    setConnected(true);

    return () => {
      unsub();
      ws.disconnect();
      setConnected(false);
    };
  }, [projectId, appendEvent, upsertExecution, setPipelineState, setConnected]);

  return wsRef.current;
}
