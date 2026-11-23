#!/usr/bin/env python3
"""Fully isolated synthetic unit tests for Life OS v3.1 (`scripts/lifeos.py`).

CRITICAL INVARIANT:
  Tests NEVER copy or assert state against the live repository!
  Every fixture is synthesized cleanly in a temporary directory so real-world user
  data growth (e.g. logging 2+ weeks of energy) can NEVER break unit tests or CI.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lifeos  # noqa: E402

BJ = ZoneInfo("Asia/Shanghai")


def make_synthetic_repo(test_case: unittest.TestCase) -> Path:
    """Creates a clean minimal valid v3.1 Life OS repository inside a temp dir."""
    root = Path(tempfile.mkdtemp(prefix="lifeos_v31_"))
    test_case.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))

    (root / "AGENTS.md").write_text("# AGENTS\nShort constitution.\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("See AGENTS.md\n", encoding="utf-8")
    (root / "GEMINI.md").write_text("See AGENTS.md\n", encoding="utf-8")
    (root / "now.md").write_text(
        "---\nas_of: 2026-11-23\n---\n# now\n- 约束：稳健\n",
        encoding="utf-8",
    )
    (root / "compass.md").write_text(
        "---\nas_of: 2026-11-23\n---\n# compass\n\n## 2. 硬红线\n- 不碰高利贷\n\n## 3. 权衡\n- 长期复利\n\n## 4. 限时例外\n```yaml\nexceptions: []\n```\n",
        encoding="utf-8",
    )

    for folder in ["domains", "decisions", "log", "system"]:
        (root / folder).mkdir()

    (root / "domains/career.md").write_text(
        '---\ndomain: career\nfacts:\n  role: {v: "?", as_of: null, src: null, ttl_days: 180}\n---\n# career\n',
        encoding="utf-8",
    )
    (root / "domains/wealth.md").write_text(
        '---\ndomain: wealth\nfacts:\n  runway: {v: "?", as_of: null, src: null, ttl_days: 90}\n---\n# wealth\n',
        encoding="utf-8",
    )
    (root / "domains/health.md").write_text(
        '---\ndomain: health\nfacts:\n  allergies: {v: "?", as_of: null, src: null, ttl_days: 365}\n---\n# health\n',
        encoding="utf-8",
    )
    (root / "domains/safety_net.md").write_text(
        '---\ndomain: safety_net\nfacts:\n  insurance: {v: "?", as_of: null, src: null, ttl_days: 365}\n---\n# safety_net\n',
        encoding="utf-8",
    )
    (root / "domains/daily.md").write_text(
        '---\ndomain: daily\nfacts:\n  city: {v: "?", as_of: null, src: null, ttl_days: 365}\n---\n# daily\n- 默认：清淡高蛋白\n',
        encoding="utf-8",
    )

    (root / "log/2026-11.md").write_text("---\nmonth: 2026-11\n---\n# 2026-11\n", encoding="utf-8")

    (root / "system/config.yml").write_text(
        """owner: teddy88teddy
repo: teddy88teddy/Life
tz: Asia/Shanghai
notify:
  channel: serverchan
  sunday_heartbeat: true
mobile_ai:
  app: gemini_skill
  context_pack_max_chars: 9000
escalation:
  high_hours: 72
  ignored_rule_threshold: 3
spending_gates_cny:
  tier0_instant_max: 200
  tier1_review_max: 1000
  tier2_cooling_max: 5000
  cooling_hours: 72
rules:
  decision_deadline_windows: [7, 1]
  decision_review_lead_days: 3
  exception_expiry_lead_days: 3
  pending_ai_max_days: 7
  energy_low_threshold: 2
  energy_low_consecutive_weeks: 2
  energy_max_gap_days: 21
