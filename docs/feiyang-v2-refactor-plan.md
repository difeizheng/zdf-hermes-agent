# 飞鹰 v2 重构规划

> 目标：将飞鹰从"外包模式"升级为"自建团队模式"，消除开发质量问题根源。
> 创建日期：2026-06-02
> 状态：✅ 全部完成（Phase A-E），已通过测试验证

---

## 一、问题诊断

### 根因链

```
短会话 → 无审查 → 无门禁 → 质量不可控 → 问题堆积 → 返工成本高
无记忆 → 同样错误重复犯
无角色分工 → 同一个人写代码+自审=无效审查
无技能绑定 → 60+ skills 全浪费
无 Profile → 所有任务用同一套配置，不区分角色
```

### 当前状态指标

| 指标 | 飞鹰现状 | 目标 |
|------|---------|------|
| CRITICAL 问题 | 8 | 0-2 |
| HIGH 问题 | 18 | 3-5 |
| 测试覆盖率 | 55-60% | ≥80% |
| 前端测试 | 0% | ≥80% |
| 租户隔离 | 未生效 | 强制 |
| 硬编码密钥 | 有 | 无 |
| 打回重做 | 无 | 有 |
| 跨会话记忆 | 无 | 有 |

---

## 二、目标架构

### 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        飞鹰 v2 完整架构                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐          │
│  │  用户 PRD    │───→│ 飞鹰编排层   │───→│ 任务队列     │          │
│  │  (需求输入)  │    │ (任务拆解)   │    │ (依赖排序)   │          │
│  └─────────────┘    └─────────────┘    └──────┬──────┘          │
│                                                │                │
│                    ┌───────────────────────────▼───────────┐   │
│                    │        Hermes Orchestrator             │   │
│                    │                                        │   │
│                    │  ┌────────────────────────────────┐    │   │
│                    │  │ Phase 1: Architect (opus)      │    │   │
│                    │  │   → 技术设计 + 模块拆解        │    │   │
│                    │  └───────────────┬────────────────┘    │   │
│                    │  ┌───────────────▼────────────────┐    │   │
│                    │  │ Phase 2: TDD Developer (sonnet)│    │   │
│                    │  │   → 测试 + 实现 + 自审查        │    │   │
│                    │  └───────────────┬────────────────┘    │   │
│                    │  ┌───────────────▼────────────────┐    │   │
│                    │  │ Phase 3+4: Security + QA       │    │   │
│                    │  │   (并行执行)                     │    │   │
│                    │  └───────────────┬────────────────┘    │   │
│                    │  ┌───────────────▼────────────────┐    │   │
│                    │  │ Phase 5: Quality Gate          │    │   │
│                    │  │   → PASS → 合并 + 记忆         │    │   │
│                    │  │   → REJECT → 打回 + 原因       │    │   │
│                    │  └────────────────────────────────┘    │   │
│                    └───────────────────┬────────────────────┘   │
│                                        │                        │
│                    ┌───────────────────▼────────────────────┐   │
│                    │           Memory 层                     │   │
│                    │   错误记忆 | 模式记忆 | 决策记忆        │   │
│                    └───────────────────┬────────────────────┘   │
│                                        │                        │
│                    ┌───────────────────▼────────────────────┐   │
│                    │          飞鹰交付层                      │   │
│                    │   结果聚合 → 质量报告 → 用户交付        │   │
│                    └────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 飞鹰与 Hermes 边界

| 层 | 负责方 | 职责 |
|----|--------|------|
| 任务接收 | 飞鹰 | 用户交互、PRD 收集 |
| 需求分析 | 飞鹰 | 业务理解、领域建模 |
| 任务拆解 | 飞鹰 | PRD → 可执行任务 |
| 依赖排序 | 飞鹰 | 确定执行顺序 |
| **技术设计** | **Hermes** | **架构设计、模块拆解、接口契约** |
| **代码开发** | **Hermes** | **TDD 实现、单元测试、自审查** |
| **安全审查** | **Hermes** | **安全审计、漏洞修复** |
| **质量验证** | **Hermes** | **覆盖率检查、E2E 测试、集成测试** |
| **质量门禁** | **Hermes** | **PASS/REJECT 决策、打回循环** |
| **记忆系统** | **Hermes** | **跨会话学习、错误预防** |
| 结果聚合 | 飞鹰 | 多模块结果合并 |
| 质量报告 | 飞鹰 | 生成交付报告 |
| 用户交付 | 飞鹰 | 最终交付 |

**核心原则：飞鹰管"做什么"，Hermes 管"怎么做 + 做得好不好"**

---

## 三、Profile 设计

### architect

```yaml
profile: architect
model: claude-opus-4-8
agents: [planner, architect]
skills: []
rules: [coding-style, patterns, security]
behavior: "只输出设计文档，不写代码"
input: "任务描述 + PRD"
output: "技术设计文档 + 模块拆解 + 接口契约"
```

### tdd-developer

```yaml
profile: tdd-developer
model: claude-sonnet-4-6
agents: [tdd-guide, code-reviewer, build-error-resolver]
skills: [tdd-workflow, quality-gate]
rules: [coding-style, testing, security]
behavior: "先写测试，再实现，覆盖率<80%打回"
input: "技术设计 + 接口契约"
output: "测试用例 + 实现代码 + 单元测试"
```

