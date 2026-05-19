# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Hermes Agent** — self-improving AI agent by Nous Research. Python 3.11+ project with a Node.js TUI frontend. Version 0.14.0.

Key surfaces:
- Interactive CLI (prompt_toolkit + Rich)
- Terminal TUI (React Ink + Python JSON-RPC gateway)
- Messaging gateway (Telegram, Discord, Slack, WhatsApp, DingTalk, Feishu, etc.)
- Web dashboard (FastAPI + xterm.js PTY bridge)
- Plugin/skill system with MCP support

## Development Setup

```bash
# Create venv with Python 3.11
uv venv .venv --python 3.11
source .venv/bin/activate          # POSIX
.venv\Scripts\activate             # Windows

# Install all deps
uv pip install -e ".[all,dev]"

# User config lives at ~/.hermes/config.yaml; secrets in ~/.hermes/.env
```

## Essential Commands

```bash
# Run the agent interactively
hermes

# Run the TUI
hermes --tui

# Start messaging gateway
hermes gateway start

# Run tests (ALWAYS use this — hermetic env, 4 xdist workers, matches CI)
scripts/run_tests.sh                          # full suite
scripts/run_tests.sh tests/gateway/           # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # one test
scripts/run_tests.sh -v --tb=long             # pass-through pytest flags

# Lint / type check
ruff check .                                  # lint
ty check                                      # type check (from pyproject.toml)

# TUI development
cd ui-tui && npm run dev                      # watch mode
cd ui-tui && npm run build                    # full build
cd ui-tui && npm run type-check               # tsc --noEmit
cd ui-tui && npm test                         # vitest
```

## Architecture (High-Level)

### Core Loop

```
User message → AIAgent.run_conversation()
  → Build system prompt (prompt_builder.py)
  → Call LLM (OpenAI-compatible API)
  → If tool_calls: dispatch via tools/registry.py → add results → loop
  → If text response: persist session, return
  → Context compression if approaching token limit
```

### Key Files

| File | Responsibility |
|------|---------------|
| `run_agent.py` | `AIAgent` class — core conversation loop (~12k LOC) |
| `cli.py` | `HermesCLI` class — interactive CLI (~11k LOC) |
| `model_tools.py` | Tool orchestration, `handle_function_call()` |
| `toolsets.py` | Tool groupings, `_HERMES_CORE_TOOLS` list |
| `hermes_state.py` | SQLite session store with FTS5 search |
| `hermes_constants.py` | `get_hermes_home()`, `display_hermes_home()` — profile-aware paths |
| `hermes_cli/commands.py` | Central slash command registry (`COMMAND_REGISTRY`) |
| `hermes_cli/main.py` | Entry point, argument parsing, profile override |

### Directory Map

```
hermes-agent/
├── run_agent.py, cli.py, model_tools.py, toolsets.py   # Core
├── agent/          # Agent internals (prompts, memory, compression, providers)
├── hermes_cli/     # CLI subcommands, setup wizard, plugins loader, skin engine
├── tools/          # Tool implementations (self-registering via registry.py)
│   └── environments/  # Terminal backends (local, docker, ssh, modal, ...)
├── gateway/        # Messaging gateway (run.py + platforms/)
├── plugins/        # Plugin system (memory, model-providers, kanban, ...)
├── skills/         # Built-in skills
├── optional-skills/  # Heavier skills not active by default
├── ui-tui/         # Ink (React) terminal UI — `hermes --tui`
├── tui_gateway/    # Python JSON-RPC backend for TUI
├── cron/           # Scheduler (jobs.py, scheduler.py)
├── acp_adapter/    # ACP server (VS Code / Zed / JetBrains integration)
└── tests/          # Pytest suite (~17k tests)
```

### Tool Registration

Tools self-register at import time via `tools/registry.py`. Two steps required for new core tools:

1. Create `tools/your_tool.py` with `registry.register(...)` call
2. Add tool name to appropriate list in `toolsets.py` (e.g. `_HERMES_CORE_TOOLS`)

Auto-discovery handles step 1; step 2 is manual — without it the tool registers but isn't exposed to agents.

### Slash Command Registry

All slash commands defined in `COMMAND_REGISTRY` in `hermes_cli/commands.py`. Single source of truth — CLI dispatch, gateway routing, Telegram menu, Slack mapping, autocomplete, and help text all derive from it automatically.

