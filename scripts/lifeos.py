#!/usr/bin/env python3
"""Life OS v3.1 — Single Strict Core Engine.

Design invariants:
  1. Strict YAML loading (duplicate keys rejected, unknown fields rejected with typo hints,
     sanitized file:line:col errors so public logs never leak raw lines).
  2. One stable occurrence Issue per non-low event (bot-authored, auto-locked), carrying its
     own JSON outbox state in the body (`<!-- lifeos:{...} -->`). No state files written to main.
  3. Two-phase crash-safe delivery: `prepare` (sync Issues, build out/, write digest.json) ->
     publish `out` -> `deliver` (ServerChan code==0 verified -> PATCH Issue outbox state) ->
     ping healthchecks.io ONLY after every step succeeds.
  4. Zero silent failures: invalid dates, missing keys, non-zero ServerChan codes, or missing
     production SendKey always exit non-zero.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
STATE_RE = re.compile(r"<!--\s*lifeos:(\{[^\n]*?\})\s*-->")
SNOOZE_RE = re.compile(r"^\s*/snooze\s+(\d+d|\d{4}-\d{2}-\d{2})\b", re.IGNORECASE | re.MULTILINE)
CAPTURE_DUE_RE = re.compile(
    r"###\s*截止日期[^\n]*\n+\s*(\d{4}-\d{2}-\d{2})\b",
    re.MULTILINE,
)

SEVERITY_RANK = {"low": 1, "med": 2, "high": 3}
SEVERITY_UP = {"low": "med", "med": "high", "high": "high"}
SEVERITY_ZH = {"high": "高", "med": "中", "low": "低"}

VALID_LEVELS = {"L1", "L2", "L3"}
VALID_DECISION_STATUS = {"open", "decided", "reviewed", "dropped"}
VALID_DATE_KINDS = {"renewal", "deadline", "cooling", "custom"}
VALID_FACT_SOURCES = {"me", "ai", "doc"}
PLACEHOLDER_STRINGS = {"", "?", "tbd", "待补全", "待定", "todo", "n/a"}
BOT_AUTHORS = {"github-actions[bot]", "app/github-actions"}

# Field whitelists (unknown keys fail validation loudly with typo suggestions)
CONFIG_TOP_KEYS = {
    "owner",
    "repo",
    "tz",
    "notify",
    "mobile_ai",
    "escalation",
    "spending_gates_cny",
    "rules",
}
CONFIG_NOTIFY_KEYS = {"channel", "sunday_heartbeat"}
CONFIG_MOBILE_KEYS = {"app", "context_pack_max_chars"}
CONFIG_ESCALATION_KEYS = {"high_hours", "ignored_rule_threshold"}
CONFIG_SPENDING_KEYS = {"tier0_instant_max", "tier1_review_max", "tier2_cooling_max", "cooling_hours"}
CONFIG_RULES_KEYS = {
    "decision_deadline_windows",
    "decision_review_lead_days",
    "exception_expiry_lead_days",
    "pending_ai_max_days",
    "energy_low_threshold",
    "energy_low_consecutive_weeks",
    "energy_max_gap_days",
}
DATE_COMMON_KEYS = {"id", "title", "kind", "severity", "note"}
DATE_COOLING_KEYS = DATE_COMMON_KEYS | {"added", "hours", "price", "resolved"}
DATE_STANDARD_KEYS = DATE_COMMON_KEYS | {"due", "remind", "auto_renew"}
DOMAIN_FM_KEYS = {"domain", "facts"}
FACT_ENTRY_KEYS = {"v", "as_of", "src", "status", "ttl_days", "unit"}
DECISION_FM_KEYS = {
    "level",
    "status",
    "created",
    "deadline",
    "review_due",
    "option0",
    "downside",
    "prediction",
    "flip_if",
}
DOWNSIDE_KEYS = {"cash_low", "commitments", "affected", "exit_by", "reversal_cost"}
PREDICTION_KEYS = {"claim", "p", "range"}
EXCEPTION_KEYS = {"id", "scope", "expires", "approved_by"}

DEFAULT_LABELS = {
    "lifeos-event": ("1d76db", "LifeOS proactive event issue"),
    "lifeos-heartbeat": ("5319e7", "LifeOS weekly heartbeat state"),
    "sev:high": ("d93f0b", "High severity LifeOS event"),
    "sev:med": ("fbca04", "Medium severity LifeOS event"),
    "escalated": ("b60205", "Escalated once after high_hours"),
    "capture": ("0e8a16", "Mobile quick capture"),
}


class DuplicateKeyError(yaml.constructor.ConstructorError):
    """Raised when a YAML mapping contains duplicate keys."""


class StrictLoader(yaml.SafeLoader):
    """YAML SafeLoader that rejects duplicate mapping keys loudly."""


def _construct_strict_mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_strict_mapping,
)


class DeliveryError(RuntimeError):
    """Raised when ServerChan or GitHub delivery fails."""


@dataclasses.dataclass
class Event:
    eid: str  # Stable per occurrence (no window suffix!)
    window: str  # T30 / T7 / T1 / T0 / overdue / THAW / DUE / EXPIRED / ALERT / STALE / PENDING
    rule: str
    severity: str  # low | med | high
    title: str
    evidence: str
    action: str
    due_date: dt.date | None = None
    source_id: str | None = None


@dataclasses.dataclass
class CalendarItem:
    uid: str  # Stable without due date so rescheduling updates in place
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
    safety_blindspots: list[str]
    energy_status: str  # ok | low | insufficient_data
    energy_detail: str
    open_decisions: list[tuple[str, str, dt.date | None]]
    pending_ai_facts: list[str]
    stale_facts: list[str]
    source_as_of: dict[str, str]
    past_dates_items: list[tuple[str, str, dt.date]]  # (eid, id, due) for closed-unrolled hygiene check


# ---------------------------------------------------------------------------
# Strict YAML & field validation helpers (NEVER leak raw source lines!)
# ---------------------------------------------------------------------------


def _format_yaml_error(rel: str, exc: Exception, line_offset: int = 0) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or type(exc).__name__
    if mark is not None:
        return f"{rel}:{mark.line + 1 + line_offset}:{mark.column + 1}: YAML error ({problem})"
    return f"{rel}: YAML error ({problem})"


def strict_yaml_load(text: str, rel: str, errors: list[str], line_offset: int = 0) -> Any:
    try:
        return yaml.load(text, Loader=StrictLoader)
    except Exception as exc:
        errors.append(_format_yaml_error(rel, exc, line_offset=line_offset))
        return None


def check_unknown_keys(mapping: dict[Any, Any], allowed: set[str], ctx: str, errors: list[str]) -> None:
    for raw_key in mapping:
        key = str(raw_key)
        if key not in allowed:
            hint = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.6)
            suffix = f" (did you mean {hint[0]!r}?)" if hint else ""
            errors.append(f"{ctx}: unknown field {key!r}{suffix}")


def is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in PLACEHOLDER_STRINGS
    return False


def parse_strict_date(value: Any, ctx: str, errors: list[str], *, allow_none: bool = False) -> dt.date | None:
    if value is None:
        if allow_none:
            return None
        errors.append(f"{ctx}: required date is null/missing")
        return None
    if isinstance(value, bool):
        errors.append(f"{ctx}: invalid date (bool)")
        return None
    if isinstance(value, dt.datetime):
        errors.append(f"{ctx}: expected YYYY-MM-DD date, got datetime")
        return None
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            errors.append(f"{ctx}: invalid date format {s!r} (expected YYYY-MM-DD)")
            return None
        try:
            return dt.date.fromisoformat(s)
        except ValueError as exc:
            errors.append(f"{ctx}: impossible calendar date {s!r} ({exc})")
            return None
    errors.append(f"{ctx}: invalid date type {type(value).__name__}")
    return None


def parse_strict_datetime(value: Any, ctx: str, errors: list[str]) -> dt.datetime | None:
    if value is None or isinstance(value, bool):
        errors.append(f"{ctx}: required timezone-aware ISO datetime is missing/invalid")
        return None
    parsed: dt.datetime | None = None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        errors.append(f"{ctx}: expected ISO datetime with timezone, got date-only")
        return None
    elif isinstance(value, str):
        s = value.strip()
        if "T" not in s and " " not in s:
            errors.append(f"{ctx}: expected ISO datetime with timezone")
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(s)
        except ValueError as exc:
            errors.append(f"{ctx}: invalid ISO datetime ({exc})")
            return None
    else:
        errors.append(f"{ctx}: invalid datetime type {type(value).__name__}")
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{ctx}: datetime must include timezone offset (e.g. +08:00)")
        return None
    return parsed


def parse_frontmatter(
    path: Path, rel: str, errors: list[str], *, required: bool = True
) -> tuple[dict[str, Any] | None, str]:
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
    data = strict_yaml_load(fm_raw, rel, errors, line_offset=1)
    if data is None:
        return None, body
    if not isinstance(data, dict):
        errors.append(f"{rel}: frontmatter must be a YAML mapping")
        return None, body
    return data, body


def load_config(root: Path, errors: list[str]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "owner": "teddy88teddy",
        "repo": "teddy88teddy/Life",
        "tz": "Asia/Shanghai",
        "notify": {"channel": "serverchan", "sunday_heartbeat": True},
        "mobile_ai": {"app": "gemini_skill", "context_pack_max_chars": 9000},
        "escalation": {"high_hours": 72, "ignored_rule_threshold": 3},
        "spending_gates_cny": {
            "tier0_instant_max": 200,
            "tier1_review_max": 1000,
            "tier2_cooling_max": 5000,
            "cooling_hours": 72,
        },
        "rules": {
            "decision_deadline_windows": [7, 1],
            "decision_review_lead_days": 3,
            "exception_expiry_lead_days": 3,
            "pending_ai_max_days": 7,
            "energy_low_threshold": 2,
            "energy_low_consecutive_weeks": 2,
            "energy_max_gap_days": 21,
        },
    }
    cfg_path = root / "system/config.yml"
    if not cfg_path.exists():
        errors.append("system/config.yml: file missing")
        return defaults
    data = strict_yaml_load(cfg_path.read_text(encoding="utf-8"), "system/config.yml", errors)
    if not isinstance(data, dict):
        if data is not None:
            errors.append("system/config.yml: must be a YAML mapping")
        return defaults

    check_unknown_keys(data, CONFIG_TOP_KEYS, "system/config.yml", errors)

    tz_name = data.get("tz")
    if not isinstance(tz_name, str) or not tz_name.strip():
        errors.append("system/config.yml:tz: must be non-empty IANA timezone string")
    else:
        try:
            ZoneInfo(tz_name)
        except Exception:
            errors.append(f"system/config.yml:tz: invalid timezone {tz_name!r}")

    if not isinstance(data.get("owner"), str) or not data["owner"].strip():
        errors.append("system/config.yml:owner: must be non-empty string")
    if not isinstance(data.get("repo"), str) or "/" not in data["repo"]:
        errors.append("system/config.yml:repo: must be 'owner/name'")

    for sec, allowed in [
        ("notify", CONFIG_NOTIFY_KEYS),
        ("mobile_ai", CONFIG_MOBILE_KEYS),
        ("escalation", CONFIG_ESCALATION_KEYS),
        ("spending_gates_cny", CONFIG_SPENDING_KEYS),
        ("rules", CONFIG_RULES_KEYS),
    ]:
        val = data.get(sec)
        if val is None:
            continue
        if not isinstance(val, dict):
            errors.append(f"system/config.yml:{sec}: must be a mapping")
            continue
        check_unknown_keys(val, allowed, f"system/config.yml:{sec}", errors)

    merged = dict(defaults)
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict):
            sub = dict(defaults[k])
            sub.update(v)
            merged[k] = sub
        else:
            merged[k] = v

    # Validate rule parameter types cleanly
    rules = merged["rules"]
    dl_wins = rules.get("decision_deadline_windows")
    if not isinstance(dl_wins, list) or not dl_wins or not all(
        isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in dl_wins
    ):
        errors.append("system/config.yml:rules.decision_deadline_windows: must be non-empty list of positive ints")
        rules["decision_deadline_windows"] = [7, 1]

    for int_key in [
        "decision_review_lead_days",
        "exception_expiry_lead_days",
        "pending_ai_max_days",
        "energy_low_threshold",
        "energy_low_consecutive_weeks",
        "energy_max_gap_days",
    ]:
        val = rules.get(int_key)
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            errors.append(f"system/config.yml:rules.{int_key}: must be positive int")
            rules[int_key] = defaults["rules"][int_key]

    return merged


# ---------------------------------------------------------------------------
# Deterministic Scanner (single `now_local` passed throughout)
# ---------------------------------------------------------------------------


def scan_repo(root: Path, now_dt: dt.datetime | None = None) -> tuple[dict[str, Any], ScanResult]:
    errors: list[str] = []
    events: list[Event] = []
    calendar_items: list[CalendarItem] = []
    domain_stats: list[FactStat] = []
    safety_blindspots: list[str] = []
    open_decisions: list[tuple[str, str, dt.date | None]] = []
    pending_ai_facts: list[str] = []
    stale_facts: list[str] = []
    source_as_of: dict[str, str] = {}
    past_dates_items: list[tuple[str, str, dt.date]] = []

    config = load_config(root, errors)
    rules = config["rules"]
    tz_name = config.get("tz", "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name) if isinstance(tz_name, str) else ZoneInfo("Asia/Shanghai")
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

    # 2. Validate now.md
    now_path = root / "now.md"
    if now_path.exists():
        fm, _ = parse_frontmatter(now_path, "now.md", errors, required=True)
        if fm is not None:
            check_unknown_keys(fm, {"as_of"}, "now.md", errors)
            d = parse_strict_date(fm.get("as_of"), "now.md:as_of", errors)
            if d:
                source_as_of["now.md"] = d.isoformat()

    # 3. Validate compass.md & timed exceptions
    compass_path = root / "compass.md"
    if not compass_path.exists():
        errors.append("compass.md: required file missing")
    else:
        fm, body = parse_frontmatter(compass_path, "compass.md", errors, required=True)
        if fm is not None:
            check_unknown_keys(fm, {"as_of"}, "compass.md", errors)
            d = parse_strict_date(fm.get("as_of"), "compass.md:as_of", errors)
            if d:
                source_as_of["compass.md"] = d.isoformat()
        exc_lead = int(rules["exception_expiry_lead_days"])
        for m in re.finditer(r"```yaml\n(.*?)```", body, flags=re.DOTALL):
            block = strict_yaml_load(m.group(1), "compass.md:yaml-block", errors)
            if isinstance(block, dict) and "exceptions" in block:
                check_unknown_keys(block, {"exceptions"}, "compass.md:yaml-block", errors)
                excs = block["exceptions"]
                if not isinstance(excs, list):
                    errors.append("compass.md: 'exceptions' must be a list")
                    continue
                for idx, item in enumerate(excs):
                    ctx = f"compass.md:exceptions[{idx}]"
                    if not isinstance(item, dict):
                        errors.append(f"{ctx}: must be a mapping")
                        continue
                    check_unknown_keys(item, EXCEPTION_KEYS, ctx, errors)
                    eid = item.get("id")
                    scope = item.get("scope")
                    if not isinstance(eid, str) or not SLUG_RE.fullmatch(eid):
                        errors.append(f"{ctx}.id: must match {SLUG_RE.pattern}")
                    if is_placeholder(scope):
                        errors.append(f"{ctx}.scope: required non-placeholder string")
                    exp = parse_strict_date(item.get("expires"), f"{ctx}.expires", errors)
                    if exp and isinstance(eid, str) and SLUG_RE.fullmatch(eid):
                        days_left = (exp - today).days
                        if days_left <= exc_lead:
                            window = "EXPIRED" if days_left <= 0 else f"T{exc_lead}"
                            tag = f"已过期 {-days_left} 天" if days_left < 0 else ("今天到期" if days_left == 0 else f"剩 {days_left} 天")
                            events.append(
                                Event(
                                    eid=f"exception-expiry:{eid}:{exp.isoformat()}",
                                    window=window,
                                    rule="exception-expiry",
                                    severity="high" if days_left <= 0 else "med",
                                    title=f"限时例外：{eid}（{tag}）",
                                    evidence=f"compass.md 例外「{scope}」到期日 {exp.isoformat()}",
                                    action="恢复常规红线，或显式延长到期日",
                                    due_date=exp,
                                    source_id=eid,
                                )
                            )

    # Collect existing decision slugs first so dates.yml can reject double-entry
    decision_stems: set[str] = set()
    decisions_dir = root / "decisions"
    if decisions_dir.exists():
        for p in decisions_dir.glob("*.md"):
            if not p.name.startswith("_"):
                decision_stems.add(p.stem)

    # 4. Validate & scan system/dates.yml
    dates_path = root / "system/dates.yml"
    if not dates_path.exists():
        errors.append("system/dates.yml: file missing")
    else:
        ddata = strict_yaml_load(dates_path.read_text(encoding="utf-8"), "system/dates.yml", errors)
        if not isinstance(ddata, dict):
            if ddata is not None:
                errors.append("system/dates.yml: must be a YAML mapping")
        else:
            check_unknown_keys(ddata, {"items"}, "system/dates.yml", errors)
            items = ddata.get("items")
            if not isinstance(items, list):
                errors.append("system/dates.yml: 'items' must be a list")
                items = []
            seen_ids: set[str] = set()
            for idx, item in enumerate(items):
                ctx = f"system/dates.yml:items[{idx}]"
                if not isinstance(item, dict):
                    errors.append(f"{ctx}: must be a mapping")
                    continue
                kind = item.get("kind")
                if kind not in VALID_DATE_KINDS:
                    errors.append(f"{ctx}.kind: invalid kind {kind!r}")
                    continue
                check_unknown_keys(
                    item,
                    DATE_COOLING_KEYS if kind == "cooling" else DATE_STANDARD_KEYS,
                    ctx,
                    errors,
                )

                item_id = item.get("id")
                if not isinstance(item_id, str) or not SLUG_RE.fullmatch(item_id):
                    errors.append(f"{ctx}.id: must match {SLUG_RE.pattern} (no spaces/uppercase), got {item_id!r}")
                    continue
                if item_id in seen_ids:
                    errors.append(f"{ctx}.id: duplicate id {item_id!r}")
                seen_ids.add(item_id)

                note_str = str(item.get("note") or "")
                if item_id in decision_stems or item_id.startswith("decision-") or "decisions/" in note_str:
                    errors.append(
                        f"{ctx}: decision dates belong only in decisions/*.md, do not duplicate in system/dates.yml"
                    )

                title = str(item.get("title") or item_id).strip()
                sev = item.get("severity", "med")
                if sev not in SEVERITY_RANK:
                    errors.append(f"{ctx}.severity: invalid severity {sev!r}")
                    continue

                if kind == "cooling":
                    resolved = item.get("resolved", False)
                    if not isinstance(resolved, bool):
                        errors.append(f"{ctx}.resolved: must be bool")
                        continue
                    added_dt = parse_strict_datetime(item.get("added"), f"{ctx}.added", errors)
                    hours = item.get("hours", config["spending_gates_cny"].get("cooling_hours", 72))
                    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
                        errors.append(f"{ctx}.hours: must be positive number")
                        continue
                    # Cooling thaw minimum severity is `med` so it NEVER silently vanishes!
                    eff_sev = "med" if sev == "low" else sev
                    if added_dt and not resolved:
                        thaw_local = (added_dt + dt.timedelta(hours=float(hours))).astimezone(tz)
                        if now_local >= thaw_local:
                            price = item.get("price", "?")
                            events.append(
                                Event(
                                    eid=f"cooling:{item_id}:{thaw_local.strftime('%Y-%m-%dT%H')}",
                                    window="THAW",
                                    rule="cooling-thaw",
                                    severity=eff_sev,
                                    title=f"冷静期解冻：{title}",
                                    evidence=(
                                        f"已满 {hours:g}h（加入于 {added_dt.astimezone(tz).isoformat(timespec='minutes')}，"
                                        f"预估价 {price}）。{note_str}"
                                    ).strip(),
                                    action="确认仍需则购买并改 resolved: true；已拔草则直接关闭 Issue",
                                    due_date=thaw_local.date(),
                                    source_id=item_id,
                                )
                            )
                else:
                    due = parse_strict_date(item.get("due"), f"{ctx}.due", errors)
                    remind = item.get("remind", [7, 1])
                    if not isinstance(remind, list) or not all(
                        isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in remind
                    ):
                        errors.append(f"{ctx}.remind: must be list of non-negative ints")
                        remind = [7, 1]
                    auto_renew = item.get("auto_renew", False)
                    if not isinstance(auto_renew, bool):
                        errors.append(f"{ctx}.auto_renew: must be bool")
                        auto_renew = False
                    eff_sev = SEVERITY_UP[sev] if auto_renew else sev
                    if due:
                        occ_eid = f"{kind}:{item_id}:{due.isoformat()}"
                        if due < today:
                            past_dates_items.append((occ_eid, item_id, due))
                        calendar_items.append(
                            CalendarItem(
                                uid=f"lifeos-{kind}-{item_id}@teddy88teddy",
                                title=f"[LifeOS] {title}",
                                due=due,
                                description=f"类型: {kind} | 自动续费: {auto_renew} | {note_str}".strip(),
                                remind_days=sorted(set(remind), reverse=True),
                            )
                        )
                        days_left = (due - today).days
                        if days_left <= 0:
                            window = "T0" if days_left == 0 else "overdue"
                            tag = "今天到期" if days_left == 0 else f"已逾期 {-days_left} 天"
                            events.append(
                                Event(
                                    eid=occ_eid,
                                    window=window,
                                    rule="date-reminder",
                                    severity="high",
                                    title=f"{title}（{tag}）",
                                    evidence=f"dates.yml 到期日 {due.isoformat()}（auto_renew={auto_renew}）。{note_str}".strip(),
                                    action="立即处理并更新 dates.yml 下一周期或移除",
                                    due_date=due,
                                    source_id=item_id,
                                )
                            )
                        else:
                            matching = [r for r in remind if days_left <= r]
                            if matching:
                                win_days = min(matching)
                                events.append(
                                    Event(
                                        eid=occ_eid,
                                        window=f"T{win_days}",
                                        rule="date-reminder",
                                        severity=eff_sev,
                                        title=f"{title}（剩 {days_left} 天）",
                                        evidence=f"dates.yml 到期日 {due.isoformat()}（窗口 T-{win_days}，auto_renew={auto_renew}）。{note_str}".strip(),
                                        action="提前确认续费/取消/准备材料，完成后关闭 Issue 并更新下期日期",
                                        due_date=due,
                                        source_id=item_id,
                                    )
                                )

    # 5. Validate & scan domains/*.md
    domains_dir = root / "domains"
    if not domains_dir.exists():
        errors.append("domains/: directory missing")
    else:
        max_pending_days = int(rules["pending_ai_max_days"])
        for path in sorted(domains_dir.glob("*.md")):
            rel = path.relative_to(root).as_posix()
            fm, _ = parse_frontmatter(path, rel, errors, required=True)
            if fm is None:
                continue
            check_unknown_keys(fm, DOMAIN_FM_KEYS, rel, errors)
            dom = fm.get("domain")
            if dom != path.stem:
                errors.append(f"{rel}: 'domain' ({dom!r}) must match filename stem {path.stem!r}")
            facts = fm.get("facts")
            if not isinstance(facts, dict):
                errors.append(f"{rel}: 'facts' must be a mapping")
                continue

            stat = FactStat(domain=path.stem)
            latest_as_of: dt.date | None = None
            for key, entry in facts.items():
                fctx = f"{rel}:facts.{key}"
                if not isinstance(entry, dict):
                    errors.append(f"{fctx}: must be a mapping")
                    continue
                check_unknown_keys(entry, FACT_ENTRY_KEYS, fctx, errors)
                if "v" not in entry:
                    errors.append(f"{fctx}: missing required key 'v'")
                    continue
                val = entry["v"]
                if val is None:
                    errors.append(f"{fctx}.v: null is forbidden; use '?' (unknown), 'none' (confirmed none), or real value")
                    continue

                src = entry.get("src")
                status = entry.get("status")
                ttl = entry.get("ttl_days")
                if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0):
                    errors.append(f"{fctx}.ttl_days: must be positive int")
                    ttl = None

                if val == "?":
                    stat.unknown += 1
                    if entry.get("as_of") is not None:
                        parse_strict_date(entry.get("as_of"), f"{fctx}.as_of", errors)
                    if path.stem in {"health", "safety_net"}:
                        safety_blindspots.append(f"{path.stem}.{key}: 未知（涉及相关建议前须先确认）")
                    continue

                as_of = parse_strict_date(entry.get("as_of"), f"{fctx}.as_of", errors)
                if as_of and (latest_as_of is None or as_of > latest_as_of):
                    latest_as_of = as_of
                if src not in VALID_FACT_SOURCES:
                    errors.append(f"{fctx}.src: must be one of {sorted(VALID_FACT_SOURCES)}, got {src!r}")
                    continue

                # Strict AI-fact confirmation invariant:
                # src: ai MUST have status: pending; confirming requires changing src to me/doc!
                if src == "ai":
                    if status != "pending":
                        errors.append(
                            f"{fctx}: src='ai' must have status='pending' (to confirm, change src to 'me' or 'doc')"
                        )
                        continue
                else:
                    if status == "pending":
                        errors.append(f"{fctx}: src={src!r} should not be marked status='pending'")
                        continue
                    if status is not None and status != "confirmed":
                        errors.append(f"{fctx}.status: invalid status {status!r}")
                        continue

                if status == "pending":
                    stat.pending += 1
                    age_txt = f"{(today - as_of).days}d" if as_of else "?"
                    pending_ai_facts.append(f"{path.stem}.{key}={val}（待确认 {age_txt}）")
                    if as_of and (today - as_of).days > max_pending_days:
                        events.append(
                            Event(
                                eid=f"pending-ai-fact:{path.stem}.{key}:{as_of.isoformat()}",
                                window="PENDING",
                                rule="pending-ai-fact",
                                severity="med",
                                title=f"待确认AI事实：{path.stem}.{key}",
                                evidence=f"{rel} 中 AI 写入的 {key}={val} 已 pending 超 {max_pending_days} 天（写入日 {as_of.isoformat()}）",
                                action="周检查中核实后将 src 改为 me/doc（移除 status: pending），或删除",
                                due_date=today,
                                source_id=f"{path.stem}.{key}",
                            )
                        )
                    continue

                if as_of and ttl and (today - as_of).days > ttl:
                    age = (today - as_of).days
                    stat.stale += 1
                    stale_facts.append(f"{path.stem}.{key}（{age}d > {ttl}d）")
                    events.append(
                        Event(
                            eid=f"fact-stale:{path.stem}.{key}:{as_of.isoformat()}",
                            window="STALE",
                            rule="fact-stale",
                            severity="low",
                            title=f"事实过期：{path.stem}.{key}",
                            evidence=f"{rel} 中 {key} 上次确认于 {as_of.isoformat()}（{age} 天 > TTL {ttl} 天）",
                            action="周检查核实并刷新 as_of，或改为 ?",
                            due_date=today,
                            source_id=f"{path.stem}.{key}",
                        )
                    )
                else:
                    stat.confirmed += 1
                    unit = f" {entry['unit']}" if entry.get("unit") else ""
                    stat.confirmed_items.append((key, f"{val}{unit}"))

            if latest_as_of is not None:
                source_as_of[rel] = latest_as_of.isoformat()
            domain_stats.append(stat)

    # 6. Validate & scan decisions/*.md
    if not decisions_dir.exists():
        errors.append("decisions/: directory missing")
    else:
        dl_windows = sorted(set(rules["decision_deadline_windows"]), reverse=True)
        rv_lead = int(rules["decision_review_lead_days"])
        for path in sorted(decisions_dir.glob("*.md")):
            if path.name.startswith("_"):
                continue
            rel = path.relative_to(root).as_posix()
            if not SLUG_RE.fullmatch(path.stem):
                errors.append(f"{rel}: filename stem must match {SLUG_RE.pattern}")
                continue
            fm, _ = parse_frontmatter(path, rel, errors, required=True)
            if fm is None:
                continue
            check_unknown_keys(fm, DECISION_FM_KEYS, rel, errors)

            level = fm.get("level")
            status = fm.get("status")
            if level not in VALID_LEVELS:
                errors.append(f"{rel}: invalid/missing 'level' ({level!r})")
            if status not in VALID_DECISION_STATUS:
                errors.append(f"{rel}: invalid/missing 'status' ({status!r})")
            created = parse_strict_date(fm.get("created"), f"{rel}:created", errors)
            deadline = parse_strict_date(fm.get("deadline"), f"{rel}:deadline", errors, allow_none=True)
            review_due = parse_strict_date(
                fm.get("review_due"),
                f"{rel}:review_due",
                errors,
                allow_none=(status != "decided"),
            )

            # Validate downside/prediction sub-mappings if provided
            downside = fm.get("downside")
            if downside is not None:
                if not isinstance(downside, dict):
                    errors.append(f"{rel}:downside: must be a mapping")
                else:
                    check_unknown_keys(downside, DOWNSIDE_KEYS, f"{rel}:downside", errors)
                    if downside.get("exit_by") is not None:
                        parse_strict_date(downside.get("exit_by"), f"{rel}:downside.exit_by", errors)

            pred = fm.get("prediction")
            if pred is not None:
                if not isinstance(pred, dict):
                    errors.append(f"{rel}:prediction: must be a mapping")
                else:
                    check_unknown_keys(pred, PREDICTION_KEYS, f"{rel}:prediction", errors)

            # Hard decision gate ONLY at status == "decided" (drafts stay frictionless!)
            if status == "decided":
                if review_due and created and review_due <= created:
                    errors.append(f"{rel}:review_due ({review_due}) must be after created ({created})")
                if level in {"L2", "L3"}:
                    if is_placeholder(fm.get("option0")):
                        errors.append(f"{rel}: decided {level} must fill non-placeholder 'option0'")
                    if is_placeholder(fm.get("flip_if")):
                        errors.append(f"{rel}: decided {level} must fill non-placeholder 'flip_if'")
                    if not isinstance(downside, dict):
                        errors.append(f"{rel}: decided {level} must fill structured 'downside'")
                    else:
                        for dk in ("cash_low", "commitments", "affected", "reversal_cost"):
                            if is_placeholder(downside.get(dk)):
                                errors.append(f"{rel}:downside.{dk}: required non-placeholder value for decided {level}")
                        parse_strict_date(downside.get("exit_by"), f"{rel}:downside.exit_by", errors, allow_none=False)
                    if not isinstance(pred, dict) or is_placeholder(pred.get("claim")):
                        errors.append(f"{rel}: decided {level} must specify non-placeholder 'prediction.claim'")
                    else:
                        prob = pred.get("p")
                        if isinstance(prob, bool) or not isinstance(prob, (int, float)) or not (0.0 <= float(prob) <= 1.0):
                            errors.append(f"{rel}:prediction.p: must be number in [0, 1]")
                        if level == "L3":
                            rng = pred.get("range")
                            if (
                                not isinstance(rng, list)
                                or len(rng) != 2
                                or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in rng)
                                or float(rng[0]) > float(rng[1])
                            ):
                                errors.append(f"{rel}:prediction.range: decided L3 requires numeric [lo, hi] with lo <= hi")

            if status == "open":
                open_decisions.append((path.stem, str(level), deadline))
                if deadline:
                    calendar_items.append(
                        CalendarItem(
                            uid=f"lifeos-decision-deadline-{path.stem}@teddy88teddy",
                            title=f"[LifeOS 决策截止] {path.stem}",
                            due=deadline,
                            description=f"{rel} ({level})",
                            remind_days=sorted(set(dl_windows), reverse=True),
                        )
                    )
                    days_left = (deadline - today).days
                    occ_eid = f"decision-deadline:{path.stem}:{deadline.isoformat()}"
                    if days_left <= 0:
                        window = "T0" if days_left == 0 else "overdue"
                        tag = "今天截止" if days_left == 0 else f"已逾期 {-days_left} 天"
                        events.append(
                            Event(
                                eid=occ_eid,
                                window=window,
                                rule="decision-deadline",
                                severity="high",
                                title=f"决策截止：{path.stem}（{tag}）",
                                evidence=f"{rel} ({level}, status=open) 截止日 {deadline.isoformat()}",
                                action="完成核验并拍板，改 status 为 decided 并填 review_due",
                                due_date=deadline,
                                source_id=path.stem,
                            )
                        )
                    else:
                        matching = [w for w in dl_windows if days_left <= w]
                        if matching:
                            win = min(matching)
                            events.append(
                                Event(
                                    eid=occ_eid,
                                    window=f"T{win}",
                                    rule="decision-deadline",
                                    severity="high" if win <= 1 else "med",
                                    title=f"决策截止：{path.stem}（剩 {days_left} 天）",
                                    evidence=f"{rel} ({level}, status=open) 截止日 {deadline.isoformat()}（窗口 T-{win}）",
                                    action="完成核验并拍板，改 status 为 decided 并填 review_due",
                                    due_date=deadline,
                                    source_id=path.stem,
                                )
                            )

            if status == "decided" and review_due:
                calendar_items.append(
                    CalendarItem(
                        uid=f"lifeos-decision-review-{path.stem}@teddy88teddy",
                        title=f"[LifeOS 决策复盘] {path.stem}",
                        due=review_due,
                        description=f"{rel} ({level}) 到期复盘：拆分过程与结果",
                        remind_days=[rv_lead, 1] if rv_lead > 1 else [1],
                    )
                )
                days_left = (review_due - today).days
                if days_left <= rv_lead:
                    window = "DUE" if days_left <= 0 else f"T{rv_lead}"
                    tag = f"已到期 {-days_left} 天" if days_left < 0 else ("今天到期" if days_left == 0 else f"剩 {days_left} 天")
                    events.append(
                        Event(
                            eid=f"decision-review:{path.stem}:{review_due.isoformat()}",
                            window=window,
                            rule="decision-review",
                            severity="high" if days_left <= 0 else "med",
                            title=f"决策复盘：{path.stem}（{tag}）",
                            evidence=f"{rel} ({level}) 复盘日 {review_due.isoformat()}",
                            action="对照当时预测与翻转条件，分开打分过程与结果，并改 status 为 reviewed",
                            due_date=review_due,
                            source_id=path.stem,
                        )
                    )

    # 7. Validate & scan log/YYYY-MM.md — bucket strictly by ISO calendar week!
    obs_by_week: dict[tuple[int, int], tuple[dt.date, int]] = {}
    log_dir = root / "log"
    if log_dir.exists():
        for path in sorted(log_dir.glob("*.md")):
            rel = path.relative_to(root).as_posix()
            if not re.fullmatch(r"\d{4}-\d{2}\.md", path.name):
                errors.append(f"{rel}: log filename must match YYYY-MM.md")
                continue
            fm, body = parse_frontmatter(path, rel, errors, required=True)
            if fm is not None:
                check_unknown_keys(fm, {"month"}, rel, errors)
            clean_body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
            current_date: dt.date | None = None
            for line_no, line in enumerate(clean_body.splitlines(), start=1):
                hm = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})\b", line)
                if hm:
                    current_date = parse_strict_date(hm.group(1), f"{rel}:L{line_no}", errors)
                    if current_date and current_date > today:
                        errors.append(f"{rel}:L{line_no}: future observation date {current_date.isoformat()} > {today.isoformat()}")
                        current_date = None
                    continue
                em = re.match(r"^\s*[-*]?\s*weekly_energy\s*:\s*(\S+)", line)
                if em:
                    raw_score = em.group(1)
                    if current_date is None:
                        errors.append(f"{rel}:L{line_no}: weekly_energy must appear under a valid '## YYYY-MM-DD' heading")
                        continue
                    try:
                        score = int(raw_score)
                    except ValueError:
                        errors.append(f"{rel}:L{line_no}: weekly_energy must be int 1..5")
                        continue
                    if not (1 <= score <= 5):
                        errors.append(f"{rel}:L{line_no}: weekly_energy out of range 1..5 ({score})")
                        continue
                    iso_key = (current_date.isocalendar().year, current_date.isocalendar().week)
                    prev = obs_by_week.get(iso_key)
                    if prev is None or current_date >= prev[0]:
                        obs_by_week[iso_key] = (current_date, score)

    sorted_weeks = sorted(obs_by_week.items(), key=lambda kv: kv[0])
    e_thresh = int(rules["energy_low_threshold"])
    e_weeks = int(rules["energy_low_consecutive_weeks"])
    e_max_gap = int(rules["energy_max_gap_days"])

    if len(sorted_weeks) < e_weeks:
        energy_status = "insufficient_data"
        energy_detail = f"数据不足（当前仅 {len(sorted_weeks)} 个自然周观测，至少需要 {e_weeks} 个连续自然周）"
    else:
        recent = sorted_weeks[-e_weeks:]
        latest_date, latest_score = recent[-1][1]
        is_consecutive = all(
            dt.date.fromisocalendar(recent[i][0][0], recent[i][0][1], 1) + dt.timedelta(days=7)
            == dt.date.fromisocalendar(recent[i + 1][0][0], recent[i + 1][0][1], 1)
            for i in range(len(recent) - 1)
        )
        if (today - latest_date).days > e_max_gap or not is_consecutive:
            energy_status = "insufficient_data"
            energy_detail = f"近期观测非连续自然周或已超期（最新 {latest_date.isoformat()}）"
        elif all(score <= e_thresh for _, (_, score) in recent):
            energy_status = "low"
            scores_str = ", ".join(f"{y}-W{w:02d}:{s}/5" for (y, w), (_, s) in recent)
            ly, lw = recent[-1][0]
            energy_detail = f"连续 {e_weeks} 个自然周精力 ≤{e_thresh}（{scores_str}）"
            events.append(
                Event(
                    eid=f"energy-low:weekly:{ly}-W{lw:02d}",
                    window="ALERT",
                    rule="energy-low",
                    severity="med",
                    title="精力连续偏低",
                    evidence=energy_detail,
                    action="建议未来 7 天进入恢复模式；L2 以上重大决策推迟",
                    due_date=today,
                    source_id=f"{ly}-W{lw:02d}",
                )
            )
        else:
            energy_status = "ok"
            energy_detail = f"最新周精力 {latest_score}/5（{latest_date.isoformat()}）"

    # 8. Accidental secret scan across all .md / .yml / .yaml files
    for p in sorted(root.rglob("*")):
        if ".git" in p.parts or not p.is_file():
            continue
        if p.suffix.lower() not in {".md", ".yml", ".yaml"}:
            continue
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        for label, pat in SECRET_PATTERNS:
            if pat.search(text):
                errors.append(f"{rel}: potential secret detected ({label}) — NEVER commit keys")

    events.sort(key=lambda e: (-SEVERITY_RANK[e.severity], e.due_date or dt.date.max, e.eid))
    result = ScanResult(
        errors=errors,
        events=events,
        calendar_items=calendar_items,
        domain_stats=domain_stats,
        safety_blindspots=safety_blindspots,
        energy_status=energy_status,
        energy_detail=energy_detail,
        open_decisions=open_decisions,
        pending_ai_facts=pending_ai_facts,
        stale_facts=stale_facts,
        source_as_of=source_as_of,
        past_dates_items=past_dates_items,
    )

    # 9. Validate Gemini context_pack mandatory sections budget right inside scan_repo
    max_chars = int(config["mobile_ai"].get("context_pack_max_chars", 9000))
    if not errors:
        try:
            build_context_pack_md(root, result, [], max_chars, now_local)
        except ValueError as exc:
            result.errors.append(str(exc))

    return config, result


# ---------------------------------------------------------------------------
# Issue-Body Outbox State Machine (1 occurrence = 1 locked bot Issue)
# ---------------------------------------------------------------------------


def parse_issue_state(body: str) -> dict[str, Any] | None:
    m = STATE_RE.search(body or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def update_issue_state_marker(body: str, state: dict[str, Any]) -> str:
    marker = f"<!-- lifeos:{json.dumps(state, ensure_ascii=False, separators=(',', ':'))} -->"
    if STATE_RE.search(body or ""):
        return STATE_RE.sub(marker, body, count=1)
    return f"{marker}\n{body or ''}"


def parse_snooze_until(body: str, comment_time: dt.datetime, tz: ZoneInfo) -> dt.datetime | None:
    m = SNOOZE_RE.search(body or "")
    if not m:
        return None
    token = m.group(1).lower()
    if token.endswith("d"):
        days = int(token[:-1])
        if not (1 <= days <= 365):
            return None
        return comment_time.astimezone(tz) + dt.timedelta(days=days)
    try:
        target = dt.date.fromisoformat(token)
        return dt.datetime(target.year, target.month, target.day, 8, 0, tzinfo=tz)
    except ValueError:
        return None


def extract_capture_due_events(
    owner_captures: list[dict[str, Any]], today: dt.date
) -> list[Event]:
    """Parse optional `### 截止日期` from owner-authored capture issues for urgent T1/T0 alerts."""
    out: list[Event] = []
    for iss in owner_captures:
        m = CAPTURE_DUE_RE.search(iss.get("body") or "")
        if not m:
            continue
        try:
            due = dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        days_left = (due - today).days
        if days_left <= 1:
            num = int(iss["number"])
            window = "overdue" if days_left < 0 else ("T0" if days_left == 0 else "T1")
            tag = f"已逾期 {-days_left} 天" if days_left < 0 else ("今天到期" if days_left == 0 else "明天到期")
            raw_title = re.sub(r"^\[capture\]\s*", "", str(iss.get("title") or f"Capture #{num}"), flags=re.I).strip()
            out.append(
                Event(
                    eid=f"capture-due:issue-{num}:{due.isoformat()}",
                    window=window,
                    rule="capture-due",
                    severity="high" if days_left <= 0 else "med",
                    title=f"速记期限：{raw_title}（{tag}）",
                    evidence=f"手机 Capture #{num} 标注截止日 {due.isoformat()}",
                    action="立即处理或迁入 system/dates.yml 并关闭原 Capture",
                    due_date=due,
                    source_id=f"capture-{num}",
                )
            )
    return out


def format_issue_title(ev: Event) -> str:
    return f"[{ev.window}] {ev.title}"


def format_issue_body(ev: Event, state: dict[str, Any]) -> str:
    marker = f"<!-- lifeos:{json.dumps(state, ensure_ascii=False, separators=(',', ':'))} -->"
    return (
        f"{marker}\n"
        f"**规则**：`{ev.rule}` · **级别**：`{ev.severity}` · **当前窗口**：`{ev.window}`\n\n"
        f"### 证据\n{ev.evidence}\n\n"
        f"### 建议动作\n{ev.action}\n\n"
        "---\n"
        "**快捷处置（仅认仓库主人 `teddy88teddy`）**：\n"
        "- 已处理：直接 **Close issue**（周期性事项请顺手更新 `system/dates.yml` 下一期）\n"
        "- 忽略：选择 **Close as not planned**\n"
        "- 延后重复提醒：评论 `/snooze 3d` 或 `/snooze YYYY-MM-DD`（进入新窗口如 T-1/T0 仍会准时提醒）\n"
    )


def format_serverchan_title(items: list[dict[str, Any]]) -> str:
    """High-density <=32-char title because WeChat ServerChan card displays ONLY title."""
    if not items:
        return "LifeOS 提醒"
    top = sorted(items, key=lambda x: -SEVERITY_RANK.get(x["severity"], 1))[0]
    sev_zh = SEVERITY_ZH.get(top["severity"], "中")
    win = top.get("window", "")
    win_tag = f" {win}" if win and win not in {"ALERT", "PENDING"} else ""
    short_name = re.sub(r"（[^）]*）", "", top["title"]).strip()
    extra = f"·另{len(items) - 1}项" if len(items) > 1 else ""
    prefix = f"[{sev_zh}]"
    max_name = max(4, 32 - len(prefix) - len(win_tag) - len(extra))
    if len(short_name) > max_name:
        short_name = short_name[: max_name - 1] + "…"
    return f"{prefix}{short_name}{win_tag}{extra}"[:32]


def sync_and_plan_delivery(
    gh: Any,
    scan: ScanResult,
    config: dict[str, Any],
    *,
    now_local: dt.datetime,
    tz: ZoneInfo,
    mode: str = "normal",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Synchronizes bot Issues & computes crash-safe digest plan WITHOUT sending push yet.

    Security invariants:
      - Event Issues & heartbeat Issue trusted ONLY when `author in BOT_AUTHORS`.
      - Capture Issues & `/snooze` comments trusted ONLY when `author == owner`.
      - Event Issues are locked immediately upon creation (`lock_issue`).
    """
    owner = config["owner"]
    esc_cfg = config["escalation"]
    high_hours = int(esc_cfg.get("high_hours", 72))
    ignored_threshold = int(esc_cfg.get("ignored_rule_threshold", 3))

    if not dry_run:
        gh.ensure_labels(DEFAULT_LABELS)

    # 1. Paginated fetch of all relevant issues; filter trusted authors BEFORE fetching comments!
    raw_event_issues = gh.list_issues("lifeos-event", state="all")
    raw_capture_issues = gh.list_issues("capture", state="open")
    raw_heartbeat_issues = gh.list_issues("lifeos-heartbeat", state="all")

    owner_captures = [
        iss for iss in raw_capture_issues if (iss.get("user") or {}).get("login", "") == owner
    ]

    # Add urgent mid-week capture due events into the event stream
    capture_events = extract_capture_due_events(owner_captures, now_local.date())
    all_events = sorted(
        [*scan.events, *capture_events],
        key=lambda e: (-SEVERITY_RANK[e.severity], e.due_date or dt.date.max, e.eid),
    )

    eid_to_issue: dict[str, dict[str, Any]] = {}
    ignored_by_rule: dict[str, int] = {}
    closed_eids: set[str] = set()

    for iss in raw_event_issues:
        author = (iss.get("user") or {}).get("login", "")
        if author not in BOT_AUTHORS:
            continue
        state_obj = parse_issue_state(iss.get("body") or "")
        if not state_obj or not isinstance(state_obj.get("eid"), str):
            continue
        eid = state_obj["eid"]
        eid_to_issue[eid] = iss
        if iss.get("state") == "closed":
            closed_eids.add(eid)
            if iss.get("state_reason") == "not_planned":
                rule_prefix = eid.split(":", 1)[0]
                ignored_by_rule[rule_prefix] = ignored_by_rule.get(rule_prefix, 0) + 1

    # Closed-issue-with-unrolled-past-date hygiene check
    unrolled_closed_dates = [
        f"`{item_id}`（原到期 {due.isoformat()}，Issue 已关闭但 dates.yml 未滚期或移除）"
        for occ_eid, item_id, due in scan.past_dates_items
        if occ_eid in closed_eids
    ]

    # Locate or create persistent bot heartbeat issue
    hb_issue: dict[str, Any] | None = None
    for iss in raw_heartbeat_issues:
        if (iss.get("user") or {}).get("login", "") in BOT_AUTHORS and iss.get("state") == "open":
            hb_issue = iss
            break
    if hb_issue is None and not dry_run:
        hb_state = {"kind": "heartbeat", "last_weekly": ""}
        hb_body = f"<!-- lifeos:{json.dumps(hb_state, separators=(',', ':'))} -->\nLifeOS 每周心跳状态锚点（由机器人自动维护，请勿关闭）。\n"
        hb_issue = gh.create_issue(title="LifeOS 心跳", body=hb_body, labels=["lifeos-heartbeat"])
        gh.lock_issue(int(hb_issue["number"]))

    # 2. Separate non-low events (Issue channel) vs low events (Weekly digest / radar hygiene)
    low_events = [e for e in all_events if e.severity == "low"]
    issue_events = [e for e in all_events if e.severity in {"high", "med"}]

    pending_deliveries: list[dict[str, Any]] = []
    active_radar_items: list[dict[str, Any]] = []
    snoozed_radar_items: list[dict[str, Any]] = []
    open_high_count = 0

    for ev in issue_events:
        iss = eid_to_issue.get(ev.eid)
        if iss is None:
            init_state = {"eid": ev.eid, "sent": [], "last_sent": None, "esc": False}
            if dry_run:
                iss = {
                    "number": 0,
                    "state": "open",
                    "html_url": "(dry-run)",
                    "title": format_issue_title(ev),
                    "body": format_issue_body(ev, init_state),
                    "labels": [{"name": "lifeos-event"}, {"name": f"sev:{ev.severity}"}],
                    "comments": 0,
                }
            else:
                iss = gh.create_issue(
                    title=format_issue_title(ev),
                    body=format_issue_body(ev, init_state),
                    labels=["lifeos-event", f"sev:{ev.severity}"],
                )
                gh.lock_issue(int(iss["number"]))
            eid_to_issue[ev.eid] = iss

        if iss.get("state") == "closed":
            continue  # Closed issue NEVER shown as active or pushed!

        if ev.severity == "high":
            open_high_count += 1

        issue_num = int(iss["number"])
        issue_url = str(iss.get("html_url") or f"#{issue_num}")
        state_obj = parse_issue_state(iss.get("body") or "") or {
            "eid": ev.eid,
            "sent": [],
            "last_sent": None,
            "esc": False,
        }
        sent_list = list(state_obj.get("sent") or [])
        last_sent_raw = state_obj.get("last_sent")
        last_sent_dt: dt.datetime | None = None
        if isinstance(last_sent_raw, str) and last_sent_raw:
            try:
                last_sent_dt = dt.datetime.fromisoformat(last_sent_raw.replace("Z", "+00:00")).astimezone(tz)
            except ValueError:
                last_sent_dt = None
        already_esc = bool(state_obj.get("esc", False))

        # Update Issue title if window progressed (e.g. T7 -> T1 -> T0)
        desired_title = format_issue_title(ev)
        if not dry_run and issue_num > 0 and iss.get("title") != desired_title:
            gh.update_issue(issue_num, title=desired_title)
            iss["title"] = desired_title

        # Fetch ALL comments (paginated) ONLY for trusted open bot event issues
        comments = gh.list_comments(issue_num) if (issue_num > 0 and int(iss.get("comments", 0)) > 0) else []
        latest_snooze_until: dt.datetime | None = None
        for cm in comments:
            if (cm.get("user") or {}).get("login", "") != owner:
                continue  # STRICT: ignore non-owner comments
            c_raw = cm.get("created_at", "")
            try:
                c_dt = dt.datetime.fromisoformat(c_raw.replace("Z", "+00:00")).astimezone(tz)
            except ValueError:
                continue
            # Only snooze comments AFTER last_sent are valid (new pushes invalidate old snoozes!)
            if last_sent_dt is not None and c_dt <= last_sent_dt:
                continue
            until = parse_snooze_until(cm.get("body") or "", c_dt, tz)
            if until is not None:
                latest_snooze_until = until

        reason: str | None = None
        new_sent = list(sent_list)
        new_esc = already_esc
        add_esc_label = False

        if ev.window not in sent_list:
            # Rule: New window NEVER blocked by snooze!
            reason = "new_window"
            new_sent.append(ev.window)
        elif latest_snooze_until is not None:
            if now_local < latest_snooze_until:
                snoozed_radar_items.append(
                    {
                        "eid": ev.eid,
                        "window": ev.window,
                        "severity": ev.severity,
                        "title": ev.title,
                        "url": issue_url,
                        "until": latest_snooze_until.strftime("%Y-%m-%d %H:%M"),
                    }
                )
                continue
            # Snooze expired -> resurface once (updating last_sent will obsolete this snooze comment!)
            reason = "snooze_expired"
        elif (
            ev.severity == "high"
            and not already_esc
            and last_sent_dt is not None
            and (now_local - last_sent_dt) >= dt.timedelta(hours=high_hours)
        ):
            reason = "escalate_72h"
            new_esc = True
            add_esc_label = True

        active_radar_items.append(
            {
                "eid": ev.eid,
                "window": ev.window,
                "severity": ev.severity,
                "title": ev.title,
                "evidence": ev.evidence,
                "action": ev.action,
                "url": issue_url,
            }
        )

        if reason is not None:
            updated_state = {
                "eid": ev.eid,
                "sent": new_sent,
                "last_sent": now_local.isoformat(timespec="minutes"),
                "esc": new_esc,
            }
            pending_deliveries.append(
                {
                    "issue_number": issue_num,
                    "eid": ev.eid,
                    "window": ev.window,
                    "severity": ev.severity,
                    "title": ev.title,
                    "reason": reason,
                    "url": issue_url,
                    "new_state": updated_state,
                    "add_escalated_label": add_esc_label,
                }
            )

    noisy_rules = [
        f"规则 `{rule}` 累计被忽略 {cnt} 次（≥{ignored_threshold}），建议调阈值或删除"
        for rule, cnt in sorted(ignored_by_rule.items())
        if cnt >= ignored_threshold
    ]

    # 3. Determine Sunday weekly heartbeat eligibility (at most once per ISO week!)
    iso_week_key = f"{now_local.isocalendar().year}-W{now_local.isocalendar().week:02d}"
    hb_state = parse_issue_state((hb_issue or {}).get("body") or "") or {"kind": "heartbeat", "last_weekly": ""}
    is_sunday = now_local.weekday() == 6 and bool(config["notify"].get("sunday_heartbeat", True))
    need_weekly = is_sunday and hb_state.get("last_weekly") != iso_week_key

    # 4. Build final notification payload (0 or 1 message)
    should_send = False
    push_title = ""
    push_desp = ""
    hb_patch: dict[str, Any] | None = None

    if mode == "test-push":
        should_send = True
        push_title = f"LifeOS测试·{now_local.strftime('%m-%d %H:%M')}"
        push_desp = (
            f"手动测试推送成功（北京时间 `{now_local.isoformat(timespec='minutes')}`）。\n\n"
            f"- 当前活跃高优：{open_high_count} 项\n"
            f"- 雷达链接：https://github.com/{config['repo']}/blob/out/radar.md\n"
        )
    elif mode == "simulate-send-failure":
        should_send = True
        push_title = "LifeOS 模拟发送失败"
        push_desp = "simulate-send-failure"
    elif pending_deliveries or need_weekly:
        should_send = True
        push_lines = []
        for item in pending_deliveries:
            tag = {
                "new_window": f"[{SEVERITY_ZH[item['severity']]}|{item['window']}]",
                "snooze_expired": "[延后到期]",
                "escalate_72h": "[72h升级]",
            }[item["reason"]]
            push_lines.append(f"- **{tag}** {item['title']} → [处置]({item['url']})")

        if need_weekly:
            if open_high_count == 0 and not pending_deliveries and not scan.errors:
                push_title = f"LifeOS周报({iso_week_key})·一切正常"
            elif pending_deliveries:
                push_title = format_serverchan_title(pending_deliveries)
            else:
                push_title = f"LifeOS周报·{open_high_count}项高优待处理"

            sections = []
            if push_lines:
                sections.extend(["### 今日待处置提醒", *push_lines, ""])
            sections.extend(
                [
                    f"### 周日 5 分钟检查（{iso_week_key}）",
                    f"- 未关闭高优事件：{open_high_count} 项",
                    f"- 手机待整理 Capture：{len(owner_captures)} 条",
                    f"- 待确认 AI 事实：{len(scan.pending_ai_facts)} 条",
                    f"- 精力观测：{scan.energy_detail}",
                ]
            )
            if low_events:
                sections.extend(["", "### 低优先级提醒（TTL 过期等）", *(f"- {e.title}" for e in low_events[:8])])
            if unrolled_closed_dates:
                sections.extend(["", "### 日期卫生提醒", *(f"- {u}" for u in unrolled_closed_dates)])
            if noisy_rules:
                sections.extend(["", "### 告警减噪建议", *(f"- {r}" for r in noisy_rules)])
            sections.extend(["", f"完整雷达：https://github.com/{config['repo']}/blob/out/radar.md"])
            push_desp = "\n".join(sections)
            if hb_issue and int(hb_issue.get("number", 0)) > 0:
                hb_patch = {
                    "issue_number": int(hb_issue["number"]),
                    "new_state": {"kind": "heartbeat", "last_weekly": iso_week_key},
                }
        else:
            push_title = format_serverchan_title(pending_deliveries)
            push_desp = "\n".join(
                [
                    "### 今日待处置事件",
                    *push_lines,
                    "",
                    "操作：点链接直接 Close=已处理 / not planned=忽略 / 评论 `/snooze 3d` 延后。",
                ]
            )

    return {
        "mode": mode,
        "repo": config["repo"],
        "generated_at": now_local.isoformat(timespec="minutes"),
        "should_send": should_send,
        "title": push_title,
        "desp": push_desp,
        "issue_patches": [
            {
                "issue_number": p["issue_number"],
                "new_state": p["new_state"],
                "add_escalated_label": p["add_escalated_label"],
            }
            for p in pending_deliveries
            if p["issue_number"] > 0
        ],
        "heartbeat_patch": hb_patch,
        "active_radar_items": active_radar_items,
        "snoozed_radar_items": snoozed_radar_items,
        "low_events": low_events,
        "owner_captures": owner_captures,
        "noisy_rules": noisy_rules,
        "unrolled_closed_dates": unrolled_closed_dates,
    }


