#!/usr/bin/env python3
"""Deterministic healthcheck & Karpathy-style Lint auditor for Teddy's Life OS v2.

Zero external dependencies (Python 3 stdlib only).
Checks:
  1. Hot-layer bloat guard (AI_GUIDE.md <= 250, 00_now.md <= 120, 00_radar.md <= 120)
  2. File staleness based on YAML frontmatter `last_updated` & `review_cycle`
  3. Domain & onboarding readiness progress
  4. Overdue decision deadlines or follow-up reviews (`deadline` / `review_due`)
  5. Dead routing pointers inside AI_GUIDE.md
  6. Basic accidental-secret scan (API keys / private keys)
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HOT_LIMITS = {
    "AI_GUIDE.md": 250,
    "00_now.md": 120,
    "00_radar.md": 120,
}

CYCLE_MAX_DAYS = {
    "weekly": 14,
    "monthly": 45,
    "quarterly": 100,
    "seasonal": 110,
    "semiannual": 200,
    "annual": 380,
}

SECRET_PATTERNS = [
    ("OpenAI/API Key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub PAT", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("Private Key Header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    fm: dict[str, str] = {}
    for line in text[4:end].splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip("'\"")
    return fm


def parse_date(value: str) -> dt.date | None:
    if not value or "YYYY" in value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def main() -> int:
    today = dt.date.today()
    warnings: list[str] = []
    errors: list[str] = []
    pending_files: list[str] = []

    print(f"=== Life OS v2 Lint Audit ({today.isoformat()}) ===\n")

    # 1. Hot-layer length check
    print("[1] Hot-Layer Context Hygiene")
    for rel, limit in HOT_LIMITS.items():
        path = ROOT / rel
        if not path.exists():
            errors.append(f"Missing hot-layer file: {rel}")
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        status = "OK" if lines <= limit else "BLOAT"
        print(f"  - {rel}: {lines}/{limit} lines [{status}]")
        if lines > limit:
            warnings.append(f"{rel} has {lines} lines (recommended <= {limit})")

    # 2. Scan all Markdown files
    md_files = sorted(
        p for p in ROOT.rglob("*.md") if ".git" not in p.parts
    )
    stale_items: list[str] = []
    overdue_items: list[str] = []

    for path in md_files:
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)

        if fm.get("status") == "pending_interview":
            pending_files.append(rel)

        # Staleness check
        cycle = fm.get("review_cycle", "")
        updated = parse_date(fm.get("last_updated", ""))
        if cycle in CYCLE_MAX_DAYS and updated:
            age = (today - updated).days
            if age > CYCLE_MAX_DAYS[cycle]:
                stale_items.append(f"{rel} ({age}d old, cycle={cycle})")

        # Decision deadline / review_due check
        if rel.startswith("03_decisions/") and not rel.startswith("03_decisions/templates/"):
            status = fm.get("status", "")
            deadline = parse_date(fm.get("deadline", ""))
            review_due = parse_date(fm.get("review_due", ""))
            if status == "active" and deadline and deadline <= today:
                overdue_items.append(f"{rel}: decision deadline passed ({deadline})")
            if status == "decided" and review_due and review_due <= today:
                overdue_items.append(f"{rel}: decision review due ({review_due})")

        # Accidental secret scan
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"Potential secret ({label}) detected in {rel}")

    # 3. Onboarding progress
    checklist_path = ROOT / "04_cadence/onboarding_map.md"
    print("\n[2] Onboarding & Domain Coverage")
    if checklist_path.exists():
        c_text = checklist_path.read_text(encoding="utf-8")
        done = len(re.findall(r"^- \[x\] ", c_text, flags=re.MULTILINE))
        todo = len(re.findall(r"^- \[ \] ", c_text, flags=re.MULTILINE))
        print(f"  - Onboarding modules completed: {done}/{done + todo}")
    print(f"  - Files awaiting personal data: {len(pending_files)}")

    # 4. Staleness & overdue
    print("\n[3] Freshness & Decision Follow-ups")
    if stale_items:
        for item in stale_items:
            print(f"  - STALE: {item}")
            warnings.append(f"Stale file: {item}")
    else:
        print("  - All dated files are within review cycles.")

    if overdue_items:
        for item in overdue_items:
            print(f"  - OVERDUE: {item}")
            warnings.append(f"Overdue decision item: {item}")
    else:
        print("  - No overdue decisions or reviews.")

    # 5. Dead-pointer check for AI_GUIDE.md
    guide_path = ROOT / "AI_GUIDE.md"
    dead_pointers: list[str] = []
    if guide_path.exists():
        guide_text = guide_path.read_text(encoding="utf-8")
        refs = sorted(
            set(
                re.findall(
                    r"`((?:0[0-4]_[^`\s*<]+|scripts/[^`\s*<]+|AI_GUIDE\.md))`",
                    guide_text,
                )
            )
        )
        for ref in refs:
            target = ROOT / ref.rstrip("/")
            if not target.exists():
                dead_pointers.append(ref)
                errors.append(f"Dead pointer in AI_GUIDE.md: `{ref}` does not exist")

    # 6. Safety & Integrity
    print("\n[4] Safety & Integrity")
    if dead_pointers:
        for dp in dead_pointers:
            print(f"  - DEAD POINTER: {dp}")
    else:
        print("  - AI_GUIDE.md routing pointers: ALL VALID")

    if errors:
        for err in errors:
            print(f"  - ERROR: {err}")
    else:
        print("  - Secret sanity check: CLEAN")

    print(f"\nAudit complete: {len(errors)} error(s), {len(warnings)} warning(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
