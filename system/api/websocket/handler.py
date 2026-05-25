import asyncio
import json

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from system.observability.logging.logger import get_logger

logger = get_logger(__name__)
router = APIRouter()


class ConnectionManager:
    """
    Manages WebSocket connections keyed by project_id.
    Supports broadcast to all clients for a project and direct send.
    Thread-safe: asyncio single-threaded event loop; no locking needed.
    """

    def __init__(self) -> None:
        # project_id -> list of active WebSocket connections
        self.active: dict[str, list[WebSocket]] = {}

    async def connect(self, ws: WebSocket, project_id: str) -> None:
        """Accept connection and register it under project_id."""
        await ws.accept()
        self.active.setdefault(project_id, []).append(ws)
        logger.info(
            "WebSocket connected",
            project_id=project_id,
            connections=len(self.active[project_id]),
        )

    def disconnect(self, ws: WebSocket, project_id: str) -> None:
        """Remove a connection. Clean up empty project buckets."""
        if project_id in self.active:
            self.active[project_id] = [c for c in self.active[project_id] if c is not ws]
            if not self.active[project_id]:
                del self.active[project_id]
                logger.info("No more connections for project", project_id=project_id)

    async def broadcast(self, project_id: str, message: dict) -> None:
        """Send a message to all connected clients for a project."""
        connections = list(self.active.get(project_id, []))
        dead: list[WebSocket] = []
        for ws in connections:
            try:
                await ws.send_json(message)
            except Exception as exc:
                logger.warning(
                    "Failed to send to WebSocket, marking dead",
                    project_id=project_id,
                    error=str(exc),
                )
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, project_id)

    async def send(self, ws: WebSocket, message: dict) -> None:
        """Send a message to a single WebSocket connection."""
        await ws.send_json(message)

    def connection_count(self, project_id: str) -> int:
        """Return number of active connections for a project."""
        return len(self.active.get(project_id, []))

    def all_projects(self) -> list[str]:
        """Return list of project IDs with active connections."""
        return list(self.active.keys())


# Singleton shared across the application lifetime
manager = ConnectionManager()


@router.websocket("/ws/pipeline/{project_id}")
async def websocket_pipeline(
    websocket: WebSocket,
    project_id: str,
    token: str | None = Query(default=None, description="Optional JWT bearer token"),
) -> None:
    """
    WebSocket endpoint for real-time pipeline event streaming.

    Clients connect here and receive:
    - Pipeline phase transitions
    - Task state changes (pending → running → completed/failed)
    - Agent events (LLM call started, code generated, etc.)
    - Heartbeat frames every 30 seconds to keep the connection alive

    Messages sent by the client:
    - {"type": "ping"}  → server responds with {"type": "pong"}
    - {"type": "subscribe", "events": [...]}  → filter to specific event types (future)
    """
    # Optional JWT validation — if token is provided, verify it
    if token:
        try:
            from system.api.auth.jwt import verify_token

            verify_token(token)
        except Exception as exc:
            await websocket.close(code=4001, reason="Unauthorized")
            logger.warning(
                "WebSocket rejected: invalid token",
                project_id=project_id,
                error=str(exc),
            )
            return

    await manager.connect(websocket, project_id)
    try:
        # Send initial connected acknowledgement
        await manager.send(
            websocket,
            {
                "type": "connected",
                "project_id": project_id,
                "connections": manager.connection_count(project_id),
            },
        )

        while True:
            try:
                # Wait up to 30 seconds for a client message; send heartbeat on timeout
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await manager.send(
                        websocket,
                        {"type": "error", "message": "Invalid JSON"},
                    )
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "ping":
                    await manager.send(websocket, {"type": "pong"})

                elif msg_type == "subscribe":
                    # Future: honour event-type filters per connection
                    await manager.send(
                        websocket,
                        {"type": "subscribed", "events": msg.get("events", [])},
                    )

                elif msg_type == "status":
                    await manager.send(
                        websocket,
                        {
                            "type": "status",
                            "project_id": project_id,
                            "connections": manager.connection_count(project_id),
                        },
                    )

                else:
                    logger.debug(
                        "Unknown WebSocket message type",
                        project_id=project_id,
                        msg_type=msg_type,
                    )

            except TimeoutError:
                # Send heartbeat so client knows we're alive and to keep NAT alive
                await manager.send(websocket, {"type": "heartbeat"})

    except WebSocketDisconnect:
        manager.disconnect(websocket, project_id)
        logger.info("WebSocket disconnected", project_id=project_id)

    except Exception as exc:
        logger.error(
            "WebSocket error",
            project_id=project_id,
            error=str(exc),
            exc_info=True,
        )
        manager.disconnect(websocket, project_id)
