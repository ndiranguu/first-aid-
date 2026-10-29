#!/usr/bin/env python3
"""
AURA-1 Full-Stack Local Bridge Server (Zero External Dependencies)
Wires aura_frontend.html directly to aura_backend.py (SQLite WAL + FSM + Daemon).

Usage:
  1. Ensure aura_backend.py, aura_frontend.html, and aura_server.py are in the same directory.
  2. Run: python3 aura_server.py
  3. Open: http://127.0.0.1:8080
"""

import json
import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict

# Import the core engine classes directly from your aura_backend.py file
from aura_backend import (
    AuraDatabase,
    DeterministicTriageFSM,
    MistSerializer,
    VitalsMonitorDaemon,
)

DB_FILE_PATH = "aura_field.db"
FRONTEND_HTML_PATH = "aura_frontend.html"
HOST = "0.0.0.0"
PORT = 8080


# ============================================================================
# 1. STATEFUL BACKEND CONTROLLER
# ============================================================================

class AuraLocalController:
    def __init__(self, db_path: str = DB_FILE_PATH):
        self.db = AuraDatabase(db_path)
        self.daemon = VitalsMonitorDaemon(self.db)
        self.incident_id = ""
        self.fsm: DeterministicTriageFSM = None  # type: ignore
        self.start_new_incident()

    def start_new_incident(self) -> Dict[str, Any]:
        now = int(time.time())
        self.incident_id = self.db.create_incident(
            mechanism="4m Rock Ledge Fall",
            ambient_temp_c=8.5,
            is_amputation=False,
            now_unix=now,
        )
        # Log baseline stable vitals at T+0 so the HUD starts with a valid Shock Index
        self.db.log_vitals(
            incident_id=self.incident_id,
            hr=88,
            sbp=118,
            rr=18,
            avpu="A",
            cap_refill=2.0,
            now_unix=now,
        )
        self.fsm = DeterministicTriageFSM(self.db, self.incident_id)
        return self.build_state_payload(include_alerts=False)

    def step_fsm(self, option_label: str, anatomical_site: str) -> Dict[str, Any]:
        if self.fsm.current_node_id not in self.fsm.nodes:
            self.fsm.current_node_id = next(iter(self.fsm.nodes))
        current_node = self.fsm.nodes[self.fsm.current_node_id]
        
        # Case 1: Standard decision node with branching options in aura_backend.py
        if "options" in current_node and current_node["options"]:
            self.fsm.select_option(
                option_label=option_label,
                anatomical_site=anatomical_site,
                now_unix=int(time.time()),
            )
        # Case 2: Action protocol node advancing to its next_node (MONITOR_TQ / MONITOR_DRESSING)
        elif "next_node" in current_node:
            self.fsm.current_node_id = current_node["next_node"]
        # Case 3: Monitoring loop node re-entering hemorrhage control if bleeding resumes
        elif option_label == "BLEEDING_RESUMED":
            self.fsm.current_node_id = (
                "CHECK_COMMERCIAL_TQ"
                if self.fsm.current_node_id == "MONITOR_TQ"
                else "BLEED_DETECT"
            )

        return self.build_state_payload(include_alerts=False)

    def record_vitals(
        self, hr: int, sbp: int, rr: int, avpu: str, cap: float
    ) -> Dict[str, Any]:
        now = int(time.time())
        self.db.log_vitals(
            incident_id=self.incident_id,
            hr=int(hr),
            sbp=int(sbp),
            rr=int(rr),
            avpu=str(avpu),
            cap_refill=float(cap),
            now_unix=now,
        )
        return self.build_state_payload(include_alerts=True, now_unix=now)

    def simulate_2h_shock(self) -> Dict[str, Any]:
        """
        Executes the +125-minute Prolonged Field Care shock scenario in SQLite:
        - Backdates an active R-THIGH tourniquet to 125 minutes ago
        - Logs decompensated shock vitals (HR 118, SBP 96 -> SI 1.23)
        - Evaluates VitalsMonitorDaemon to trigger the 5 clinical interrupts
        """
        now = int(time.time())
        applied_125m_ago = now - (125 * 60)

        with self.db.conn:
            self.db.conn.execute(
                "DELETE FROM active_timers WHERE incident_id = ?",
                (self.incident_id,),
            )
            self.db.conn.execute(
                """
                INSERT INTO active_timers
                (timer_id, incident_id, intervention_type, anatomical_site, applied_at_unix, next_check_unix, is_active)
                VALUES (?, ?, 'TOURNIQUET', 'R-THIGH', ?, ?, 1)
                """,
                (
                    f"TMR-SIM125",
                    self.incident_id,
                    applied_125m_ago,
                    now - (5 * 60),
                ),
            )

        self.db.update_evac_category(self.incident_id, "CAT_A")
        self.fsm.current_node_id = "MONITOR_TQ"

        self.db.log_vitals(
            incident_id=self.incident_id,
            hr=118,
            sbp=96,
            rr=24,
            avpu="V",
            cap_refill=3.8,
            now_unix=now,
        )
        return self.build_state_payload(include_alerts=True, now_unix=now)

    def build_state_payload(
        self, include_alerts: bool = False, now_unix: int = 0
    ) -> Dict[str, Any]:
        ts = now_unix if now_unix > 0 else int(time.time())
        inc = self.db.get_incident(self.incident_id)
        view = self.fsm.current_view()
        raw_node = self.fsm.nodes[self.fsm.current_node_id]

        # Build UI button options directly from backend FSM node state
        ui_options = []
        if view.options:
            for label in view.options:
                if view.options:
                    is_danger = any(
                        k in label.lower()
                        for k in (
                            "yes",
                            "arm",
                            "groin",
                            "no",
                            "arterial",
                            "fang",
                            "swelling",
                            "droopy",
                            "breathing",
                    )
                )
                ui_options.append(
                    {"label": label, "value": label, "danger": is_danger, "confirm": False}
                )
                
        elif "next_node" in raw_node:
            ui_options.append(
                {
                    "label": "CONFIRM INTERVENTION & START MONITOR",
                    "value": "ADVANCE_NEXT",
                    "danger": False,
                    "confirm": True,
                }
            )
        elif self.fsm.current_node_id in ("MONITOR_TQ", "MONITOR_DRESSING"):
            ui_options.append(
                {
                    "label": "BLEEDING RESUMED — RE-ENTER TRIAGE",
                    "value": "BLEEDING_RESUMED",
                    "danger": True,
                    "confirm": False,
                }
            )

        vitals_rows = self.db.get_vitals_history(self.incident_id)
        vitals_history = [
            {
                "timestamp": r["recorded_at_unix"] * 1000,
                "hr": r["heart_rate_bpm"],
                "sbp": r["systolic_bp_mmhg"],
                "rr": r["respiratory_rate_bpm"],
                "avpu": r["avpu_state"],
                "cap": r["cap_refill_sec"],
            }
            for r in vitals_rows
        ]

        timer_rows = self.db.get_active_timers(self.incident_id)
        active_timers = [
            {
                "type": t["intervention_type"],
                "site": t["anatomical_site"],
                "appliedAtMs": t["applied_at_unix"] * 1000,
                "nextCheckMs": t["next_check_unix"] * 1000,
            }
            for t in timer_rows
        ]

        alerts = []
        if include_alerts:
            raw_alerts = self.daemon.evaluate(self.incident_id, now_unix=ts)
            alerts = [
                {
                    "severity": "WARN" if a.severity == "WARNING" else a.severity,
                    "code": a.code,
                    "message": a.message,
                }
                for a in raw_alerts
            ]
            # Refresh incident row in case daemon escalated evac_category to CAT_A
            inc = self.db.get_incident(self.incident_id)

        return {
            "incidentId": self.incident_id,
            "mechanism": inc.get("mechanism_of_injury", "4M-ROCK-FALL"),
            "evacCategory": inc.get("evac_category", "CAT_C"),
            "currentNodeId": view.node_id,
            "nodeView": {
                "question": view.question,
                "critical_warning": view.critical_warning,
                "instructions": view.instructions,
                "requires_pressure_timer": view.node_id == "JUNCTIONAL_PACKING",
                "options": ui_options,
            },
            "vitalsHistory": vitals_history,
            "activeTimers": active_timers,
            "mistPacket": MistSerializer.build_packet(self.db, self.incident_id),
            "alerts": alerts,
        }


