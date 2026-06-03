"""Memory system for Hermes orchestrator.

Three-layer memory structure:
- errors/    : 错误记忆（跨项目复用）
- patterns/  : 模式记忆（最佳实践）
- decisions/ : 决策记忆（架构选择理由）

Stored in workspace_dir/memory/ and loaded at each Phase start.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """Single memory entry."""
    category: str  # errors, patterns, decisions
    name: str  # file name without .md
    content: str
    created_at: datetime
    metadata: dict[str, Any] | None = None

    def to_markdown(self) -> str:
        """Format memory entry as markdown file."""
        meta_str = ""
        if self.metadata:
            for k, v in self.metadata.items():
                meta_str += f"{k}: {v}\n"

        return f"""---
name: {self.name}
description: {self.content[:100]}
created: {self.created_at.isoformat()}
{meta_str}
---

# {self.category}: {self.name}

{self.content}

## Why

<!-- Add reason for this memory -->

## How to Apply

<!-- Add actionable steps -->
"""


class MemorySystem:
    """Memory system for Hermes orchestrator."""

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.memory_dir = workspace_dir / "memory"
        self.errors_dir = self.memory_dir / "errors"
        self.patterns_dir = self.memory_dir / "patterns"
        self.decisions_dir = self.memory_dir / "decisions"

        # Create directories
        self.errors_dir.mkdir(parents=True, exist_ok=True)
        self.patterns_dir.mkdir(parents=True, exist_ok=True)
        self.decisions_dir.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> list[MemoryEntry]:
        """Load all memory entries."""
        entries = []
        for category_dir in [self.errors_dir, self.patterns_dir, self.decisions_dir]:
            category = category_dir.name
            for md_file in category_dir.glob("*.md"):
                try:
                    entry = self._parse_memory_file(md_file, category)
                    entries.append(entry)
                except Exception as e:
                    logger.warning("Failed to parse memory file %s: %s", md_file, e)
        return entries

    def load_category(self, category: str) -> list[MemoryEntry]:
        """Load memory entries for a specific category."""
        entries = []
        category_dir = self.memory_dir / category
        if not category_dir.exists():
            return entries

        for md_file in category_dir.glob("*.md"):
            try:
                entry = self._parse_memory_file(md_file, category)
                entries.append(entry)
            except Exception as e:
                logger.warning("Failed to parse memory file %s: %s", md_file, e)
        return entries

    def write(self, entry: MemoryEntry) -> Path:
        """Write a memory entry to disk."""
        category_dir = self.memory_dir / entry.category
        category_dir.mkdir(parents=True, exist_ok=True)

        file_path = category_dir / f"{entry.name}.md"
        file_path.write_text(entry.to_markdown(), encoding="utf-8")
        logger.info("Wrote memory: %s/%s", entry.category, entry.name)
        return file_path

    def write_error(self, name: str, content: str, metadata: dict[str, Any] | None = None) -> Path:
        """Write an error memory entry."""
        entry = MemoryEntry(
            category="errors",
            name=name,
            content=content,
            created_at=datetime.now(),
            metadata=metadata,
        )
        return self.write(entry)

    def write_pattern(self, name: str, content: str, metadata: dict[str, Any] | None = None) -> Path:
        """Write a pattern memory entry."""
        entry = MemoryEntry(
            category="patterns",
            name=name,
            content=content,
            created_at=datetime.now(),
            metadata=metadata,
        )
        return self.write(entry)

    def write_decision(self, name: str, content: str, metadata: dict[str, Any] | None = None) -> Path:
        """Write a decision memory entry."""
        entry = MemoryEntry(
            category="decisions",
            name=name,
            content=content,
            created_at=datetime.now(),
            metadata=metadata,
        )
        return self.write(entry)

    def _parse_memory_file(self, file_path: Path, category: str) -> MemoryEntry:
        """Parse a memory file into MemoryEntry."""
        content = file_path.read_text(encoding="utf-8")

        # Parse frontmatter
        name = file_path.stem
        created_at = datetime.now()
        metadata = {}

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1].strip()
                body = parts[2].strip()

                for line in frontmatter.split("\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k = k.strip()
                        v = v.strip()
                        if k == "name":
                            name = v
                        elif k == "created":
                            try:
                                created_at = datetime.fromisoformat(v)
                            except Exception:
                                pass
                        else:
                            metadata[k] = v

                content = body

        return MemoryEntry(
            category=category,
            name=name,
            content=content,
            created_at=created_at,
            metadata=metadata,
        )

    def build_context_prompt(self, categories: list[str] | None = None) -> str:
        """Build a prompt with all memory context."""
        if categories is None:
            entries = self.load_all()
        else:
            entries = []
            for cat in categories:
                entries.extend(self.load_category(cat))

        if not entries:
            return ""

        parts = [
            "# Memory Context",
            "",
            "以下是从过往项目积累的经验和教训，请参考并避免同样错误：",
            "",
        ]

        for entry in entries:
            parts.append(f"## [{entry.category}] {entry.name}")
            parts.append("")
            parts.append(entry.content[:500])
            parts.append("")

        return "\n".join(parts)


# Default memory templates

DEFAULT_ERROR_MEMORIES = [
    ("tenant-isolation", "租户隔离必须 WHERE tenant_id。所有数据库查询必须包含 tenant_id 条件，否则会导致数据泄露。"),
    ("no-hardcoded-secrets", "禁止硬编码密钥。所有密钥必须从环境变量或密钥管理器读取。"),
    ("cors-whitelist", "CORS 必须白名单。不允许 Access-Control-Allow-Origin: *，必须明确指定允许的域名。"),
    ("rate-limit-required", "API 必须速率限制。所有公开 API 必须配置速率限制，防止滥用。"),
    ("setattr-whitelist", "setattr 必须白名单。动态 setattr 必须限制允许的属性名，防止任意属性修改。"),
]

DEFAULT_PATTERN_MEMORIES = [
    ("tdd-flow", "TDD 标准流程：先写测试（RED）→ 实现最小代码（GREEN）→ 重构优化（REFACTOR）→ 验证覆盖率≥80%。"),
    ("service-layer", "服务层正确使用模式：Controller → Service → Repository。Controller 只做参数验证和路由，Service 处理业务逻辑，Repository 处理数据访问。"),
    ("error-handling", "错误处理标准模式：使用自定义异常类，捕获特定异常，记录完整错误上下文，返回用户友好错误消息。"),
]

DEFAULT_DECISION_MEMORIES = [
    ("multi-tenant-strategy", "多租户方案选择：使用 tenant_id 列隔离，而非独立数据库。优点：运维简单、成本低。缺点：需严格 WHERE tenant_id 约束。"),
]


def init_default_memories(workspace_dir: Path) -> None:
    """Initialize default memories in workspace."""
    memory = MemorySystem(workspace_dir)

    for name, content in DEFAULT_ERROR_MEMORIES:
        memory.write_error(name, content)

    for name, content in DEFAULT_PATTERN_MEMORIES:
        memory.write_pattern(name, content)

    for name, content in DEFAULT_DECISION_MEMORIES:
        memory.write_decision(name, content)

    logger.info("Initialized %d default memories", len(DEFAULT_ERROR_MEMORIES) + len(DEFAULT_PATTERN_MEMORIES) + len(DEFAULT_DECISION_MEMORIES))


def load_memory_context(workspace_dir: Path, categories: list[str] | None = None) -> str:
    """Load memory context for injection into agent prompts.

    Returns a formatted string with all memory entries, or empty string
    if no memories exist. Safe to call even if workspace_dir doesn't exist.

    Args:
        workspace_dir: Path to the workspace directory (contains memory/ subdir)
        categories: Optional filter — ["errors", "patterns", "decisions"]
    """
    memory_dir = workspace_dir / "memory"
    if not memory_dir.exists():
        return ""

    try:
        memory = MemorySystem(workspace_dir)
        return memory.build_context_prompt(categories=categories)
    except Exception as e:
        logger.warning("Failed to load memory context: %s", e)
        return ""