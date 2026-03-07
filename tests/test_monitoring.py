"""
tests/test_monitoring.py

Tests for monitoring/monitor_services.py — Phase 4.
Covers: ping_check, tcp_check, http_check, check_service,
        should_alert, build_alert_message, load_state, save_state, run_once.
"""

import json
import socket
import subprocess
import sys
import os
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitoring.monitor_services import (
    ping_check,
    tcp_check,
    http_check,
    check_service,
    should_alert,
    build_alert_message,
    load_state,
    save_state,
    run_once,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_proc(returncode=0):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = ""
    return r


def _make_http_response(status=200, body=b"Hello World"):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ── ping_check ─────────────────────────────────────────────────────────────────

class TestPingCheck:
    @patch("monitoring.monitor_services.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = _make_proc(returncode=0)
        result = ping_check("8.8.8.8", timeout=2)
        assert result["ok"] is True
        assert result["detail"] == "reachable"
        assert result["latency_ms"] is not None

    @patch("monitoring.monitor_services.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = _make_proc(returncode=1)
        result = ping_check("192.0.2.0", timeout=2)
        assert result["ok"] is False
        assert result["detail"] == "unreachable"

    @patch("monitoring.monitor_services.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ping", timeout=2)
        result = ping_check("192.0.2.0", timeout=2)
        assert result["ok"] is False
        assert result["detail"] == "timeout"
        assert result["latency_ms"] is None

    @patch("monitoring.monitor_services.subprocess.run")
    def test_os_error(self, mock_run):
        mock_run.side_effect = OSError("ping not found")
        result = ping_check("8.8.8.8", timeout=2)
        assert result["ok"] is False
        assert "ping not found" in result["detail"]

    @patch("monitoring.monitor_services.subprocess.run")
    def test_returns_float_latency_on_success(self, mock_run):
        mock_run.return_value = _make_proc(returncode=0)
        result = ping_check("8.8.8.8", timeout=2)
        assert isinstance(result["latency_ms"], float)


# ── tcp_check ──────────────────────────────────────────────────────────────────

class TestTcpCheck:
    @patch("monitoring.monitor_services.socket.create_connection")
    def test_success(self, mock_conn):
        mock_conn.return_value.__enter__ = MagicMock(return_value=None)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        result = tcp_check("localhost", 80, timeout=2)
        assert result["ok"] is True
        assert result["detail"] == "connected"
        assert result["latency_ms"] is not None

    @patch("monitoring.monitor_services.socket.create_connection")
    def test_connection_refused(self, mock_conn):
        mock_conn.side_effect = ConnectionRefusedError()
        result = tcp_check("localhost", 9999, timeout=2)
        assert result["ok"] is False
        assert result["detail"] == "connection refused"

    @patch("monitoring.monitor_services.socket.create_connection")
    def test_timeout(self, mock_conn):
        mock_conn.side_effect = socket.timeout()
        result = tcp_check("localhost", 80, timeout=2)
        assert result["ok"] is False
        assert result["detail"] == "timeout"

    @patch("monitoring.monitor_services.socket.create_connection")
    def test_os_error(self, mock_conn):
        mock_conn.side_effect = OSError("network unreachable")
        result = tcp_check("localhost", 80, timeout=2)
        assert result["ok"] is False
        assert "network unreachable" in result["detail"]

    @patch("monitoring.monitor_services.socket.create_connection")
    def test_called_with_correct_args(self, mock_conn):
        mock_conn.return_value.__enter__ = MagicMock(return_value=None)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        tcp_check("myhost", 443, timeout=3)
        mock_conn.assert_called_once_with(("myhost", 443), timeout=3)


# ── http_check ─────────────────────────────────────────────────────────────────

class TestHttpCheck:
    @patch("monitoring.monitor_services.urllib.request.urlopen")
    def test_success_200(self, mock_open):
        mock_open.return_value = _make_http_response(200, b"Welcome")
        result = http_check("http://example.com", expected_status=200)
        assert result["ok"] is True
        assert result["status_code"] == 200
        assert result["latency_ms"] is not None

    @patch("monitoring.monitor_services.urllib.request.urlopen")
    def test_keyword_found(self, mock_open):
        mock_open.return_value = _make_http_response(200, b"Hello World")
        result = http_check("http://example.com", expected_status=200, keyword="Hello")
        assert result["ok"] is True
        assert "keyword found" in result["detail"]

    @patch("monitoring.monitor_services.urllib.request.urlopen")
    def test_keyword_missing(self, mock_open):
        mock_open.return_value = _make_http_response(200, b"Hello World")
        result = http_check("http://example.com", expected_status=200, keyword="Missing")
        assert result["ok"] is False
        assert "keyword" in result["detail"]

    @patch("monitoring.monitor_services.urllib.request.urlopen")
    def test_wrong_status(self, mock_open):
        mock_open.return_value = _make_http_response(200, b"page")
        result = http_check("http://example.com", expected_status=201)
        assert result["ok"] is False

    @patch("monitoring.monitor_services.urllib.request.urlopen")
    def test_http_error_404(self, mock_open):
        mock_open.side_effect = urllib.error.HTTPError(
            url="http://example.com", code=404, msg="Not Found", hdrs=None, fp=None
        )
        result = http_check("http://example.com")
        assert result["ok"] is False
        assert result["status_code"] == 404
        assert "404" in result["detail"]

    @patch("monitoring.monitor_services.urllib.request.urlopen")
    def test_url_error(self, mock_open):
        mock_open.side_effect = urllib.error.URLError("connection refused")
        result = http_check("http://example.com")
        assert result["ok"] is False
        assert result["status_code"] is None

    @patch("monitoring.monitor_services.urllib.request.urlopen")
    def test_no_keyword_check_when_status_mismatch(self, mock_open):
        mock_open.return_value = _make_http_response(404, b"Not found")
        result = http_check("http://example.com", expected_status=200, keyword="hello")
        # status mismatch sets ok=False before keyword is checked
        assert result["ok"] is False
        assert "keyword" not in result["detail"]


# ── check_service ──────────────────────────────────────────────────────────────

class TestCheckService:
    @patch("monitoring.monitor_services.ping_check",
           return_value={"ok": True, "detail": "reachable", "latency_ms": 5.0})
    def test_dispatch_ping(self, mock_ping):
        svc = {"name": "DNS", "type": "ping", "host": "8.8.8.8"}
        result = check_service(svc, timeout=2)
        mock_ping.assert_called_once_with("8.8.8.8", 2)
        assert result["name"] == "DNS"
        assert result["type"] == "ping"
        assert "checked_at" in result

    @patch("monitoring.monitor_services.tcp_check",
           return_value={"ok": True, "detail": "connected", "latency_ms": 3.0})
    def test_dispatch_tcp(self, mock_tcp):
        svc = {"name": "SSH", "type": "tcp", "host": "localhost", "port": 22}
        result = check_service(svc, timeout=2)
        mock_tcp.assert_called_once_with("localhost", 22, 2)
        assert result["name"] == "SSH"
        assert result["type"] == "tcp"

    @patch("monitoring.monitor_services.http_check",
           return_value={"ok": True, "detail": "HTTP 200", "latency_ms": 10.0, "status_code": 200})
    def test_dispatch_http(self, mock_http):
        svc = {"name": "Web", "type": "http", "url": "http://example.com", "expected_status": 200}
        result = check_service(svc, timeout=2)
        mock_http.assert_called_once_with(
            "http://example.com", expected_status=200, keyword=None, timeout=2
        )
        assert result["name"] == "Web"
        assert result["type"] == "http"

    @patch("monitoring.monitor_services.http_check",
           return_value={"ok": True, "detail": "HTTP 200", "latency_ms": 10.0, "status_code": 200})
    def test_http_keyword_forwarded(self, mock_http):
        svc = {"name": "Web", "type": "http", "url": "http://example.com",
               "expected_status": 200, "keyword": "Welcome"}
        check_service(svc, timeout=2)
        mock_http.assert_called_once_with(
            "http://example.com", expected_status=200, keyword="Welcome", timeout=2
        )

    def test_unknown_type(self):
        svc = {"name": "X", "type": "ftp", "host": "ftp.example.com"}
        result = check_service(svc)
        assert result["ok"] is False
        assert "unknown type" in result["detail"]
        assert result["name"] == "X"
        assert "checked_at" in result

    @patch("monitoring.monitor_services.ping_check",
           return_value={"ok": True, "detail": "reachable", "latency_ms": 5.0})
    def test_checked_at_is_iso_format(self, mock_ping):
        svc = {"name": "DNS", "type": "ping", "host": "8.8.8.8"}
        result = check_service(svc)
        datetime.fromisoformat(result["checked_at"])   # must not raise


# ── should_alert ───────────────────────────────────────────────────────────────

class TestShouldAlert:
    def test_newly_down(self):
        state = {"svc": {"ok": True}}
        assert should_alert("svc", ok=False, state=state, cooldown_minutes=30) is True

    def test_recovery(self):
        state = {"svc": {"ok": False}}
        assert should_alert("svc", ok=True, state=state, cooldown_minutes=30) is True

    def test_still_up_no_alert(self):
        state = {"svc": {"ok": True}}
        assert should_alert("svc", ok=True, state=state, cooldown_minutes=30) is False

    def test_still_down_within_cooldown(self):
        recent = datetime.now(timezone.utc).isoformat()
        state = {"svc": {"ok": False, "last_alert_ts": recent}}
        assert should_alert("svc", ok=False, state=state, cooldown_minutes=30) is False

    def test_still_down_cooldown_expired(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        state = {"svc": {"ok": False, "last_alert_ts": old_ts}}
        assert should_alert("svc", ok=False, state=state, cooldown_minutes=30) is True

    def test_unknown_service_goes_down(self):
        # unknown = assume was UP → alert as newly down
        assert should_alert("new_svc", ok=False, state={}, cooldown_minutes=30) is True

    def test_unknown_service_is_up(self):
        # unknown = assume was UP → no spurious recovery alert
        assert should_alert("new_svc", ok=True, state={}, cooldown_minutes=30) is False

    def test_still_down_no_last_alert_ts(self):
        # down but no last_alert_ts recorded yet → no repeat alert
        state = {"svc": {"ok": False}}
        assert should_alert("svc", ok=False, state=state, cooldown_minutes=30) is False


# ── build_alert_message ────────────────────────────────────────────────────────

class TestBuildAlertMessage:
    def test_down_alert_subject_and_body(self):
        result = {"name": "Web", "type": "http", "ok": False, "detail": "HTTP 500"}
        state = {"Web": {"ok": True}}
        subject, body = build_alert_message(result, state)
        assert "[ALERT]" in subject
        assert "Web" in subject
        assert "DOWN" in subject
        assert "HTTP 500" in body

    def test_recovery_alert_subject_and_body(self):
        result = {
            "name": "Web", "type": "http", "ok": True,
            "detail": "HTTP 200", "latency_ms": 45.0,
        }
        state = {"Web": {"ok": False}}
        subject, body = build_alert_message(result, state)
        assert "[RECOVERY]" in subject
        assert "UP" in subject
        assert "recovered" in body

    def test_down_subject_exact_format(self):
        result = {"name": "DNS", "type": "ping", "ok": False, "detail": "unreachable"}
        state = {"DNS": {"ok": True}}
        subject, _ = build_alert_message(result, state)
        assert subject == "[ALERT] DNS is DOWN"

    def test_recovery_subject_exact_format(self):
        result = {"name": "DNS", "type": "ping", "ok": True,
                  "detail": "reachable", "latency_ms": 5.0}
        state = {"DNS": {"ok": False}}
        subject, _ = build_alert_message(result, state)
        assert subject == "[RECOVERY] DNS is back UP"


# ── load_state / save_state ────────────────────────────────────────────────────

class TestState:
    def test_load_nonexistent(self, tmp_path):
        result = load_state(tmp_path / "missing.json")
        assert result == {}

    def test_load_invalid_json(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text("not valid json {{{")
        result = load_state(f)
        assert result == {}

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "state.json"
        data = {"Web": {"ok": True, "latency_ms": 45.0}, "DNS": {"ok": False}}
        save_state(path, data)
        loaded = load_state(path)
        assert loaded == data

    def test_save_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "state.json"
        save_state(path, {"x": 1})
        assert path.exists()

    def test_save_overwrites_existing(self, tmp_path):
        path = tmp_path / "state.json"
        save_state(path, {"v": 1})
        save_state(path, {"v": 2})
        assert load_state(path) == {"v": 2}


# ── run_once ───────────────────────────────────────────────────────────────────

class TestRunOnce:
    def _notifier(self):
        return MagicMock()

    @patch("monitoring.monitor_services.check_service")
    def test_all_up_no_alerts(self, mock_check):
        mock_check.return_value = {
            "name": "DNS", "type": "ping", "ok": True,
            "detail": "reachable", "latency_ms": 5.0,
            "checked_at": "2026-03-08T00:00:00+00:00",
        }
        services = [{"name": "DNS", "type": "ping", "host": "8.8.8.8"}]
        notifier = self._notifier()
        state, results = run_once(services, 5, 30, notifier, {}, dry_run=False)
        notifier.send.assert_not_called()
        assert state["DNS"]["ok"] is True
        assert len(results) == 1

    @patch("monitoring.monitor_services.check_service")
    def test_newly_down_triggers_alert(self, mock_check):
        mock_check.return_value = {
            "name": "DNS", "type": "ping", "ok": False,
            "detail": "unreachable", "latency_ms": None,
            "checked_at": "2026-03-08T00:00:00+00:00",
        }
        services = [{"name": "DNS", "type": "ping", "host": "8.8.8.8"}]
        notifier = self._notifier()
        run_once(services, 5, 30, notifier, {}, dry_run=False)
        notifier.send.assert_called_once()
        subject = notifier.send.call_args[0][0]
        assert "DNS" in subject
        assert "DOWN" in subject

    @patch("monitoring.monitor_services.check_service")
    def test_dry_run_suppresses_alert(self, mock_check):
        mock_check.return_value = {
            "name": "DNS", "type": "ping", "ok": False,
            "detail": "unreachable", "latency_ms": None,
            "checked_at": "2026-03-08T00:00:00+00:00",
        }
        services = [{"name": "DNS", "type": "ping", "host": "8.8.8.8"}]
        notifier = self._notifier()
        run_once(services, 5, 30, notifier, {}, dry_run=True)
        notifier.send.assert_not_called()

    @patch("monitoring.monitor_services.check_service")
    def test_recovery_triggers_alert(self, mock_check):
        mock_check.return_value = {
            "name": "DNS", "type": "ping", "ok": True,
            "detail": "reachable", "latency_ms": 5.0,
            "checked_at": "2026-03-08T00:00:00+00:00",
        }
        services = [{"name": "DNS", "type": "ping", "host": "8.8.8.8"}]
        notifier = self._notifier()
        prev_state = {"DNS": {"ok": False}}
        run_once(services, 5, 30, notifier, prev_state, dry_run=False)
        notifier.send.assert_called_once()
        subject = notifier.send.call_args[0][0]
        assert "RECOVERY" in subject

    @patch("monitoring.monitor_services.check_service")
    def test_state_updated_after_run(self, mock_check):
        mock_check.return_value = {
            "name": "Web", "type": "http", "ok": True,
            "detail": "HTTP 200", "latency_ms": 45.0,
            "status_code": 200, "checked_at": "2026-03-08T00:00:00+00:00",
        }
        services = [{"name": "Web", "type": "http", "url": "http://example.com"}]
        notifier = self._notifier()
        state, _ = run_once(services, 5, 30, notifier, {}, dry_run=False)
        assert state["Web"]["ok"] is True
        assert state["Web"]["latency_ms"] == 45.0
        assert state["Web"]["type"] == "http"
        assert state["Web"]["detail"] == "HTTP 200"

    @patch("monitoring.monitor_services.check_service")
    def test_multiple_services_all_checked(self, mock_check):
        def side_effect(svc, timeout):
            return {
                "name": svc["name"], "type": svc["type"], "ok": True,
                "detail": "ok", "latency_ms": 1.0,
                "checked_at": "2026-03-08T00:00:00+00:00",
            }
        mock_check.side_effect = side_effect
        services = [
            {"name": "A", "type": "ping", "host": "1.1.1.1"},
            {"name": "B", "type": "ping", "host": "8.8.8.8"},
        ]
        notifier = self._notifier()
        state, results = run_once(services, 5, 30, notifier, {}, dry_run=False)
        assert len(results) == 2
        assert "A" in state
        assert "B" in state
