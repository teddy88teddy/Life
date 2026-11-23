# Life of Teddy — Life OS v3.0

面向全尺度人生决策（职业、财富、健康、关系、安全网兜底、成长与日常生活）的轻量化 AI 操作系统。

**三条底层铁律**：
1. **会出后果的规则交给代码，需要判断的交给模型**：期限、72h 冷静期、TTL 过期、合法日期全由 `scripts/lifeos.py` 硬校验，CI 校验失败直接红灯并通过微信 Server酱报警。
2. **借力现成设施，零自研状态机**：事件台账 = **GitHub Issues**；手机日历 = **`out` 分支 `calendar.ics`**；微信触达 = **Server酱**；手机快速记事 = **`capture` Issue 模板**；手机 AI = **Gemini 载入 `out` 分支 `context_pack.md`**。
3. **没有数据源的规则不上线**：`?` 表示未知盲区，`none` 表示已确认没有，绝不把“没报警”误当“一切正常”。

---

## 3 分钟启用清单（合并进 `main` 后执行）

### 1. 配置微信 Server酱推送（必需）
1. 在 GitHub 仓库打开 `Settings → Secrets and variables → Actions → New repository secret`；
2. 新建 Secret 名称 **`SERVERCHAN_SENDKEY`**，填入你的 Server酱 SendKey（自动兼容 Turbo `SCT...` 与 Server酱³ `sctp...`）；
3. *（可选外部死信心跳）* 新建 **`HEALTHCHECKS_PING_URL`**（如 healthchecks.io 免费 ping 地址），防止 GitHub 定时任务意外停摆。
4. 进入 GitHub `Actions → Daily Proactive Radar & CI → Run workflow` 手动触发一次，确认微信收到推送、且 `out` 分支已生成。

### 2. 订阅手机日历（硬期限看得见）
`out` 分支生成后，在手机日历添加 URL 订阅（日历只负责看见，微信 Server酱负责按 T-30/T-7/T-1 准时提醒）：
```text
https://raw.githubusercontent.com/teddy88teddy/Life/out/calendar.ics
```
*(极少数涉及法律/罚款的硬期限，可在手机原生提醒再设一条双保险。)*

### 3. 配置手机端 Gemini（L0 压缩上下文）
1. 在 Gemini 创建一个专属 Gem（如 `Teddy LifeOS`）；
2. 复制 `https://raw.githubusercontent.com/teddy88teddy/Life/out/context_pack.md`（≤3k tokens）贴入 Gem 指令或知识文件；
3. 每周 5 分钟周检查后顺手刷新一次即可（L0 日常决策完全容忍一周上下文延迟）。

---

## 日常怎么用？（零摩擦操作指南）

| 场景 | 怎么操作 |
| :--- | :--- |
| **手机 10 秒记一件事** | GitHub App 新建 Issue，选 `Quick Capture` 模板（自动打 `capture` 标签）；周检查时统一整理 |
| **收到微信 Server酱提醒** | 点推送里的 Issue 链接：直接 **Close** = 已处理；**Close as not planned** = 忽略；评论 `/snooze 3d` 或 `/snooze 2026-12-01` = 延后；评论 `/fact key=value` = 待入库事实（**仅认 `teddy88teddy` 本人操作**） |
| **做一次真实决策（L1–L3）** | 在桌面 AI 会话直接抛真实问题，AI 按 `AGENTS.md` 路由并在 `decisions/YYYY-MM-DD-<slug>.md` 单文件落盘 |
| **日常微决策（L0）** | 优先按 `domains/daily.md` 默认规则直接执行；覆盖不到时用手机 Gemini Gem 10 秒拿 1 个可执行答案 |
| **每周 5 分钟周检查** | 收到周日 08:30 微信心跳周报后：清 `capture`、确认 `pending` 事实、记 `weekly_energy: 1..5`、刷新 `now.md`；**漏了一周直接从今天重启，不补账** |

---

## 本地校验与测试

```bash
pip install -r scripts/requirements.txt
python3 scripts/lifeos.py validate
python3 -m unittest scripts/test_lifeos.py -v
```
