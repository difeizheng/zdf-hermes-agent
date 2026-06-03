"""Profile configuration for multi-agent orchestrator.

Each profile defines:
- model: Claude model to use
- agents: Hermes agent types to invoke
- skills: Skills to activate
- rules: Rules to enforce
- behavior: Expected output behavior
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProfileConfig:
    """Profile configuration for a specific agent role."""
    name: str
    model: str
    agents: list[str]
    skills: list[str]
    rules: list[str]
    behavior: str
    input_format: str
    output_format: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.name,
            "model": self.model,
            "agents": self.agents,
            "skills": self.skills,
            "rules": self.rules,
            "behavior": self.behavior,
            "input": self.input_format,
            "output": self.output_format,
        }


# Default profiles for multi-agent pipeline

PROFILES = {
    "architect": ProfileConfig(
        name="architect",
        model="claude-opus-4-8",
        agents=["planner", "architect"],
        skills=[],
        rules=["coding-style", "patterns", "security"],
        behavior="只输出设计文档，不写代码",
        input_format="任务描述 + PRD",
        output_format="技术设计文档 + 模块拆解 + 接口契约",
    ),

    "tdd-developer": ProfileConfig(
        name="tdd-developer",
        model="claude-sonnet-4-6",
        agents=["tdd-guide", "code-reviewer", "build-error-resolver"],
        skills=["tdd-workflow", "quality-gate"],
        rules=["coding-style", "testing", "security"],
        behavior="先写测试，再实现，覆盖率<80%打回",
        input_format="技术设计 + 接口契约",
        output_format="测试用例 + 实现代码 + 单元测试",
    ),

    "security-auditor": ProfileConfig(
        name="security-auditor",
        model="claude-opus-4-8",
        agents=["security-reviewer"],
        skills=["security-review", "security-scan"],
        rules=["security"],
        behavior="默认找到问题，找不到=没认真查",
        input_format="实现代码 + 安全清单",
        output_format="安全审计报告 + 修复建议",
    ),

    "qa-engineer": ProfileConfig(
        name="qa-engineer",
        model="claude-sonnet-4-6",
        agents=["e2e-runner", "test-results-analyzer"],
        skills=["quality-gate"],
        rules=["testing"],
        behavior="覆盖率<80% 或 E2E 失败 → REJECT",
        input_format="代码 + 测试用例",
        output_format="覆盖率报告 + E2E 测试 + 集成测试",
    ),
}


def get_profile(name: str) -> ProfileConfig | None:
    """Get profile configuration by name."""
    return PROFILES.get(name)


def list_profiles() -> list[str]:
    """List all available profile names."""
    return list(PROFILES.keys())


def profile_to_prompt(profile: ProfileConfig) -> str:
    """Convert profile config to a system prompt for Claude."""
    parts = [
        f"# Profile: {profile.name}",
        "",
        f"## Model: {profile.model}",
        "",
        "## Agents Available:",
        "",
    ]
    for agent in profile.agents:
        parts.append(f"- `{agent}`")

    parts.extend([
        "",
        "## Skills Active:",
        "",
    ])
    for skill in profile.skills:
        parts.append(f"- `{skill}`")

    parts.extend([
        "",
        "## Rules Enforced:",
        "",
    ])
    for rule in profile.rules:
        parts.append(f"- `{rule}`")

    parts.extend([
        "",
        "## Behavior:",
        "",
        profile.behavior,
        "",
        "## Input Format:",
        "",
        profile.input_format,
        "",
        "## Output Format:",
        "",
        profile.output_format,
    ])

    return "\n".join(parts)