"""Bundle every test call into one zip to send back.

  python -m scripts.export            writes dot-test-results-<name>-<date>.zip next to this project
  python -m scripts.export out.zip    or to a path of your choice

What's inside is listed in agent/export.py. Works while the server is running or stopped.
"""

import sys
from pathlib import Path

from agent import db, export
from agent.config import ROOT


def main() -> None:
    db.init()
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / export.bundle_name()
    export.write_bundle(dest)
    calls = len(db.all_calls())
    print(f"Saved {calls} call(s) to:\n  {dest.resolve()}\nSend this file back.")


if __name__ == "__main__":
    main()
