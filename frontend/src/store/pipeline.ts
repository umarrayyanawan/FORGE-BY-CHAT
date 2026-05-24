import { create } from "zustand";
import type { PipelineState, TaskNode, ForgeEvent, AgentExecution } from "@/types";

interface PipelineStore {
  projectId: string | null;
  pipelineState: PipelineState | null;
  tasks: TaskNode[];
  events: ForgeEvent[];
  executions: AgentExecution[];
  isConnected: boolean;

  setProjectId: (id: string) => void;
  setPipelineState: (state: PipelineState) => void;
  setTasks: (tasks: TaskNode[]) => void;
  appendEvent: (event: ForgeEvent) => void;
  upsertExecution: (exec: AgentExecution) => void;
  setConnected: (connected: boolean) => void;
  reset: () => void;
}

const initialState = {
  projectId: null,
  pipelineState: null,
  tasks: [],
  events: [],
  executions: [],
  isConnected: false,
};

export const usePipelineStore = create<PipelineStore>((set) => ({
  ...initialState,

  setProjectId: (id) => set({ projectId: id }),

  setPipelineState: (pipelineState) => set({ pipelineState }),

  setTasks: (tasks) => set({ tasks }),

  appendEvent: (event) =>
    set((s) => ({ events: [event, ...s.events].slice(0, 200) })),

  upsertExecution: (exec) =>
    set((s) => {
      const idx = s.executions.findIndex((e) => e.task_id === exec.task_id);
      if (idx >= 0) {
        const updated = [...s.executions];
        updated[idx] = exec;
        return { executions: updated };
      }
      return { executions: [exec, ...s.executions] };
    }),

  setConnected: (isConnected) => set({ isConnected }),

  reset: () => set(initialState),
}));
