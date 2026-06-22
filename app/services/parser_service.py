import json
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
COURSES_FILE = _PROJECT_ROOT / "app" / "data" / "courses.json"


def has_courses() -> bool:
    if not COURSES_FILE.exists():
        return False
    try:
        data = json.loads(COURSES_FILE.read_text(encoding="utf-8"))
        return len(data) > 0
    except Exception:
        return False


def run_parser(url: str) -> None:
    subprocess.run(
        [sys.executable, "app/scripts/parse_courses.py", url],
        check=True,
    )