""",
        encoding="utf-8",
    )
    (root / "system/dates.yml").write_text("items: []\n", encoding="utf-8")
    return root


class FakeGitHub:
    """In-memory fake GitHub API supporting pagination semantics, locking, and comments."""

    def __init__(self) -> None:
        self.next_number = 1
        self.labels: set[str] = set()
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.locked: set[int] = set()
        self.comments_fetched_for: list[int] = []

    def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        self.labels.update(labels.keys())

    def add_raw_issue(
        self,
        *,
        author: str,
        title: str,
        body: str,
        labels: list[str],
        state: str = "open",
        state_reason: str | None = None,
    ) -> dict[str, Any]:
        num = self.next_number
        self.next_number += 1
        iss = {
            "number": num,
            "state": state,
            "state_reason": state_reason,
            "title": title,
            "body": body,
            "html_url": f"https://github.com/teddy88teddy/Life/issues/{num}",
            "user": {"login": author},
            "labels": [{"name": lb} for lb in labels],
            "comments": 0,
        }
        self.issues[num] = iss
        self.comments[num] = []
        return iss

    def add_comment(self, issue_number: int, author: str, body: str, created_at: str) -> None:
        self.comments[issue_number].append(
            {"user": {"login": author}, "body": body, "created_at": created_at}
        )
        self.issues[issue_number]["comments"] = len(self.comments[issue_number])

    def list_issues(self, label: str, state: str = "all") -> list[dict[str, Any]]:
        out = []
        for iss in self.issues.values():
            lb_names = {lb["name"] for lb in iss["labels"]}
            if label not in lb_names:
                continue
            if state != "all" and iss["state"] != state:
                continue
            out.append(iss)
        return out

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        self.comments_fetched_for.append(issue_number)
        return list(self.comments.get(issue_number, []))

    def get_issue(self, issue_number: int) -> dict[str, Any]:
        return self.issues[issue_number]

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self.add_raw_issue(author="github-actions[bot]", title=title, body=body, labels=labels)

    def update_issue(self, issue_number: int, *, title: str | None = None, body: str | None = None) -> dict[str, Any]:
        if title is not None:
            self.issues[issue_number]["title"] = title
        if body is not None:
            self.issues[issue_number]["body"] = body
        return self.issues[issue_number]

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        cur = {lb["name"] for lb in self.issues[issue_number]["labels"]}
        for lb in labels:
            if lb not in cur:
                self.issues[issue_number]["labels"].append({"name": lb})

    def lock_issue(self, issue_number: int) -> None:
        self.locked.add(issue_number)

    def apply_patches(self, plan: dict[str, Any]) -> None:
        """Simulates successful `deliver` PATCHing Issue outbox state lines."""
        for p in plan["issue_patches"]:
            num = p["issue_number"]
            cur = self.get_issue(num)
            new_body = lifeos.update_issue_state_marker(cur["body"], p["new_state"])
            self.update_issue(num, body=new_body)
            if p.get("add_escalated_label"):
                self.add_labels(num, ["escalated"])
        hb = plan.get("heartbeat_patch")
        if hb:
            num = hb["issue_number"]
            cur = self.get_issue(num)
            self.update_issue(num, body=lifeos.update_issue_state_marker(cur["body"], hb["new_state"]))


class _FakeHTTPResp:
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None):
        self.status = status
        self._body = body.encode("utf-8")
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResp":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


class LifeOSV31Tests(unittest.TestCase):
    def test_duplicate_key_rejected_and_yaml_error_no_raw_line(self) -> None:
        root = make_synthetic_repo(self)
        (root / "system/dates.yml").write_text(
            "items:\n  - id: a\n    title: A\n    kind: deadline\n    due: 2026-12-01\n    remind: [7]\n    severity: high\nitems: []\n",
            encoding="utf-8",
        )
        _, scan = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertTrue(any("duplicate key" in e for e in scan.errors), scan.errors)

        # Ensure broken YAML reports file:line:col without echoing secret/raw line text
        (root / "system/dates.yml").write_text("items: [SUPER_SECRET_RAW_LINE_XYZ\n", encoding="utf-8")
        _, scan2 = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertTrue(scan2.errors)
        self.assertFalse(any("SUPER_SECRET_RAW_LINE_XYZ" in e for e in scan2.errors), scan2.errors)

    def test_typo_deadine_rejected_with_hint(self) -> None:
        root = make_synthetic_repo(self)
        (root / "decisions/2026-11-23-offer.md").write_text(
            "---\nlevel: L2\nstatus: open\ncreated: 2026-11-23\ndeadine: 2026-11-30\n---\n# draft\n",
            encoding="utf-8",
        )
        _, scan = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertTrue(any("deadine" in e and "deadline" in e for e in scan.errors), scan.errors)

    def test_id_with_space_and_decision_double_entry_rejected(self) -> None:
        root = make_synthetic_repo(self)
        (root / "system/dates.yml").write_text(
            "items:\n  - id: car insurance\n    title: 车险\n    kind: renewal\n    due: 2026-12-01\n    remind: [7]\n    severity: high\n",
            encoding="utf-8",
        )
        _, scan = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertTrue(any("id" in e for e in scan.errors), scan.errors)

        # Decision double entry in dates.yml rejected
        (root / "decisions/2026-11-23-offer.md").write_text(
            "---\nlevel: L2\nstatus: open\ncreated: 2026-11-23\ndeadline: 2026-12-01\n---\n",
            encoding="utf-8",
        )
        (root / "system/dates.yml").write_text(
            "items:\n  - id: 2026-11-23-offer\n    title: Offer\n    kind: deadline\n    due: 2026-12-01\n    remind: [7]\n    severity: high\n",
            encoding="utf-8",
        )
        _, scan2 = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertTrue(any("decisions/*.md" in e for e in scan2.errors), scan2.errors)

    def test_v_null_and_ai_self_confirm_rejected(self) -> None:
        root = make_synthetic_repo(self)
        (root / "domains/wealth.md").write_text(
            "---\ndomain: wealth\nfacts:\n  x: {v: null, as_of: 2026-11-23, src: me}\n  y: {v: 100, as_of: 2026-11-23, src: ai, status: confirmed}\n---\n# w\n",
            encoding="utf-8",
        )
        _, scan = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertTrue(any("null is forbidden" in e for e in scan.errors), scan.errors)
        self.assertTrue(any("src='ai' must have status='pending'" in e for e in scan.errors), scan.errors)

        # Valid `?`, `none`, numeric `0`, and `ai+pending` pass cleanly and are distinguished
        (root / "domains/wealth.md").write_text(
            """---