To add a command:
1. Add `CommandDef` to `COMMAND_REGISTRY` in `hermes_cli/commands.py`
2. Add handler in `HermesCLI.process_command()` in `cli.py`
3. If gateway-available, add handler in `gateway/run.py`

### Plugin System

Two plugin surfaces:
- **General plugins** (`plugins/<name>/`): `register(ctx)` → lifecycle hooks, tools, CLI subcommands
- **Memory providers** (`plugins/memory/<name>/`): `MemoryProvider` ABC — set of built-in providers is closed; new ones ship as standalone repos

**Rule:** Plugins MUST NOT modify core files (`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`). Expand the generic plugin surface instead.

## Critical Policies

### Prompt Caching
Never alter past context mid-conversation. Cache-breaking forces dramatically higher costs. Slash commands that mutate system-prompt state default to deferred invalidation with `--now` opt-in.

### Dependency Pinning
All PyPI deps need upper bounds: `>=floor,<next_major`. Post-1.0: `<next_major`. Pre-1.0: `<0.(current_minor + 2)`. Never bare `>=X.Y.Z` without ceiling. Run `uv lock` after any dep change.

### Profile-Safe Code
Always use `get_hermes_home()` for code paths and `display_hermes_home()` for user-facing messages. Never hardcode `~/.hermes` or `Path.home() / ".hermes"` — breaks profile isolation.

### Windows Cross-Platform
- Never `os.kill(pid, 0)` for liveness — use `psutil.pid_exists()` instead
- Use `shutil.which()` before shelling out
- Guard `termios`, `fcntl`, `os.setsid`, `os.killpg` behind platform checks
- Use `pathlib.Path` for paths
- Config files: `encoding="utf-8-sig"` (Notepad BOM)
- Detached daemons: `pythonw.exe`, not `python.exe`

### Test Guidelines
- Always use `scripts/run_tests.sh` — never call `pytest` directly
- Tests must not write to `~/.hermes/` — `_isolate_hermes_home` fixture redirects to temp dir
- Don't write change-detector tests (snapshot assertions on model catalogs, config version literals, enumeration counts). Write invariant tests instead.
- Integration tests: mark with `@pytest.mark.integration` (excluded by default)

### Adding Configuration
- Non-secret settings → `DEFAULT_CONFIG` in `hermes_cli/config.py`
- Secrets → `OPTIONAL_ENV_VARS` in `hermes_cli/config.py`
- Bump `_config_version` ONLY if migrating existing user config (renaming keys, changing structure). Adding a new key does NOT require a version bump.
- Three loaders: `load_cli_config()` (CLI), `load_config()` (hermes tools/setup), direct YAML load (gateway). Use the right one.

## Skill Authoring Standards

- `description` ≤ 60 chars, one sentence, ends with period. No marketing words.
- Reference Hermes tools by name in backticks (`` `terminal` ``, `` `web_search` ``, etc.), not shell utilities (`grep` → `search_files`, `cat` → `read_file`).
- Scripts in `scripts/`, references in `references/`, templates in `templates/`.
- Tests at `tests/skills/test_<skill>_skill.py` — stdlib + pytest + mock only, no network calls.
- Heavy/niche skills go in `optional-skills/`, not `skills/`.

## TUI Architecture

```
hermes --tui
  └─ Node (Ink/React) ──stdio JSON-RPC──  Python (tui_gateway)
                                                └─ AIAgent + tools + sessions
```

TypeScript owns the screen. Python owns sessions, tools, model calls, slash command logic. Transport: newline-delimited JSON-RPC over stdio.

**Dashboard embeds real `hermes --tui` via PTY bridge** — do not re-implement the chat experience in React. Structured sidebar/inspector views around the TUI are fine when they complement, not replace, the embedded terminal.

## Known Pitfalls

- **`_last_resolved_tool_names`** is a process-global in `model_tools.py`. Subagent execution temporarily stales it.
- **Gateway has TWO message guards** — base adapter + gateway runner. New commands must bypass both to reach the runner while agent is blocked.
- **Plugin discovery timing:** `discover_plugins()` runs as side effect of importing `model_tools.py`. Code reading plugin state without importing `model_tools.py` first must call `discover_plugins()` explicitly.
- **`simple_term_menu`** — do not introduce new usage. Use `hermes_cli/curses_ui.py` instead.
- **Don't use `\033[K`** (ANSI erase-to-EOL) in spinner code — leaks as literal `?[K` under prompt_toolkit. Use space-padding.
- **Squash merges from stale branches** silently revert recent fixes. Verify with `git diff HEAD~1..HEAD` after merging.
