"""PostToolUse hook: format Python files with autopep8 after Claude edits them."""

import json
import subprocess
import sys
from pathlib import Path

EXCLUDED_DIRS = {"venv-manifest-validator"}


def main():
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not file_path:
        return

    path = Path(file_path)
    if path.suffix != ".py" or not path.is_file():
        return
    if EXCLUDED_DIRS.intersection(path.parts):
        return

    subprocess.run(
        [sys.executable, "-m", "autopep8", "--in-place", str(path)],
        check=False,
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