domain: wealth
facts:
  unknown_f: {v: "?", as_of: null, src: null, ttl_days: 90}
  none_f: {v: none, as_of: 2026-11-23, src: me, ttl_days: 90}
  zero_f: {v: 0, as_of: 2026-11-23, src: me, ttl_days: 90}
  ai_f: {v: 500, as_of: 2026-11-10, src: ai, status: pending, ttl_days: 90}
---
# w
""",
            encoding="utf-8",
        )
        _, scan_ok = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertEqual(scan_ok.errors, [])
        w = next(s for s in scan_ok.domain_stats if s.domain == "wealth")
        self.assertEqual((w.unknown, w.confirmed, w.pending), (1, 2, 1))

    def test_decided_L3_missing_downside_and_open_draft_allowed(self) -> None:
        root = make_synthetic_repo(self)
        # Open L3 draft with minimal frontmatter MUST pass!
        (root / "decisions/2026-11-23-draft-l3.md").write_text(
            "---\nlevel: L3\nstatus: open\ncreated: 2026-11-23\ndeadline: 2026-12-10\n---\n",
            encoding="utf-8",
        )
        _, scan_draft = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertEqual(scan_draft.errors, [])

        # Decided L3 missing downside / range MUST fail!
        (root / "decisions/2026-11-23-draft-l3.md").write_text(
            "---\nlevel: L3\nstatus: decided\ncreated: 2026-11-23\nreview_due: 2027-02-01\noption0: 留任\nflip_if: 降薪\nprediction: {claim: 成功, p: 0.6}\n---\n",
            encoding="utf-8",
        )
        _, scan_bad = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertTrue(any("downside" in e for e in scan_bad.errors), scan_bad.errors)
        self.assertTrue(any("prediction.range" in e for e in scan_bad.errors), scan_bad.errors)

        # Complete decided L3 passes!
        (root / "decisions/2026-11-23-draft-l3.md").write_text(
            """---
level: L3
status: decided
created: 2026-11-23
review_due: 2027-02-01
option0: 留任年薪60万但成长停滞
downside:
  cash_low: 25万税后现金
  commitments: 房贷每月1.2万
  affected: 伴侣（同意：yes）
  exit_by: 2027-05-01
  reversal_cost: 3个月空窗找同类岗位
prediction:
  claim: 12个月后总包增长
  p: 0.65
  range: [55, 90]
