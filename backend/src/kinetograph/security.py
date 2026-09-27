"""Local API boundary shared by HTTP and WebSocket connections."""

from secrets import compare_digest
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from kinetograph.config import settings

LOCAL_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}


class LocalAccessMiddleware:
    """Require the desktop's per-launch token, including for media and sockets.

    Standalone development allows local Vite origins and non-browser clients.
    CORS alone does not prevent cross-origin writes or WebSocket connections.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        try:
            host = urlsplit("//" + headers.get("host", "")).hostname
        except ValueError:
            host = None
        origin = headers.get("origin")
        token = settings.kinetograph_api_token
        authorized = host in {"localhost", "127.0.0.1", "::1"}
        if token:
            authorized &= compare_digest(
                headers.get("x-kinetograph-token", "").encode(), token.encode()
            )
        else:
            authorized &= origin is None or origin in LOCAL_ORIGINS

        if authorized:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            response = JSONResponse(
                {"detail": "Local application access required"}, status_code=403
            )
            await response(scope, receive, send)