# ============================================================================
# 2. JAVASCRIPT BRIDGE INJECTED INTO aura_frontend.html AT RUNTIME
# ============================================================================

BRIDGE_INJECTION_SCRIPT = """
<script>
  // Overrides in-browser mock functions to drive UI strictly from aura_backend.py SQLite
  async function apiCall(path, method = "GET", body = null) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    return await res.json();
  }

  function applyBackendState(payload) {
    appState.incidentId = payload.incidentId;
    appState.mechanism = payload.mechanism;
    appState.evacCategory = payload.evacCategory;
    appState.currentNodeId = payload.currentNodeId;
    appState.vitalsHistory = payload.vitalsHistory;
    appState.activeTimers = payload.activeTimers;
    window.latestMistPacket = payload.mistPacket;

    // Dynamically sync current node from Python DeterministicTriageFSM into UI graph
    PROTOCOL_NODES[payload.currentNodeId] = {
      question: payload.nodeView.question,
      critical_warning: payload.nodeView.critical_warning,
      instructions: payload.nodeView.instructions,
      requires_pressure_timer: payload.nodeView.requires_pressure_timer,
      options: payload.nodeView.options.map((o) => ({
        label: o.label,
        next_node: o.value,
        danger: o.danger,
        action_confirm: o.confirm
      }))
    };

    renderCurrentNode();

    if (payload.alerts && payload.alerts.length > 0) {
      showInterruptModal(payload.alerts);
    }
  }

  // Override: 72dp Decision Option Tap -> POST /api/step
  selectNodeOption = async function(optionValue) {
    playTone(520, 60, "sine");
    const site = document.getElementById("siteSelect").value;
    const payload = await apiCall("/api/step", "POST", {
      option_label: optionValue,
      anatomical_site: site
    });
    applyBackendState(payload);
  };

  // Override: Save Vitals & Run Python VitalsMonitorDaemon -> POST /api/vitals
  commitVitalsAndEvaluate = async function() {
    closeVitalsDrawer();
    const d = appState.draftVitals;
    const payload = await apiCall("/api/vitals", "POST", {
      hr: d.hr,
      sbp: d.sbp,
      rr: d.rr,
      avpu: d.avpu,
      cap: d.cap
    });
    applyBackendState(payload);
  };

  // Override: +2H Shock & PFC Tourniquet Lock Simulation -> POST /api/sim_2h
  simulate2HourShockScenario = async function() {
    const payload = await apiCall("/api/sim_2h", "POST", {});
    applyBackendState(payload);
  };

  // Override: Use Python MistSerializer packet directly
  buildMistPacket = function() {
    return window.latestMistPacket || "MIST/SYNCING";
  };

  // Override: Reset SQLite session -> POST /api/reset
  resetIncident = async function() {
    const payload = await apiCall("/api/reset", "POST", {});
    applyBackendState(payload);
  };

  // Initial sync with Python SQLite backend on page load
  window.addEventListener("DOMContentLoaded", async () => {
    const payload = await apiCall("/api/state", "GET");
    applyBackendState(payload);
  });
</script>
"""