# ---------------------------------------------------------------------------
# GitHub REST Client (Full Link-header pagination, labels, issue locking)
# ---------------------------------------------------------------------------


class GitHubClient:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.token = token
        self.base = f"https://api.github.com/repos/{repo}"

    def _req_with_headers(
        self, method: str, url_or_path: str, payload: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, str]]:
        url = url_or_path if url_or_path.startswith("https://") else f"{self.base}{url_or_path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Teddy-LifeOS-v3.1",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return (json.loads(raw) if raw else None), headers

    def _paginate(self, path: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        next_url: str | None = path
        while next_url:
            page, headers = self._req_with_headers("GET", next_url)
            if isinstance(page, list):
                results.extend(page)
            link = headers.get("link", "")
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            next_url = m.group(1) if m else None
        return results

    def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        existing = {lb.get("name", "") for lb in self._paginate("/labels?per_page=100")}
        for name, (color, desc) in labels.items():
            if name not in existing:
                try:
                    self._req_with_headers("POST", "/labels", {"name": name, "color": color, "description": desc})
                except urllib.error.HTTPError as exc:
                    if exc.code != 422:  # already exists race
                        raise

    def list_issues(self, label: str, state: str = "all") -> list[dict[str, Any]]:
        q = urllib.parse.urlencode({"labels": label, "state": state, "per_page": 100})
        return [i for i in self._paginate(f"/issues?{q}") if "pull_request" not in i]

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/issues/{issue_number}/comments?per_page=100")

    def get_issue(self, issue_number: int) -> dict[str, Any]:
        data, _ = self._req_with_headers("GET", f"/issues/{issue_number}")
        return data

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        data, _ = self._req_with_headers("POST", "/issues", {"title": title, "body": body, "labels": labels})
        return data

    def update_issue(self, issue_number: int, *, title: str | None = None, body: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        data, _ = self._req_with_headers("PATCH", f"/issues/{issue_number}", payload)
        return data

    def add_labels(self, issue_number: int, labels: list[str]) -> Any:
        data, _ = self._req_with_headers("POST", f"/issues/{issue_number}/labels", {"labels": labels})
        return data

    def lock_issue(self, issue_number: int) -> None:
        try:
            self._req_with_headers("PUT", f"/issues/{issue_number}/lock", {"lock_reason": "off-topic"})
        except urllib.error.HTTPError as exc:
            if exc.code not in {403, 404, 422}:
                raise


# ---------------------------------------------------------------------------
# ServerChan Delivery (strict code==0 verification, sanitized public logs)
# ---------------------------------------------------------------------------


def build_serverchan_url(sendkey: str) -> str:
    key = sendkey.strip()
    m = re.match(r"^sctp(\d+)t", key)
    if m:
        return f"https://{m.group(1)}.push.ft07.com/send/{key}.send"
    return f"https://sctapi.ftqq.com/{key}.send"


def send_serverchan(sendkey: str, title: str, desp: str, *, opener: Any = urllib.request.urlopen) -> int:
    """Sends a ServerChan message and strictly verifies JSON `code == 0`.

    Returns `0` on verified acceptance; raises DeliveryError on any HTTP/timeout/parse/code failure.
    Never returns or logs raw response fields (such as pushid/readkey).
    """
    if not sendkey or not sendkey.strip():
        raise DeliveryError("SERVERCHAN_SENDKEY is empty")
    url = build_serverchan_url(sendkey)
    clean_title = re.sub(r"[\r\n]+", " ", title).strip()[:32]
    encoded = urllib.parse.urlencode({"title": clean_title, "desp": desp}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Teddy-LifeOS-v3.1"},
    )
    try:
        with opener(req, timeout=20) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8", errors="replace").strip()
    except Exception as exc:
        raise DeliveryError(f"ServerChan request failed ({type(exc).__name__})") from exc

    if status != 200:
        raise DeliveryError(f"ServerChan HTTP status {status}")
    if not raw:
        raise DeliveryError("ServerChan returned empty response body")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeliveryError("ServerChan returned non-JSON response") from exc
    if not isinstance(data, dict) or "code" not in data:
        raise DeliveryError("ServerChan response missing 'code' field")
    code = data.get("code")
    if code != 0:
        raise DeliveryError(f"ServerChan rejected message (code={code})")
    return 0


def ping_healthcheck(url: str) -> None:
    req = urllib.request.Request(url.strip(), method="GET", headers={"User-Agent": "Teddy-LifeOS-v3.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        if getattr(resp, "status", 200) >= 400:
            raise DeliveryError(f"Healthcheck HTTP {resp.status}")


# ---------------------------------------------------------------------------
# Artifact Builders (`radar.md`, RFC 5545 `calendar.ics`, Gemini `context_pack.md`)
# ---------------------------------------------------------------------------


def build_radar_md(
    scan: ScanResult,
    active_items: list[dict[str, Any]],
    snoozed_items: list[dict[str, Any]],
    low_events: list[Event],
    owner_captures: list[dict[str, Any]],
    noisy_rules: list[str],
    unrolled_closed_dates: list[str],
    now_local: dt.datetime,
) -> str:
    lines = [
        "# LifeOS 主动雷达（自动生成，请勿手改）",
        "",
        f"- **最后成功巡检**：`{now_local.isoformat(timespec='minutes')}`（北京时间）",
        f"- **精力信号状态**：{scan.energy_detail}",
        f"- **待整理手机 Capture（owner）**：{len(owner_captures)} 条",
        "",
        "## 1. 当前活跃事件（仅 open 且未延后）",
    ]
    if active_items:
        for item in active_items:
            link = f" · [Issue]({item['url']})" if item.get("url") else ""
            lines.append(f"- **[{item['severity'].upper()}|{item['window']}]** {item['title']} (`{item['eid']}`){link}")
            lines.append(f"  - 证据：{item['evidence']}")
            lines.append(f"  - 建议：{item['action']}")
    else:
        lines.append("- 当前无待处置活跃事件。")

    if snoozed_items:
        lines.extend(["", "## 2. 已延后（Snoozed）"])
        for s in snoozed_items:
            lines.append(f"- **[{s['severity'].upper()}]** {s['title']} · 延后至 `{s['until']}` · [Issue]({s['url']})")

    lines.extend(["", "## 3. 低优提醒与数据健康"])
    if low_events:
        for le in low_events:
            lines.append(f"- **[LOW]** {le.title}：{le.evidence}")
    if scan.pending_ai_facts:
        lines.append(f"- **待确认 AI 事实（禁止作 L2+ 硬依据）**：{'; '.join(scan.pending_ai_facts)}")
    if unrolled_closed_dates:
        lines.append(f"- **已关闭但未滚期的日期**：{'; '.join(unrolled_closed_dates)}")
    if noisy_rules:
        for nr in noisy_rules:
            lines.append(f"- **告警减噪建议**：{nr}")
    if not (low_events or scan.pending_ai_facts or unrolled_closed_dates or noisy_rules):
        lines.append("- 无过期事实、无超期 pending 事实、无未滚期记录。")

    lines.extend(["", "| 领域 | 已确认 | 待确认(AI) | 过期 | 未知(`?`) |", "| :--- | :---: | :---: | :---: | :---: |"])
    for st in scan.domain_stats:
        lines.append(f"| `{st.domain}` | {st.confirmed} | {st.pending} | {st.stale} | {st.unknown} |")

    if scan.open_decisions:
        lines.extend(["", "## 4. 未拍板决策（由 decisions/*.md 自动生成）"])
        for slug, level, dl in scan.open_decisions:
            dl_txt = dl.isoformat() if dl else "无硬截止"
            lines.append(f"- `{slug}` ({level}) · 截止：`{dl_txt}`")

    return "\n".join(lines) + "\n"


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold_ics_line(line: str, max_octets: int = 75) -> str:
    """RFC 5545 §3.1 folding at <=75 UTF-8 octets without slicing multibyte characters."""
    if len(line.encode("utf-8")) <= max_octets:
        return line
    chunks: list[str] = []
    cur: list[str] = []
    cur_bytes = 0
    limit = max_octets
    for ch in line:
        ch_bytes = len(ch.encode("utf-8"))
        if cur_bytes + ch_bytes > limit and cur:
            chunks.append("".join(cur))
            cur = [ch]
            cur_bytes = ch_bytes
            limit = max_octets - 1  # account for leading continuation space
        else:
            cur.append(ch)
            cur_bytes += ch_bytes
    if cur:
        chunks.append("".join(cur))
    return "\r\n ".join(chunks)


def build_calendar_ics(items: list[CalendarItem], now_utc: dt.datetime) -> str:
    dtstamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    raw_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Teddy LifeOS//v3.1//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Teddy LifeOS Deadlines",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for it in sorted(items, key=lambda x: (x.due, x.uid)):
        start = it.due.strftime("%Y%m%d")
        end = (it.due + dt.timedelta(days=1)).strftime("%Y%m%d")
        raw_lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{it.uid}",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART;VALUE=DATE:{start}",
                f"DTEND;VALUE=DATE:{end}",
                f"SUMMARY:{_ics_escape(it.title)}",
                f"DESCRIPTION:{_ics_escape(it.description)}",
            ]
        )
        # Keep ALL configured remind alarms (including T-1!)
        for rd in sorted(set(it.remind_days), reverse=True):
            trigger = "-PT0S" if rd == 0 else f"-P{rd}D"
            raw_lines.extend(
                [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    f"TRIGGER:{trigger}",
                    f"DESCRIPTION:{_ics_escape(it.title)}",
                    "END:VALARM",
                ]
            )
        raw_lines.append("END:VEVENT")
    raw_lines.append("END:VCALENDAR")
    folded = [fold_ics_line(ln) for ln in raw_lines]
    return "\r\n".join(folded) + "\r\n"


def _extract_compass_parts(compass_body: str) -> tuple[str, str]:
    """Splits compass.md into (hard_redlines_section, rest_of_compass)."""
    m = re.search(r"(##\s*2\.\s*硬红线.*?)(?=\n##\s*3\.|\Z)", compass_body, flags=re.DOTALL)
    if not m:
        return "## 2. 硬红线\n- 未定义", compass_body.strip()
    redlines = m.group(1).strip()
    rest = (compass_body[: m.start()] + compass_body[m.end() :]).strip()
    return redlines, rest


def build_context_pack_md(
    root: Path,
    scan: ScanResult,
    active_items: list[dict[str, Any]],
    max_chars: int,
    now_local: dt.datetime,
) -> str:
    """Whole-section priority-budgeted snapshot for mobile Gemini Skill (Google Doc).

    Sections 1–4 are mandatory (exceeding `max_chars` raises ValueError instead of silently truncating).
    Section 5 trims from the tail item-by-item; Sections 6 and 7 drop whole-section if over budget.
    """
    as_of_summary = ", ".join(f"{k}={v}" for k, v in sorted(scan.source_as_of.items())) or "无"
    sec1 = "\n".join(
        [
            f"# Teddy LifeOS — Gemini Skill 上下文快照",
            f"- `generated_at`: `{now_local.isoformat(timespec='minutes')}`（北京时间）",
            f"- 来源 `as_of`: {as_of_summary}",
            "- **过期提示指令**：如果今天距离 `generated_at` 已超过 8 天，回答前先用一行提醒 Teddy 刷新 Google Doc。",
            "- **输出契约**：L0 日常小事直接给 1 个可执行答案（≤3 行，不例行追问档案）；遇自动续费/不可逆条款/大额支出或红线升级 L1–L3，写硬闸门与翻转条件，并提示转回桌面会话落盘。",
        ]
    )

    compass_body = ""
    if (root / "compass.md").exists():
        _, compass_body = parse_frontmatter(root / "compass.md", "compass.md", [], required=False)
    redlines_text, compass_rest = _extract_compass_parts(compass_body)

    blind_lines = [f"- {b}" for b in scan.safety_blindspots] or ["- 无未知医疗/安全网盲区。"]
    sec2 = "\n".join(["## 硬红线与安全盲区（不可忽略）", redlines_text, "", "### 未知安全盲区（`?`）", *blind_lines])

    now_body = ""
    if (root / "now.md").exists():
        _, now_body = parse_frontmatter(root / "now.md", "now.md", [], required=False)
    sec3 = f"## 当前实时约束（now.md, as_of={scan.source_as_of.get('now.md', '?')}）\n{now_body.strip()}"

    daily_body = ""
    if (root / "domains/daily.md").exists():
        _, daily_body = parse_frontmatter(root / "domains/daily.md", "domains/daily.md", [], required=False)
    sec4 = f"## 日常默认规则与否决清单（daily.md）\n{daily_body.strip()}"

    mandatory = "\n\n".join([sec1, sec2, sec3, sec4]).strip() + "\n"
    if len(mandatory) > max_chars:
        raise ValueError(
            f"context_pack mandatory sections (1-4) are {len(mandatory)} chars > limit {max_chars}; "
            "trim now.md / daily.md / compass redlines instead of silent truncation"
        )

    parts = [sec1, sec2, sec3, sec4]
    current_len = len(mandatory)

    # Section 5: open high/med events (trim from tail item-by-item if needed)
    if active_items:
        ev_header = "## 当前未关闭活跃事件"
        ev_lines: list[str] = []
        for it in active_items:
            candidate_line = f"- [{it['severity'].upper()}|{it['window']}] {it['title']}"
            trial = "\n\n".join([*parts, "\n".join([ev_header, *ev_lines, candidate_line])]) + "\n"
            if len(trial) <= max_chars:
                ev_lines.append(candidate_line)
            else:
                break
        if ev_lines:
            parts.append("\n".join([ev_header, *ev_lines]))
            current_len = len("\n\n".join(parts) + "\n")

    # Section 6: confirmed domain facts (whole-section drop if over budget)
    fact_lines = [
        f"- **{st.domain}**：" + "；".join(f"{k}={v}" for k, v in st.confirmed_items)
        for st in scan.domain_stats
        if st.confirmed_items
    ]
    if fact_lines:
        sec6 = "\n".join(["## 各领域已确认事实（不含 pending）", *fact_lines])
        if current_len + 2 + len(sec6) <= max_chars:
            parts.append(sec6)
            current_len += 2 + len(sec6)

    # Section 7: rest of compass.md (lowest priority, whole-section drop if over budget)
    if compass_rest:
        sec7 = f"## 罗盘补充（权衡 / Bug / 规律）\n{compass_rest}"
        if current_len + 2 + len(sec7) <= max_chars:
            parts.append(sec7)

    return "\n\n".join(parts).strip() + "\n"


# ---------------------------------------------------------------------------
# CLI Entrypoint (with top-level sanitized exception guard)
# ---------------------------------------------------------------------------


def _parse_now_override(now_str: str | None, tz: ZoneInfo) -> dt.datetime | None:
    if not now_str:
        return None
    s = now_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(s)
    return parsed.astimezone(tz) if parsed.tzinfo else parsed.replace(tzinfo=tz)


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Life OS v3.1 strict core engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="Strict validation of all Life OS files")
    p_val.add_argument("--root", type=Path, default=ROOT_DEFAULT)
    p_val.add_argument("--now", default=None)

    p_out = sub.add_parser("build-out", help="Validate & build out/ artifacts (no Issue writes/push)")
    p_out.add_argument("--root", type=Path, default=ROOT_DEFAULT)
    p_out.add_argument("--out-dir", type=Path, required=True)
    p_out.add_argument("--read-issues", action="store_true", help="Read-only Issue filter for radar/context_pack")
    p_out.add_argument("--now", default=None)

    p_prep = sub.add_parser("prepare", help="Validate, sync bot Issues, build out/ & digest.json")
    p_prep.add_argument("--root", type=Path, default=ROOT_DEFAULT)
    p_prep.add_argument("--out-dir", type=Path, required=True)
    p_prep.add_argument("--mode", choices=["normal", "test-push", "simulate-send-failure"], default="normal")
    p_prep.add_argument("--dry-run", action="store_true")
    p_prep.add_argument("--now", default=None)

    p_del = sub.add_parser("deliver", help="Deliver digest via ServerChan (code==0) and PATCH Issue states")
    p_del.add_argument("--digest", type=Path, required=True)
    p_del.add_argument("--dry-run", action="store_true")

    p_fail = sub.add_parser("notify-failure", help="Send at most 1 failure alert via ServerChan")
    p_fail.add_argument("--message", required=True)

    p_hc = sub.add_parser("ping-healthcheck", help="Ping HEALTHCHECKS_PING_URL if configured")

    args = parser.parse_args(argv)

    if args.cmd == "notify-failure":
        key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
        if not key:
            print("ERROR: SERVERCHAN_SENDKEY missing; cannot deliver failure alert.", file=sys.stderr)
            return 1
        code = send_serverchan(key, "LifeOS系统运行失败", args.message)
        print(f"Failure alert delivered: code={code}")
        return 0

    if args.cmd == "ping-healthcheck":
        url = os.environ.get("HEALTHCHECKS_PING_URL", "").strip()
        if not url:
            print("HEALTHCHECKS_PING_URL not set; skipping ping.")
            return 0
        ping_healthcheck(url)
        print("Healthcheck ping OK.")
        return 0

    if args.cmd == "deliver":
        digest = json.loads(args.digest.read_text(encoding="utf-8"))
        if not digest.get("should_send"):
            print("0 unsent; skipping push.")
            return 0
        if digest.get("mode") == "simulate-send-failure":
            raise DeliveryError("Simulated ServerChan delivery failure (Issue states left untouched)")

        key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
        if args.dry_run:
            print(f"[DRY-RUN DELIVER] title={digest['title']!r} patches={len(digest.get('issue_patches', []))}")
            return 0
        if not key:
            raise DeliveryError("SERVERCHAN_SENDKEY is not set in production delivery")

        code = send_serverchan(key, digest["title"], digest["desp"])
        print(f"ServerChan delivered: code={code}, patches={len(digest.get('issue_patches', []))}")

        # ONLY AFTER ServerChan confirms code==0: PATCH Issue outbox state lines!
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        patches = digest.get("issue_patches") or []
        hb_patch = digest.get("heartbeat_patch")
        if patches or hb_patch:
            if not token:
                raise DeliveryError("GITHUB_TOKEN required to PATCH Issue outbox state after delivery")
            gh = GitHubClient(digest["repo"], token)
            for p in patches:
                num = int(p["issue_number"])
                cur = gh.get_issue(num)
                new_body = update_issue_state_marker(cur.get("body") or "", p["new_state"])
                gh.update_issue(num, body=new_body)
                if p.get("add_escalated_label"):
                    gh.add_labels(num, ["escalated"])
            if hb_patch:
                num = int(hb_patch["issue_number"])
                cur = gh.get_issue(num)
                new_body = update_issue_state_marker(cur.get("body") or "", hb_patch["new_state"])
                gh.update_issue(num, body=new_body)
        return 0

    # validate / build-out / prepare all use a single `now_local`
    temp_errors: list[str] = []
    cfg_peek = load_config(args.root, temp_errors)
    try:
        tz = ZoneInfo(cfg_peek.get("tz", "Asia/Shanghai"))
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    now_override = _parse_now_override(args.now, tz) if getattr(args, "now", None) else None
    if now_override is not None and not getattr(args, "dry_run", False) and args.cmd == "prepare":
        print("ERROR: --now override on `prepare` is only allowed with --dry-run", file=sys.stderr)
        return 1

    now_local = now_override or dt.datetime.now(tz)
    now_utc = now_local.astimezone(dt.timezone.utc)
    config, scan = scan_repo(args.root, now_local)

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

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    max_chars = int(config["mobile_ai"].get("context_pack_max_chars", 9000))

    if args.cmd == "build-out":
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if args.read_issues and token:
            gh_ro = GitHubClient(config["repo"], token)
            plan = sync_and_plan_delivery(
                gh_ro,
                scan,
                config,
                now_local=now_local,
                tz=tz,
                mode="normal",
                dry_run=True,
            )
            active_items = plan["active_radar_items"]
            snoozed_items = plan["snoozed_radar_items"]
            low_events = plan["low_events"]
            owner_captures = plan["owner_captures"]
            noisy_rules = plan["noisy_rules"]
            unrolled = plan["unrolled_closed_dates"]
        else:
            active_items = [
                {
                    "eid": e.eid,
                    "window": e.window,
                    "severity": e.severity,
                    "title": e.title,
                    "evidence": e.evidence,
                    "action": e.action,
                    "url": "",
                }
                for e in scan.events
                if e.severity in {"high", "med"}
            ]
            snoozed_items = []
            low_events = [e for e in scan.events if e.severity == "low"]
            owner_captures = []
            noisy_rules = []
            unrolled = []

        radar_md = build_radar_md(
            scan, active_items, snoozed_items, low_events, owner_captures, noisy_rules, unrolled, now_local
        )
        ics_text = build_calendar_ics(scan.calendar_items, now_utc)
        ctx_pack = build_context_pack_md(args.root, scan, active_items, max_chars, now_local)
        (out_dir / "radar.md").write_text(radar_md, encoding="utf-8")
        (out_dir / "calendar.ics").write_text(ics_text, encoding="utf-8", newline="")
        (out_dir / "context_pack.md").write_text(ctx_pack, encoding="utf-8")
        print(f"Built out artifacts ({len(ctx_pack)} chars context_pack).")
        return 0

    # `prepare`: sync Issues + write out/ + write digest.json (no secrets!)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token and not args.dry_run:
        print("ERROR: `prepare` requires GITHUB_TOKEN", file=sys.stderr)
        return 1

    class _DryRunGH:
        def ensure_labels(self, _: Any) -> None:
            pass

        def list_issues(self, _label: str, state: str = "all") -> list[dict[str, Any]]:
            return []

        def list_comments(self, _num: int) -> list[dict[str, Any]]:
            return []

    gh = _DryRunGH() if args.dry_run else GitHubClient(config["repo"], token)
    plan = sync_and_plan_delivery(
        gh,
        scan,
        config,
        now_local=now_local,
        tz=tz,
        mode=args.mode,
        dry_run=args.dry_run,
    )

    radar_md = build_radar_md(
        scan,
        plan["active_radar_items"],
        plan["snoozed_radar_items"],
        plan["low_events"],
        plan["owner_captures"],
        plan["noisy_rules"],
        plan["unrolled_closed_dates"],
        now_local,
    )
    ics_text = build_calendar_ics(scan.calendar_items, now_utc)
    ctx_pack = build_context_pack_md(args.root, scan, plan["active_radar_items"], max_chars, now_local)

    (out_dir / "radar.md").write_text(radar_md, encoding="utf-8")
    (out_dir / "calendar.ics").write_text(ics_text, encoding="utf-8", newline="")
    (out_dir / "context_pack.md").write_text(ctx_pack, encoding="utf-8")

    digest_payload = {
        "mode": plan["mode"],
        "repo": plan["repo"],
        "generated_at": plan["generated_at"],
        "should_send": plan["should_send"],
        "title": plan["title"],
        "desp": plan["desp"],
        "issue_patches": plan["issue_patches"],
        "heartbeat_patch": plan["heartbeat_patch"],
    }
    (out_dir / "digest.json").write_text(json.dumps(digest_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Prepared out artifacts & digest (should_send={plan['should_send']}, "
        f"patches={len(plan['issue_patches'])}, context_pack={len(ctx_pack)} chars)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run_cli(argv)
    except DeliveryError as exc:
        print(f"DELIVERY ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"UNEXPECTED ERROR ({type(exc).__name__}): check configuration/data format", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