flip_if: 书面竞业禁止同行业且无补偿
---
""",
            encoding="utf-8",
        )
        _, scan_good = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertEqual(scan_good.errors, [])

    def test_bad_tz_and_impossible_date_are_validation_errors(self) -> None:
        root = make_synthetic_repo(self)
        cfg = (root / "system/config.yml").read_text(encoding="utf-8").replace("Asia/Shanghai", "Mars/Olympus")
        (root / "system/config.yml").write_text(cfg, encoding="utf-8")
        (root / "system/dates.yml").write_text(
            'items:\n  - id: bad\n    title: Bad\n    kind: deadline\n    due: "2026-11-31"\n    remind: [7]\n    severity: high\n',
            encoding="utf-8",
        )
        _, scan = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertTrue(any("timezone" in e for e in scan.errors), scan.errors)
        self.assertTrue(any("2026-11-31" in e for e in scan.errors), scan.errors)

    def test_cooling_exact_72h_and_every_event_routed_exactly_once(self) -> None:
        root = make_synthetic_repo(self)
        # Even if user sets severity: low on cooling, cooling thaw upgrades to med so it NEVER vanishes!
        (root / "system/dates.yml").write_text(
            """items:
  - id: espresso
    title: 咖啡机
    kind: cooling
    added: 2026-11-20T21:30:00+08:00
    hours: 72
    price: 2999
    severity: low
    resolved: false
