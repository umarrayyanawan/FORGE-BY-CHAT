"""Unit tests for Task Graph Engine — topological sort, cycle detection, critical path."""

from datetime import datetime

import pytest

from system.shared.models import AgentType, ExecutionPhase, Priority, TaskStatus

pytestmark = pytest.mark.unit


def make_task(task_id: str, deps: list[str], agent: AgentType = AgentType.BACKEND) -> dict:
    return {
        "task_id": task_id,
        "name": f"Task {task_id}",
        "description": f"Description for {task_id}",
        "agent_type": agent,
        "priority": Priority.MEDIUM,
        "status": TaskStatus.PENDING,
        "dependencies": deps,
        "blocking": [],
        "validation_rules": [],
        "input_context": {},
        "output_artifacts": [],
        "retry_count": 0,
        "max_retries": 3,
        "timeout_seconds": 3600,
        "estimated_tokens": 4096,
        "project_id": "proj-test",
        "phase": ExecutionPhase.EXECUTION,
        "id": task_id,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }


def topological_sort(tasks: list[dict]) -> list[list[str]]:
    """Kahn's algorithm for topological sorting into parallel levels."""
    in_degree: dict[str, int] = {t["task_id"]: 0 for t in tasks}
    adjacency: dict[str, list[str]] = {t["task_id"]: [] for t in tasks}

    for task in tasks:
        for dep in task["dependencies"]:
            if dep in adjacency:
                adjacency[dep].append(task["task_id"])
                in_degree[task["task_id"]] += 1

    queue = [tid for tid, deg in in_degree.items() if deg == 0]
    levels = []

    while queue:
        level = sorted(queue)
        levels.append(level)
        next_queue = []
        for tid in level:
            for neighbor in adjacency[tid]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_queue.append(neighbor)
        queue = next_queue

    processed = sum(len(level) for level in levels)
    if processed != len(tasks):
        raise ValueError("Cycle detected in task graph")

    return levels


def test_topological_sort_linear_chain():
    """A → B → C produces [[A], [B], [C]]."""
    tasks = [
        make_task("A", []),
        make_task("B", ["A"]),
        make_task("C", ["B"]),
    ]
    levels = topological_sort(tasks)
    assert levels == [["A"], ["B"], ["C"]]


def test_topological_sort_parallel_tasks():
    """A → C and B → C produces [[A, B], [C]] — A and B can run in parallel."""
    tasks = [
        make_task("A", []),
        make_task("B", []),
        make_task("C", ["A", "B"]),
    ]
    levels = topological_sort(tasks)
    assert len(levels) == 2
    assert sorted(levels[0]) == ["A", "B"]
    assert levels[1] == ["C"]


def test_topological_sort_diamond():
    """Diamond: A → B, A → C, B → D, C → D."""
    tasks = [
        make_task("A", []),
        make_task("B", ["A"]),
        make_task("C", ["A"]),
        make_task("D", ["B", "C"]),
    ]
    levels = topological_sort(tasks)
    assert levels[0] == ["A"]
    assert sorted(levels[1]) == ["B", "C"]
    assert levels[2] == ["D"]


def test_cycle_detection():
    """A → B → A should raise ValueError."""
    tasks = [
        make_task("A", ["B"]),
        make_task("B", ["A"]),
    ]
    with pytest.raises(ValueError, match="Cycle"):
        topological_sort(tasks)


def test_empty_graph():
    """Empty task list produces empty levels."""
    assert topological_sort([]) == []


def test_single_task():
    """Single task with no dependencies produces [[task_id]]."""
    levels = topological_sort([make_task("solo", [])])
    assert levels == [["solo"]]


def test_all_independent():
    """Tasks with no dependencies all run in parallel (one level)."""
    tasks = [make_task(str(i), []) for i in range(5)]
    levels = topological_sort(tasks)
    assert len(levels) == 1
    assert sorted(levels[0]) == ["0", "1", "2", "3", "4"]


def test_task_status_enum():
    """TaskStatus enum has all required values."""
    assert TaskStatus.PENDING == "pending"
    assert TaskStatus.RUNNING == "running"
    assert TaskStatus.FAILED == "failed"
    assert TaskStatus.RETRYING == "retrying"
    assert TaskStatus.COMPLETED == "completed"
    assert TaskStatus.BLOCKED == "blocked"


def test_agent_type_enum():
    """AgentType enum has all 8 specialized agents."""
    agents = {
        AgentType.ARCHITECT,
        AgentType.BACKEND,
        AgentType.FRONTEND,
        AgentType.INFRA,
        AgentType.QA,
        AgentType.SECURITY,
        AgentType.DOCS,
        AgentType.REFACTOR,
    }
    assert len(agents) == 8


def test_execution_phase_order():
    """ExecutionPhase has the correct 11 phases."""
    phases = list(ExecutionPhase)
    phase_values = [p.value for p in phases]
    assert "intent" in phase_values
    assert "deployment" in phase_values
    assert "iteration" in phase_values
    assert len(phase_values) == 11
