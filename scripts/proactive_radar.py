#!/usr/bin/env python3
"""Proactive Radar generator for Teddy's Life OS.

Deterministically scans the repository and generates `00_radar.md` so any AI
session (or weekly GitHub Actions check) immediately surfaces:
  - Active triggers (`triggered` / `warning`) in 01_north_star/triggers.md
  - Approaching (<= 7d) or overdue decision deadlines & follow-up reviews
  - Cooling-off pool items ready to thaw in 02_domains/wealth/strategy.md
  - Weekly alignment drift (stale 00_now.md or recent_pulse.md)
  - Memory inbox items ready for 3x promotion
  - Domain readiness heatmap & the single highest-leverage next data gap

Zero external dependencies (Python 3 stdlib only).
Usage:
  python3 scripts/proactive_radar.py           # refreshes 00_radar.md
  python3 scripts/proactive_radar.py --stdout  # prints report without writing
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOMAINS_ORDERED = [
    ("north_star", "顶层方向盘（愿景/红线/说明书）", "01_north_star"),
    ("career", "职业生涯发展", "02_domains/career"),
    ("wealth", "财富与消费决策", "02_domains/wealth"),
    ("health", "健康与精力管理", "02_domains/health"),
    ("relationships", "亲密关系与人脉", "02_domains/relationships"),
    ("growth", "认知成长与AI杠杆", "02_domains/growth"),
    ("lifestyle", "日常生活与微决策", "02_domains/lifestyle"),
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
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        return None


def scan_triggers() -> list[str]:
    path = ROOT / "01_north_star/triggers.md"
    if not path.exists():
        return []
    alerts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| **TR-"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 4:
            code, cond, action, status = cells[0], cells[1], cells[2], cells[3].lower()
            if "triggered" in status or "warning" in status:
                alerts.append(f"{code} [{status}]：{cond} → 建议：{action}")
    return alerts


def scan_decisions(today: dt.date) -> tuple[list[str], list[str]]:
    urgent_deadlines: list[str] = []
    due_reviews: list[str] = []
    decisions_root = ROOT / "03_decisions"
    if not decisions_root.exists():
        return urgent_deadlines, due_reviews

    for path in sorted(decisions_root.rglob("*.md")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("03_decisions/templates/") or path.name == "README.md":
            continue
        fm = parse_frontmatter(path.read_text(encoding="utf-8"))
        status = fm.get("status", "")
        deadline = parse_date(fm.get("deadline", ""))
        review_due = parse_date(fm.get("review_due", ""))

        if status == "active" and deadline:
            delta = (deadline - today).days
            if delta <= 7:
                tag = f"已逾期 {-delta} 天" if delta < 0 else f"剩余 {delta} 天"
                urgent_deadlines.append(f"`{rel}`（截止 {deadline}，{tag}）")

        if status == "decided" and review_due:
            delta = (review_due - today).days
            if delta <= 7:
                tag = f"已到期 {-delta} 天" if delta <= 0 else f"{delta} 天后到期"
                due_reviews.append(f"`{rel}`（复盘日 {review_due}，{tag}）")

    return urgent_deadlines, due_reviews


def scan_cooling_pool(today: dt.date) -> list[str]:
    path = ROOT / "02_domains/wealth/strategy.md"
    if not path.exists():
        return []
    ready: list[str] = []
    in_pool = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## 4. 72 小时冷静池"):
            in_pool = True
            continue
        if in_pool and line.startswith("## "):
            break
        if in_pool and line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 6:
                item, price, thaw_str, status = cells[1], cells[2], cells[4], cells[5]
                thaw_date = parse_date(thaw_str)
                if thaw_date and thaw_date <= today and "冷静" in status:
                    ready.append(f"{item}（{price}，解冻日 {thaw_date}）")
    return ready


def scan_drift(today: dt.date) -> list[str]:
    drifts: list[str] = []
    for rel, max_days, label in [
        ("00_now.md", 7, "实时作战室 `00_now.md` 超过 7 天未校准，建议做一次 5 分钟周对齐"),
        ("02_domains/lifestyle/recent_pulse.md", 10, "生活脉冲 `recent_pulse.md` 超过 10 天无记录，可能失去漂移感知"),
    ]:
        p = ROOT / rel
        if p.exists():
            fm = parse_frontmatter(p.read_text(encoding="utf-8"))
            updated = parse_date(fm.get("last_updated", ""))
            if updated and (today - updated).days > max_days:
                drifts.append(f"{label}（上次更新 {updated}）")
    return drifts


def scan_inbox() -> list[str]:
    path = ROOT / "04_cadence/inbox.md"
    if not path.exists():
        return []
    promotions: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("- ["):
            m = re.search(r"计数:\s*(\d+)", line)
            if m and int(m.group(1)) >= 3:
                promotions.append(line.lstrip("- ").strip())
    return promotions


def scan_domain_coverage() -> tuple[list[tuple[str, str, int, int]], str]:
    rows: list[tuple[str, str, int, int]] = []
    next_gap = "全部核心域已激活，可专注周期复盘与决策复利。"
    found_gap = False

    for _, label, rel_dir in DOMAINS_ORDERED:
        folder = ROOT / rel_dir
        if not folder.exists():
            continue
        files = sorted(p for p in folder.glob("*.md") if p.name != "README.md")
        active_cnt = 0
        for f in files:
            fm = parse_frontmatter(f.read_text(encoding="utf-8"))
            if fm.get("status") != "pending_interview":
                active_cnt += 1
        total = len(files)
        rows.append((label, rel_dir, active_cnt, total))
        if not found_gap and active_cnt < total:
            next_gap = f"优先补齐 **{label}**（`{rel_dir}/`，当前 {active_cnt}/{total} 已激活）"
            found_gap = True

    return rows, next_gap


def render_radar(today: dt.date) -> str:
    triggers = scan_triggers()
    deadlines, reviews = scan_decisions(today)
    cooling = scan_cooling_pool(today)
    drifts = scan_drift(today)
    promotions = scan_inbox()
    coverage_rows, next_gap = scan_domain_coverage()

    urgent_items: list[str] = []
    urgent_items.extend(f"- **[触发器亮灯]** {t}" for t in triggers)
    urgent_items.extend(f"- **[决策截止]** {d}" for d in deadlines)
    urgent_items.extend(f"- **[到期复盘]** {r}" for r in reviews)
    urgent_items.extend(f"- **[冷静池解冻]** {c}" for c in cooling)
    urgent_items.extend(f"- **[目标漂移提醒]** {d}" for d in drifts)
    urgent_items.extend(f"- **[规律待晋升]** {p}" for p in promotions)

    lines = [
        "---",
        "layer: hot",
        "generated_by: scripts/proactive_radar.py",
        f"last_updated: {today.isoformat()}",
        "status: active",
        "---",
        "",
        "# 00_radar.md — 主动预警雷达（Proactive Radar）",
        "",
        "> **用途**：由 `scripts/proactive_radar.py` 与 AI 共同维护。每次对话开场 AI 自动扫一眼本文件，决定是否主动发出简报提醒。",
        "",
        "## 1. 需主动介入的紧急 / 到期事项",
        "",
    ]

    if urgent_items:
        lines.extend(urgent_items)
    else:
        lines.append("- 当前无逾期决策、无触发器亮灯、无冷静池解冻项。")

    lines.extend([
        "",
        "## 2. 六大人生域就绪度热力图（Domain Readiness）",
        "",
        "| 领域 | 目录 | 已激活 / 总文件 | 状态 |",
        "| :--- | :--- | :--- | :--- |",
    ])

    for label, rel_dir, active_cnt, total in coverage_rows:
        if total == 0:
            badge = "空"
        elif active_cnt == total:
            badge = "就绪"
        elif active_cnt > 0:
            badge = "部分就绪"
        else:
            badge = "待建档"
        lines.append(f"| {label} | `{rel_dir}/` | {active_cnt}/{total} | {badge} |")

    lines.extend([
        "",
        "## 3. AI 下一步主动建议（最高杠杆盲区）",
        "",
        f"- {next_gap}",
        "- 日常任意对话中，AI 可在不打扰主线的前提下，末尾最多顺手抛 **1 个** 高价值微问题逐步点亮以上领域。",
        "",
    ])

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Teddy's Proactive Life Radar")
    parser.add_argument("--stdout", action="store_true", help="Print radar markdown to stdout instead of writing 00_radar.md")
    args = parser.parse_args()

    today = dt.date.today()
    content = render_radar(today)

    if args.stdout:
        print(content)
    else:
        target = ROOT / "00_radar.md"
        target.write_text(content, encoding="utf-8")
        print(f"Updated {target.relative_to(ROOT)} ({len(content.splitlines())} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