""",
            encoding="utf-8",
        )
        (root / "domains/career.md").write_text(
            "---\ndomain: career\nfacts:\n  role: {v: eng, as_of: 2026-01-01, src: me, ttl_days: 90}\n---\n# c\n",
            encoding="utf-8",
        )
        _, before = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 21, 29, tzinfo=BJ))
        self.assertFalse(any(e.rule == "cooling-thaw" for e in before.events))

        cfg, at_exact = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 21, 30, tzinfo=BJ))
        self.assertEqual(at_exact.errors, [])
        gh = FakeGitHub()
        plan = lifeos.sync_and_plan_delivery(gh, at_exact, cfg, now_local=dt.datetime(2026, 11, 23, 21, 30, tzinfo=BJ), tz=BJ)
        # Invariant: every scanned event routed to EITHER active Issue OR low_events (0 dropped!)
        routed_eids = {i["eid"] for i in plan["active_radar_items"]} | {e.eid for e in plan["low_events"]}
        self.assertEqual(routed_eids, {e.eid for e in at_exact.events})
        self.assertTrue(any("espresso" in i["eid"] for i in plan["active_radar_items"]))
        self.assertTrue(any(e.rule == "fact-stale" for e in plan["low_events"]))

    def test_same_day_double_entry_not_two_weeks_and_future_rejected(self) -> None:
        root = make_synthetic_repo(self)
        (root / "log/2026-11.md").write_text(
            "---\nmonth: 2026-11\n---\n## 2026-11-22 周检查1\n- weekly_energy: 1\n\n## 2026-11-22 周检查2\n- weekly_energy: 2\n",
            encoding="utf-8",
        )
        _, scan = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertEqual(scan.errors, [])
        self.assertEqual(scan.energy_status, "insufficient_data")
        self.assertFalse(any(e.rule == "energy-low" for e in scan.events))

        # Two consecutive ISO weeks <=2 triggers energy-low!
        (root / "log/2026-11.md").write_text(
            "---\nmonth: 2026-11\n---\n## 2026-11-15 周检查\n- weekly_energy: 2\n\n## 2026-11-22 周检查\n- weekly_energy: 1\n",
            encoding="utf-8",
        )
        _, scan_low = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertEqual(scan_low.energy_status, "low")
        self.assertTrue(any(e.rule == "energy-low" for e in scan_low.events))

        # Future observation rejected
        (root / "log/2026-11.md").write_text(
            "---\nmonth: 2026-11\n---\n## 2026-12-01\n- weekly_energy: 4\n",
            encoding="utf-8",
        )
        _, scan_fut = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        self.assertTrue(any("future observation date" in e for e in scan_fut.errors))

    def test_push_does_not_eat_and_send_fail_recover_and_same_day_no_resend(self) -> None:
        root = make_synthetic_repo(self)
        (root / "system/dates.yml").write_text(
            "items:\n  - id: tax\n    title: 税务申报\n    kind: deadline\n    due: 2026-11-24\n    remind: [7, 1]\n    severity: high\n",
            encoding="utf-8",
        )
        now = dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ)
        gh = FakeGitHub()

        # Step 1: `push` to main runs `build-out` (read-only) -> 0 Issues created!
        tmp_out = Path(tempfile.mkdtemp(prefix="lifeos_out_"))
        self.addCleanup(lambda: shutil.rmtree(tmp_out, ignore_errors=True))
        rc = lifeos.main(["build-out", "--root", str(root), "--out-dir", str(tmp_out), "--now", now.isoformat()])
        self.assertEqual(rc, 0)
        self.assertEqual(len(gh.issues), 0)

        # Step 2: cron `prepare` creates locked Issue with sent=[] & schedules delivery
        cfg, scan = lifeos.scan_repo(root, now)
        plan1 = lifeos.sync_and_plan_delivery(gh, scan, cfg, now_local=now, tz=BJ)
        self.assertTrue(plan1["should_send"])
        self.assertEqual(len(plan1["issue_patches"]), 1)
        iss_num = plan1["issue_patches"][0]["issue_number"]
        self.assertIn(iss_num, gh.locked)
        # Simulate ServerChan failure -> do NOT apply_patches!
        # Step 3: Retry next run -> MUST recover and still want to send!
        plan_retry = lifeos.sync_and_plan_delivery(gh, scan, cfg, now_local=now + dt.timedelta(minutes=10), tz=BJ)
        self.assertTrue(plan_retry["should_send"])
        self.assertEqual(len(plan_retry["issue_patches"]), 1)
        self.assertEqual(len([i for i in gh.issues.values() if "lifeos-event" in {l["name"] for l in i["labels"]}]), 1)

        # Now delivery succeeds -> apply_patches!
        gh.apply_patches(plan_retry)

        # Step 4: Same-day rerun -> 0 unsent, no resend!
        plan_same_day = lifeos.sync_and_plan_delivery(gh, scan, cfg, now_local=now + dt.timedelta(hours=2), tz=BJ)
        self.assertFalse(plan_same_day["should_send"])
        self.assertEqual(plan_same_day["issue_patches"], [])

    def test_T7_to_T1_same_issue_snooze_and_escalation_and_closed_not_in_radar(self) -> None:
        root = make_synthetic_repo(self)
        (root / "system/dates.yml").write_text(
            "items:\n  - id: car-ins\n    title: 车险续保\n    kind: renewal\n    due: 2026-12-01\n    remind: [7, 1]\n    severity: high\n",
            encoding="utf-8",
        )
        gh = FakeGitHub()

        # Nov 24 (T7): creates 1 Issue and delivers T7
        t7_now = dt.datetime(2026, 11, 24, 8, 30, tzinfo=BJ)
        cfg, s7 = lifeos.scan_repo(root, t7_now)
        p7 = lifeos.sync_and_plan_delivery(gh, s7, cfg, now_local=t7_now, tz=BJ)
        self.assertTrue(p7["should_send"])
        iss_num = p7["issue_patches"][0]["issue_number"]
        gh.apply_patches(p7)

        # Stranger comments /snooze 10d -> MUST be ignored; Owner comments /snooze 2d -> active
        gh.add_comment(iss_num, "evil_stranger", "/snooze 10d", "2026-11-24T01:00:00Z")
        gh.add_comment(iss_num, "teddy88teddy", "/snooze 2d", "2026-11-24T02:00:00Z")  # until Nov 26 10:00 BJ

        # Nov 25: snoozed -> no push, appears in snoozed_radar_items (NOT active!)
        nov25 = dt.datetime(2026, 11, 25, 8, 30, tzinfo=BJ)
        _, s25 = lifeos.scan_repo(root, nov25)
        p25 = lifeos.sync_and_plan_delivery(gh, s25, cfg, now_local=nov25, tz=BJ)
        self.assertFalse(p25["should_send"])
        self.assertEqual(len(p25["snoozed_radar_items"]), 1)
        self.assertEqual(len(p25["active_radar_items"]), 0)

        # Nov 27: snooze expired -> resurfaces once; after delivery, last_sent advances so old snooze is obsolete!
        nov27 = dt.datetime(2026, 11, 27, 8, 30, tzinfo=BJ)
        _, s27 = lifeos.scan_repo(root, nov27)
        p27 = lifeos.sync_and_plan_delivery(gh, s27, cfg, now_local=nov27, tz=BJ)
        self.assertTrue(p27["should_send"])
        self.assertEqual(p27["issue_patches"][0]["issue_number"], iss_num)
        gh.apply_patches(p27)

        # Nov 30 (T1, 72h after Nov 27): SAME Issue updated to [T1], new window fires even if owner snoozed on Nov 29!
        gh.add_comment(iss_num, "teddy88teddy", "/snooze 5d", "2026-11-29T02:00:00Z")
        nov30 = dt.datetime(2026, 11, 30, 8, 30, tzinfo=BJ)
        _, s30 = lifeos.scan_repo(root, nov30)
        p30 = lifeos.sync_and_plan_delivery(gh, s30, cfg, now_local=nov30, tz=BJ)
        self.assertTrue(p30["should_send"])
        self.assertEqual(len([i for i in gh.issues.values() if "lifeos-event" in {l["name"] for l in i["labels"]}]), 1)
        self.assertIn("[T1]", gh.issues[iss_num]["title"])
        gh.apply_patches(p30)

        # Close Issue -> MUST disappear from radar & context_pack!
        gh.issues[iss_num]["state"] = "closed"
        p_closed = lifeos.sync_and_plan_delivery(gh, s30, cfg, now_local=nov30, tz=BJ)
        self.assertEqual(p_closed["active_radar_items"], [])
        self.assertFalse(p_closed["should_send"])

    def test_med_to_high_review_renotifies_and_72h_escalation_once(self) -> None:
        root = make_synthetic_repo(self)
        (root / "decisions/2026-11-01-job.md").write_text(
            """---
