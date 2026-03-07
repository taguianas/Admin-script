"""
monitoring/monitor_services.py

Network service monitor: ping, TCP, and HTTP health checks
with alerting via common/notifier.py and persistent state tracking.

Usage
-----
    python monitoring/monitor_services.py
    python monitoring/monitor_services.py --config monitoring/config/services.yaml
    python monitoring/monitor_services.py --once       # single pass then exit
    python monitoring/monitor_services.py --dry-run    # check + log, no alerts sent
"""

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config_loader import load_config
from common.logger import get_logger
from common.notifier import Notifier

logger = get_logger(__name__)

DEFAULT_CONFIG = Path(__file__).parent / "config" / "services.yaml"
STATE_FILE = Path(__file__).parent / "state.json"


# ── Check functions ────────────────────────────────────────────────────────────

def ping_check(host: str, timeout: int = 5) -> dict:
    """ICMP ping check.

    Returns
    -------
    dict with keys: ok, latency_ms, detail
    """
    is_windows = platform.system().lower() == "windows"
    count_flag = "-n" if is_windows else "-c"
    wait_flag = "-w" if is_windows else "-W"
    wait_val = str(timeout * 1000) if is_windows else str(timeout)

    cmd = ["ping", count_flag, "1", wait_flag, wait_val, host]
    t0 = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        latency = (time.monotonic() - t0) * 1000
        ok = result.returncode == 0
        return {
            "ok": ok,
            "latency_ms": round(latency, 1),
            "detail": "reachable" if ok else "unreachable",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "latency_ms": None, "detail": "timeout"}
    except Exception as exc:
        return {"ok": False, "latency_ms": None, "detail": str(exc)}


def tcp_check(host: str, port: int, timeout: int = 5) -> dict:
    """TCP connection check.

    Returns
    -------
    dict with keys: ok, latency_ms, detail
    """
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency = (time.monotonic() - t0) * 1000
            return {"ok": True, "latency_ms": round(latency, 1), "detail": "connected"}
    except socket.timeout:
        return {"ok": False, "latency_ms": None, "detail": "timeout"}
    except ConnectionRefusedError:
        return {"ok": False, "latency_ms": None, "detail": "connection refused"}
    except OSError as exc:
        return {"ok": False, "latency_ms": None, "detail": str(exc)}


def http_check(
    url: str,
    expected_status: int = 200,
    keyword: str = None,
    timeout: int = 5,
) -> dict:
    """HTTP GET health check.

    Returns
    -------
    dict with keys: ok, latency_ms, status_code, detail
    """
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sysadmin-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = (time.monotonic() - t0) * 1000
            body = resp.read(4096).decode("utf-8", errors="replace")
            status = resp.status
            ok = status == expected_status
            detail = f"HTTP {status}"
            if ok and keyword:
                if keyword not in body:
                    ok = False
                    detail = f"HTTP {status} but keyword '{keyword}' not found"
                else:
                    detail = f"HTTP {status} keyword found"
            return {
                "ok": ok,
                "latency_ms": round(latency, 1),
                "status_code": status,
                "detail": detail,
            }
    except urllib.error.HTTPError as exc:
        latency = (time.monotonic() - t0) * 1000
        return {
            "ok": False,
            "latency_ms": round(latency, 1),
            "status_code": exc.code,
            "detail": f"HTTP {exc.code} {exc.reason}",
        }
    except urllib.error.URLError as exc:
        return {"ok": False, "latency_ms": None, "status_code": None, "detail": str(exc.reason)}
    except Exception as exc:
        return {"ok": False, "latency_ms": None, "status_code": None, "detail": str(exc)}


def check_service(svc: dict, timeout: int = 5) -> dict:
    """Dispatch to the correct check function based on service type.

    Returns a result dict that always contains: name, type, ok, detail, checked_at
    """
    kind = svc.get("type", "").lower()

    if kind == "ping":
        result = ping_check(svc["host"], timeout)
    elif kind == "tcp":
        result = tcp_check(svc["host"], int(svc["port"]), timeout)
    elif kind == "http":
        result = http_check(
            svc["url"],
            expected_status=int(svc.get("expected_status", 200)),
            keyword=svc.get("keyword"),
            timeout=timeout,
        )
    else:
        result = {"ok": False, "latency_ms": None, "detail": f"unknown type '{kind}'"}

    result["name"] = svc["name"]
    result["type"] = kind
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    return result


# ── State management ───────────────────────────────────────────────────────────

