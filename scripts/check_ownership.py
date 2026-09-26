#!/usr/bin/env python3
"""Pre-commit guard: block commits that touch files your role doesn't own (PRD §6, §14 rule 2).

Setup once per clone:
    git config reigns.role B          # A, B, C or D
    sh scripts/install_hooks.sh

Bypass (only after agreeing in team chat): git commit --no-verify
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys

# Files anyone may touch.
SHARED = ["docs/requests.md", "PRD.md", "docs/*"]

# Most specific rule wins: EXCEPTIONS are checked before OWNED.
EXCEPTIONS = {
    "engine/app/detectors/pushback.py": "B",
    "engine/app/detectors/source_faithfulness.py": "D",
    "engine/app/learning/memory.py": "C",
    "engine/app/learning/calibration.py": "D",
    "engine/app/detectors/__init__.py": "B",
    "engine/app/course_correct/__init__.py": "B",
    "engine/app/learning/__init__.py": "B",
}

OWNED = [
    ("companion/*", "A"),
    ("engine/app/detectors/*", "C"),
    ("engine/tests/detectors/*", "C"),
    ("engine/app/course_correct/*", "D"),
    ("engine/tests/course_correct/*", "D"),
    ("eval/*", "D"),
    ("pitch/*", "D"),
    # Everything else (engine/, shared/, scripts/, root config) → Role B (steward).
    ("*", "B"),
]


def owner_of(path: str) -> str | None:
    if any(fnmatch.fnmatch(path, pat) for pat in SHARED):
        return None
    if path in EXCEPTIONS:
        return EXCEPTIONS[path]
    for pattern, role in OWNED:
        if fnmatch.fnmatch(path, pattern):
            return role
    return "B"


def main() -> int:
    try:
        role = subprocess.check_output(["git", "config", "reigns.role"], text=True).strip().upper()
    except subprocess.CalledProcessError:
        print("reigns: set your role first →  git config reigns.role <A|B|C|D>")
        return 1
    staged = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRD"], text=True
    ).split()
    bad = [(p, owner_of(p)) for p in staged if owner_of(p) not in (None, role)]
    if bad:
        print(f"reigns: you are Role {role}, but this commit touches files you don't own:")
        for path, owner in bad:
            print(f"   {path}  (owner: Role {owner})")
        print("Unstage them (git restore --staged <file>) and add a note to docs/requests.md,")
        print("or, if the owner agreed in chat, commit with --no-verify.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
