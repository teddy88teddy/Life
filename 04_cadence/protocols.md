---
layer: cadence
review_cycle: quarterly
last_updated: 2026-11-22
status: active
---

# 主动运作节律协议（Proactive Operating Cadence）

> 借鉴 Daniel Miessler `LifeOS`（One Loop / Pulse）、`seandavi/lifeos-template` 与 Karpathy `LLM Wiki`：
> **决定一个人生操作系统是越用越聪明，还是 2 周后吃灰的，不是模板有多漂亮，而是有没有稳定的主动节律。**

---

## 1. 实时节律（每次对话自动触发）

- **开场扫描**：AI 先读 `00_now.md` + `00_radar.md`，决定是否需要主动输出 2–3 行【主动雷达简报】。
- **对话捕获**：任何新事实、偏好、状态变化、情绪波动或纠偏，由 AI 顺手写入对应文件或 `04_cadence/inbox.md`。
- **渐进补盲**：如果本次对话撞上关键档案盲区，AI 在末尾最多顺手抛 1 个最高杠杆补充问题，绝不冷场也不审讯。

## 2. 每周对齐节律（Weekly Alignment — 约 10 分钟）

> 由 GitHub Actions 每周定时巡检（`.github/workflows/weekly-radar.yml`）或每周首次对话时由 AI **主动发起**：

1. **刷新雷达**：运行 `python3 scripts/proactive_radar.py`，核对临近截止决策、冷静池解冻、触发器状态。
2. **One-Loop 漂移校准**：拿过去 7 天 `recent_pulse.md` 对照 `00_now.md` 的本季度北极星主线——**过去一周的时间、精力和钱，真的花在最重要的大事上了吗？**
3. **下周焦点锁定**：更新 `00_now.md` 的 Top 3 重点、精力水位与健康/消费模式。

## 3. 每月 Lint 体检（Monthly Review & Karpathy Lint — 约 15 分钟）

> 每月初由 AI **主动提议执行**，在 `04_cadence/reviews/YYYY-MM-review.md` 归档：

1. 运行 `python3 scripts/lint_life.py`，处理所有过期文件、热层超长或死链警告；
2. 扫描全库语义矛盾（如口头目标 vs 实际消费/作息/时间分配冲突）；
3. 回访所有到期的 `03_decisions/log/` 决策，更新真实结果与认知教训；
4. 清理 `04_cadence/inbox.md`，将出现 ≥ 3 次的模式呈报 Teddy 确认晋升至 `learned_patterns.md`。

## 4. 每季度战略重审（Quarterly Strategic Reset — 约 30 分钟）

> 每季度末由 AI **主动发起**，聚焦人生大方向而非日常琐事：

1. 重点复盘 **职业生涯（`career/`）与财富（`wealth/`）**：过去 90 天核心筹码增加了吗？赛道判断变了吗？是否触发了 `TR-C1/C2`？
2. 审视 **健康、关系与认知成长**：有没有为了工作透支长期健康或重要关系？
3. 校准 `01_north_star/telos.md` 与 `triggers.md`，定出下个季度的唯一北极星主线。
