import sqlite3
import stat
import zipfile

from agent import db, export
from agent.config import settings
from scripts import make_tester_kit, merge_results


def _results_zip(tmp_path, name, calls):
    db.init()
    for call_id, tester in calls:
        db.insert_call(call_id, "browser", None, tester)
        db.update_call(call_id, connected_at=db.now(), ended_at=db.now(), end_reason="caller_hangup",
                       prompt_version=1, transcript=[{"role": "user", "text": f"hi from {tester}"}])
        db.insert_turn(call_id, "greeting", 500)
    settings.recordings_dir.mkdir(parents=True, exist_ok=True)
    (settings.recordings_dir / f"{calls[0][0]}.wav").write_bytes(b"RIFF")
    path = export.write_bundle(tmp_path / name)
    settings.db_path.unlink()
    (settings.recordings_dir / f"{calls[0][0]}.wav").unlink()
    return path


def test_merge_combines_testers_and_skips_repeats(tmp_path):
    a = _results_zip(tmp_path, "a.zip", [("a1", "Priya"), ("a2", "Priya")])
    b = _results_zip(tmp_path, "b.zip", [("b1", "Rahul")])
    out = tmp_path / "merged"
    assert merge_results.merge([a, b], out) == 3
    assert merge_results.merge([a], out) == 0  # already merged
    con = sqlite3.connect(out / "calls.db")
    assert sorted(r[0] for r in con.execute("SELECT tester FROM calls")) == ["Priya", "Priya", "Rahul"]
    assert con.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(DISTINCT prompt_version) FROM calls").fetchone()[0] == 1
    assert (out / "recordings" / "a1.wav").exists() and (out / "recordings" / "b1.wav").exists()
    assert "Rahul" in (out / "all_calls.csv").read_text()
    assert len(list((out / "transcripts").glob("*.txt"))) == 3


def test_kit_layout_and_permissions(tmp_path):
    path = make_tester_kit.build(tmp_path / "kit.zip", key="sk_test_123456")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        top = {n.split("/")[1] for n in names}
        assert top == {"READ ME FIRST.txt", "Start (Windows).bat", "Start (Mac).command", "app"}
        assert "DOT Voice Agent Test/app/scripts/launch.py" in names
        assert not any("/data/" in n or n.endswith("/.venv") for n in names)
        mac = z.getinfo("DOT Voice Agent Test/Start (Mac).command")
        assert stat.S_IMODE(mac.external_attr >> 16) & 0o111
        assert stat.S_IMODE(z.getinfo("DOT Voice Agent Test/app/start.command").external_attr >> 16) & 0o111
        assert "SARVAM_API_KEY=sk_test_123456" in z.read("DOT Voice Agent Test/app/.env").decode()
        assert b"\r\n" in z.read("DOT Voice Agent Test/Start (Windows).bat")
    no_key = make_tester_kit.build(tmp_path / "kit2.zip", key=None)
    assert "DOT Voice Agent Test/app/.env" not in zipfile.ZipFile(no_key).namelist()
