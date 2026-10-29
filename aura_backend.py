#!/usr/bin/env python3
"""
AURA-1 Offline First Aid Backend Prototype
Layers included:
  1. Embedded SQLite Persistence (WAL mode) for sessions, vitals, and timers
  2. Deterministic Hemorrhage Triage Finite-State Machine (FSM)
  3. Physiological Trend & Shock-Index Alarm Daemon (CoTCCC / PFC rules)
  4. Low-Bandwidth MIST Telemetry Serializer (<160 chars for SMS / Satcom)
"""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ============================================================================
# 1. EMBEDDED CLINICAL DECISION TREE (MARCH - HEMORRHAGE PROTOCOL)
# ============================================================================

HEMORRHAGE_PROTOCOL_JSON = """
{
  "title": "AURA_Field_Hemorrhage_Protocol",
  "version": "1.0.0",
  "nodes": {
    "ROOT_TRIAGE": {
      "question": "Where is the severe bleeding coming from?",
      "options": [
        {"label": "Arm or Leg (Extremity)", "next_node": "EXTREMITY_BLEED_TYPE"},
        {"label": "Groin, Armpit, or Neck (Junctional)", "next_node": "JUNCTIONAL_PACKING"},
        {"label": "Torso, Chest, or Abdomen", "next_node": "TORSO_PRESSURE"}
      ]
    },
    "EXTREMITY_BLEED_TYPE": {
      "question": "Is blood spurting, pooling rapidly, or soaking through clothing?",
      "options": [
        {"label": "Yes (Arterial / Life Threat)", "next_node": "CHECK_COMMERCIAL_TQ"},
        {"label": "No (Steadily oozing or dripping)", "next_node": "DIRECT_PRESSURE_ONLY"}
      ]
    },
    "CHECK_COMMERCIAL_TQ": {
      "question": "Do you have a dedicated medical tourniquet (CAT, SOFTT, SAM XT)?",
      "options": [
        {"label": "Yes", "next_node": "COMMERCIAL_TQ_ACTION"},
        {"label": "No", "next_node": "IMPROVISED_TQ_ACTION"}
      ]
    },
    "COMMERCIAL_TQ_ACTION": {
      "priority": "CAT_A",
      "intervention_type": "TOURNIQUET",
      "reassessment_interval_minutes": 15,
      "instructions": [
        "Place strap 2-3 inches above the wound (never over a joint; if unsure, go high and tight).",
        "Pull strap completely tight so no fingers fit under it before turning windlass.",
        "Twist windlass until bright red bleeding STOPS and distal pulse disappears.",
        "Lock rod into clip and write application time on the strap."
      ],
      "next_node": "MONITOR_TQ"
    },
    "IMPROVISED_TQ_ACTION": {
      "priority": "CAT_A",
      "intervention_type": "TOURNIQUET",
      "reassessment_interval_minutes": 15,
      "critical_warning": "NEVER use zip-ties, wire, paracord, or shoelaces. Use cloth at least 2 inches wide.",
      "instructions": [
        "Wrap wide cloth band (2+ inches wide) 2-3 inches above wound; tie a half-knot.",
        "Place a rigid stick or heavy metal stake on top of the half-knot.",
        "Tie a full square knot firmly over the stick.",
        "Twist the stick repeatedly until bright red bleeding completely stops.",
        "Tie the loose cloth ends firmly around the stick so it cannot unwind.",
        "Write 'TQ' and the exact time on the patient's forehead or limb."
      ],
      "next_node": "MONITOR_TQ"
    },
    "JUNCTIONAL_PACKING": {
      "priority": "CAT_A",
      "intervention_type": "PRESSURE_DRESSING",
      "reassessment_interval_minutes": 15,
      "critical_warning": "DO NOT apply a tourniquet to the neck, armpit, or groin.",
      "instructions": [
        "Expose wound completely and wipe out large surface clots.",
        "Push gauze or clean cotton cloth tightly into the deepest point of the wound cavity.",
        "Pack the cavity completely full, then lean your body weight onto the wound for 3-10 full minutes."
      ],
      "next_node": "MONITOR_DRESSING"
    },
    "TORSO_PRESSURE": {
      "priority": "CAT_A",
      "intervention_type": "PRESSURE_DRESSING",
      "reassessment_interval_minutes": 15,
      "critical_warning": "Do NOT pack deep chest or abdominal cavities (internal organs give way).",
      "instructions": [
        "If chest wound is bubbling/sucking, seal immediately with plastic/foil taped on 3 sides.",
        "For abdominal bleeding, apply direct firm surface pressure with clean dry cloth.",
        "Keep patient warm in a hypothermia wrap and prepare for immediate evacuation."
      ],
      "next_node": "MONITOR_DRESSING"
    },
    "DIRECT_PRESSURE_ONLY": {
      "priority": "CAT_B",
      "intervention_type": "PRESSURE_DRESSING",
      "reassessment_interval_minutes": 30,
      "instructions": [
        "Place clean absorbent pad directly over wound and press firmly with both hands.",
        "Wrap tightly with elastic bandage or cloth strip without turning fingers/toes blue."
      ],
      "next_node": "MONITOR_DRESSING"
    },
    "MONITOR_TQ": {
      "priority": "CAT_A",
      "instructions": [
        "Recheck tourniquet every 15 minutes for re-bleeding or windlass slippage.",
        "If bleeding continues, apply a second tourniquet 2 inches above the first.",
        "If evacuation exceeds 2 hours, run the PFC Tourniquet Conversion safety check."
      ]
    },
    "MONITOR_DRESSING": {
      "priority": "CAT_B",
      "instructions": [
        "Check dressing every 15-30 minutes for blood soaking through.",
        "Check distal warmth and capillary refill below the bandage."
      ]
    },
    "SNAKEBITE_TRIAGE": {
      "question": "Where did the snake venom make contact?",
      "options": [
        {"label": "Fang Bite on Limb or Body", "next_node": "SNAKEBITE_SYNDROME_CHECK"},
        {"label": "Venom Spat into Eyes", "next_node": "SNAKE_OCULAR_IRRIGATION"}
      ]
    },
    "SNAKEBITE_SYNDROME_CHECK": {
      "question": "What symptoms are appearing at the bite or in the patient?",
      "options": [
        {"label": "Rapid Swelling, Severe Pain, Blistering, or Bleeding", "next_node": "SNAKE_CYTOTOXIC_PROTOCOL"},
        {"label": "Droopy Eyelids, Slurred Speech, Tingling, or Known Mamba/Cobra", "next_node": "SNAKE_NEUROTOXIC_PROTOCOL"},
        {"label": "Just Happened / Unknown Snake (No Symptoms Yet)", "next_node": "SNAKE_UNIVERSAL_PROTOCOL"}
      ]
    },
    "SNAKE_CYTOTOXIC_PROTOCOL": {
      "priority": "CAT_A",
      "intervention_type": "SPLINT_CHECK",
      "reassessment_interval_minutes": 15,
      "critical_warning": "DO NOT apply a tourniquet or tight compression wrap! Concentrating tissue-destroying venom causes amputation.",
      "instructions": [
        "Lay patient flat and enforce strict rest (walking pumps venom through the lymphatic system).",
        "Remove all rings, watches, bracelets, and tight shoes on the bitten limb immediately before swelling locks them on.",
        "Use a pen (or charcoal/mud) to circle the fang marks, draw a line at the top edge of the swelling, and write the current time.",
        "Immobilize the limb loosely with a stick/pole splint kept at or slightly below heart level.",
        "Every 15 minutes, draw a new line at the swelling edge to track how fast it is advancing toward the torso."
      ],
      "next_node": "MONITOR_SNAKEBITE"
    },
    "SNAKE_NEUROTOXIC_PROTOCOL": {
      "priority": "CAT_A",
      "intervention_type": "PRESSURE_DRESSING",
      "reassessment_interval_minutes": 15,
      "critical_warning": "FATAL AIRWAY RISK: Neurotoxins paralyze breathing muscles while the patient is still awake. Prepare to give rescue breaths!",
      "instructions": [
        "Keep patient completely still and calm; carry them if movement is required.",
        "Wrap an elastic bandage snugly (like a sprained ankle—you should still feel a pulse in fingers/toes) starting at the fingers/toes and wrapping all the way up the limb.",
        "Splint the wrapped limb rigidly so the joints cannot bend.",
        "Watch continuously for the '3 Ds': Droopy eyelids, Difficulty swallowing/speaking, and Decreasing breath depth.",
        "If patient cannot swallow their own saliva, roll them onto their side (Recovery Position) immediately."
      ],
      "next_node": "MONITOR_SNAKEBITE"
    },
    "SNAKE_UNIVERSAL_PROTOCOL": {
      "priority": "CAT_A",
      "intervention_type": "SPLINT_CHECK",
      "reassessment_interval_minutes": 15,
      "critical_warning": "NEVER cut the wound, suck venom, apply ice, use electric shocks, or give alcohol/aspirin.",
      "instructions": [
        "Move away from the snake (do not waste time trying to catch or kill it; take a safe photo from a distance only if visible).",
        "Remove all rings, watches, and tight clothing from the bitten limb immediately.",
        "Wipe surface venom gently with a damp cloth, cover lightly, and mark the bite border and time with a pen.",
        "Splint the limb to prevent joint movement and keep the patient lying flat and still.",
        "Re-check every 15 minutes for local swelling (Cytotoxic) vs. droopy eyelids/slurred speech (Neurotoxic)."
      ],
      "next_node": "MONITOR_SNAKEBITE"
    },
    "SNAKE_OCULAR_IRRIGATION": {
      "priority": "CAT_B",
      "critical_warning": "Do NOT let the patient rub their eyes—rubbing grinds venom deeper into the cornea.",
      "instructions": [
        "Hold the eyelids open and pour a gentle, steady stream of clean water from the inner corner of the eye outward for 15 to 20 full minutes.",
        "If water is scarce, any bland non-irritating liquid (milk, ORS solution, cooled boiled water) works in an emergency.",
        "After 15 minutes of flushing, cover eyes loosely with a clean dark cloth to block bright light and evacuate."
      ],
      "next_node": "MONITOR_DRESSING"
    },
    "MONITOR_SNAKEBITE": {
      "priority": "CAT_A",
      "question": "15-Minute Envenomation Reassessment Loop:",
      "instructions": [
        "1. SWELLING CHECK: Draw a new pen line at the top edge of swelling. Has it crossed a major joint (wrist/elbow or ankle/knee)?",
        "2. NEURO CHECK: Ask patient to look up without tilting their head (check for droopy eyelids) and stick out their tongue.",
        "3. BLEEDING CHECK: Inspect gums, nose, and fang marks for spontaneous oozing (sign of unclottable blood).",
        "4. Log Respiratory Rate and Heart Rate every 15 minutes using 'LOG VITALS' below."
      ],
      "options": [
        {"label": "BREATHING FAILING / UNCONSCIOUS", "next_node": "AIRWAY_CHECK"},
        {"label": "SWELLING / SYMPTOMS CHANGED", "next_node": "SNAKEBITE_SYNDROME_CHECK"},
        {"label": "TREAT ANOTHER INJURY (MAIN MENU)", "next_node": "ROOT_TRIAGE"}
      ]
    },
  }
}
"""


