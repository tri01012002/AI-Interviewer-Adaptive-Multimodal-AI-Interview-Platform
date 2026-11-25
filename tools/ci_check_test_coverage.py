"""Fail CI when a test file or test directory is not run by any workflow job.

Prevents suites from being silently omitted: every ``tests/unit/test_*.py``
file and every ``tests/unit/<subdir>/`` with tests must be referenced in
``.github/workflows/test.yml`` (directly or through its directory).

Usage: python tools/ci_check_test_coverage.py [workflow_path]
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    workflow_path = Path(sys.argv[1] if len(sys.argv) > 1 else ".github/workflows/test.yml")
    workflow = workflow_path.read_text(encoding="utf-8")
    root = Path("tests/unit")
    missing: list[str] = []
    for path in sorted(root.rglob("test_*.py")):
        relative = path.as_posix()
        parents = [parent.as_posix() for parent in path.parents if parent != Path(".")]
        if relative in workflow or any(
            f"{parent} " in workflow or f"{parent}\n" in workflow or f"{parent}/ " in workflow
            for parent in parents
            if parent not in {"tests", "tests/unit"}
        ):
            continue
        missing.append(relative)
    if missing:
        print("Test files not run by any CI job:")
        for item in missing:
            print(f"  - {item}")
        return 1
    print("Every tests/unit test file is referenced by CI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