### security-auditor

```yaml
profile: security-auditor
model: claude-opus-4-8
agents: [security-reviewer]
skills: [security-review, security-scan]
rules: [security]
behavior: "默认找到问题，找不到=没认真查"
input: "实现代码 + 安全清单"
output: "安全审计报告 + 修复建议"
```

### qa-engineer

```yaml
profile: qa-engineer
model: claude-sonnet-4-6
agents: [e2e-runner, test-results-analyzer]
skills: [quality-gate]
rules: [testing]
behavior: "覆盖率<80% 或 E2E 失败 → REJECT"
input: "代码 + 测试用例"
output: "覆盖率报告 + E2E 测试 + 集成测试"
```

---

## 四、质量门禁

### quality-gate skill

**PASS 条件（全部满足）：**

- [x] lint 通过（ruff/black，0 error）
- [x] type check 通过（ty，0 error）
- [x] 测试通过（pytest，0 fail）
- [x] 测试覆盖率 ≥ 80%（行覆盖率）
- [x] 安全扫描通过（0 CRITICAL，≤ 2 HIGH）
- [x] 无硬编码密钥
- [x] 无 console.log/debug 语句
- [x] PRD 要求的每个功能都有对应测试

**REJECT 条件（任一满足）：**

- [ ] lint 有 error
- [ ] type check 有 error
- [ ] 测试有 fail
- [ ] 覆盖率 < 80%
- [ ] 安全扫描有 CRITICAL
- [ ] 安全扫描 HIGH > 2
- [ ] 发现硬编码密钥
- [ ] PRD 功能缺测试

### 打回逻辑

```
正常路径:
  Phase 2 (Dev) → Phase 3 (Security) → Phase 4 (QA) → Gate → PASS → done

打回路径 1（安全问题）:
  Phase 3 发现 HIGH → Gate 打回 Phase 2 → 修复 → Phase 3 重新审查 → PASS

打回路径 2（测试问题）:
  Phase 4 发现覆盖率 70% → Gate 打回 Phase 2 → 补测试 → Phase 4 重新检查 → PASS

打回路径 3（设计问题）:
  Phase 2 发现接口契约不清晰 → Gate 打回 Phase 1 → 重设计 → Phase 2 重新开发

升级路径:
  同一问题打回 3 次 → 升级：换 opus model / 人工介入 / 修改 PRD

打回上限:
  单 Phase 最多 3 次打回
  总任务最多 5 次打回
  超过 → 标记 FAILED + 人工 Review
```

---

## 五、记忆系统

### 目录结构

```
memory/
├── errors/              # 错误记忆（跨项目复用）
│   ├── tenant-isolation.md     # 租户隔离必须 WHERE tenant_id
│   ├── no-hardcoded-secrets.md # 禁止硬编码密钥
│   ├── cors-whitelist.md       # CORS 必须白名单
│   ├── rate-limit-required.md  # API 必须速率限制
│   └── setattr-whitelist.md    # setattr 必须白名单
│
├── patterns/            # 模式记忆（最佳实践）
│   ├── tdd-flow.md             # TDD 标准流程
│   ├── service-layer.md        # 服务层正确使用模式
│   └── error-handling.md       # 错误处理标准模式
│
├── decisions/           # 决策记忆（架构选择理由）
│   └── multi-tenant-strategy.md # 多租户方案选择理由
│
└── metrics/             # 指标记忆（历史质量数据）
    ├── coverage-trend.md        # 覆盖率趋势
    └── bug-patterns.md          # Bug 模式统计
```

### 记忆工作流

```
Phase 完成 → 自动写入记忆 → 下次开发自动加载 → 预防同样错误

示例：
  今天发现"租户隔离 WHERE tenant_id 遗漏"
  → 写入 memory/errors/tenant-isolation.md
  
  下周开发新项目
  → memory 自动加载
  → architect phase 输出设计时提醒："上次租户隔离遗漏，这次注意"
  → tdd-developer phase 测试用例自动生成租户隔离测试
  → security-auditor phase 专门检查租户隔离
  
  → 同样错误不再犯
```

---

## 六、飞鹰集成点

### 改造位置

只需要改飞鹰的 `dev_agent.py` 执行方式：

**改前：**

```python
def execute(task):
    result = call_claude_code_api(task.prompt)
    return result
```

**改后：**

```python
def execute(task):
    # 调 Hermes Orchestrator
    result = hermes_orchestrator.run(
        task=task.prompt,
        profile="tdd-developer",
        phases=["architect", "dev", "security", "qa"],
        quality_gate=True
    )
    return result
```

### 接口契约

飞鹰 → Hermes 的输入格式：

```json
{
  "task_id": "uuid",
  "task_name": "string",
  "task_description": "string",
  "prd_context": "string",
  "technical_design": "string (Phase 1 输出)",
  "interface_contract": "string (Phase 1 输出)",
  "security_checklist": ["string"],
  "profile": "tdd-developer | security-auditor | qa-engineer"
}
```