# ============================================================================
# 2. DATA MODELS
# ============================================================================

@dataclass
class TriageStateView:
    node_id: str
    priority: str
    question: Optional[str] = None
    critical_warning: Optional[str] = None
    instructions: List[str] = field(default_factory=list)
    options: List[str] = field(default_factory=list)
    reassessment_minutes: Optional[int] = None


@dataclass
class ClinicalAlert:
    severity: str  # "CRITICAL" | "WARNING" | "INFO"
    code: str
    message: str


# ============================================================================
# 3. EMBEDDED SQLITE DATABASE LAYER (WAL MODE)
# ============================================================================

class AuraDatabase:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            # Enable Write-Ahead Logging and foreign keys for crash resilience
            self.conn.execute("PRAGMA journal_mode = WAL;")
            self.conn.execute("PRAGMA synchronous = NORMAL;")
            self.conn.execute("PRAGMA foreign_keys = ON;")

            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS incident_session (
                    incident_id TEXT PRIMARY KEY,
                    created_at_unix INTEGER NOT NULL,
                    evac_category TEXT CHECK(evac_category IN ('CAT_A', 'CAT_B', 'CAT_C')) DEFAULT 'CAT_C',
                    mechanism_of_injury TEXT NOT NULL,
                    ambient_temp_c REAL,
                    is_amputation INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS vitals_timeseries (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT NOT NULL REFERENCES incident_session(incident_id),
                    recorded_at_unix INTEGER NOT NULL,
                    heart_rate_bpm INTEGER NOT NULL,
                    systolic_bp_mmhg INTEGER NOT NULL,
                    respiratory_rate_bpm INTEGER NOT NULL,
                    avpu_state TEXT CHECK(avpu_state IN ('A', 'V', 'P', 'U')) NOT NULL,
                    cap_refill_sec REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS active_timers (
                    timer_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL REFERENCES incident_session(incident_id),
                    intervention_type TEXT CHECK(
                        intervention_type IN ('TOURNIQUET', 'PRESSURE_DRESSING', 'SPLINT_CHECK', 'WARMTH_CHECK')
                    ) NOT NULL,
                    anatomical_site TEXT NOT NULL,
                    applied_at_unix INTEGER NOT NULL,
                    next_check_unix INTEGER NOT NULL,
                    is_active INTEGER DEFAULT 1
                );
            """)

    def create_incident(
        self,
        mechanism: str,
        ambient_temp_c: float = 15.0,
        is_amputation: bool = False,
        now_unix: Optional[int] = None,
    ) -> str:
        incident_id = f"INC-{uuid.uuid4().hex[:6].upper()}"
        ts = now_unix if now_unix is not None else int(time.time())
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO incident_session
                (incident_id, created_at_unix, mechanism_of_injury, ambient_temp_c, is_amputation)
                VALUES (?, ?, ?, ?, ?)
                """,
                (incident_id, ts, mechanism, ambient_temp_c, int(is_amputation)),
            )
        return incident_id

    def update_evac_category(self, incident_id: str, category: str) -> None:
        if category not in ("CAT_A", "CAT_B", "CAT_C"):
            return
        with self.conn:
            self.conn.execute(
                "UPDATE incident_session SET evac_category = ? WHERE incident_id = ?",
                (category, incident_id),
            )

    def log_vitals(
        self,
        incident_id: str,
        hr: int,
        sbp: int,
        rr: int,
        avpu: str,
        cap_refill: float,
        now_unix: Optional[int] = None,
    ) -> int:
        ts = now_unix if now_unix is not None else int(time.time())
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO vitals_timeseries
                (incident_id, recorded_at_unix, heart_rate_bpm, systolic_bp_mmhg,
                respiratory_rate_bpm, avpu_state, cap_refill_sec)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (incident_id, ts, hr, sbp, rr, avpu, cap_refill)
            )
            return cur.lastrowid

    def register_timer(
        self,
        incident_id: str,
        intervention_type: str,
        anatomical_site: str,
        interval_minutes: int,
        now_unix: Optional[int] = None,
    ) -> str:
        timer_id = f"TMR-{uuid.uuid4().hex[:6].upper()}"
        ts = now_unix if now_unix is not None else int(time.time())
        next_check = ts + (interval_minutes * 60)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO active_timers
                (timer_id, incident_id, intervention_type, anatomical_site, applied_at_unix, next_check_unix)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (timer_id, incident_id, intervention_type, anatomical_site, ts, next_check),
            )
        return timer_id

    def get_vitals_history(self, incident_id: str) -> List[Dict]:
        cur = self.conn.execute(
            """
            SELECT * FROM vitals_timeseries
            WHERE incident_id = ?
            ORDER BY recorded_at_unix ASC, entry_id ASC
            """,
            (incident_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_incident(self, incident_id: str) -> Dict:
        cur = self.conn.execute(
            "SELECT * FROM incident_session WHERE incident_id = ?", (incident_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else {}

    def get_active_timers(self, incident_id: str) -> List[Dict]:
        cur = self.conn.execute(
            "SELECT * FROM active_timers WHERE incident_id = ? AND is_active = 1",
            (incident_id,),
        )
        return [dict(row) for row in cur.fetchall()]


# ============================================================================
# 4. DETERMINISTIC HEMORRHAGE TRIAGE FSM ENGINE
# ============================================================================

class DeterministicTriageFSM:
    def __init__(self, db: AuraDatabase, incident_id: str):
        self.db = db
        self.incident_id = incident_id
        import re
        self.nodes = json.loads(re.sub(r",(\s*[\]}])", r"\1", HEMORRHAGE_PROTOCOL_JSON))["nodes"]
        
        root_key = next(iter(self.nodes))
        self.current_node_id = root_key
        
        root_opts = self.nodes[root_key].setdefault("options", [])
        if not any("SNAKE" in opt.get("label", "").upper() for opt in root_opts):
            root_opts.insert(0, {
                "label": "snakebite / Envenomation",
                "next_node": "SNAKEBITE_ASSESS"
            })
        
        self.nodes.update({
            "SNAKEBITE_ASSESS": {
                "priority": "CAT A (URGENT)",
                "question": "Snakebite confirming or suspected. What are the primary symptoms?",
                "critical_warning": "DO NOT apply a tourniquet, cut the wound, suck venom, or apply ice!",
                "instructions": [
                    "Move patient away from the snake's striking distance.",
                    "Keep the patient calm and completely still to slow venom spread.",
                    "Remove rings, watches, and tight clothing near the bie site before swelling starts."
                ],
                "options": [
                    {"label": "Neurotoxic (Drooping eyelids, slurred speech, breading difficulty)", "next_node": "SNAKEBITE_NEURO"},
                    {"label": "Cytotoxic / Hemotoxic (Severe pain, rapid swelling, slurred speech, bleeding/blistering)", "next_node": "SNAKEBITE_CYTO"},
                    {"label": "Asymptomatic / Dry Bite (No swelling or systematic signs yet)", "next_node": "SNAKEBITE_OBSERVE"}
                    
                ],
                "reassessment_interval_minutes": 5
            },
            "SNAKEBITE_NEURO": {
                "priority": "CAT A (CRITICAL)",
                "question": "Neurotoxic Envenomation (Elapid / Mamba / Cobra). Is airway/ breathing compromised?",
                "critical_warning": "HIGH RISK OF RESPIRATORY PARALYSIS. Prepare for assisted ventilation.",
                "instructions": [
                    "Immobilize the bitten limb with a splint at heart level.",
                    "Apply Pressure Immobilization Bandage (PIB) ONLY if confirmed neurotoxic (non-necrotic) bite.",
                    "Mark the leading edge of local swelling with a pen every 15 minutes.",
                    "Evacuate immediately to a facility with polyvalent antivenom."
                ],
                "options": [
                    {"label": "Breathing failing -> Begin BVM / Rescue Breathing & Evacuate", "next_node": "SNAKEBITE_EVAC"},
                    {"label": "Airway stable -> Immobilize limb & Monitor Every 5 Min", "next_node": "SNAKEBITE_EVAC"}
                ],
                "reassessment_interval_minutes": 5
            },
            "SNAKEBITE-CYTO": {
                "priority": "CAT A (URGENT)",
                "question": "Cytotoxic / Hemotoxic Envenomation (Viper / Puff Adder / Spitting Cobra).",
                "critical_warning": "DO NOT apply tight pressure bandages or tourniquets-this worsens local tissue necrosis!",
                "instructions": [
                    "Immobilize the limb with a splint and keep it resting below or at heart level.",
                    "Circle the bite site and mark the advantage edge of swelling with a timestamp every 15 mins.",
                    "Avoid NSAIDs (ibuprofen/aspirin) as they worsen bleeding risk."
                ],
                "options": [
                    {"label": "Limb Splinted & Swelling Marked -> Proceed to Evacuation & Monitoring", "next_node": "SNAKEBITE_EVAC"}
                ],
                "reassessment_interval_minutes": 10
            },
            "SNAKEBITE_OBSERVE": {
                "priority": "CAT B (PRIORITY)",
                "question": "Suspended Dry Bite / Early Envenomation Observation.",
                "critical_warning": "Venom symptoms can be delayed by several hours. Do not leave patient unattended.",
                "instructions": [
                    "Clean the wound gently with soap and water; cover with a clean, sry dressing.",
                    "Immobilize the bitten extremity and keep the patient at rest.",
                    "Reassess every 15 minutes for ptosis (drooping eyelids), swelling progression, or bleeding gums."
                ],
                "option": [
                    {"label": "Symptoms Developing -> Escalate to Active Envenomation", "next_node": "SNAKEBITE_ASSESS"},
                    {"label": "Stable -> Continue Observation & Arrange Transport", "next_node": "SNAKEBITE_EVA"}
                ],
                "reassessment_interval_minutes": 15
            },
            "SNAKEBITE_EVAC": {
                "priority": "CAT A (MEDEVAC)",
                "question": "Snakebite Stabilization Complete - Continuous Field Monitoring & Evacuation",
                "critical_warning": "Transmit MIST Beacon with bite time, swelling progression rate, and systemic signs.",
                "instructions": [
                    "Keep patient completely supine and immobilized during transport.",
                    "Log vitals every 5-10 minutes and watch for sudden drop in BP or Sp02.",
                    "Transmit MIST packet to receiving hospital so antivenom can be prepared prior to arrival."
                ],
                "options": [
                    {"label": "Re-evaluate Snakebite Symptoms", "next_node": "SNAKEBITE_ASSESS"},
                    {"label": "Return to Root Trauma Triage", "next_node": root_key}
                ],
                "reassessment_interval_minutes": 5
            }
        })
        # --- END OF PASTE ---
    def current_view(self) -> TriageStateView:
        node = self.nodes[self.current_node_id]
        return TriageStateView(
            node_id=self.current_node_id,
            priority=node.get("priority", "IN_TRIAGE"),
            question=node.get("question"),
            critical_warning=node.get("critical_warning"),
            instructions=node.get("instructions", []),
            options=[opt["label"] for opt in node.get("options", [])],
            reassessment_minutes=node.get("reassessment_interval_minutes"),
        )

    def select_option(
        self,
        option_label: str,
        anatomical_site: str = "R-THIGH",
        now_unix: Optional[int] = None,
    ) -> TriageStateView:
        node = self.nodes[self.current_node_id]
        matched_next = None
        for opt in node.get("options", []):
            if opt["label"].lower() == option_label.lower():
                matched_next = opt["next_node"]
                break

        if not matched_next:
            raise ValueError(f"Invalid option '{option_label}' for node {self.current_node_id}")

        self.current_node_id = matched_next
        target_node = self.nodes[self.current_node_id]

        # Automatically update evacuation priority in SQLite if node dictates one
        if "priority" in target_node:
            self.db.update_evac_category(self.incident_id, target_node["priority"])

        # Automatically register a hardware reassessment timer if node performs an intervention
        if "intervention_type" in target_node and "reassessment_interval_minutes" in target_node:
            self.db.register_timer(
                incident_id=self.incident_id,
                intervention_type=target_node["intervention_type"],
                anatomical_site=anatomical_site,
                interval_minutes=target_node["reassessment_interval_minutes"],
                now_unix=now_unix,
            )

        return self.current_view()


# ============================================================================
# 5. PHYSIOLOGICAL TREND, SHOCK-INDEX & PFC CONVERSION DAEMON
# ============================================================================

class VitalsMonitorDaemon:
    """
    Evaluates vital sign trajectories, Shock Index (HR / SBP), hypothermia risk,
    and Prolonged Field Care (PFC) 2-hour tourniquet conversion eligibility.
    """

    def __init__(self, db: AuraDatabase):
        self.db = db

    def evaluate(self, incident_id: str, now_unix: Optional[int] = None) -> List[ClinicalAlert]:
        ts = now_unix if now_unix is not None else int(time.time())
        alerts: List[ClinicalAlert] = []

        incident = self.db.get_incident(incident_id)
        history = self.db.get_vitals_history(incident_id)
        timers = self.db.get_active_timers(incident_id)

        if not history:
            return alerts

        latest = history[-1]
        hr = latest["heart_rate_bpm"]
        sbp = latest["systolic_bp_mmhg"]
        rr = latest["respiratory_rate_bpm"]
        avpu = latest["avpu_state"]
        cap = latest["cap_refill_sec"]
        shock_index = round(hr / sbp, 2) if sbp > 0 else 9.9

        # 1. Shock Index Evaluation (Normal: 0.5-0.7; > 0.9 indicates occult/active shock)
        in_shock = False
        if shock_index > 0.9 or sbp < 100:
            in_shock = True
            self.db.update_evac_category(incident_id, "CAT_A")
            alerts.append(
                ClinicalAlert(
                    severity="CRITICAL",
                    code="SHOCK_INDEX_HIGH",
                    message=(
                        f"Shock Index is {shock_index} (HR {hr} / SBP {sbp} mmHg). "
                        "Decompensated hemorrhagic shock risk! Inspect tourniquets for seepage, "
                        "apply a 2nd proximal tourniquet if needed, and seal patient in hypothermia wrap."
                    ),
                )
            )

        # 2. Sequential Heart Rate Trajectory Check (>= 20 BPM rise between logs)
        if len(history) >= 2:
            prev = history[-2]
            hr_delta = hr - prev["heart_rate_bpm"]
            if hr_delta >= 20:
                alerts.append(
                    ClinicalAlert(
                        severity="WARNING",
                        code="HR_TREND_SPIKE",
                        message=(
                            f"Heart rate increased by +{hr_delta} BPM since last reading "
                            f"({prev['heart_rate_bpm']} -> {hr} BPM). Early sign of volume loss."
                        ),
                    )
                )

        # 3. Respiratory, Perfusion & Neurological Thresholds
        if rr < 8 or rr > 30:
            alerts.append(
                ClinicalAlert(
                    severity="CRITICAL",
                    code="RESP_DISTRESS",
                    message=f"Respiratory rate {rr} BPM is outside safe bounds (8-30). Check airway and chest wall.",
                )
            )

        if avpu in ("P", "U"):
            alerts.append(
                ClinicalAlert(
                    severity="CRITICAL",
                    code="NEURO_DECLINE",
                    message=f"AVPU status is '{avpu}'. Protect airwayimmediately; place in recovery position if spine clear.",
                )
            )

        if cap > 3.0:
            alerts.append(
                ClinicalAlert(
                    severity="WARNING",
                    code="DELAYED_PERFUSION",
                    message=f"Capillary refill is {cap}s (>3.0s). Peripheral vasoconstriction / hypothermia / shock.",
                )
            )

        # 4. Active Timer & Prolonged Field Care (PFC) Tourniquet Conversion Rules
        for tmr in timers:
            elapsed_min = (ts - tmr["applied_at_unix"]) // 60

            if ts >= tmr["next_check_unix"]:
                alerts.append(
                    ClinicalAlert(
                        severity="WARNING",
                        code="TIMER_REASSESS_DUE",
                        message=(
                            f"{tmr['intervention_type']} on {tmr['anatomical_site']} "
                            f"is due for mandatory check (Applied {elapsed_min} min ago)."
                        ),
                    )
                )

            # CoTCCC / PFC Tourniquet Conversion Check (Window: 120 to 360 minutes)
            if tmr["intervention_type"] == "TOURNIQUET" and elapsed_min >= 120:
                is_amputation = bool(incident.get("is_amputation", 0))
                if elapsed_min > 360:
                    alerts.append(
                        ClinicalAlert(
                            severity="CRITICAL",
                            code="TQ_DO_NOT_REMOVE_6HR",
                            message=(
                                f"Tourniquet on {tmr['anatomical_site']} has been on for {elapsed_min} min (>6 hours). "
                                "DO NOT loosen in the field—fatal reperfusion/hyperkalemia risk."
                            ),
                        )
                    )
                elif in_shock or is_amputation:
                    reason = "patient is in active SHOCK" if in_shock else "limb is an AMPUTATION"
                    alerts.append(
                        ClinicalAlert(
                            severity="CRITICAL",
                            code="TQ_CONVERSION_CONTRAINDICATED",
                            message=(
                                f"Tourniquet on {tmr['anatomical_site']} reached {elapsed_min} min (>2h PFC window), "
                                f"but conversion is CONTRAINDICATED because {reason}. Leave tourniquet locked!"
                            ),
                        )
                    )
                else:
                    alerts.append(
                        ClinicalAlert(
                            severity="INFO",
                            code="TQ_CONVERSION_ELIGIBLE",
                            message=(
                                f"Tourniquet on {tmr['anatomical_site']} at {elapsed_min} min: Patient is hemodynamically "
                                "stable (not in shock). Eligible to attempt Plus-1 Tourniquet & Pressure Dressing conversion."
                            ),
                        )
                    )

        return alerts


# ============================================================================
# 6. LOW-BANDWIDTH MIST TELEMETRY SERIALIZER (<160 CHARS)
# ============================================================================

class MistSerializer:
    @staticmethod
    def build_packet(db: AuraDatabase, incident_id: str) -> str:
        inc = db.get_incident(incident_id)
        vitals = db.get_vitals_history(incident_id)
        timers = db.get_active_timers(incident_id)

        mech = inc.get("mechanism_of_injury", "UNK")[:18].upper().replace(" ", "-")
        cat = inc.get("evac_category", "CAT_A")

        if vitals:
            v = vitals[-1]
            si = round(v["heart_rate_bpm"] / v["systolic_bp_mmhg"], 2)
            signs = f"HR{v['heart_rate_bpm']}-BP{v['systolic_bp_mmhg']}-SI{si}-RR{v['respiratory_rate_bpm']}-{v['avpu_state']}"
        else:
            signs = "NO-VITALS"

        tx = ",".join(
            f"{t['intervention_type'][:2]}@{t['anatomical_site']}" for t in timers
        ) or "NONE"

        packet = f"MIST/{cat}/ID:{incident_id}/M:{mech}/S:{signs}/T:{tx}"
        return packet[:160]


# ============================================================================
# 7. END-TO-END SIMULATION
# ============================================================================

def run_simulation() -> None:
    print("=" * 76)
    print(" AURA-1 OFFLINE BACKEND PROTOTYPE - WILDERNESS TRAUMA SIMULATION")
    print("=" * 76)

    db = AuraDatabase(":memory:")
    t0 = 1760000000  # Base simulation timestamp

    # 1. Start incident session
    inc_id = db.create_incident(
        mechanism="4m Rock Ledge Fall",
        ambient_temp_c=8.5,
        is_amputation=False,
        now_unix=t0,
    )
    print(f"\n[1] Created Offline Incident Session: {inc_id} (Temp: 8.5°C)")

    # 2. Step through the Deterministic Hemorrhage FSM
    fsm = DeterministicTriageFSM(db, inc_id)
    print(f"\n[2] Starting Deterministic Triage FSM at node: {fsm.current_node_id}")

    choices = [
        "Arm or Leg (Extremity)",
        "Yes (Arterial / Life Threat)",
        "No",  # No commercial tourniquet -> routes to Improvised Windlass TQ
    ]

    for choice in choices:
        view = fsm.current_view()
        print(f"    Q: {view.question}")
        print(f"    -> User selected: '{choice}'")
        view = fsm.select_option(choice, anatomical_site="R-THIGH", now_unix=t0)

    print(f"\n[3] Terminal Protocol Reached: {view.node_id} | Priority: {view.priority}")
    if view.critical_warning:
        print(f"    ! WARNING: {view.critical_warning}")
    for idx, step in enumerate(view.instructions, 1):
        print(f"      Step {idx}: {step}")

    # 3. Log Initial Post-Tourniquet Vitals (T + 5 minutes: Compensated / Stable)
    daemon = VitalsMonitorDaemon(db)
    t_5m = t0 + (5 * 60)
    db.log_vitals(inc_id, hr=88, sbp=118, rr=18, avpu="A", cap_refill=2.0, now_unix=t_5m)
    alerts_t5 = daemon.evaluate(inc_id, now_unix=t_5m)
    print(f"\n[4] Logged Vitals @ T+5m (HR: 88, SBP: 118, SI: 0.75) -> Alerts: {len(alerts_t5)}")

    # 4. Log Deteriorating Vitals @ T + 125 minutes (Patient developing occult shock)
    t_125m = t0 + (125 * 60)
    db.log_vitals(inc_id, hr=118, sbp=96, rr=24, avpu="V", cap_refill=3.8, now_unix=t_125m)
    alerts_t125 = daemon.evaluate(inc_id, now_unix=t_125m)

    print(f"\n[5] Logged Vitals @ T+125m (HR: 118, SBP: 96, SI: 1.23) -> Alerts Triggered ({len(alerts_t125)}):")
    for alert in alerts_t125:
        print(f"    [{alert.severity}] ({alert.code}): {alert.message}")

    # 5. Generate Compressed Satellite / SMS MIST Packet
    mist_sms = MistSerializer.build_packet(db, inc_id)
    print("\n[6] Outbound Satellite / SMS MIST Packet (<160 chars):")
    print(f"    Payload ({len(mist_sms)} chars): {mist_sms}")
    print("=" * 76)


if __name__ == "__main__":
    run_simulation()