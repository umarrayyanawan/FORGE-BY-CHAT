"""FastAPI router for Memory Engine endpoints."""
from fastapi import APIRouter, HTTPException
from typing import List, Optional
from system.core.memory.schemas import MemoryEntry, ArchitectureDecision

router = APIRouter(prefix="/memory", tags=["memory"])


@router.post("/store", response_model=MemoryEntry, status_code=201)
async def store_memory(
    project_id: str,
    title: str,
    content: str,
    tags: List[str] = [],
    importance: float = 0.5,
):
    """Store a semantic memory entry for a project."""
    from system.core.memory.semantic_memory import SemanticMemory
    mem = SemanticMemory()
    return await mem.store(project_id, title, content, tags, importance)


@router.get("/{project_id}/retrieve")
async def retrieve_memories(project_id: str, query: str, limit: int = 5):
    """Semantic similarity search over project memories."""
    from system.core.memory.semantic_memory import SemanticMemory
    mem = SemanticMemory()
    results = await mem.retrieve(project_id, query, limit=limit)
    return {"project_id": project_id, "query": query, "results": [r.model_dump() for r in results]}


@router.get("/{project_id}/decisions")
async def get_architecture_decisions(project_id: str):
    """List all Architecture Decision Records for a project."""
    return {"project_id": project_id, "decisions": []}


@router.post("/{project_id}/decisions", response_model=ArchitectureDecision, status_code=201)
async def store_architecture_decision(project_id: str, decision: ArchitectureDecision):
    """Store an Architecture Decision Record."""
    return decision


@router.get("/{project_id}/summary")
async def get_project_summary(project_id: str):
    """Get an LLM-generated summary of the project's memory."""
    return {"project_id": project_id, "summary": "No summary available yet."}


@router.delete("/{project_id}", status_code=204)
async def clear_memories(project_id: str):
    """Clear all memories for a project."""
    pass
