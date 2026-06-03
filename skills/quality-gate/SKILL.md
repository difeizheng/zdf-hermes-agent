# quality-gate

> 质量门禁 skill — 硬性指标检查，PASS/REJECT 决策。

**用途**：在 Hermes orchestrator 的 Phase 5 (Validate) 阶段，根据 Security + QA 结果做出 PASS/REJECT 决策。

## PASS 条件（全部满足）

- [x] lint 通过（ruff/black，0 error）
- [x] type check 通过（ty/mypy，0 error）
- [x] 测试通过（pytest/vitest，0 fail）
- [x] 测试覆盖率 ≥ 80%（行覆盖率）
- [x] 安全扫描通过（0 CRITICAL，≤ 2 HIGH）
- [x] 无硬编码密钥
- [x] 无 console.log/debug 语句
- [x] PRD 要求的每个功能都有对应测试

## REJECT 条件（任一满足）

- [ ] lint 有 error
- [ ] type check 有 error
- [ ] 测试有 fail
- [ ] 覆盖率 < 80%
- [ ] 安全扫描有 CRITICAL
- [ ] 安全扫描 HIGH > 2
- [ ] 发现硬编码密钥
- [ ] PRD 功能缺测试

## 打回逻辑

```
正常路径:
  Phase 2 (Dev) → Phase 3+4 (Security + QA 并行) → Phase 5 (Gate) → PASS → Deploy

打回路径 1（安全问题）:
  Phase 3 发现 CRITICAL → Gate 打回 Phase 2 → 修复 → Phase 3+4 重新审查 → Gate

打回路径 2（测试问题）:
  Phase 4 发现覆盖率 70% → Gate 打回 Phase 2 → 补测试 → Phase 3+4 重新检查 → Gate

打回路径 3（设计问题）:
  Phase 2 发现接口契约不清晰 → Gate 打回 Phase 1 → 重设计 → Phase 2 重新开发

升级路径:
  同一问题打回 3 次 → 升级：换 opus model / 人工介入 / 修改 PRD

打回上限:
  单 Phase 最多 3 次打回
  总任务最多 5 次打回
  超过 → 标记 FAILED + 人工 Review
```

## 集成点

在 `validate_agent.py` 的 `_parse_validation_result()` 中调用质量门禁：

```python
def quality_gate_check(security_result, qa_result) -> str:
    """综合 Security + QA 结果做出 PASS/REJECT 决策."""
    # Security: 0 CRITICAL, ≤2 HIGH
    if security_result.get("critical_issues", 0) > 0:
        return "REJECT"
    if security_result.get("high_issues", 0) > 2:
        return "REJECT"

    # QA: tests pass + coverage ≥80%
    if not qa_result.get("test_passed", False):
        return "REJECT"
    if qa_result.get("coverage_pct", 0) < 80.0:
        return "REJECT"

    return "PASS"
```

## 指标目标

| 指标 | 目标值 | 验收方式 |
|------|--------|---------|
| CRITICAL 问题 | ≤ 2 | bandit + Claude review |
| HIGH 问题 | ≤ 5 | bandit + Claude review |
| 后端测试覆盖率 | ≥ 80% | pytest-cov |
| 前端测试覆盖率 | ≥ 80% | vitest --coverage |
| 硬编码密钥 | 0 | grep/security scan |
| 租户隔离 | 100% 生效 | 专项测试 |
| 打回循环 | 正常工作 | E2E 测试 |

## Memory 写入

质量门禁 PASS 后，自动写入记忆：

```
memory/errors/
  - tenant-isolation.md (租户隔离遗漏)
  - no-hardcoded-secrets.md (禁止硬编码密钥)
  - cors-whitelist.md (CORS 白名单)
  - rate-limit-required.md (API 速率限制)

memory/patterns/
  - tdd-flow.md (TDD 标准流程)
  - service-layer.md (服务层模式)
  - error-handling.md (错误处理模式)
```

每次 Phase 完成后，自动写入新发现的错误/模式记忆。
下次开发时，memory 自动加载，预防同样错误。