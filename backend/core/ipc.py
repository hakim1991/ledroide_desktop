from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    def connect(self, ws: WebSocket):
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, data: dict):
        for ws in list(self.active):
            try:
                await ws.send_json(data)
            except Exception:
                self.active.discard(ws)


manager = ConnectionManager()