# ============================================================================
# 3. ZERO-DEPENDENCY HTTP REQUEST HANDLER
# ============================================================================

CONTROLLER = AuraLocalController()


class AuraRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            html_file = Path(FRONTEND_HTML_PATH)
            if not html_file.exists():
                self.send_error(
                    404,
                    f"Missing {FRONTEND_HTML_PATH}. Save aura_frontend.html in this directory.",
                )
                return

            html_content = html_file.read_text(encoding="utf-8")
            # Inject the live Python API bridge directly before </body>
            patched_html = html_content.replace(
                "</body>", f"{BRIDGE_INJECTION_SCRIPT}\n</body>"
            ).encode("utf-8")
            
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(patched_html)))
            self.end_headers()
            self.wfile.write(patched_html)
        elif self.path == "/api/state":
            self._send_json(CONTROLLER.build_state_payload())
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404, "Endpoint not found")

    def do_POST(self) -> None:
        body = self._read_json_body()

        if self.path == "/api/step":
            payload = CONTROLLER.step_fsm(
                option_label=body.get("option_label", ""),
                anatomical_site=body.get("anatomical_site", "R-THIGH"),
            )
            self._send_json(payload)

        elif self.path == "/api/vitals":
            payload = CONTROLLER.record_vitals(
                hr=body.get("hr", 88),
                sbp=body.get("sbp", 118),
                rr=body.get("rr", 18),
                avpu=body.get("avpu", "A"),
                cap=body.get("cap", 2.0),
            )
            self._send_json(payload)

        elif self.path == "/api/sim_2h":
            payload = CONTROLLER.simulate_2h_shock()
            self._send_json(payload)

        elif self.path == "/api/reset":
            payload = CONTROLLER.start_new_incident()
            self._send_json(payload)

        else:
            self.send_error(404, "Unknown POST route")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[AURA-SQLITE-BRIDGE] {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    print("=" * 72)
    print(f" AURA-1 FULL-STACK OFFLINE SERVER RUNNING")
    print(f" SQLite WAL Database : {os.path.abspath(DB_FILE_PATH)}")
    print(f" Active Incident ID  : {CONTROLLER.incident_id}")
    print(f" Local Triage UI URL : http://localhost:{PORT}")
    print("=" * 72)

    server = HTTPServer((HOST, PORT), AuraRequestHandler)
    try:
        #webbrowser.open(f"http://localhost:{PORT}")
        pass
        
        
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down AURA-1 local server.")
        server.server_close()
        server = HTTPServer(("0.0.0.0", 8080, AuraRequestHandler))
    