"""Fail a CI job when a JUnit report contains skipped tests or no tests at all.

Integration jobs (PostgreSQL, Redis) must never pass by silently skipping:
a skip there means the service or opt-in variable was not wired up.

Usage: python tools/ci_assert_no_skips.py report.xml [--min-tests N]
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--min-tests", type=int, default=1)
    args = parser.parse_args()

    root = ET.parse(args.report).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = sum(int(suite.get("tests", 0)) for suite in suites)
    skipped = sum(int(suite.get("skipped", 0)) for suite in suites)
    skipped_names = [
        f"{case.get('classname')}::{case.get('name')}"
        for case in root.iter("testcase")
        if case.find("skipped") is not None
    ]
    if total < args.min_tests:
        print(f"Expected at least {args.min_tests} tests, found {total}.")
        return 1
    if skipped:
        print(f"{skipped} test(s) were skipped in an integration job:")
        for name in skipped_names:
            print(f"  - {name}")
        return 1
    print(f"{total} tests ran with no skips.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
