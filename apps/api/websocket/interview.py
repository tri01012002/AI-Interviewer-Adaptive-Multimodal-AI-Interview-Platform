"""Real-time WebSocket interview session endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, interview_id: str) -> None:
        await websocket.accept()
        self.active_connections[interview_id] = websocket

    def disconnect(self, interview_id: str) -> None:
        self.active_connections.pop(interview_id, None)

    async def send_event(self, interview_id: str, event: dict[str, Any]) -> None:
        socket = self.active_connections.get(interview_id)
        if socket is not None:
            await socket.send_text(json.dumps(event))


manager = ConnectionManager()


@router.websocket("/ws/interview/{interview_id}")
async def interview_socket(websocket: WebSocket, interview_id: str) -> None:
    await manager.connect(websocket, interview_id)
    try:
        await websocket.send_text(json.dumps({"type": "connected", "interview_id": interview_id, "status": "ready"}))
        while True:
            payload = await websocket.receive_text()
            data = json.loads(payload)
            await manager.send_event(interview_id, {"type": "message", "payload": data})
    except WebSocketDisconnect:
        manager.disconnect(interview_id)
