"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { pipelineApi, taskApi } from "@/lib/api";
import { usePipelineStore } from "@/store/pipeline";

export function usePipelineStatus(projectId: string | null) {
  const setPipelineState = usePipelineStore((s) => s.setPipelineState);

  return useQuery({
    queryKey: ["pipeline", "status", projectId],
    queryFn: async () => {
      if (!projectId) return null;
      const data = await pipelineApi.getStatus(projectId);
      setPipelineState(data.state);
      return data;
    },
    enabled: !!projectId,
    refetchInterval: 3000,
  });
}

export function useStartPipeline() {
  const queryClient = useQueryClient();
  const setProjectId = usePipelineStore((s) => s.setProjectId);

  return useMutation({
    mutationFn: ({ projectId, description }: { projectId: string; description: string }) =>
      pipelineApi.start(projectId, description),
    onSuccess: (data, { projectId }) => {
      setProjectId(projectId);
      queryClient.invalidateQueries({ queryKey: ["pipeline", "status", projectId] });
    },
  });
}

export function useTasks(graphId: string | null) {
  const setTasks = usePipelineStore((s) => s.setTasks);

  return useQuery({
    queryKey: ["tasks", "ready", graphId],
    queryFn: async () => {
      if (!graphId) return [];
      const data = await taskApi.getReady(graphId);
      setTasks(data.tasks || []);
      return data.tasks || [];
    },
    enabled: !!graphId,
    refetchInterval: 5000,
  });
}
