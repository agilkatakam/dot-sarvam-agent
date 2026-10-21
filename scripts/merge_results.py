"""Combine testers' results zips into one data folder and open it in the dashboard.

  python -m scripts.merge_results ~/Downloads/dot-test-results-*.zip
  python -m scripts.merge_results --no-open a.zip b.zip      merge only

Writes results/ (calls.db, recordings/, logs/<zip name>/, all_calls.csv, transcripts/).
Running it again with more zips adds them; a call already merged is skipped. Prompt versions
are matched by content, so every tester's "version 1" stays one version if the prompt is the same.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from agent import db, export

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results"


def _merge_db(src_path: Path, dst: sqlite3.Connection) -> tuple[int, int]:
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    try:
        versions = {}
        for v in src.execute("SELECT * FROM prompt_versions"):
            doc = json.dumps(json.loads(v["doc"]), sort_keys=True)
            hit = next((r[0] for r in dst.execute("SELECT id, doc FROM prompt_versions")
                        if json.dumps(json.loads(r[1]), sort_keys=True) == doc), None)
            if hit is None:
                hit = dst.execute("INSERT INTO prompt_versions (created_at, note, doc) VALUES (?, ?, ?)",
                                  (v["created_at"], v["note"], v["doc"])).lastrowid
            versions[v["id"]] = hit

        dst_cols = {r[1] for r in dst.execute("PRAGMA table_info(calls)")}
        added = skipped = 0
        for c in src.execute("SELECT * FROM calls"):
            if dst.execute("SELECT 1 FROM calls WHERE id = ?", (c["id"],)).fetchone():
                skipped += 1
                continue
            row = {k: c[k] for k in c.keys() if k in dst_cols}
            if row.get("prompt_version") is not None:
                row["prompt_version"] = versions.get(row["prompt_version"], row["prompt_version"])
            dst.execute(f"INSERT INTO calls ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
            for t in src.execute("SELECT * FROM turns WHERE call_id = ?", (c["id"],)):
                dst.execute(f"INSERT INTO turns ({', '.join(t.keys())}) VALUES ({', '.join('?' for _ in t.keys())})", tuple(t))
            added += 1
        for b in src.execute("SELECT * FROM bookings"):
            dst.execute(f"INSERT OR IGNORE INTO bookings ({', '.join(b.keys())}) VALUES ({', '.join('?' for _ in b.keys())})", tuple(b))
        for d in src.execute("SELECT kind, value, reason, call_id, created_at FROM dnc"):
            dst.execute("INSERT OR IGNORE INTO dnc (kind, value, reason, call_id, created_at) VALUES (?, ?, ?, ?, ?)", tuple(d))
        dst.commit()
        return added, skipped
    finally:
        src.close()


def merge(zips: list[Path], out: Path = OUT) -> int:
    out.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(out / "calls.db")
    dst.row_factory = sqlite3.Row
    db.ensure_schema(dst)
    total = 0
    try:
        for zp in zips:
            with zipfile.ZipFile(zp) as z, tempfile.TemporaryDirectory() as tmp:
                if "calls.db" not in z.namelist():
                    print(f"  {zp.name}: not a results zip, skipped")
                    continue
                z.extract("calls.db", tmp)
                added, skipped = _merge_db(Path(tmp) / "calls.db", dst)
                for name in z.namelist():
                    if name.startswith("recordings/") and name.endswith(".wav"):
                        target = out / name
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(z.read(name))
                    elif name.startswith("logs/") and not name.endswith("/"):
                        target = out / "logs" / zp.stem / Path(name).name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(z.read(name))
                total += added
                print(f"  {zp.name}: {added} new call(s)" + (f", {skipped} already merged" if skipped else ""))
        calls = [db.decode_call(r) for r in dst.execute("SELECT * FROM calls ORDER BY started_at DESC")]
        turns = [dict(t) for t in dst.execute("SELECT * FROM turns")]
    finally:
        dst.close()

    (out / "all_calls.csv").write_text(export.calls_csv(calls), encoding="utf-8")
    tdir = out / "transcripts"
    tdir.mkdir(exist_ok=True)
    for c in calls:
        who = export.safe_name(c.get("tester") or "unknown")
        (tdir / f"{who}_{c['started_at'][:19].replace(':', '')}_{c['id'][:8]}.txt").write_text(
            export.transcript_text(c, turns), encoding="utf-8")
    testers = sorted({c.get("tester") or "unknown" for c in calls})
    print(f"\n{len(calls)} call(s) in {out} from: {', '.join(testers)}")
    print(f"  spreadsheet:  {out / 'all_calls.csv'}\n  transcripts:  {tdir}")
    return total


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    merge([Path(a).expanduser() for a in args])
    if "--no-open" not in sys.argv:
        print("\nOpening the dashboard on http://localhost:7860/dashboard (password from your .env). Ctrl+C to stop.")
        subprocess.run([sys.executable, "-m", "agent.server"], env={**os.environ, "DATA_DIR": str(OUT)}, cwd=ROOT)


if __name__ == "__main__":
    main()
