---
domain: wealth
facts:
  net_worth: {v: "?", as_of: null, src: null, ttl_days: 90}
  liquid_cash: {v: "?", as_of: null, src: null, ttl_days: 90}
  monthly_fixed_cost: {v: "?", as_of: null, src: null, ttl_days: 90}
  runway_months: {v: "?", as_of: null, src: null, ttl_days: 90}
  consumer_debt: {v: "?", as_of: null, src: null, ttl_days: 90}
  employer_concentration_risk: {v: "?", as_of: null, src: null, ttl_days: 180}
---

# wealth.md — 财富配置与消费防线

## 1. 资产配置与压力测试原则
- **目标配置比例与最大可接受回撤**：?
- **流动性与同源风险注意**：雇主股权/期权与工资来自同一风险源，联合下行计算时必须合并计入；大额决策按「收入中断 + 资产回撤 + 刚性支出增加」同时算税后可变现现金。

## 2. 分级消费闸门（具体金额阈值见 `system/config.yml`）
- **Tier 0 随手买**：高频刚需直接买，不消耗意志力。
- **Tier 1 轻审查**：查替代性、30 天使用频次、年化累计暴露（自动续约自动升一级）。
- **Tier 2 72h 冷静闸**：**强制写入 `system/dates.yml`（`kind: cooling`, ISO 时间戳 + 72h）**，满 72 小时由脚本推送解冻后再决断。
- **Tier 3 重大消费**：升级为 L2 决策走 `decisions/_template.md`。

## 3. 买完吃灰黑历史（防复踩）
- 暂无（记录：日期 · 物品与价格 · 后悔原因 · 防坑规则）