level: L2
status: decided
created: 2026-11-01
review_due: 2026-11-26
option0: 留任
downside: {cash_low: 10万, commitments: 房租, affected: 本人（同意：yes）, exit_by: 2027-01-01, reversal_cost: 1个月}
prediction: {claim: 涨薪, p: 0.7}
flip_if: 业务裁撤
---
""",
            encoding="utf-8",
        )
        gh = FakeGitHub()
        # Nov 24 (T3, med) -> sends T3
        nov24 = dt.datetime(2026, 11, 24, 8, 30, tzinfo=BJ)
        cfg, s24 = lifeos.scan_repo(root, nov24)
        p24 = lifeos.sync_and_plan_delivery(gh, s24, cfg, now_local=nov24, tz=BJ)
        self.assertTrue(p24["should_send"])
        gh.apply_patches(p24)

        # Nov 26 (DUE, upgraded med -> high on review day, only 48h later!) -> MUST re-notify!
        nov26 = dt.datetime(2026, 11, 26, 8, 30, tzinfo=BJ)
        _, s26 = lifeos.scan_repo(root, nov26)
        p26 = lifeos.sync_and_plan_delivery(gh, s26, cfg, now_local=nov26, tz=BJ)
        self.assertTrue(p26["should_send"])
        gh.apply_patches(p26)

        # Nov 29 (+72h unclosed high) -> escalates ONCE!
        nov29 = dt.datetime(2026, 11, 29, 8, 30, tzinfo=BJ)
        _, s29 = lifeos.scan_repo(root, nov29)
        p29 = lifeos.sync_and_plan_delivery(gh, s29, cfg, now_local=nov29, tz=BJ)
        self.assertTrue(p29["should_send"])
        self.assertTrue(p29["issue_patches"][0]["add_escalated_label"])
        gh.apply_patches(p29)

        # Dec 2 (another +72h) -> already escalated -> NO second escalation!
        dec02 = dt.datetime(2026, 12, 2, 8, 30, tzinfo=BJ)
        _, s02 = lifeos.scan_repo(root, dec02)
        p02 = lifeos.sync_and_plan_delivery(gh, s02, cfg, now_local=dec02, tz=BJ)
        self.assertFalse(p02["should_send"])

    def test_pagination_250_issues_and_bot_only_event_trust(self) -> None:
        # Test real Link header pagination logic in GitHubClient
        pages = {
            "https://api.github.com/repos/teddy88teddy/Life/issues?labels=lifeos-event&state=all&per_page=100": (
                [{"number": i} for i in range(1, 101)],
                {"link": '<https://api.github.com/repos/teddy88teddy/Life/issues?page=2>; rel="next"'},
            ),
            "https://api.github.com/repos/teddy88teddy/Life/issues?page=2": (
                [{"number": i} for i in range(101, 201)],
                {"link": '<https://api.github.com/repos/teddy88teddy/Life/issues?page=3>; rel="next"'},
            ),
            "https://api.github.com/repos/teddy88teddy/Life/issues?page=3": (
                [{"number": i} for i in range(201, 251)],
                {},
            ),
        }
        client = lifeos.GitHubClient("teddy88teddy/Life", "fake")
        client._req_with_headers = lambda _m, url, _p=None: pages[  # type: ignore[method-assign]
            url if url.startswith("https://") else f"{client.base}{url}"
        ]
        all_items = client.list_issues("lifeos-event", state="all")
        self.assertEqual(len(all_items), 250)

        # Verify untrusted stranger event issue comments are NEVER fetched!
        root = make_synthetic_repo(self)
        gh = FakeGitHub()
        stranger_iss = gh.add_raw_issue(
            author="random_stranger",
            title="Fake event",
            body='<!-- lifeos:{"eid":"renewal:x:2026-12-01","sent":[],"last_sent":null,"esc":false} -->',
            labels=["lifeos-event"],
        )
        gh.add_comment(stranger_iss["number"], "random_stranger", "spam", "2026-11-23T00:00:00Z")
        cfg, scan = lifeos.scan_repo(root, dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ))
        lifeos.sync_and_plan_delivery(gh, scan, cfg, now_local=dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ), tz=BJ)
        self.assertNotIn(stranger_iss["number"], gh.comments_fetched_for)

    def test_weekly_once_per_isoweek_not_ok_with_open_high_and_capture_due(self) -> None:
        root = make_synthetic_repo(self)
        (root / "system/dates.yml").write_text(
            "items:\n  - id: old-ins\n    title: 旧车险\n    kind: renewal\n    due: 2026-11-20\n    remind: [7, 1]\n    severity: high\n",
            encoding="utf-8",
        )
        gh = FakeGitHub()
        # Closed past-date issue -> unrolled date hygiene alert!
        gh.add_raw_issue(
            author="github-actions[bot]",
            title="[T0] 旧车险",
            body='<!-- lifeos:{"eid":"renewal:old-ins:2026-11-20","sent":["T0"],"last_sent":"2026-11-20T08:30+08:00","esc":false} -->',
            labels=["lifeos-event"],
            state="closed",
        )
        # Owner capture with Thursday due date + stranger capture ignored
        gh.add_raw_issue(
            author="teddy88teddy",
            title="[capture] 周四交房租",
            body="### 类别\n\n临期硬期限\n\n### 截止日期（可选）\n\n2026-11-30\n\n### 一句话内容\n\n交房租",
            labels=["capture"],
        )
        gh.add_raw_issue(
            author="evil_stranger",
            title="[capture] 注入",
            body="### 截止日期（可选）\n\n2026-11-29\n",
            labels=["capture"],
        )

        # Sunday 2026-11-29 (T-1 for owner capture!)
        sunday = dt.datetime(2026, 11, 29, 8, 30, tzinfo=BJ)
        cfg, scan = lifeos.scan_repo(root, sunday)
        p1 = lifeos.sync_and_plan_delivery(gh, scan, cfg, now_local=sunday, tz=BJ)
        self.assertTrue(p1["should_send"])
        self.assertNotIn("一切正常", p1["title"])
        self.assertIn("old-ins", "\n".join(p1["unrolled_closed_dates"]))
        self.assertEqual(len(p1["owner_captures"]), 1)
        self.assertTrue(any(i["eid"].startswith("capture-due:") for i in p1["active_radar_items"]))
        gh.apply_patches(p1)

        # Same Sunday second run -> NO duplicate weekly heartbeat!
        p2 = lifeos.sync_and_plan_delivery(gh, scan, cfg, now_local=sunday + dt.timedelta(hours=3), tz=BJ)
        self.assertFalse(p2["should_send"])

    def test_serverchan_strict_code_check_and_title_density(self) -> None:
        # code == 0 succeeds
        ok_opener = lambda req, timeout=20: _FakeHTTPResp(200, '{"code":0,"data":{"pushid":"SECRET"}}')
        self.assertEqual(lifeos.send_serverchan("SCT123", "t", "d", opener=ok_opener), 0)

        # code != 0 with HTTP 200 MUST raise DeliveryError
        bad_code = lambda req, timeout=20: _FakeHTTPResp(200, '{"code":40001,"message":"invalid"}')
        with self.assertRaises(lifeos.DeliveryError):
            lifeos.send_serverchan("SCT123", "t", "d", opener=bad_code)

        # Empty body MUST raise DeliveryError
        empty = lambda req, timeout=20: _FakeHTTPResp(200, "")
        with self.assertRaises(lifeos.DeliveryError):
            lifeos.send_serverchan("SCT123", "t", "d", opener=empty)

        # Non-JSON MUST raise DeliveryError
        html = lambda req, timeout=20: _FakeHTTPResp(200, "<html>502</html>")
        with self.assertRaises(lifeos.DeliveryError):
            lifeos.send_serverchan("SCT123", "t", "d", opener=html)

        # Title carries top item + window + remaining count, <=32 chars
        title = lifeos.format_serverchan_title(
            [
                {"severity": "med", "window": "THAW", "title": "咖啡机"},
                {"severity": "high", "window": "T1", "title": "车险续保（剩 1 天）"},
            ]
        )
        self.assertLessEqual(len(title), 32)
        self.assertIn("车险续保", title)
        self.assertIn("T1", title)
        self.assertIn("另1项", title)

    def test_ics_all_alarms_utf8_75_octet_folding_and_stable_uid(self) -> None:
        item = lifeos.CalendarItem(
            uid="lifeos-renewal-car-ins@teddy88teddy",
            title="[LifeOS] 这是一个非常非常非常长的中文车险续保与综合商业险比价提醒事项标题测试",
            due=dt.date(2026, 12, 15),
            description="备注：" + "中文内容" * 20,
            remind_days=[30, 7, 1],
        )
        ics = lifeos.build_calendar_ics([item], dt.datetime(2026, 11, 23, 0, 30, tzinfo=dt.timezone.utc))
        # All 3 alarms present (T-30, T-7, AND T-1!)
        self.assertIn("TRIGGER:-P30D\r\n", ics)
        self.assertIn("TRIGGER:-P7D\r\n", ics)
        self.assertIn("TRIGGER:-P1D\r\n", ics)
        # Stable UID without due date
        self.assertIn("UID:lifeos-renewal-car-ins@teddy88teddy\r\n", ics)
        # Every physical line <= 75 UTF-8 octets & decodes cleanly
        for raw_line in ics.split("\r\n"):
            self.assertLessEqual(len(raw_line.encode("utf-8")), 75, f"Line exceeded 75 octets: {raw_line!r}")

    def test_context_pack_priority_budget_safety_blindspots_and_overflow_error(self) -> None:
        root = make_synthetic_repo(self)
        now = dt.datetime(2026, 11, 23, 8, 30, tzinfo=BJ)
        cfg, scan = lifeos.scan_repo(root, now)
        self.assertEqual(scan.errors, [])

        pack = lifeos.build_context_pack_md(root, scan, [], 9000, now)
        self.assertIn("now.md=2026-11-23", pack)
        self.assertIn("health.allergies: 未知", pack)  # Safety blindspot preserved!
        self.assertIn("8 天", pack)

        # Mandatory sections exceeding budget MUST fail loudly rather than silently truncating!
        with self.assertRaises(ValueError):
            lifeos.build_context_pack_md(root, scan, [], 100, now)

        # And scan_repo catches budget overflow as a validation error when config max_chars is too small
        cfg_txt = (root / "system/config.yml").read_text(encoding="utf-8").replace("9000", "100")
        (root / "system/config.yml").write_text(cfg_txt, encoding="utf-8")
        _, scan_ov = lifeos.scan_repo(root, now)
        self.assertTrue(any("context_pack mandatory sections" in e for e in scan_ov.errors), scan_ov.errors)


if __name__ == "__main__":
    unittest.main()
