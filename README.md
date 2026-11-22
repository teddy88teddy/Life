# Life of Teddy — 主动式全尺度 AI 人生决策系统

这是 Teddy 的数字人生档案与 AI 首席参谋底座（Markdown + Git + 自动化巡检），覆盖从**人生重大战略（职业生涯发展、财富资本配置、健康长寿、关系人脉、认知成长）**到**日常高频微决策（饮食、穿搭、消费、作息、休闲等一切生活选择）**的全尺度场景。

---

## 核心设计原则（v2.0）

1. **全人生域 MECE 覆盖，不为例子所限**：
   围绕六大人生核心域（`career` / `wealth` / `health` / `relationships` / `growth` / `lifestyle`）建立 **战略（`strategy.md`）+ 现状（`state.md`）** 双轮结构，任何新需求或子主题随时自然生长。
2. **主动性优先（Proactive Chief of Staff）**：
   - **开场主动雷达（`00_radar.md`）**：每次对话自动检查到期决策复盘、触发的人生预警线、冷静池解冻、停滞目标与最高价值信息盲区，主动提醒而非等你开口；
   - **预设人生触发器（`01_north_star/triggers.md`）**：为职业停滞、财务失衡、精力过载、冲动消费提前写好 IF-THEN 预警线；
   - **大小决策双向贯通（The One Loop）**：宏观主线向下约束日常选择，日常连续异常信号向上触发战略纠偏；
   - **仓库自带心跳**：内置 `scripts/proactive_radar.py` + `scripts/lint_life.py` 与每周自动巡检工作流（`.github/workflows/weekly-radar.yml`）。
3. **反谄媚 + 越用越准**：
   L2/L3 重大决策强制盲评、事前验尸（Pre-mortem）与三视角董事会打分，并用决策日志与 3 次晋升法则持续校准判断力。

---

## 目录结构

```text
Life/
├── AI_GUIDE.md               # 【热层】参谋长宪法、主动性协议、L0-L3路由与反谄媚规则
├── AGENTS.md / CLAUDE.md     # 跨平台 AI 自动入口指针
├── 00_now.md                 # 【热层】实时作战室（当下主线、精力/健康/消费约束）
├── 00_radar.md               # 【热层】主动预警雷达（由脚本 + AI 自动刷新）
├── 01_north_star/            # 【顶层方向盘】TELOS愿景、价值观红线、个人说明书、人生触发器库
├── 02_domains/               # 【六大人生核心域】每个域均含 strategy.md + state.md
│   ├── career/               #   1. 职业生涯发展与跃迁战略
│   ├── wealth/               #   2. 财富增长、投资配置与消费防线（含72h冷静池）
│   ├── health/               #   3. 健康长寿、体能、睡眠与饮食底层规则
│   ├── relationships/        #   4. 亲密关系、家庭责任与高价值人脉
│   ├── growth/               #   5. 认知成长、技能树、信息食谱与 AI 杠杆
│   └── lifestyle/            #   6. 通用日常微决策与生活脉冲（不限吃/穿/周末，自由扩展）
├── 03_decisions/             # 【决策引擎】框架库、提案模板、active 待决、log 复盘总账
├── 04_cadence/               # 【主动节律与进化】周/月/季协议、建档地图、inbox、规律库、复盘归档
├── scripts/                  # 【确定性工具】lint_life.py 体检 + proactive_radar.py 主动雷达
└── .github/workflows/        # 【自动心跳】weekly-radar.yml 每周定时巡检
```

---

## 常用本地命令

```bash
# 刷新主动预警雷达（更新 00_radar.md）
python3 scripts/proactive_radar.py

# 运行全库健康体检（热层行数、死链、过期、到期复盘、密钥防漏）
python3 scripts/lint_life.py
```
