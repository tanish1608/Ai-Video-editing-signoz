"""Smoke-test a built sidecar using an isolated workspace and no API credentials."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    binary = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix='kinetograph-smoke-') as directory:
        project = Path(directory).resolve()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = dict(os.environ, KINETOGRAPH_PROJECT_DIR=directory,
                   KINETOGRAPH_ENV_FILE=str(project / 'missing.env'),
                   KINETOGRAPH_API_TOKEN='smoke-test-token', API_PORT=str(port),
                   API_HOST='127.0.0.1', OTEL_SDK_DISABLED='true')
        for key in ('GEMINI_API_KEY', 'ELEVENLABS_API_KEY', 'NVIDIA_API_KEY', 'HF_TOKEN'):
            env.pop(key, None)
        with (project / 'sidecar.log').open('w+') as log:
            process = subprocess.Popen([str(binary)], env=env, cwd=directory,
                                       stdout=log, stderr=subprocess.STDOUT)
            def request(path, body=None, method=None):
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(base + path, data=data, method=method,
                    headers={'X-Kinetograph-Token': 'smoke-test-token',
                             'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=3) as response:
                    return json.load(response)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f'Sidecar exited: {process.returncode}')
                    try:
                        assert request('/api/health')['status'] == 'ok'
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.25)
                else:
                    raise RuntimeError('Sidecar did not become ready')
                try:
                    urllib.request.urlopen(base + '/api/config')
                    raise AssertionError('Unauthenticated request was accepted')
                except urllib.error.HTTPError as error:
                    assert error.code == 403
                assert Path(request('/api/config')['project_dir']) == project
                edit = {'title': 'Saved A', 'clips': [{'clip_id': 'a', 'source_file': 'a.mp4',
                                                     'in_ms': 0, 'out_ms': 1000}]}
                request('/api/paper-edit', edit, 'PUT')
                other = project / 'other'
                other.mkdir()
                request('/api/project/set-dir', {'project_dir': str(other)})
                request('/api/project/set-dir', {'project_dir': str(project)})
                restored = request('/api/paper-edit')
                assert restored['title'] == 'Saved A', restored
                assert len(restored['clips']) == 1, restored
                print('Sidecar smoke passed: startup, auth, writable paths, project round-trip')
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    main()
