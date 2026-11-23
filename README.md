# Life of Teddy — Life OS v3.1

面向全尺度人生决策（职业、财富、健康、关系、安全网兜底、成长与日常生活）的轻量化 AI 操作系统。

**三条底层铁律**：
1. **会出后果的规则交给代码，需要判断的交给模型**：期限、72h 冷静期、TTL 过期、重复 YAML 键、拼写错误与合法日期全由 `scripts/lifeos.py` 硬校验；任何故障一律非 0 退出并走独立告警兜底，绝不静默。
2. **借力现成设施，零自研数据库**：事件状态与发送回执藏在 **GitHub Issues**（单事项单 Issue + bot 维护状态行，创建后自动 lock 防陌生人评论）；手机日历 = **`out` 分支 `calendar.ics`**；微信触达 = **Server酱**；手机速记 = **`capture` Issue 模板**；手机 AI = **Gemini Skill + Google Doc**。
3. **没有数据源的规则不上线**：`?` 表示未知盲区，`none` 表示已确认没有，绝不把“没报警”误当“一切正常”。

---

## 安全与时区前提（请务必记住）

- **单人公开仓库前提**：请**不要给本仓库添加任何 collaborator**（仓库默认单人持有写权限，事件 Issue 仅信任 `github-actions[bot]` 且创建即 lock，命令仅认 `teddy88teddy` 本人评论）。
- **日期语义**：所有日期与每日 08:30 巡检均以**北京时间（`Asia/Shanghai`）**为准；出差/旅居时如需切换只改 `system/config.yml` 中的 `tz`。
- **Server酱额度**：Server酱 Turbo 免费版上限 **5 条/天**，微信卡片只显示标题（正文保留 1 天），因此每条推送标题直接承载最紧急项与窗口（≤32 字），且每次运行最多发 1 条。

---

## 合并进 `main` 后的 4 步启用与验收

### 1. 配置受保护的 `production` Environment（防止非 `main` 分支接触密钥）
1. 打开 `Settings → Environments → New environment`，新建 **`production`**；
2. 在 **Deployment branches and tags** 选择 `Selected branches and tags`，仅允许 **`main`**；
3. 将 **`SERVERCHAN_SENDKEY`**（从仓库级 Secret 移至 `production` Environment Secret）填入；
4. *（强烈推荐独立死信兜底）* 在 [healthchecks.io](https://healthchecks.io) 创建免费检查（Period = `1 day`，Grace = `3 hours`），把 ping URL 存入 `production` Environment Secret **`HEALTHCHECKS_PING_URL`**；同时确认 GitHub 账号设置中已开启 Actions 失败邮件通知。

### 2. 10 分钟验收推送与 `out` 分支
1. 合并 PR #1 后，先确认 `push` 运行只执行了 `validate` 和 `build-out`（生成 `out` 分支、不建 Issue、不发微信）；
2. 在 `Actions → Daily Proactive Radar & CI → Run workflow` 选择 `mode: test-push` 运行一次，确认微信收到测试卡片且公开日志中只打印 `code=0`（无响应正文）。

### 3. 订阅手机日历（硬期限看得见）
`out` 分支生成后，在 iOS 日历或 Google Calendar 添加 URL 订阅（日历负责可见，微信 Server酱负责按窗口提醒）：
```text
https://raw.githubusercontent.com/teddy88teddy/Life/out/calendar.ics
```

### 4. 配置手机端 Gemini（适配 Gems 退场后的 Skills 架构）
> 个人账号的 Gemini Gems 将于 2026-12-17 下线，且 Gemini Skills 不支持直接挂 GitHub 文件。

1. 在 Google Drive 新建一个 Google Doc（如 `Teddy LifeOS Context`）；
2. 在 Gemini 新建一个 **Skill**，挂载该 Google Doc；
3. 每周 5 分钟周检查时，打开 `https://raw.githubusercontent.com/teddy88teddy/Life/out/context_pack.md`，全选粘贴覆盖进该 Google Doc（1 分钟，无需自动同步凭据）。

---

## 日常怎么用？

| 场景 | 怎么操作 |
| :--- | :--- |
| **手机 10 秒速记** | GitHub App 新建 Issue，选 `Quick Capture`（含可选**截止日期**字段：周一记周四到期的事会自动在 T-1/T0 推送，不用等周日） |
| **收到微信 Server酱提醒** | 点正文里的 Issue 链接：直接 **Close** = 已处理（若属周期性事项，顺手更新 `system/dates.yml` 下一期）；**Close as not planned** = 忽略；评论 `/snooze 3d` 或 `/snooze 2026-12-01` = 延后重复提醒（新窗口 T-1/T0 仍会准时提醒） |
| **做一次真实决策（L1–L3）** | 在桌面 AI 会话直接抛真实问题，AI 按 `AGENTS.md` 路由并在 `decisions/YYYY-MM-DD-<slug>.md` 单文件落盘 |
| **日常微决策（L0）** | 优先按 `domains/daily.md` 默认规则直接执行；覆盖不到时问手机 Gemini Skill 拿 1 个可执行答案 |
| **每周 5 分钟周检查** | 收到周日 08:30 微信心跳后：清 `capture`、确认 `pending` 事实、记 `weekly_energy: 1..5`、刷新 `now.md` 与 Google Doc；**漏周直接从今天重启，不补账** |

---

## 本地校验与测试

```bash
pip install -r scripts/requirements.txt
python3 scripts/lifeos.py validate
python3 -m unittest scripts/test_lifeos.py -v
```
