"""Exercise HTTP and WebSocket boundaries without starting the media pipeline."""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kinetograph.config import settings
from kinetograph.security import LocalAccessMiddleware


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, 'kinetograph_api_token', 'test-session-token')
    app = FastAPI()
    app.add_middleware(LocalAccessMiddleware)
    @app.get('/health')
    async def health():
        return {'ok': True}
    @app.websocket('/ws')
    async def socket(ws: WebSocket):
        await ws.accept()
        await ws.send_text('ok')
    return TestClient(app, base_url='http://127.0.0.1')


def test_http_requires_session_token(client):
    assert client.get('/health').status_code == 403
    assert client.get('/health', headers={'X-Kinetograph-Token': 'wrong'}).status_code == 403
    assert client.get('/health', headers={'X-Kinetograph-Token': 'test-session-token'}).status_code == 200


def test_dns_rebinding_host_rejected(client):
    assert client.get('/health', headers={
        'Host': 'attacker.example', 'X-Kinetograph-Token': 'test-session-token',
    }).status_code == 403


def test_websocket_requires_same_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('ws://127.0.0.1/ws'):
            pass
    with client.websocket_connect('ws://127.0.0.1/ws', headers={
        'X-Kinetograph-Token': 'test-session-token',
    }) as ws:
        assert ws.receive_text() == 'ok'


@pytest.mark.parametrize('origin', ['https://attacker.example', 'null'])
def test_external_browser_blocked_in_development(client, monkeypatch, origin):
    monkeypatch.setattr(settings, 'kinetograph_api_token', '')
    assert client.get('/health', headers={'Origin': origin}).status_code == 403
    assert client.get('/health', headers={'Origin': 'http://localhost:5173'}).status_code == 200