def load_state(path: Path) -> dict:
    """Load persisted service state from a JSON file."""
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def save_state(path: Path, state: dict) -> None:
    """Persist service state to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


# ── Alert logic ────────────────────────────────────────────────────────────────

def should_alert(name: str, ok: bool, state: dict, cooldown_minutes: int) -> bool:
    """Decide whether to fire an alert for a service check result.

    Alerts are fired when:
    - A service transitions from UP to DOWN (or was unknown and is now DOWN)
    - A service transitions from DOWN to UP (recovery)
    - A service is still DOWN and the cooldown period has elapsed since the last alert
    """
    prev = state.get(name, {})
    prev_ok = prev.get("ok", True)   # treat unknown services as previously UP
    last_alert = prev.get("last_alert_ts")

    if ok and not prev_ok:
        return True   # recovery

    if not ok and prev_ok:
        return True   # newly down

    if not ok and last_alert:
        elapsed = (
            datetime.now(timezone.utc) - datetime.fromisoformat(last_alert)
        ).total_seconds()
        if elapsed >= cooldown_minutes * 60:
            return True   # still down, cooldown expired

    return False


def build_alert_message(result: dict, state: dict) -> tuple:
    """Build alert subject and body for a service result.

    Returns
    -------
    (subject, body) strings
    """
    name = result["name"]
    prev_ok = state.get(name, {}).get("ok", True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if result["ok"] and not prev_ok:
        subject = f"[RECOVERY] {name} is back UP"
        body = (
            f"Service '{name}' ({result['type']}) recovered.\n"
            f"Detail  : {result['detail']}\n"
            f"Latency : {result.get('latency_ms')} ms\n"
            f"Time    : {now}"
        )
    else:
        subject = f"[ALERT] {name} is DOWN"
        body = (
            f"Service '{name}' ({result['type']}) is unreachable.\n"
            f"Detail  : {result['detail']}\n"
            f"Time    : {now}"
        )

    return subject, body


# ── Main monitoring loop ───────────────────────────────────────────────────────

def run_once(
    services: list,
    timeout: int,
    cooldown_minutes: int,
    notifier: Notifier,
    state: dict,
    dry_run: bool,
) -> tuple:
    """Run one monitoring pass over all services.

    Returns
    -------
    (updated_state, results_list)
    """
    results = []
    for svc in services:
        result = check_service(svc, timeout)
        results.append(result)

        name = result["name"]
        status = "UP  " if result["ok"] else "DOWN"
        latency = f"{result['latency_ms']} ms" if result.get("latency_ms") is not None else "—"
        logger.info("[%s] %-30s (%s) – %s – %s", status, name, result["type"], result["detail"], latency)

        if should_alert(name, result["ok"], state, cooldown_minutes):
            subject, body = build_alert_message(result, state)
            if dry_run:
                logger.info("[DRY-RUN] Would send alert: %s", subject)
            else:
                notifier.send(subject, body=body)
            state.setdefault(name, {})["last_alert_ts"] = datetime.now(timezone.utc).isoformat()

        state.setdefault(name, {}).update({
            "ok": result["ok"],
            "last_checked": result["checked_at"],
            "detail": result["detail"],
            "latency_ms": result.get("latency_ms"),
            "type": result["type"],
        })

    return state, results


def main():
    parser = argparse.ArgumentParser(description="Network service monitor")
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG),
        help="Path to services.yaml (default: monitoring/config/services.yaml)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single check pass then exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Check services but do not send alerts or write state",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    mon = cfg.get("monitoring", {})
    interval = int(mon.get("interval_seconds", 60))
    timeout = int(mon.get("timeout_seconds", 5))
    cooldown = int(mon.get("alert_cooldown_minutes", 30))
    services = cfg.get("services", [])

    notifier = Notifier(cfg.get("notifications", {}))
    state = load_state(STATE_FILE)

    logger.info(
        "Monitoring %d service(s) | interval=%ds | timeout=%ds | cooldown=%dmin",
        len(services), interval, timeout, cooldown,
    )

    if args.once:
        state, _ = run_once(services, timeout, cooldown, notifier, state, args.dry_run)
        if not args.dry_run:
            save_state(STATE_FILE, state)
        return

    while True:
        try:
            state, _ = run_once(services, timeout, cooldown, notifier, state, args.dry_run)
            if not args.dry_run:
                save_state(STATE_FILE, state)
        except KeyboardInterrupt:
            logger.info("Monitor stopped.")
            break
        except Exception as exc:
            logger.error("Monitor error: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    main()