Hermes → 飞鹰的输出格式：

```json
{
  "task_id": "uuid",
  "status": "PASS | REJECT | FAILED",
  "artifacts": {
    "design_doc": "path",
    "source_code": "path",
    "tests": "path",
    "security_report": "path",
    "coverage_report": "path"
  },
  "quality_gate": {
    "passed": true,
    "lint": "PASS",
    "type_check": "PASS",
    "tests": "PASS",
    "coverage": 85.2,
    "security": "PASS",
    "critical_issues": 0,
    "high_issues": 1
  },
  "reject_reasons": ["string (仅 REJECT 时)"],
  "memory_updates": ["string (本次写入的记忆)"]
}
```

---

## 七、风险与缓解

| 风险 | 原因 | 缓解 |
|------|------|------|
| Token 成本增加 | 多 Phase + 打回循环 | architect 用 opus（贵但少用），dev 用 sonnet，打回上限 3 次。净成本降低（返工更贵） |
| 执行时间变长 | 5 个 Phase 串行 | Phase 3+4 并行执行，预期 30min-1h |
| 打回循环卡住 | AI 修不好同一个问题 | 打回上限 3 次，升级机制（换 model/人工），记忆系统预防 |
| Hermes orchestrator Bug | 之前审计有 4 项遗留 | Phase A 前先修完 4 项遗留 |
| 接口不兼容 | 飞鹰输出格式 vs Hermes 输入格式 | 定义清晰 JSON schema，飞鹰输出适配层 |

---

## 八、落地路线图

### Phase A: 基础设施（1-2天） ✅ 已完成

- [x] 创建 quality-gate skill (`skills/quality-gate/SKILL.md`)
- [x] 配置 4 个 profile（architect/tdd-dev/security-auditor/qa）(`coordinator/profiles.py`)
- [x] 在 orchestrator 加打回逻辑（validate_agent retry + Security/QA targeted retry）
- [x] 创建 memory 模板（errors/patterns/decisions）(`coordinator/memory.py`)
- [x] 修完 Hermes orchestrator 4 项遗留问题

### Phase B: 单模块验证（1-2天） ✅ 已完成

- [x] 拿飞鹰的一个现有模块（"多租户架构"）
- [x] 用新架构重新开发
- [x] 对比质量：CRITICAL/HIGH 问题数、覆盖率
- [x] 验证打回循环是否工作

### Phase C: 飞鹰集成（1天） ✅ 已完成

- [x] 改飞鹰 dev agent，调 Hermes orchestrator
- [x] 端到端测试：PRD → 飞鹰拆解 → Hermes 执行 → 交付
- [x] 验证质量门禁拦截效果

### Phase D: 全量切换（1天） ✅ 已完成

- [x] 新任务全部走新架构
- [x] 旧任务完成后用新架构审查
- [x] 记忆系统自动积累

### Phase E: 稳定性与集成优化 ✅ 已完成

- [x] Bug 修复：8 项（2 CRITICAL / 2 HIGH / 4 MEDIUM+LOW）
- [x] Profile 集成到 Agent Pipeline（daemon 启动时加载，prompt 注入）
- [x] 质量门禁实际实现（validate_agent 聚合 Security+QA 结果做 PASS/FAIL）
- [x] Memory 自动加载（dev→errors+patterns，security→errors，design→errors+decisions）
- [x] Memory 自动写入（Security/QA 完成后自动提取记忆写入 workspace）
- [x] 打回循环完善（Security/QA failed 时创建 targeted retry 任务链）
- [x] 32/32 coordinator 测试全部通过，零回归

**总计：4-6 天（实际完成）**

---

## 九、用户参与点

| 阶段 | 需要用户做什么 |
|------|---------------|
| **Phase A 开始前** | ✅ 确认飞鹰项目路径和配置文件位置 |
| **Phase A 开始前** | ✅ 确认飞鹰的 dev agent 代码路径 |
| **Phase A 开始前** | ✅ 确认 Hermes 的 orchestrator 是否已部署可用 |
| **Phase B 验证后** | ✅ 审查单模块验证结果，确认质量达标 |
| **Phase C 集成时** | ✅ 提供飞鹰的配置/密钥（如果需要） |
| **Profile 设计** | ✅ 确认 4 个 profile 的 model 选择（成本考量） |
| **质量门禁规则** | ✅ 确认 PASS/REJECT 标准是否合理 |
| **全程** | ⚠️ 不需要写代码，但需要决策和审查 |

---

## 十、成功标准

| 指标 | 目标值 | 验收方式 |
|------|--------|---------|
| CRITICAL 问题 | ≤ 2 | 自动扫描 |
| HIGH 问题 | ≤ 5 | 自动扫描 |
| 后端测试覆盖率 | ≥ 80% | pytest-cov |
| 前端测试覆盖率 | ≥ 80% | vitest --coverage |
| 硬编码密钥 | 0 | grep/security scan |
| 租户隔离 | 100% 生效 | 专项测试 |
| 打回循环 | 正常工作 | E2E 测试 |
| 记忆系统 | 自动写入+加载 | 功能测试 |
