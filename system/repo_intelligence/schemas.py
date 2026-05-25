"""Pydantic schemas for the FORGE Repo Intelligence Engine."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from system.shared.models import BaseForgeModel


class CodeChunk(BaseForgeModel):
    """A semantic unit parsed from a source file."""

    chunk_id: str
    file_path: str
    chunk_type: str  # "function", "class", "method", "module", "import"
    name: str
    content: str
    start_line: int
    end_line: int
    language: str  # "python", "typescript", "javascript"
    docstring: str | None = None
    dependencies: list[str] = []  # imported modules/functions
    exports: list[str] = []  # exported symbols
    embedding: list[float] | None = None
    project_id: str


class FileNode(BaseForgeModel):
    """Metadata about a source file in the repo."""

    path: str
    language: str
    size_bytes: int
    chunk_count: int
    import_count: int
    export_count: int
    last_modified: datetime
    project_id: str


class DependencyEdge(BaseForgeModel):
    """A directed dependency relationship between two files."""

    from_file: str
    to_file: str
    dependency_type: str  # "import", "extends", "implements", "uses"
    symbols: list[str] = []  # specific symbols imported


class ArchitectureNode(BaseForgeModel):
    """A high-level node in the project architecture graph."""

    node_id: str
    node_type: str  # "service", "module", "class", "function", "database", "api"
    name: str
    file_path: str
    properties: dict[str, Any] = {}


class SearchResult(BaseForgeModel):
    """A single result from semantic search."""

    chunk: CodeChunk
    similarity_score: float
    file_path: str
    context: str  # surrounding lines


class IndexingResult(BaseForgeModel):
    """Statistics returned after indexing a project."""

    project_id: str
    files_indexed: int
    chunks_created: int
    time_seconds: float
    errors: list[str] = []
