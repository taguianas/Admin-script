"""
monitoring/dashboard.py

Flask web dashboard for real-time service status.
Reads the state.json file written by monitor_services.py and displays
a live status page with auto-refresh.

Usage
-----
    python monitoring/dashboard.py
    python monitoring/dashboard.py --port 8080
    python monitoring/dashboard.py --state monitoring/state.json

Endpoints
---------
    GET /            HTML dashboard (auto-refreshes every 30 s)
    GET /api/status  JSON snapshot of current service state
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, render_template_string

DEFAULT_STATE = Path(__file__).parent / "state.json"

app = Flask(__name__)
_state_path: Path = DEFAULT_STATE

# ── HTML template ──────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Service Monitor</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, -apple-system, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      padding: 2rem;
      min-height: 100vh;
    }
    header { margin-bottom: 2rem; }
    h1 { font-size: 1.6rem; color: #38bdf8; margin-bottom: 0.25rem; }
    .subtitle { color: #64748b; font-size: 0.875rem; }

    .summary {
      display: flex;
      gap: 1rem;
      margin-bottom: 1.5rem;
      flex-wrap: wrap;
    }
    .summary-card {
      background: #1e293b;
      border-radius: 8px;
      padding: 0.75rem 1.25rem;
      min-width: 120px;
    }
    .summary-card .label { font-size: 0.75rem; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }
    .summary-card .value { font-size: 1.5rem; font-weight: 700; margin-top: 0.25rem; }
    .value.green { color: #4ade80; }
    .value.red   { color: #f87171; }
    .value.blue  { color: #38bdf8; }

    table {
      width: 100%;
      border-collapse: collapse;
      background: #1e293b;
      border-radius: 8px;
      overflow: hidden;
    }
    th {
      background: #1e3a5f;
      color: #7dd3fc;
      padding: 0.65rem 1rem;
      text-align: left;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 600;
    }
    td { padding: 0.7rem 1rem; border-top: 1px solid #1e3a5f; font-size: 0.9rem; }
    tr:hover td { background: #253347; }

    .badge {
      display: inline-block;
      padding: 0.2rem 0.65rem;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.04em;
    }
    .badge-up   { background: #14532d; color: #86efac; }
    .badge-down { background: #7f1d1d; color: #fca5a5; }
    .badge-type { background: #1e3a5f; color: #7dd3fc; font-weight: 500; }

    .latency { color: #94a3b8; font-variant-numeric: tabular-nums; }
    .ts      { color: #475569; font-size: 0.78rem; }
    .detail  { color: #cbd5e1; }

    .empty { color: #475569; padding: 2rem 0; }
    footer { margin-top: 1.5rem; color: #334155; font-size: 0.78rem; }
  </style>
</head>
<body>
  <header>
    <h1>Service Monitor</h1>
    <div class="subtitle">Auto-refreshes every 30 s &mdash; Last loaded: {{ now }}</div>
  </header>

  {% if services %}
  <div class="summary">
    <div class="summary-card">
      <div class="label">Total</div>
      <div class="value blue">{{ services | length }}</div>
    </div>
    <div class="summary-card">
      <div class="label">Up</div>
      <div class="value green">{{ services | selectattr('ok') | list | length }}</div>
    </div>
    <div class="summary-card">
      <div class="label">Down</div>
      <div class="value red">{{ services | rejectattr('ok') | list | length }}</div>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>Service</th>
        <th>Type</th>
        <th>Status</th>
        <th>Latency</th>
        <th>Detail</th>
        <th>Last Checked</th>
      </tr>
    </thead>
    <tbody>
    {% for svc in services %}
      <tr>
        <td><strong>{{ svc.name }}</strong></td>
        <td><span class="badge badge-type">{{ svc.type | upper }}</span></td>
        <td>
          <span class="badge {{ 'badge-up' if svc.ok else 'badge-down' }}">
            {{ 'UP' if svc.ok else 'DOWN' }}
          </span>
        </td>
        <td class="latency">
          {{ svc.latency_ms | string + ' ms' if svc.latency_ms is not none else '—' }}
        </td>
        <td class="detail">{{ svc.detail }}</td>
        <td class="ts">{{ svc.last_checked }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p class="empty">
    No data yet. Run <code>python monitoring/monitor_services.py --once</code> first.
  </p>
  {% endif %}

  <footer>System Admin Monitoring &mdash; Phase 4</footer>
</body>
</html>"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if _state_path.exists():
        try:
            return json.loads(_state_path.read_text())
        except Exception:
            pass
    return {}


def _state_to_service_list(state: dict) -> list:
    services = []
    for name, info in state.items():
        services.append({
            "name": name,
            "type": info.get("type", "?"),
            "ok": info.get("ok", False),
            "latency_ms": info.get("latency_ms"),
            "detail": info.get("detail", ""),
            "last_checked": info.get("last_checked", ""),
        })
    # Sort: DOWN first, then alphabetically
    services.sort(key=lambda s: (s["ok"], s["name"]))
    return services


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    services = _state_to_service_list(_load_state())
    return render_template_string(_DASHBOARD_HTML, services=services, now=now)


@app.route("/api/status")
def api_status():
    return jsonify(_load_state())


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    global _state_path

    parser = argparse.ArgumentParser(description="Service monitor web dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument(
        "--state", default=str(DEFAULT_STATE),
        help="Path to state.json written by monitor_services.py",
    )
    args = parser.parse_args()

    _state_path = Path(args.state)
    print(f"Dashboard running at http://{args.host}:{args.port}  (state: {_state_path})")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
