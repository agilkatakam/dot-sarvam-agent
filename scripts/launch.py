"""One-step start for testers (run by start.command / start.bat).

First run: asks for the Sarvam API key and writes .env from .env.example.
Every run: starts the server on this computer only, opens the call page in the browser, and
when the server is stopped (Ctrl+C) saves the test results zip to send back.
"""

import os
import re
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def _key_in_env() -> str:
    if not ENV.exists():
        return ""
    m = re.search(r"^SARVAM_API_KEY=(.*)$", ENV.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else ""


def first_run_setup() -> None:
    if _key_in_env():
        return
    print("\nFirst-time setup")
    print("Paste the Sarvam API key you were given (or get a free one at https://dashboard.sarvam.ai).")
    key = ""
    while not key:
        key = input("Sarvam API key: ").strip()
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else (ROOT / ".env.example").read_text(encoding="utf-8")
    if re.search(r"^SARVAM_API_KEY=", text, re.M):
        text = re.sub(r"^SARVAM_API_KEY=.*$", lambda _m: f"SARVAM_API_KEY={key}", text, flags=re.M)
    else:
        text = f"SARVAM_API_KEY={key}\n" + text
    ENV.write_text(text, encoding="utf-8")
    print("Saved. You won't be asked again.\n")


def main() -> None:
    first_run_setup()
    os.environ.setdefault("HOST", "127.0.0.1")  # this computer only; no firewall prompt

    # Imported after setup: agent.config reads .env once, at import.
    from agent import db, export, server
    from agent.config import settings

    url = f"http://localhost:{settings.port}"
    print(f"\nStarting the agent. The call page opens at {url}")
    print(f"Your results so far: {url}/dashboard (any username, password: {settings.dashboard_password})")
    print("When you're done testing, press Ctrl+C here. Your results zip is saved automatically.\n")
    threading.Timer(3.0, webbrowser.open, args=(url,)).start()
    try:
        server.main()
    except KeyboardInterrupt:
        pass

    db.init()
    calls = [c for c in db.all_calls() if c["channel"] != "simulated"]
    if not calls:
        print("\nNo calls made yet, so there's nothing to send.")
        return
    dest = export.write_bundle(ROOT / export.bundle_name())
    print(f"\nSaved {len(calls)} call(s) to:\n  {dest}\nSend this file back. (You can also make it any time with export-results.)")


if __name__ == "__main__":
    sys.exit(main())
