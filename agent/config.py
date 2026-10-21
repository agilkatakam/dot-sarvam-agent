"""Server settings, read once from the environment / .env next to pyproject.toml.

These need a restart to change. Everything you might tune between calls (models, voice,
turn-taking, timeouts) lives in agent/runtime.py and is editable on the dashboard.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int | None) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


# Database, recordings and dashboard-edited settings live here.
_DATA = _path("DATA_DIR", ROOT / "data")


@dataclass(frozen=True)
class Settings:
    sarvam_api_key: str = os.getenv("SARVAM_API_KEY", "").strip()

    org_name: str = os.getenv("ORG_NAME", "Dot").strip()
    dashboard_password: str = os.getenv("DASHBOARD_PASSWORD", "change-me")
    max_concurrent_calls: int = _int("MAX_CONCURRENT_CALLS", 3) or 3

    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

    # Meetings. Google Calendar is used when a token exists (scripts/connect_google_calendar.py);
    # otherwise bookings go to the built-in calendar (dashboard + .ics download).
    timezone: str = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
    meeting_minutes: int = _int("MEETING_MINUTES", 20) or 20
    working_hours_start: str = os.getenv("WORKING_HOURS_START", "10:00").strip()
    working_hours_end: str = os.getenv("WORKING_HOURS_END", "17:00").strip()
    working_days: str = os.getenv("WORKING_DAYS", "mon,tue,wed,thu,fri").strip().lower()
    booking_min_lead_hours: int = _int("BOOKING_MIN_LEAD_HOURS", 2) or 0
    booking_horizon_days: int = _int("BOOKING_HORIZON_DAYS", 21) or 21
    google_calendar_id: str = os.getenv("GOOGLE_CALENDAR_ID", "primary").strip()
    google_credentials_path: Path = _path("GOOGLE_CALENDAR_CREDENTIALS_PATH", ROOT / "secrets" / "credentials.json")
    google_token_path: Path = _path("GOOGLE_CALENDAR_TOKEN_PATH", ROOT / "secrets" / "token.json")
    # Added as a guest on every booked meeting (e.g. the BGCC sales lead), comma separated.
    meeting_team_emails: str = os.getenv("MEETING_TEAM_EMAILS", "").strip()

    # Published Sarvam rates (INR, ex-GST) used for the per-call cost estimate.
    price_stt_per_hour: float = _float("PRICE_STT_INR_PER_HOUR", 30.0)
    price_tts_per_1k_chars: float = _float("PRICE_TTS_INR_PER_1K_CHARS", 3.0)
    price_llm_in_per_1m: float = _float("PRICE_LLM_INR_PER_1M_INPUT", 29.0)
    price_llm_out_per_1m: float = _float("PRICE_LLM_INR_PER_1M_OUTPUT", 73.0)

    data_dir: Path = _DATA
    db_path: Path = _DATA / "calls.db"
    recordings_dir: Path = _DATA / "recordings"
    runtime_path: Path = _DATA / "runtime.json"
    port: int = _int("PORT", 7860) or 7860

    # The latency KPI threshold the client asked for, and the working target in the design doc.
    latency_target_ms: int = 600
    latency_working_target_ms: int = 1500


settings = Settings()
