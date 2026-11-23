#!/usr/bin/env python3
"""Life OS v3 — Single Strict Core Engine.

Handles:
  1. Strict schema & date validation (zero silent failures)
  2. Deterministic event scanning (dates, exact-hour cooling-off, decisions, facts TTL, weekly energy)
  3. Owner-only GitHub Issues state machine (open / closed / not_planned / /snooze / escalate)
  4. ServerChan (Turbo & SC3) WeChat merged daily digest + Sunday heartbeat + failure alerts
  5. Generation of `out` branch artifacts: radar.md, calendar.ics, context_pack.md (for Gemini)
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import yaml
except ModuleNotFoundError:
    print(
        "ERROR: PyYAML is required. Run: pip install -r scripts/requirements.txt",
        file=sys.stderr,
    )
    sys.exit(2)

ROOT_DEFAULT = Path(__file__).resolve().parent.parent

HOT_LINE_LIMITS = {
    "AGENTS.md": 80,
    "now.md": 30,
    "domains/daily.md": 60,
}

SECRET_PATTERNS = [
    ("ServerChan Turbo Key", re.compile(r"\bSCT\d+[A-Za-z0-9]{15,}\b")),
    ("ServerChan3 Key", re.compile(r"\bsctp\d+t[A-Za-z0-9]{10,}\b")),
    ("OpenAI/API Key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub PAT", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("Private Key Header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

SEVERITY_RANK = {"low": 1, "med": 2, "high": 3}
SEVERITY_UP = {"low": "med", "med": "high", "high": "high"}
VALID_LEVELS = {"L1", "L2", "L3"}
VALID_DECISION_STATUS = {"open", "decided", "reviewed", "dropped"}
VALID_DATE_KINDS = {"renewal", "deadline", "cooling", "custom"}
VALID_FACT_SOURCES = {"me", "ai", "doc"}
BOT_AUTHORS = {"github-actions[bot]", "app/github-actions"}
EID_RE = re.compile(r"<!--\s*eid:\s*([^\s>]+)\s*-->")
SNOOZE_RE = re.compile(r"^\s*/snooze\s+(\d+d|\d{4}-\d{2}-\d{2})\b", re.IGNORECASE | re.MULTILINE)


@dataclasses.dataclass
class Event:
    event_id: str
    rule: str
    severity: str  # low | med | high
    title: str
    evidence: str
    action: str
    due_date: dt.date | None = None
    notify_daily: bool = True


@dataclasses.dataclass
class CalendarItem:
    uid: str
    title: str
    due: dt.date
    description: str
    remind_days: list[int]


@dataclasses.dataclass
class FactStat:
    domain: str
    confirmed: int = 0
    pending: int = 0
    stale: int = 0
    unknown: int = 0
    confirmed_items: list[tuple[str, str]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ScanResult:
    errors: list[str]
    events: list[Event]
    calendar_items: list[CalendarItem]
    domain_stats: list[FactStat]
    energy_status: str  # ok | low | insufficient_data
    energy_detail: str
    open_decisions: list[tuple[str, str, dt.date | None]]
    pending_ai_facts: list[str]
    stale_facts: list[str]


# ---------------------------------------------------------------------------
# Strict parsing helpers (NEVER swallow invalid dates or malformed YAML!)
# ---------------------------------------------------------------------------


def parse_strict_date(value: Any, ctx: str, errors: list[str], *, allow_none: bool = False) -> dt.date | None:
    if value is None:
        if allow_none:
            return None
        errors.append(f"{ctx}: required date is null/missing")
        return None
    if isinstance(value, bool):
        errors.append(f"{ctx}: invalid date (bool: {value!r})")
        return None
    if isinstance(value, dt.datetime):
        errors.append(f"{ctx}: expected YYYY-MM-DD date, got datetime {value.isoformat()}")
        return None
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            errors.append(f"{ctx}: invalid date format {value!r} (expected YYYY-MM-DD)")
            return None
        try:
            return dt.date.fromisoformat(s)
        except ValueError as exc:
            errors.append(f"{ctx}: impossible calendar date {value!r} ({exc})")
            return None
    errors.append(f"{ctx}: invalid date type {type(value).__name__} ({value!r})")
    return None


def parse_strict_datetime(value: Any, ctx: str, errors: list[str]) -> dt.datetime | None:
    if value is None or isinstance(value, bool):
        errors.append(f"{ctx}: required timezone-aware ISO datetime is missing/invalid ({value!r})")
        return None
    parsed: dt.datetime | None = None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        errors.append(f"{ctx}: expected ISO datetime with timezone, got date-only {value.isoformat()}")
        return None
    elif isinstance(value, str):
        s = value.strip()
        if "T" not in s and " " not in s:
            errors.append(f"{ctx}: expected ISO datetime with timezone, got {value!r}")
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(s)
        except ValueError as exc:
            errors.append(f"{ctx}: invalid ISO datetime {value!r} ({exc})")
            return None
    else:
        errors.append(f"{ctx}: invalid datetime type {type(value).__name__}")
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{ctx}: datetime must include timezone offset (e.g. +08:00), got {value!r}")
        return None
    return parsed


def load_yaml_file(path: Path, errors: list[str]) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
        return yaml.safe_load(raw)
    except Exception as exc:
        errors.append(f"{path.as_posix()}: YAML parse error ({exc})")
        return None


def parse_frontmatter(path: Path, rel: str, errors: list[str], *, required: bool = True) -> tuple[dict[str, Any] | None, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        if required:
            errors.append(f"{rel}: missing required YAML frontmatter")
        return None, text
    match = re.search(r"\n---(?:\n|$)", text[4:])
    if not match:
        errors.append(f"{rel}: unterminated YAML frontmatter")
        return None, text
    fm_raw = text[4 : 4 + match.start()]
    body = text[4 + match.end() :]
    try:
        data = yaml.safe_load(fm_raw)
    except Exception as exc:
        errors.append(f"{rel}: invalid frontmatter YAML ({exc})")
        return None, body
    if not isinstance(data, dict):
        errors.append(f"{rel}: frontmatter must be a YAML mapping")
        return None, body
    return data, body


def load_config(root: Path, errors: list[str]) -> dict[str, Any]:
    cfg_path = root / "system/config.yml"
    if not cfg_path.exists():
        errors.append("system/config.yml: file missing")
        return {"tz": "Asia/Shanghai", "owner": "teddy88teddy", "repo": "teddy88teddy/Life"}
    data = load_yaml_file(cfg_path, errors)
    if not isinstance(data, dict):
        errors.append("system/config.yml: must be a YAML mapping")
        return {"tz": "Asia/Shanghai", "owner": "teddy88teddy", "repo": "teddy88teddy/Life"}

    tz_name = data.get("tz")
    if not isinstance(tz_name, str) or not tz_name:
        errors.append("system/config.yml: missing valid 'tz'")
    else:
        try:
            ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            errors.append(f"system/config.yml: unknown timezone {tz_name!r}")

    if not isinstance(data.get("owner"), str) or not data["owner"].strip():
        errors.append("system/config.yml: missing valid 'owner'")
    if not isinstance(data.get("repo"), str) or "/" not in data["repo"]:
        errors.append("system/config.yml: missing valid 'repo' (owner/name)")
    return data


# ---------------------------------------------------------------------------
# Core validation + deterministic scanning (single shared kernel)
# ---------------------------------------------------------------------------


def scan_repo(root: Path, now_dt: dt.datetime | None = None) -> tuple[dict[str, Any], ScanResult]:
    errors: list[str] = []
    events: list[Event] = []
    calendar_items: list[CalendarItem] = []
    domain_stats: list[FactStat] = []
    open_decisions: list[tuple[str, str, dt.date | None]] = []
    pending_ai_facts: list[str] = []
    stale_facts: list[str] = []

    config = load_config(root, errors)
    tz_name = config.get("tz", "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")

    if now_dt is None:
        now_local = dt.datetime.now(tz)
    else:
        now_local = now_dt.astimezone(tz) if now_dt.tzinfo else now_dt.replace(tzinfo=tz)
    today = now_local.date()

    # 1. Hot-layer line caps
    for rel, max_lines in HOT_LINE_LIMITS.items():
        p = root / rel
        if not p.exists():
            errors.append(f"{rel}: required file missing")
            continue
        lines = len(p.read_text(encoding="utf-8").splitlines())
        if lines > max_lines:
            errors.append(f"{rel}: {lines} lines exceeds limit ({max_lines})")

    # 2. Validate now.md frontmatter
    now_path = root / "now.md"
    if now_path.exists():
        fm, _ = parse_frontmatter(now_path, "now.md", errors, required=True)
        if fm is not None:
            parse_strict_date(fm.get("as_of"), "now.md:as_of", errors)

    # 3. Validate compass.md & timed exceptions
    compass_path = root / "compass.md"
    if not compass_path.exists():
        errors.append("compass.md: required file missing")
    else:
        fm, body = parse_frontmatter(compass_path, "compass.md", errors, required=True)
        if fm is not None:
            parse_strict_date(fm.get("as_of"), "compass.md:as_of", errors)
        for m in re.finditer(r"```yaml\n(.*?)```", body, flags=re.DOTALL):
            try:
                block = yaml.safe_load(m.group(1))
            except Exception as exc:
                errors.append(f"compass.md: invalid yaml code block ({exc})")
                continue
            if isinstance(block, dict) and "exceptions" in block:
                excs = block["exceptions"]
                if not isinstance(excs, list):
                    errors.append("compass.md: 'exceptions' must be a list")
                    continue
                for idx, item in enumerate(excs):
                    ctx = f"compass.md:exceptions[{idx}]"
                    if not isinstance(item, dict):
                        errors.append(f"{ctx}: must be a mapping")
                        continue
                    eid = item.get("id")
                    scope = item.get("scope")
                    if not isinstance(eid, str) or not eid.strip():
                        errors.append(f"{ctx}: missing 'id'")
                    if not isinstance(scope, str) or not scope.strip():
                        errors.append(f"{ctx}: missing 'scope'")
                    exp = parse_strict_date(item.get("expires"), f"{ctx}.expires", errors)
                    if exp and isinstance(eid, str):
                        days_left = (exp - today).days
                        if days_left <= 3:
                            tag = f"已过期 {-days_left} 天" if days_left < 0 else f"剩余 {days_left} 天"
                            events.append(
                                Event(
                                    event_id=f"exception-expiry:{eid}:{exp.isoformat()}",
                                    rule="exception-expiry",
                                    severity="high" if days_left <= 0 else "med",
                                    title=f"限时例外到期：{eid}",
                                    evidence=f"compass.md 限时例外「{scope}」到期日 {exp.isoformat()}（{tag}）",
                                    action="确认恢复常规红线，或显式延长到期日",
                                    due_date=exp,
                                    notify_daily=True,
                                )
                            )

    # 4. Validate system/rules.yml
    rules_path = root / "system/rules.yml"
    rules_cfg: dict[str, dict[str, Any]] = {}
    if not rules_path.exists():
        errors.append("system/rules.yml: file missing")
    else:
        rdata = load_yaml_file(rules_path, errors)
        if not isinstance(rdata, dict) or not isinstance(rdata.get("rules"), list):
            errors.append("system/rules.yml: must contain 'rules' list")
        else:
            for idx, r in enumerate(rdata["rules"]):
                if not isinstance(r, dict) or not isinstance(r.get("id"), str):
                    errors.append(f"system/rules.yml:rules[{idx}]: missing string 'id'")
                else:
                    rules_cfg[r["id"]] = r

    # 5. Validate & scan system/dates.yml (hard deadlines + exact-hour cooling-off)
    dates_path = root / "system/dates.yml"
    if not dates_path.exists():
        errors.append("system/dates.yml: file missing")
    else:
        ddata = load_yaml_file(dates_path, errors)
        if not isinstance(ddata, dict) or not isinstance(ddata.get("items"), list):
            errors.append("system/dates.yml: must contain 'items' list")
        else:
            seen_ids: set[str] = set()
            for idx, item in enumerate(ddata["items"]):
                ctx = f"system/dates.yml:items[{idx}]"
                if not isinstance(item, dict):
                    errors.append(f"{ctx}: must be a mapping")
                    continue
                item_id = item.get("id")
                title = item.get("title") or item_id
                kind = item.get("kind")
                sev = item.get("severity", "med")
                if not isinstance(item_id, str) or not item_id.strip():
                    errors.append(f"{ctx}: missing non-empty string 'id'")
                    continue
                if item_id in seen_ids:
                    errors.append(f"{ctx}: duplicate id {item_id!r}")
                seen_ids.add(item_id)
                if kind not in VALID_DATE_KINDS:
                    errors.append(f"{ctx}: invalid kind {kind!r} (allowed: {sorted(VALID_DATE_KINDS)})")
                    continue
                if sev not in SEVERITY_RANK:
                    errors.append(f"{ctx}: invalid severity {sev!r}")
                    continue

                if kind == "cooling":
                    resolved = item.get("resolved", False)
                    if not isinstance(resolved, bool):
                        errors.append(f"{ctx}.resolved: must be bool")
                        continue
                    added_dt = parse_strict_datetime(item.get("added"), f"{ctx}.added", errors)
                    hours = item.get("hours", 72)
                    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
                        errors.append(f"{ctx}.hours: must be positive number, got {hours!r}")
                        continue
                    if added_dt and not resolved:
                        thaw_at = added_dt + dt.timedelta(hours=float(hours))
                        thaw_local = thaw_at.astimezone(tz)
                        if now_local >= thaw_local:
                            price = item.get("price", "?")
                            note = item.get("note", "")
                            events.append(
                                Event(
                                    event_id=f"cooling:{item_id}:{thaw_local.strftime('%Y-%m-%dT%H')}",
                                    rule="cooling-thaw",
                                    severity=sev,
                                    title=f"冷静期解冻：{title}",
                                    evidence=(
                                        f"已满 {hours:g}h（加入于 {added_dt.astimezone(tz).isoformat(timespec='minutes')}，"
                                        f"预估价 {price}）。{note}"
                                    ).strip(),
                                    action="仍确认需要则购买并在 dates.yml 标 resolved: true；已拔草则直接关闭 Issue",
                                    due_date=thaw_local.date(),
                                    notify_daily=True,
                                )
                            )
                else:
                    due = parse_strict_date(item.get("due"), f"{ctx}.due", errors)
                    remind = item.get("remind", [7, 1])
                    if not isinstance(remind, list) or not all(
                        isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in remind
                    ):
                        errors.append(f"{ctx}.remind: must be list of non-negative integers")
                        remind = [7, 1]
                    auto_renew = item.get("auto_renew", False)
                    if not isinstance(auto_renew, bool):
                        errors.append(f"{ctx}.auto_renew: must be bool")
                        auto_renew = False
                    eff_sev = SEVERITY_UP[sev] if auto_renew else sev
                    if due:
                        note = item.get("note", "")
                        calendar_items.append(
                            CalendarItem(
                                uid=f"lifeos-{kind}-{item_id}-{due.isoformat()}@teddy88teddy",
                                title=f"[LifeOS] {title}",
                                due=due,
                                description=f"类型: {kind} | 自动续费: {auto_renew} | {note}".strip(),
                                remind_days=sorted(set(remind), reverse=True),
                            )
                        )
                        days_left = (due - today).days
                        if days_left <= 0:
                            period = "T0" if days_left == 0 else "overdue"
                            tag = "今天到期" if days_left == 0 else f"已逾期 {-days_left} 天"
                            events.append(
                                Event(
                                    event_id=f"{kind}:{item_id}:{due.isoformat()}:{period}",
                                    rule="date-reminder",
                                    severity="high",
                                    title=f"期限告警：{title}（{tag}）",
                                    evidence=f"dates.yml 到期日 {due.isoformat()}（auto_renew={auto_renew}）。{note}".strip(),
                                    action="立即处理并更新 dates.yml 下一周期或移除",
                                    due_date=due,
                                    notify_daily=True,
                                )
                            )
                        else:
                            matching = [r for r in remind if days_left <= r]
                            if matching:
                                window = min(matching)
                                events.append(
                                    Event(
                                        event_id=f"{kind}:{item_id}:{due.isoformat()}:T{window}",
                                        rule="date-reminder",
                                        severity=eff_sev,
                                        title=f"期限提醒：{title}（剩 {days_left} 天）",
                                        evidence=f"dates.yml 到期日 {due.isoformat()}（触发窗口 T-{window}，auto_renew={auto_renew}）。{note}".strip(),
                                        action="提前确认续费/取消/材料准备，完成后关闭 Issue",
                                        due_date=due,
                                        notify_daily=True,
                                    )
                                )

    # 6. Validate & scan domains/*.md (strict ? / none / 0 / pending / ttl separation)
    domains_dir = root / "domains"
    if not domains_dir.exists():
        errors.append("domains/: directory missing")
    else:
        max_pending_days = int(rules_cfg.get("pending-ai-fact", {}).get("max_pending_days", 7))
        for path in sorted(domains_dir.glob("*.md")):
            rel = path.relative_to(root).as_posix()
            fm, _ = parse_frontmatter(path, rel, errors, required=True)
            if fm is None:
                continue
            dom = fm.get("domain")
            if dom != path.stem:
                errors.append(f"{rel}: frontmatter 'domain' ({dom!r}) must match filename stem {path.stem!r}")
            facts = fm.get("facts")
            if not isinstance(facts, dict):
                errors.append(f"{rel}: frontmatter 'facts' must be a mapping")
                continue

            stat = FactStat(domain=path.stem)
            for key, entry in facts.items():
                fctx = f"{rel}:facts.{key}"
                if not isinstance(entry, dict) or "v" not in entry:
                    errors.append(f"{fctx}: must be a mapping containing key 'v'")
                    continue
                val = entry["v"]
                src = entry.get("src")
                status = entry.get("status")
                ttl = entry.get("ttl_days")
                if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0):
                    errors.append(f"{fctx}.ttl_days: must be positive int, got {ttl!r}")
                    ttl = None

                if val == "?":
                    stat.unknown += 1
                    if entry.get("as_of") is not None:
                        parse_strict_date(entry.get("as_of"), f"{fctx}.as_of", errors)
                    continue

                # Non-unknown fact (includes 'none', numeric 0, bool, strings, lists)
                as_of = parse_strict_date(entry.get("as_of"), f"{fctx}.as_of", errors)
                if src not in VALID_FACT_SOURCES:
                    errors.append(f"{fctx}.src: must be one of {sorted(VALID_FACT_SOURCES)}, got {src!r}")
                if src == "ai":
                    if status not in {"pending", "confirmed"}:
                        errors.append(f"{fctx}.status: AI-written facts must explicitly set status 'pending' or 'confirmed'")
                elif status is not None and status not in {"pending", "confirmed"}:
                    errors.append(f"{fctx}.status: invalid status {status!r}")

                if status == "pending":
                    stat.pending += 1
                    age_txt = f"{(today - as_of).days}d" if as_of else "?"
                    pending_ai_facts.append(f"{path.stem}.{key}={val} (待确认 {age_txt})")
                    if as_of and (today - as_of).days > max_pending_days:
                        events.append(
                            Event(
                                event_id=f"pending-ai-fact:{path.stem}.{key}:{as_of.isoformat()}",
                                rule="pending-ai-fact",
                                severity="med",
                                title=f"待确认 AI 事实超时：{path.stem}.{key}",
                                evidence=f"{rel} 中 AI 写入的 {key}={val} 已 pending 超过 {max_pending_days} 天（写入日 {as_of.isoformat()}）",
                                action="周检查中确认改为 status: confirmed 或删除，防止未确认事实污染决策",
                                due_date=today,
                                notify_daily=False,
                            )
                        )
                    continue

                if as_of and ttl and (today - as_of).days > ttl:
                    age = (today - as_of).days
                    stat.stale += 1
                    stale_facts.append(f"{path.stem}.{key} ({age}d > {ttl}d)")
                    events.append(
                        Event(
                            event_id=f"fact-stale:{path.stem}.{key}:{as_of.isoformat()}",
                            rule="fact-stale",
                            severity="low",
                            title=f"事实过期：{path.stem}.{key}",
                            evidence=f"{rel} 中 {key} 上次确认于 {as_of.isoformat()}（{age} 天 > TTL {ttl} 天）",
                            action="下次周检查核实并刷新 as_of，或改为 ?",
                            due_date=today,
                            notify_daily=False,
                        )
                    )
                else:
                    stat.confirmed += 1
                    unit = f" {entry['unit']}" if entry.get("unit") else ""
                    stat.confirmed_items.append((key, f"{val}{unit}"))

            domain_stats.append(stat)

    # 7. Validate & scan decisions/*.md (single stable file per decision)
    decisions_dir = root / "decisions"
    if not decisions_dir.exists():
        errors.append("decisions/: directory missing")
    else:
        dl_lead = int(rules_cfg.get("decision-deadline", {}).get("lead_days", 7))
        rv_lead = int(rules_cfg.get("decision-review", {}).get("lead_days", 3))
        for path in sorted(decisions_dir.glob("*.md")):
            if path.name.startswith("_"):
                continue
            rel = path.relative_to(root).as_posix()
            fm, _ = parse_frontmatter(path, rel, errors, required=True)
            if fm is None:
                continue
            level = fm.get("level")
            status = fm.get("status")
            if level not in VALID_LEVELS:
                errors.append(f"{rel}: invalid/missing 'level' ({level!r})")
            if status not in VALID_DECISION_STATUS:
                errors.append(f"{rel}: invalid/missing 'status' ({status!r})")
            parse_strict_date(fm.get("created"), f"{rel}:created", errors)
            deadline = parse_strict_date(fm.get("deadline"), f"{rel}:deadline", errors, allow_none=True)
            review_due = parse_strict_date(
                fm.get("review_due"),
                f"{rel}:review_due",
                errors,
                allow_none=(status != "decided"),
            )

            if status == "decided" and level in {"L2", "L3"}:
                flip_if = fm.get("flip_if")
                if not isinstance(flip_if, str) or not flip_if.strip():
                    errors.append(f"{rel}: decided {level} decision must specify non-empty 'flip_if'")
                pred = fm.get("prediction")
                if not isinstance(pred, dict) or not isinstance(pred.get("claim"), str) or not pred["claim"].strip():
                    errors.append(f"{rel}: decided {level} decision must specify 'prediction.claim'")
                else:
                    prob = pred.get("p")
                    if isinstance(prob, bool) or not isinstance(prob, (int, float)) or not (0.0 <= float(prob) <= 1.0):
                        errors.append(f"{rel}: 'prediction.p' must be a number in [0, 1], got {prob!r}")

            if status == "open":
                open_decisions.append((path.stem, str(level), deadline))
                if deadline:
                    calendar_items.append(
                        CalendarItem(
                            uid=f"lifeos-decision-deadline-{path.stem}-{deadline.isoformat()}@teddy88teddy",
                            title=f"[LifeOS 决策截止] {path.stem}",
                            due=deadline,
                            description=f"{rel} ({level})",
                            remind_days=[7, 2, 1],
                        )
                    )
                    days_left = (deadline - today).days
                    dl_windows = [dl_lead, 1] if dl_lead > 1 else [1]
                    if days_left <= 0:
                        period = "T0" if days_left == 0 else "overdue"
                        tag = "今天截止" if days_left == 0 else f"已逾期 {-days_left} 天"
                        events.append(
                            Event(
                                event_id=f"decision-deadline:{path.stem}:{deadline.isoformat()}:{period}",
                                rule="decision-deadline",
                                severity="high",
                                title=f"决策截止：{path.stem}（{tag}）",
                                evidence=f"{rel} ({level}, status=open) 截止日 {deadline.isoformat()}",
                                action="完成核验并拍板，更新 status 为 decided 并填写 review_due",
                                due_date=deadline,
                                notify_daily=True,
                            )
                        )
                    else:
                        matching = [w for w in dl_windows if days_left <= w]
                        if matching:
                            window = min(matching)
                            events.append(
                                Event(
                                    event_id=f"decision-deadline:{path.stem}:{deadline.isoformat()}:T{window}",
                                    rule="decision-deadline",
                                    severity="high" if window <= 1 else "med",
                                    title=f"决策截止：{path.stem}（剩 {days_left} 天）",
                                    evidence=f"{rel} ({level}, status=open) 截止日 {deadline.isoformat()}（窗口 T-{window}）",
                                    action="完成核验并拍板，更新 status 为 decided 并填写 review_due",
                                    due_date=deadline,
                                    notify_daily=True,
                                )
                            )

            if status == "decided" and review_due:
                calendar_items.append(
                    CalendarItem(
                        uid=f"lifeos-decision-review-{path.stem}-{review_due.isoformat()}@teddy88teddy",
                        title=f"[LifeOS 决策复盘] {path.stem}",
                        due=review_due,
                        description=f"{rel} ({level}) 到期复盘：拆分过程与结果",
                        remind_days=[3, 1],
                    )
                )
                days_left = (review_due - today).days
                if days_left <= rv_lead:
                    tag = f"已到期 {-days_left} 天" if days_left <= 0 else f"{days_left} 天后到期"
                    events.append(
                        Event(
                            event_id=f"decision-review:{path.stem}:{review_due.isoformat()}",
                            rule="decision-review",
                            severity="high" if days_left <= 0 else "med",
                            title=f"决策复盘到期：{path.stem}（{tag}）",
                            evidence=f"{rel} ({level}) 复盘日 {review_due.isoformat()}",
                            action="对照当时预测与翻转条件，分开打分「过程质量」与「客观结果」，并改 status 为 reviewed",
                            due_date=review_due,
                            notify_daily=True,
                        )
                    )

    # 8. Validate & scan log/YYYY-MM.md for weekly_energy signal
    energy_obs: list[tuple[dt.date, int]] = []
    log_dir = root / "log"
    if log_dir.exists():
        for path in sorted(log_dir.glob("*.md")):
            rel = path.relative_to(root).as_posix()
            if not re.fullmatch(r"\d{4}-\d{2}\.md", path.name):
                errors.append(f"{rel}: log filename must match YYYY-MM.md")
                continue
            _, body = parse_frontmatter(path, rel, errors, required=True)
            # Strip HTML comments before scanning headings/fields
            clean_body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
            current_date: dt.date | None = None
            for line_no, line in enumerate(clean_body.splitlines(), start=1):
                hm = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})\b", line)
                if hm:
                    current_date = parse_strict_date(hm.group(1), f"{rel}:L{line_no}", errors)
                    continue
                em = re.match(r"^\s*[-*]?\s*weekly_energy\s*:\s*(\S+)", line)
                if em:
                    raw_score = em.group(1)
                    if current_date is None:
                        errors.append(f"{rel}:L{line_no}: weekly_energy must appear under a '## YYYY-MM-DD' heading")
                        continue
                    try:
                        score = int(raw_score)
                    except ValueError:
                        errors.append(f"{rel}:L{line_no}: weekly_energy must be integer 1..5, got {raw_score!r}")
                        continue
                    if not (1 <= score <= 5):
                        errors.append(f"{rel}:L{line_no}: weekly_energy out of range 1..5 ({score})")
                        continue
                    energy_obs.append((current_date, score))

    energy_obs.sort(key=lambda x: x[0])
    e_rule = rules_cfg.get("energy-low", {})
    e_thresh = int(e_rule.get("threshold", 2))
    e_weeks = int(e_rule.get("consecutive_weeks", 2))
    e_max_gap = int(e_rule.get("max_gap_days", 21))

    if len(energy_obs) < e_weeks:
        energy_status = "insufficient_data"
        energy_detail = f"数据不足（当前仅 {len(energy_obs)} 条周精力观测，至少需要 {e_weeks} 周）"
    else:
        recent = energy_obs[-e_weeks:]
        stale_obs = (today - recent[-1][0]).days > e_max_gap
        too_far_apart = any(
            (recent[i + 1][0] - recent[i][0]).days > e_max_gap for i in range(len(recent) - 1)
        )
        if stale_obs or too_far_apart:
            energy_status = "insufficient_data"
            energy_detail = f"近期观测断档（最新记录 {recent[-1][0].isoformat()}，超过 {e_max_gap} 天）"
        elif all(score <= e_thresh for _, score in recent):
            energy_status = "low"
            scores_str = ", ".join(f"{d.isoformat()}:{s}/5" for d, s in recent)
            energy_detail = f"连续 {e_weeks} 周精力 ≤{e_thresh}（{scores_str}）"
            events.append(
                Event(
                    event_id=f"energy-low:weekly:{recent[-1][0].isoformat()}",
                    rule="energy-low",
                    severity="med",
                    title="精力连续偏低预警",
                    evidence=energy_detail,
                    action=str(e_rule.get("action", "建议未来 7 天进入恢复模式；L2 以上重大决策推迟")),
                    due_date=today,
                    notify_daily=True,
                )
            )
        else:
            energy_status = "ok"
            energy_detail = f"最新周精力 {recent[-1][1]}/5（{recent[-1][0].isoformat()}）"

    # 9. Secret sanity scan across all .md / .yml / .yaml files
    for p in sorted(root.rglob("*")):
        if ".git" in p.parts or not p.is_file():
            continue
        if p.suffix.lower() not in {".md", ".yml", ".yaml"}:
            continue
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        for label, pat in SECRET_PATTERNS:
            if pat.search(text):
                errors.append(f"{rel}: potential secret detected ({label}) — NEVER commit keys!")

    events.sort(key=lambda e: (-SEVERITY_RANK[e.severity], e.due_date or dt.date.max, e.event_id))
    return config, ScanResult(
        errors=errors,
        events=events,
        calendar_items=calendar_items,
        domain_stats=domain_stats,
        energy_status=energy_status,
        energy_detail=energy_detail,
        open_decisions=open_decisions,
        pending_ai_facts=pending_ai_facts,
        stale_facts=stale_facts,
    )


# ---------------------------------------------------------------------------
# Owner-only GitHub Issues State Machine (pure logic + thin REST wrapper)
# ---------------------------------------------------------------------------


def parse_snooze_until(body: str, comment_time: dt.datetime, tz: ZoneInfo) -> dt.datetime | None:
    m = SNOOZE_RE.search(body or "")
    if not m:
        return None
    token = m.group(1).lower()
    if token.endswith("d"):
        days = int(token[:-1])
        if days <= 0 or days > 365:
            return None
        return comment_time.astimezone(tz) + dt.timedelta(days=days)
    try:
        target = dt.date.fromisoformat(token)
        return dt.datetime(target.year, target.month, target.day, 8, 0, tzinfo=tz)
    except ValueError:
        return None


def evaluate_issue_plan(
    events: list[Event],
    existing_issues: list[dict[str, Any]],
    comments_by_number: dict[int, list[dict[str, Any]]],
    *,
    owner: str,
    now_local: dt.datetime,
    tz: ZoneInfo,
    high_hours: int = 72,
    ignored_threshold: int = 3,
) -> dict[str, Any]:
    """Pure state-machine evaluation (fully unit-testable without network!).

    Security invariant:
      - Event issues are trusted ONLY if author is `owner` or `github-actions[bot]`.
      - `/snooze` comments are trusted ONLY if `comment.user.login == owner`.
      - `capture` issues are trusted ONLY if `issue.user.login == owner`.
    """
    trusted_event_authors = {owner} | BOT_AUTHORS
    eid_to_issue: dict[str, dict[str, Any]] = {}
    ignored_by_rule: dict[str, int] = {}
    owner_captures: list[dict[str, Any]] = []

    for iss in existing_issues:
        author = (iss.get("user") or {}).get("login", "")
        labels = {lb.get("name", "") if isinstance(lb, dict) else str(lb) for lb in iss.get("labels", [])}

        if "capture" in labels and iss.get("state") == "open":
            if author == owner:
                owner_captures.append(iss)
            continue

        if "lifeos-event" not in labels or author not in trusted_event_authors:
            continue

        m = EID_RE.search(iss.get("body") or "")
        if not m:
            continue
        eid = m.group(1)
        eid_to_issue[eid] = iss

        if iss.get("state") == "closed" and iss.get("state_reason") == "not_planned":
            rule_prefix = eid.split(":", 1)[0]
            ignored_by_rule[rule_prefix] = ignored_by_rule.get(rule_prefix, 0) + 1

    to_create: list[Event] = []
    to_escalate: list[tuple[Event, dict[str, Any]]] = []
    snoozed_active: list[tuple[Event, dt.datetime]] = []
    unsnoozed_resurface: list[tuple[Event, dict[str, Any]]] = []
    issue_links: dict[str, str] = {}

    for ev in events:
        if ev.severity == "low":
            continue  # low severity stays in radar.md & Sunday digest, never spams Issues

        existing = eid_to_issue.get(ev.event_id)
        if existing is None:
            to_create.append(ev)
            continue

        issue_links[ev.event_id] = existing.get("html_url", f"#{existing.get('number')}")
        if existing.get("state") == "closed":
            continue  # completed or not_planned -> never re-alert!

        # Open issue: inspect ONLY owner comments for /snooze
        number = int(existing["number"])
        comments = comments_by_number.get(number, [])
        latest_snooze: dt.datetime | None = None
        for cm in comments:
            c_author = (cm.get("user") or {}).get("login", "")
            if c_author != owner:
                continue  # CRITICAL: ignore non-owner comments (prompt/command injection defense)
            c_time_raw = cm.get("created_at", "")
            try:
                c_time = dt.datetime.fromisoformat(c_time_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            until = parse_snooze_until(cm.get("body", ""), c_time, tz)
            if until is not None:
                latest_snooze = until

        if latest_snooze is not None:
            if now_local < latest_snooze:
                snoozed_active.append((ev, latest_snooze))
                continue
            marker = f"<!-- resurfaced:{latest_snooze.isoformat()} -->"
            already_resurfaced = any(
                (cm.get("user") or {}).get("login", "") in trusted_event_authors
                and marker in (cm.get("body") or "")
                for cm in comments
            )
            if not already_resurfaced:
                unsnoozed_resurface.append((ev, existing, latest_snooze))
            continue

        # Escalation check: high severity open >= high_hours and not yet escalated
        labels = {lb.get("name", "") if isinstance(lb, dict) else str(lb) for lb in existing.get("labels", [])}
        if ev.severity == "high" and "escalated" not in labels:
            created_raw = existing.get("created_at", "")
            try:
                created_dt = dt.datetime.fromisoformat(created_raw.replace("Z", "+00:00")).astimezone(tz)
                if (now_local - created_dt) >= dt.timedelta(hours=high_hours):
                    to_escalate.append((ev, existing))
            except ValueError:
                pass

    noisy_rules = [
        f"规则 `{rule}` 累计被忽略 {cnt} 次（≥{ignored_threshold}），建议调阈值或删除"
        for rule, cnt in sorted(ignored_by_rule.items())
        if cnt >= ignored_threshold
    ]

    return {
        "to_create": to_create,
        "to_escalate": to_escalate,
        "unsnoozed_resurface": unsnoozed_resurface,
        "snoozed_active": snoozed_active,
        "issue_links": issue_links,
        "owner_captures": owner_captures,
        "noisy_rules": noisy_rules,
    }


class GitHubClient:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.token = token
        self.base = f"https://api.github.com/repos/{repo}"

    def _req(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Teddy-LifeOS-v3",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None

    def list_issues(self, label: str, state: str = "all") -> list[dict[str, Any]]:
        q = urllib.parse.urlencode({"labels": label, "state": state, "per_page": 100})
        items = self._req("GET", f"/issues?{q}")
        return [i for i in (items or []) if "pull_request" not in i]

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return self._req("GET", f"/issues/{issue_number}/comments?per_page=100") or []

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self._req("POST", "/issues", {"title": title, "body": body, "labels": labels})

    def create_comment(self, issue_number: int, body: str) -> dict[str, Any]:
        return self._req("POST", f"/issues/{issue_number}/comments", {"body": body})

    def add_labels(self, issue_number: int, labels: list[str]) -> Any:
        return self._req("POST", f"/issues/{issue_number}/labels", {"labels": labels})


# ---------------------------------------------------------------------------
# ServerChan WeChat Notification (Turbo SCT... & ServerChan3 sctp... auto-detect)
# ---------------------------------------------------------------------------


def build_serverchan_url(sendkey: str) -> str:
    key = sendkey.strip()
    m = re.match(r"^sctp(\d+)t", key)
    if m:
        uid = m.group(1)
        return f"https://{uid}.push.ft07.com/send/{key}.send"
    return f"https://sctapi.ftqq.com/{key}.send"


def send_serverchan(sendkey: str, title: str, desp: str) -> dict[str, Any]:
    url = build_serverchan_url(sendkey)
    clean_title = re.sub(r"[\r\n]+", " ", title).strip()[:32]
    encoded = urllib.parse.urlencode({"title": clean_title, "desp": desp}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Teddy-LifeOS-v3"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def ping_healthcheck(url: str) -> None:
    req = urllib.request.Request(url.strip(), method="GET", headers={"User-Agent": "Teddy-LifeOS-v3"})
    with urllib.request.urlopen(req, timeout=15):
        pass


# ---------------------------------------------------------------------------
# `out` branch artifact builders: radar.md, calendar.ics, context_pack.md
# ---------------------------------------------------------------------------


def build_radar_md(
    scan: ScanResult,
    issue_links: dict[str, str],
    owner_captures: list[dict[str, Any]],
    noisy_rules: list[str],
    now_local: dt.datetime,
) -> str:
    lines = [
        "# LifeOS 主动雷达（自动生成，请勿手改）",
        "",
        f"- **最后成功巡检**：`{now_local.isoformat(timespec='minutes')}`（Asia/Shanghai）",
        f"- **精力信号状态**：{scan.energy_detail}",
        f"- **待整理手机 Capture（owner）**：{len(owner_captures)} 条",
        "",
        "## 1. 当前活跃事件",
    ]
    top_events = [e for e in scan.events if e.severity in {"high", "med"}][:10]
    if top_events:
        for ev in top_events:
            link = f" · [Issue]({issue_links[ev.event_id]})" if ev.event_id in issue_links else ""
            lines.append(f"- **[{ev.severity.upper()}]** {ev.title} (`{ev.event_id}`){link}")
            lines.append(f"  - 证据：{ev.evidence}")
            lines.append(f"  - 建议：{ev.action}")
    else:
        lines.append("- 当前无高/中优先级活跃事件。")

    lines.extend(["", "## 2. 数据健康与盲区"])
    if scan.pending_ai_facts:
        lines.append(f"- **待确认 AI 事实（禁止作 L2+ 硬依据）**：{'; '.join(scan.pending_ai_facts)}")
    if scan.stale_facts:
        lines.append(f"- **已过期事实（超出 TTL）**：{'; '.join(scan.stale_facts)}")
    if noisy_rules:
        for nr in noisy_rules:
            lines.append(f"- **告警减噪建议**：{nr}")
    if not (scan.pending_ai_facts or scan.stale_facts or noisy_rules):
        lines.append("- 无过期事实、无超期 pending 事实。")

    lines.extend(["", "| 领域 | 已确认 | 待确认(AI) | 过期 | 未知(`?`) |", "| :--- | :---: | :---: | :---: | :---: |"])
    for st in scan.domain_stats:
        lines.append(f"| `{st.domain}` | {st.confirmed} | {st.pending} | {st.stale} | {st.unknown} |")

    if scan.open_decisions:
        lines.extend(["", "## 3. 未拍板决策（由 decisions/*.md 自动生成）"])
        for slug, level, dl in scan.open_decisions:
            dl_txt = dl.isoformat() if dl else "无硬截止"
            lines.append(f"- `{slug}` ({level}) · 截止：`{dl_txt}`")

    return "\n".join(lines) + "\n"


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_calendar_ics(items: list[CalendarItem], now_utc: dt.datetime) -> str:
    dtstamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Teddy LifeOS//v3.0//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Teddy LifeOS Deadlines",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for it in sorted(items, key=lambda x: (x.due, x.uid)):
        start = it.due.strftime("%Y%m%d")
        end = (it.due + dt.timedelta(days=1)).strftime("%Y%m%d")
        out.extend([
            "BEGIN:VEVENT",
            f"UID:{it.uid}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;VALUE=DATE:{start}",
            f"DTEND;VALUE=DATE:{end}",
            f"SUMMARY:{_ics_escape(it.title)}",
            f"DESCRIPTION:{_ics_escape(it.description)}",
        ])
        for rd in it.remind_days[:2]:
            trigger = "-PT0S" if rd == 0 else f"-P{rd}D"
            out.extend([
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"TRIGGER:{trigger}",
                f"DESCRIPTION:{_ics_escape(it.title)}",
                "END:VALARM",
            ])
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def build_context_pack_md(
    root: Path,
    scan: ScanResult,
    radar_md: str,
    max_chars: int,
    now_local: dt.datetime,
) -> str:
    """Compact <=3k-token snapshot specifically tailored for mobile Gemini Gem."""
    parts = [
        f"# Teddy LifeOS — Gemini 移动端快照（生成于 {now_local.strftime('%Y-%m-%d %H:%M')} 北京时间）",
        "",
        "## 使用约定（精简版）",
        "- 你是 Teddy 的人生决策参谋。L0 日常小事只给 **1 个可执行答案（≤3行）**，不例行追问档案；只有存在真实权衡时给 1 个备选。",
        "- 遇到自动续费、不可逆条款、大额支出或触碰红线，立刻升级为 L1–L3：先查硬红线与机会成本，再给最强反方论证与翻转条件。",
        "- 本快照允许滞后一周；涉及 L2/L3 重大决定或需要写回仓库时，提醒转回桌面 Agent 会话。",
        "",
    ]

    for title, rel in [("当前实时约束 (now.md)", "now.md"), ("罗盘与红线 (compass.md)", "compass.md"), ("日常默认系统 (daily.md)", "domains/daily.md")]:
        p = root / rel
        if p.exists():
            _, body = parse_frontmatter(p, rel, [], required=False)
            parts.extend([f"## {title}", body.strip(), ""])

    confirmed_lines = []
    for st in scan.domain_stats:
        if st.confirmed_items:
            kv = "；".join(f"{k}={v}" for k, v in st.confirmed_items)
            confirmed_lines.append(f"- **{st.domain}**：{kv}")
    parts.append("## 各领域已确认关键事实（不含未确认 pending）")
    parts.extend(confirmed_lines or ["- 暂无已确认结构化事实（按需在真实决策中补充）。"])
    parts.extend(["", "## 当前活跃雷达摘要", "\n".join(radar_md.splitlines()[:25])])

    text = "\n".join(parts).strip() + "\n"
    if len(text) > max_chars:
        text = text[: max_chars - 40].rstrip() + "\n\n...(已按 Gemini token 预算截断)\n"
    return text


def format_issue_body(ev: Event) -> str:
    return (
        f"<!-- eid: {ev.event_id} -->\n"
        f"**规则**：`{ev.rule}` · **级别**：`{ev.severity}`\n\n"
        f"### 证据\n{ev.evidence}\n\n"
        f"### 建议动作\n{ev.action}\n\n"
        "---\n"
        "**快捷处置（仅认仓库主人 `teddy88teddy` 操作）**：\n"
        "- 已处理：直接 **Close issue**\n"
        "- 忽略：选择 **Close as not planned**\n"
        "- 延后：评论 `/snooze 3d` 或 `/snooze YYYY-MM-DD`\n"
        "- 补充事实：评论 `/fact domain.key=value`（周检查入库）\n"
    )


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Life OS v3 strict core engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="Strict validation of all Life OS files")
    p_val.add_argument("--root", type=Path, default=ROOT_DEFAULT)

    p_out = sub.add_parser("build-out", help="Validate and generate out/ artifacts locally")
    p_out.add_argument("--root", type=Path, default=ROOT_DEFAULT)
    p_out.add_argument("--out-dir", type=Path, required=True)

    p_run = sub.add_parser("run", help="Full daily scan, issue sync, ServerChan notify, out build")
    p_run.add_argument("--root", type=Path, default=ROOT_DEFAULT)
    p_run.add_argument("--out-dir", type=Path, required=True)
    p_run.add_argument("--sync-issues", action="store_true")
    p_run.add_argument("--notify", action="store_true")
    p_run.add_argument("--dry-run", action="store_true")

    p_fail = sub.add_parser("notify-failure", help="Send ServerChan failure alert when CI validation fails")
    p_fail.add_argument("--message", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "notify-failure":
        key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
        if not key:
            print("SERVERCHAN_SENDKEY not set; skipping failure push.")
            return 0
        resp = send_serverchan(key, "LifeOS 系统校验失败", args.message)
        print("Failure notification response:", resp)
        return 0

    config, scan = scan_repo(args.root)
    tz = ZoneInfo(config.get("tz", "Asia/Shanghai"))
    now_local = dt.datetime.now(tz)
    now_utc = dt.datetime.now(dt.timezone.utc)

    if scan.errors:
        print(f"VALIDATION FAILED ({len(scan.errors)} error(s)):", file=sys.stderr)
        for err in scan.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    if args.cmd == "validate":
        print(
            f"OK ({now_local.strftime('%Y-%m-%d %H:%M %Z')}): "
            f"{len(scan.events)} event(s), {len(scan.calendar_items)} calendar item(s), 0 error(s)."
        )
        return 0

    issue_links: dict[str, str] = {}
    owner_captures: list[dict[str, Any]] = []
    noisy_rules: list[str] = []
    push_lines: list[str] = []

    if args.cmd == "run" and args.sync_issues:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = config["repo"]
        owner = config["owner"]
        if not token:
            print("ERROR: --sync-issues requires GITHUB_TOKEN", file=sys.stderr)
            return 1
        gh = GitHubClient(repo, token)
        event_issues = gh.list_issues("lifeos-event", state="all")
        capture_issues = gh.list_issues("capture", state="open")
        all_issues = event_issues + capture_issues

        comments_map: dict[int, list[dict[str, Any]]] = {}
        for iss in event_issues:
            if iss.get("state") == "open" and int(iss.get("comments", 0)) > 0:
                comments_map[int(iss["number"])] = gh.list_comments(int(iss["number"]))

        esc_cfg = config.get("escalation", {})
        plan = evaluate_issue_plan(
            scan.events,
            all_issues,
            comments_map,
            owner=owner,
            now_local=now_local,
            tz=tz,
            high_hours=int(esc_cfg.get("high_hours", 72)),
            ignored_threshold=int(esc_cfg.get("ignored_rule_threshold", 3)),
        )
        issue_links.update(plan["issue_links"])
        owner_captures = plan["owner_captures"]
        noisy_rules = plan["noisy_rules"]

        for ev in plan["to_create"]:
            if args.dry_run:
                print(f"[DRY-RUN] Would create issue for {ev.event_id}")
                link = "(dry-run)"
            else:
                created = gh.create_issue(
                    title=f"[{ev.severity.upper()}] {ev.title}",
                    body=format_issue_body(ev),
                    labels=["lifeos-event", f"sev:{ev.severity}"],
                )
                link = created.get("html_url", "")
                issue_links[ev.event_id] = link
            if ev.notify_daily:
                push_lines.append(f"- **[{ev.severity.upper()}]** {ev.title} → [处置]({link})")

        for ev, iss in plan["to_escalate"]:
            link = iss.get("html_url", "")
            if not args.dry_run:
                gh.add_labels(int(iss["number"]), ["escalated"])
            push_lines.append(f"- **[升级提醒]** {ev.title}（已超72h未处理）→ [处置]({link})")

        for ev, iss, snooze_dt in plan["unsnoozed_resurface"]:
            link = iss.get("html_url", "")
            if not args.dry_run:
                gh.create_comment(
                    int(iss["number"]),
                    f"<!-- resurfaced:{snooze_dt.isoformat()} -->\n延后已到期，已重新合并至今日推送。",
                )
            push_lines.append(f"- **[延后到期]** {ev.title} → [处置]({link})")

    # Build out/ artifacts
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    radar_md = build_radar_md(scan, issue_links, owner_captures, noisy_rules, now_local)
    ics_text = build_calendar_ics(scan.calendar_items, now_utc)
    max_chars = int(config.get("mobile_ai", {}).get("context_pack_max_chars", 9000))
    ctx_pack = build_context_pack_md(args.root, scan, radar_md, max_chars, now_local)

    (out_dir / "radar.md").write_text(radar_md, encoding="utf-8")
    (out_dir / "calendar.ics").write_text(ics_text, encoding="utf-8", newline="")
    (out_dir / "context_pack.md").write_text(ctx_pack, encoding="utf-8")
    print(f"Built out artifacts in {out_dir}: radar.md, calendar.ics, context_pack.md ({len(ctx_pack)} chars)")

    # Daily merged notification (<=1 message/day) + Sunday heartbeat
    if args.cmd == "run" and args.notify:
        sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
        is_sunday = now_local.weekday() == 6 and bool(config.get("notify", {}).get("sunday_heartbeat", True))

        if push_lines or is_sunday:
            if is_sunday:
                title = f"LifeOS 周报心跳 · {len(push_lines)} 项提醒" if push_lines else "LifeOS 周报心跳 · 一切正常"
                sections = []
                if push_lines:
                    sections.extend(["### 今日待处置提醒", *push_lines, ""])
                sections.extend([
                    "### 周日 5 分钟检查摘要",
                    f"- 手机待整理 Capture：{len(owner_captures)} 条",
                    f"- 待确认 AI 事实：{len(scan.pending_ai_facts)} 条",
                    f"- 超期 TTL 事实：{len(scan.stale_facts)} 条",
                    f"- 精力观测：{scan.energy_detail}",
                ])
                if noisy_rules:
                    sections.extend(["", "### 告警减噪建议", *(f"- {r}" for r in noisy_rules)])
                sections.extend([
                    "",
                    f"查看完整雷达：https://github.com/{config['repo']}/blob/out/radar.md",
                ])
                desp = "\n".join(sections)
            else:
                title = f"LifeOS 今日提醒（{len(push_lines)} 项）"
                desp = "\n".join(
                    [
                        "### 今日待处置事件",
                        *push_lines,
                        "",
                        "操作：点链接直接关闭=已处理 / not planned=忽略 / 评论 `/snooze 3d` 延后。",
                    ]
                )

            if args.dry_run or not sendkey:
                print(f"[NOTIFY SKIPPED dry_run={args.dry_run} has_key={bool(sendkey)}] {title}\n{desp}")
            else:
                resp = send_serverchan(sendkey, title, desp)
                print("ServerChan sent:", title, resp)

        hc_url = os.environ.get("HEALTHCHECKS_PING_URL", "").strip()
        if hc_url and not args.dry_run:
            ping_healthcheck(hc_url)
            print("Healthchecks ping OK")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
