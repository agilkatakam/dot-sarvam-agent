"""One-step start for testers (run by start.command / start.bat).

First run: asks for the Sarvam API key if the kit didn't come with one, and writes .env.
Every run: starts the server on this computer only, in tester mode (quiet window, "save my
results" on the call page), and opens the call page once the server answers. If it's already
running, it just opens the page again. Stopping with Ctrl+C also saves the results zip.
"""

import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
PORTS = range(7860, 7870)


def say(text: str = "") -> None:
    print(text, flush=True)


def _key_in_env() -> str:
    if not ENV.exists():
        return ""
    m = re.search(r"^SARVAM_API_KEY=(.*)$", ENV.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else ""


def first_run_setup() -> None:
    if _key_in_env():
        return
    say("One-time setup: this needs a Sarvam API key.")
    say("Paste the key Akhil sent you, then press Enter.")
    key = ""
    while len(key) < 10:
        key = input("Key: ").strip().strip('"').strip("'")
        if len(key) < 10:
            say("That doesn't look like a key. Copy the whole key and paste it here.")
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else (ROOT / ".env.example").read_text(encoding="utf-8")
    if re.search(r"^SARVAM_API_KEY=", text, re.M):
        text = re.sub(r"^SARVAM_API_KEY=.*$", lambda _m: f"SARVAM_API_KEY={key}", text, flags=re.M)
    else:
        text = f"SARVAM_API_KEY={key}\n" + text
    ENV.write_text(text, encoding="utf-8")
    say("Saved. You won't be asked again.\n")


def _ours(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tester", timeout=2) as r:
            return json.load(r).get("tester_mode") is True
    except Exception:  # noqa: BLE001 - nothing there, or not this app
        return False


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _open_when_ready(url: str, port: int) -> None:
    for _ in range(120):
        if _ours(port):
            webbrowser.open(url)
            say(f"Ready! Your browser should now show the call page. If it didn't, open {url}")
            say("Keep this window open while you test. When you're done, click")
            say('"I\'m finished: save my results" on that page.\n')
            return
        time.sleep(0.5)
    say("The agent is taking unusually long to start. Send Akhil a photo of this window.")


def main() -> int:
    say("\n=== DOT voice agent test ===\n")
    for port in PORTS:
        if _ours(port):
            url = f"http://localhost:{port}"
            webbrowser.open(url)
            say(f"It's already running in another window, so the call page was opened again: {url}")
            return 0
    port = next((p for p in PORTS if _free(p)), None)
    if port is None:
        say("Couldn't find a free port. Restart the computer and try again.")
        return 1

    first_run_setup()
    os.environ["HOST"] = "127.0.0.1"  # this computer only; no firewall prompt
    os.environ["PORT"] = str(port)
    os.environ["TESTER_MODE"] = "1"
    say("Starting the agent (this takes a few seconds)...")

    # Quiet before importing Pipecat, which logs a banner on import. server.main() sets up the log file.
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level="ERROR", format="<red>{message}</red>")

    # Imported after setup: agent.config reads .env and the environment once, at import.
    from agent import db, export, server

    url = f"http://localhost:{port}"
    threading.Thread(target=_open_when_ready, args=(url, port), daemon=True).start()
    try:
        server.main()
    except KeyboardInterrupt:
        pass

    db.init()
    if not any(c["channel"] == "browser" and c.get("connected_at") for c in db.all_calls()):
        say("\nStopped. No calls were made, so there's nothing to send.")
        return 0
    dest = export.write_bundle(export.default_folder() / export.bundle_name())
    say(f"\nStopped. Your results were also saved to your Downloads folder:\n  {dest}")
    say("Send that file to Akhil on WhatsApp (as a Document).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
