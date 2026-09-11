import http.server
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import watchdog


class HealthyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture()
def healthy_base_url():
    server = http.server.HTTPServer(("127.0.0.1", 0), HealthyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{port}".format(port=server.server_address[1])
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def closed_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def make_service(name, url, probe="http", restart_command="echo restarted", cooldown=0, threshold=2):
    return {
        "name": name,
        "url": url,
        "health_path": "/health",
        "probe": probe,
        "timeout": 2,
        "restart_command": restart_command,
        "cooldown": cooldown,
        "failure_threshold": threshold,
    }


def make_config(services, tmp_path, threshold=2):
    return {
        "check_interval": 1,
        "failure_threshold": threshold,
        "incidents_file": str(tmp_path / "incidents.jsonl"),
        "telegram": {"enabled": False},
        "services": services,
    }


def test_probe_http_healthy(healthy_base_url):
    service = make_service("web", healthy_base_url)
    assert watchdog.probe_service(service, 2) is True


def test_probe_http_unhealthy_closed_port():
    service = make_service("down", "http://127.0.0.1:{port}".format(port=closed_port()))
    assert watchdog.probe_service(service, 1) is False


def test_probe_tcp_healthy(healthy_base_url):
    host_port = healthy_base_url.replace("http://", "")
    service = make_service("tcp-ok", host_port, probe="tcp")
    assert watchdog.probe_service(service, 2) is True


def test_probe_tcp_unhealthy_closed_port():
    service = make_service("tcp-down", "127.0.0.1:{port}".format(port=closed_port()), probe="tcp")
    assert watchdog.probe_service(service, 1) is False


def test_restart_command_recorded_after_threshold(tmp_path, monkeypatch):
    port = closed_port()
    service = make_service("flaky", "http://127.0.0.1:{port}".format(port=port))
    config = make_config([service], tmp_path)
    state = {"failures": {}, "last_restart": {}}
    recorded = []

    class Completed:
        returncode = 0
        stdout = "restarted"
        stderr = ""

    def fake_run(command, shell, capture_output, text, timeout):
        recorded.append(command)
        return Completed()

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    first = watchdog.run_all_services(config, state, 1, str(tmp_path / "incidents.jsonl"), False, now=1000.0)
    assert first[0]["restarted"] is False
    assert recorded == []
    second = watchdog.run_all_services(config, state, 1, str(tmp_path / "incidents.jsonl"), False, now=1001.0)
    assert second[0]["restarted"] is True
    assert recorded == ["echo restarted"]
    incidents = (tmp_path / "incidents.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(incidents) == 1
    entry = json.loads(incidents[0])
    assert entry["service"] == "flaky"
    assert entry["returncode"] == 0
    assert entry["restarted"] is True


def test_cooldown_blocks_repeat_restart(tmp_path, monkeypatch):
    port = closed_port()
    service = make_service("cool", "http://127.0.0.1:{port}".format(port=port), cooldown=600, threshold=1)
    config = make_config([service], tmp_path, threshold=1)
    state = {"failures": {}, "last_restart": {}}
    calls = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, shell, capture_output, text, timeout):
        calls.append(command)
        return Completed()

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    incidents = str(tmp_path / "incidents.jsonl")
    watchdog.run_all_services(config, state, 1, incidents, False, now=2000.0)
    assert len(calls) == 1
    result = watchdog.run_all_services(config, state, 1, incidents, False, now=2001.0)
    assert result[0]["restarted"] is False
    assert len(calls) == 1


def test_healthy_resets_failure_count(tmp_path):
    service = make_service("reset", "http://127.0.0.1:1", threshold=5)
    config = make_config([service], tmp_path, threshold=5)
    state = {"failures": {"reset": 4}, "last_restart": {}}
    original = watchdog.probe_service
    watchdog.probe_service = lambda service, timeout: True
    try:
        result = watchdog.run_all_services(config, state, 1, str(tmp_path / "incidents.jsonl"), False, now=3000.0)
    finally:
        watchdog.probe_service = original
    assert result[0]["healthy"] is True
    assert result[0]["failures"] == 0


def test_telegram_mocked(monkeypatch):
    seen = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=10):
        seen["url"] = request.full_url
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)
    telegram = {"enabled": True, "bot_token": "token123", "chat_id": "42", "parse_mode": "HTML"}
    assert watchdog.send_telegram_message(telegram, "<b>hi</b>") is True
    assert seen["url"] == "https://api.telegram.org/bottoken123/sendMessage"
    assert seen["payload"]["chat_id"] == "42"
    assert seen["payload"]["parse_mode"] == "HTML"


def test_telegram_disabled_sends_nothing():
    assert watchdog.send_telegram_message({"enabled": False}, "hi") is False
    assert watchdog.send_telegram_message({}, "hi") is False


def test_dry_run_avoids_network(monkeypatch):
    def explode(service, timeout):
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(watchdog, "probe_service", explode)
    config = make_config([make_service("dry", "http://127.0.0.1:9")], Path.cwd())
    results = watchdog.check_all_services(config, 1, True)
    assert results[0]["dry_run"] is True
    assert results[0]["healthy"] is None
