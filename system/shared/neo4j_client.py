"""Neo4j graph-database client for the FORGE platform.

Provides:
- An ``AsyncGraphDatabase`` driver singleton.
- A ``get_neo4j()`` async context manager for short-lived sessions.
- A ``NeoDB`` high-level helper class with typed methods for the operations
  used across the FORGE knowledge graph (repo intelligence, task graph, etc.).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
import logging
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession, Record

from system.config.settings import settings

logger = logging.getLogger(__name__)


# ========================================================================== #
# Driver singleton
# ========================================================================== #


@lru_cache(maxsize=1)
def _get_driver() -> AsyncDriver:
    """Create the Neo4j async driver once and cache it for the process lifetime."""
    return AsyncGraphDatabase.driver(
        settings.neo4j_url,
        auth=(settings.neo4j_user, settings.neo4j_password),
        max_connection_pool_size=50,
        connection_acquisition_timeout=30.0,
    )


# ========================================================================== #
# Context manager
# ========================================================================== #


@asynccontextmanager
async def get_neo4j() -> AsyncIterator[AsyncSession]:
    """Async context manager that yields a Neo4j ``AsyncSession``.

    Usage::

        async with get_neo4j() as session:
            result = await session.run("MATCH (n) RETURN count(n) AS total")
            record = await result.single()
    """
    driver = _get_driver()
    async with driver.session() as session:
        yield session


# ========================================================================== #
# NeoDB helper class
# ========================================================================== #


class NeoDB:
    """High-level async interface over the Neo4j graph database.

    All public methods open their own session so callers don't need to
    manage session lifecycle.  For bulk operations, obtain a session via
    ``get_neo4j()`` directly and pass it to Neo4j's transaction API.
    """

    # ------------------------------------------------------------------ #
    # Generic query runner
    # ------------------------------------------------------------------ #

    @staticmethod
    async def run_query(
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[Record]:
        """Execute an arbitrary Cypher query and return all result records.

        Args:
            cypher: Cypher query string.
            params: Optional dict of query parameters (named, ``$param``).

        Returns:
            A list of ``neo4j.Record`` objects.
        """
        async with get_neo4j() as session:
            result = await session.run(cypher, parameters=params or {})
            return await result.data()

    # ------------------------------------------------------------------ #
    # Node operations
    # ------------------------------------------------------------------ #

    @staticmethod
    async def create_node(
        label: str,
        props: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a node with the given label and properties.

        The node's ``id`` property is used as the unique identifier; callers
        are expected to populate ``props["id"]`` before calling this method.

        Returns:
            The properties of the newly created node.
        """
        cypher = f"CREATE (n:{label} $props) RETURN properties(n) AS node"
        async with get_neo4j() as session:
            result = await session.run(cypher, props=props)
            record = await result.single()
            if record is None:
                raise RuntimeError(f"Failed to create Neo4j node with label '{label}'")
            return record["node"]

    @staticmethod
    async def find_node(
        label: str,
        props: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Find the first node matching label + property filter.

        Returns:
            The node's properties as a dict, or ``None`` if not found.
        """
        # Build a WHERE clause from props
        conditions = " AND ".join(f"n.{k} = ${k}" for k in props)
        cypher = f"MATCH (n:{label}) WHERE {conditions} RETURN properties(n) AS node LIMIT 1"
        async with get_neo4j() as session:
            result = await session.run(cypher, **props)
            record = await result.single()
            return record["node"] if record else None

    @staticmethod
    async def upsert_node(
        label: str,
        match_props: dict[str, Any],
        set_props: dict[str, Any],
    ) -> dict[str, Any]:
        """MERGE a node on *match_props* and set *set_props* on match/create.

        Returns:
            The node's properties after the merge.
        """
        ", ".join(f"n.{k} = $match_{k}" for k in match_props)
        set_clause = ", ".join(f"n.{k} = $set_{k}" for k in set_props)

        params: dict[str, Any] = {f"match_{k}": v for k, v in match_props.items()}
        params.update({f"set_{k}": v for k, v in set_props.items()})

        # Build match props literal for MERGE
        match_literal = "{" + ", ".join(f"{k}: $match_{k}" for k in match_props) + "}"
        cypher = f"MERGE (n:{label} {match_literal}) SET {set_clause} RETURN properties(n) AS node"
        async with get_neo4j() as session:
            result = await session.run(cypher, **params)
            record = await result.single()
            if record is None:
                raise RuntimeError(f"Upsert failed for label '{label}'")
            return record["node"]

    # ------------------------------------------------------------------ #
    # Relationship operations
    # ------------------------------------------------------------------ #

    @staticmethod
    async def create_relationship(
        from_id: str,
        to_id: str,
        rel_type: str,
        props: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a directed relationship between two nodes identified by id.

        Args:
            from_id: ``id`` property of the source node.
            to_id: ``id`` property of the target node.
            rel_type: Relationship type label (e.g. ``DEPENDS_ON``).
            props: Optional properties on the relationship.

        Returns:
            A dict with ``from``, ``to``, ``type``, and ``props`` keys.
        """
        cypher = (
            "MATCH (a {id: $from_id}), (b {id: $to_id}) "
            f"CREATE (a)-[r:{rel_type} $props]->(b) "
            "RETURN properties(a) AS from_node, properties(b) AS to_node, "
            "properties(r) AS rel_props"
        )
        async with get_neo4j() as session:
            result = await session.run(
                cypher,
                from_id=from_id,
                to_id=to_id,
                props=props or {},
            )
            record = await result.single()
            if record is None:
                raise RuntimeError(
                    f"Could not create relationship {rel_type} between "
                    f"'{from_id}' and '{to_id}' — nodes may not exist."
                )
            return {
                "from": record["from_node"],
                "to": record["to_node"],
                "type": rel_type,
                "props": record["rel_props"],
            }

    # ------------------------------------------------------------------ #
    # Graph traversal
    # ------------------------------------------------------------------ #

    @staticmethod
    async def traverse_graph(
        start_id: str,
        max_depth: int = 3,
        rel_types: list[str] | None = None,
        direction: str = "OUTBOUND",
    ) -> list[dict[str, Any]]:
        """BFS/DFS traversal starting from *start_id* up to *max_depth* hops.

        Args:
            start_id: ``id`` property of the root node.
            max_depth: Maximum number of relationship hops to traverse.
            rel_types: Optional list of relationship types to follow.
                       If ``None``, all types are followed.
            direction: ``"OUTBOUND"``, ``"INBOUND"``, or ``"ANY"``.

        Returns:
            A list of dicts: ``{"node": {...}, "depth": int, "path": [...]}``.
        """
        if max_depth < 1 or max_depth > 10:
            raise ValueError("max_depth must be between 1 and 10")

        if direction not in {"OUTBOUND", "INBOUND", "ANY"}:
            raise ValueError("direction must be OUTBOUND, INBOUND, or ANY")

        rel_filter = ""
        if rel_types:
            rel_filter = ":" + "|".join(rel_types)

        cypher = (
            "MATCH (start {id: $start_id}) "
            f"CALL apoc.path.subgraphNodes(start, {{"
            f"  relationshipFilter: '{rel_filter or ''}', "
            f"  maxLevel: $max_depth, "
            f"  bfs: true"
            f"}}) YIELD node "
            "RETURN properties(node) AS node_props"
        )

        # Fallback without APOC for vanilla Neo4j
        cypher_no_apoc = (
            "MATCH p=(start {id: $start_id})"
            f"-[{rel_filter}*1..{max_depth}]->(n) "
            "RETURN properties(n) AS node_props, length(p) AS depth "
            "ORDER BY depth"
        )

        async with get_neo4j() as session:
            try:
                result = await session.run(cypher, start_id=start_id, max_depth=max_depth)
                records = await result.data()
            except Exception:
                # APOC not available — use plain Cypher path query
                result = await session.run(cypher_no_apoc, start_id=start_id)
                records = await result.data()

        return [
            {
                "node": r.get("node_props", {}),
                "depth": r.get("depth", -1),
            }
            for r in records
        ]

    # ------------------------------------------------------------------ #
    # Utility
    # ------------------------------------------------------------------ #

    @staticmethod
    async def delete_node(node_id: str) -> int:
        """Detach-delete a node and all its relationships.

        Returns:
            Number of nodes deleted (0 or 1).
        """
        cypher = "MATCH (n {id: $node_id}) DETACH DELETE n RETURN count(n) AS deleted"
        async with get_neo4j() as session:
            result = await session.run(cypher, node_id=node_id)
            record = await result.single()
            return record["deleted"] if record else 0

    @staticmethod
    async def ping() -> bool:
        """Return True if the Neo4j server is reachable."""
        try:
            records = await NeoDB.run_query("RETURN 1 AS ok")
            return bool(records and records[0].get("ok") == 1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j ping failed: %s", exc)
            return False


# ========================================================================== #
# Module-level convenience instance
# ========================================================================== #

neo4j_db = NeoDB()
"""Convenience singleton — import and use directly::

    from system.shared.neo4j_client import neo4j_db
    await neo4j_db.create_node("Task", {"id": "...", "name": "..."})
"""
