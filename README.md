# AURA-1 | Austere Trauma & Prolonged Field Care (PFC) Triage System

AURA-1 is a zero-dependency, offline-first medical triage and field decision-support web application designed for wilderness trauma and austere environments. Built on Python's standard library and SQLite, it guides responders through deterministic clinical protocols while logging vitals, tracking active interventions, and formatting emergency evacuation transmissions without requiring internet connectivity.

---

## Key Features

* **Deterministic Triage FSM Engine:** Step-by-step clinical decision trees covering massive hemorrhage control (extremity and junctional bleeding) and snakebite envenomation protocols (neurotoxic, cytotoxic/hemotoxic, and dry-bite observation).
* **100% Offline Architecture:** Runs locally using Python's built-in `http.server` and an embedded SQLite Write-Ahead Logging (WAL) database (`aura_field.db`) with zero external package requirements.
* **Prolonged Field Care (PFC) Monitoring:** Tracks active tourniquet timers (`TQ`), Shock Index (`SI`), priority categories (`CAT A/B/C`), and automated reassessment intervals (`+2H SIM`).
* **MIST Beacon Generation:** Compiles patient vitals, injury mechanisms, and anatomical intervention sites into compact SMS/SATCOM-ready MIST evacuation strings.
* **Cross-Device Field Access:** Accessible on laptops (`localhost:8080`) and mobile devices (iPhone/Android) via local Wi-Fi, personal hotspot, or public tunneling.

---

## Project Structure

| File | Description |
| :--- | :--- |
| `aura_server.py` | HTTP server, REST API router (`/api/state`, `do_POST`), and local controller bridging the UI and backend. |
| `aura_backend.py` | Core business logic containing `AuraDatabase` (SQLite persistence) and `DeterministicTriageFSM` (JSON protocol state machine). |
| `aura_frontend.html` | Single-page tactical medical UI featuring high-contrast triage cards, site selectors, vitals logging, and optional text-to-speech (`TTS`). |
| `aura_field.db` | Auto-generated SQLite WAL database storing incident sessions, vitals logs, and active intervention timers. |

---

## Quick Start

### 1. Run Locally (Laptop)
Start the server from your project directory:
```powershell
python aura_server.py
