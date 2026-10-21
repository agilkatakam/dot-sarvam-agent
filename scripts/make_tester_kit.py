"""Build the zip you send to testers.

  python -m scripts.make_tester_kit              asks whether to include your Sarvam key
  python -m scripts.make_tester_kit --with-key   include the key from .env (testers paste nothing)
  python -m scripts.make_tester_kit --no-key     testers paste a key on first run

Writes dist/DOT-Voice-Agent-Test.zip. Unzipped, testers see only:

  DOT Voice Agent Test/
    READ ME FIRST.txt
    Start (Windows).bat
    Start (Mac).command
    app/                      everything else: files in git plus new uncommitted ones, never
                              anything gitignored (.env, data/); --with-key adds a fresh .env
"""

import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP = "DOT Voice Agent Test"
GUIDE = "READ ME FIRST.txt"

START_WINDOWS = "\r\n".join([
    "@echo off",
    'if not exist "%~dp0app\\start.bat" (',
    "  echo.",
    "  echo Please unzip the folder first:",
    "  echo right-click the zip file, choose Extract All, then open the new folder",
    '  echo and double-click "Start (Windows)" in there.',
    "  echo.",
    "  pause",
    "  exit /b 1",
    ")",
    'call "%~dp0app\\start.bat"',
    "",
])

START_MAC = "\n".join([
    "#!/usr/bin/env bash",
    "# Mac: double-click. Linux: run  bash \"Start (Mac).command\"",
    'exec bash "$(dirname "$0")/app/start.command"',
    "",
])


def _info(name: str, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(f"{TOP}/{name}", date_time=time.localtime()[:6])
    info.external_attr = ((0o755 if executable else 0o644) | 0o100000) << 16  # regular file + mode
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _env_with_key(key: str) -> str:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return re.sub(r"^SARVAM_API_KEY=.*$", lambda _m: f"SARVAM_API_KEY={key}", text, flags=re.M)


def _local_key() -> str:
    env = ROOT / ".env"
    m = re.search(r"^SARVAM_API_KEY=(.*)$", env.read_text(encoding="utf-8"), re.M) if env.exists() else None
    return m.group(1).strip() if m else ""


def build(dest: Path, key: str | None) -> Path:
    files = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           cwd=ROOT, capture_output=True, check=True, text=True).stdout
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as z:
        for rel in sorted(f for f in files.split("\0") if f):
            path = ROOT / rel
            if not path.is_file():
                continue  # deleted but not yet committed
            if rel == GUIDE:
                z.writestr(_info(GUIDE), path.read_bytes())
                continue
            z.writestr(_info(f"app/{rel}", executable=rel.endswith((".command", ".sh"))), path.read_bytes())
        z.writestr(_info("Start (Windows).bat"), START_WINDOWS)
        z.writestr(_info("Start (Mac).command", executable=True), START_MAC)
        if key:
            z.writestr(_info("app/.env"), _env_with_key(key))
    return dest


def main() -> None:
    key = _local_key()
    if "--no-key" in sys.argv:
        include = False
    elif "--with-key" in sys.argv:
        include = True
    else:
        say = "Include your Sarvam key so testers don't have to paste one? Their calls will use your credit. [y/N] "
        include = input(say).strip().lower().startswith("y")
    if include and not key:
        sys.exit("No SARVAM_API_KEY in .env to include.")
    dest = build(ROOT / "dist" / "DOT-Voice-Agent-Test.zip", key if include else None)
    mb = dest.stat().st_size / 1e6
    print(f"Built {dest} ({mb:.1f} MB), {'with' if include else 'without'} your Sarvam key.")
    print("Send it to testers on WhatsApp (as a Document), email, or Google Drive.")


if __name__ == "__main__":
    main()
