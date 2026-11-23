---
domain: safety_net
facts:
  health_insurance_coverage: {v: "?", as_of: null, src: null, ttl_days: 365}
  critical_illness_or_life_insurance: {v: "?", as_of: null, src: null, ttl_days: 365}
  emergency_primary_contact: {v: "?", as_of: null, src: null, ttl_days: 365}
  legal_tax_jurisdiction: {v: "?", as_of: null, src: null, ttl_days: 365}
---

# safety_net.md — 安全网与极端事件兜底清单

> **作用**：所有带到期日的续保、证件、申报、合同条款同步写入 `system/dates.yml` 由脚本主动推送；重大 L2/L3 决策必须引用本文件过闸。

## 1. 保险与保障缺口（核对等待期、免责条款、离职断缴风险）
- **医保 / 商保 / 重疾 / 意外 / 车险**：?（到期日录 `system/dates.yml`）
- **若离职或创业，原有雇主保障的无缝衔接方案**：?

## 2. 证件、税务、合同与法律边界
- **身份证 / 护照 / 签证 / 驾照到期管理**：同步录入 `system/dates.yml`
- **适用税务辖区与年度汇算清缴期限**：?
- **在身重要合同与竞业/违约条款原件位置**：?

## 3. 极端事件应急预案（失业 / 失能 / 家庭突发大额支出）
- **第一联系人与可立即动用的流动资源**：?
- **关键资产账户清单与受益人/应急访问说明**（绝不存密码或密钥，只写存放位置与联络路径）：?
