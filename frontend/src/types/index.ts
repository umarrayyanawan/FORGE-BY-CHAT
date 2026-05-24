export type AgentType =
  | "architect"
  | "backend"
  | "frontend"
  | "infra"
  | "qa"
  | "security"
  | "docs"
  | "refactor";

export type TaskStatus =
  | "pending"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type PipelinePhase =
  | "intent"
  | "clarification"
  | "specification"
  | "architecture"
  | "task_graph"
  | "assignment"
  | "execution"
  | "verification"
  | "deployment"
  | "monitoring"
  | "iteration";

export interface ProjectIntent {
  intent_id: string;
  project_name: string;
  description: string;
  features: string[];
  tech_preferences: Record<string, string>;
  confidence_score: number;
  status: string;
}

export interface TaskNode {
  task_id: string;
  title: string;
  description: string;
  agent_type: AgentType;
  status: TaskStatus;
  dependencies: string[];
  priority: string;
  created_at: string;
}

export interface PipelineState {
  project_id: string;
  current_phase: PipelinePhase;
  phases_completed: PipelinePhase[];
  tasks_total: number;
  tasks_completed: number;
  tasks_failed: number;
  is_running: boolean;
}

export interface AgentExecution {
  task_id: string;
  agent_type: AgentType;
  status: TaskStatus;
  started_at?: string;
  completed_at?: string;
  tokens_used: number;
  files_written: string[];
  error?: string;
}

export interface ForgeEvent {
  event_id: string;
  event_type: string;
  project_id: string;
  payload: Record<string, unknown>;
  timestamp: string;
}

export interface User {
  user_id: string;
  email: string;
  username: string;
}

export interface AuthTokens {
  access_token: string;
  token_type: string;
}
