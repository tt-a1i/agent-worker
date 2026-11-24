<div align="center">

# agent-worker

**Let any host AI agent (Claude Code, Codex, Antigravity, Cursor, etc.) orchestrate local `codex`, `claude`, `qoder`, `kimi`, and `devin` CLIs as disciplined background workers and cross-model reviewers.**

*One unified runner · Glanceable live progress · Zero context bloat · Clean process-group teardown · Zero dependencies*

<p>
  <a href="https://github.com/tt-a1i/agent-worker/actions/workflows/ci.yml"><img src="https://github.com/tt-a1i/agent-worker/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Skill-Agent%20Skills%20Standard-2563EB?style=flat-square" alt="Agent Skills Standard">
  <img src="https://img.shields.io/badge/Python-3.9%2B%20(Stdlib)-10B981?style=flat-square" alt="Python 3.9+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-64748B?style=flat-square" alt="MIT License"></a>
</p>

<p>
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

</div>

---

## Installation

```bash
# Option 1: Install via the Agent Skills CLI
npx skills add tt-a1i/agent-worker

# Option 2: Clone directly into your agent's skills directory
git clone https://github.com/tt-a1i/agent-worker.git ~/.claude/skills/agent-worker
```

Once installed, ask your host agent naturally (*"Have Codex fix the failing parser tests"*, *"Run an independent review with Qoder"*) or invoke `/agent-worker`.

### Live Progress Stream

`run_worker.py` streams calm, human-readable milestones to the background task panel while keeping noisy shell output and the full final report isolated in `$d/out.md`:

```text
🚀 Qoder · DeepSeek-Flash (high) · my-repo
──────────────────────────────────────────────────────

💬 我先检查这两个文件的内容。                    00:02

💬 已定位问题：mul 返回的是和而不是积，现在修复并
   补充零值边界测试。                            00:04
   ✏️ src/math_ops.py
   ✏️ tests/test_math.py

💬 修复完成，现在运行测试验证。                  00:06
   🧪 运行测试/检查中...

──────────────────────────────────────────────────────
✅ 完成 · 5 步 · 0.27 credits                    00:09
```

## Features

- **Unified entrypoint**: `scripts/run_worker.py <backend> ...` with shared flags and output conventions across all five backends.
- **Live progress**: raw streaming events are translated into `💬` commentary, `✏️` file edits, `🧪` test cues, `🤝` subagents, and 40-second heartbeats instead of noisy raw logs.
- **Clean context**: on exit 0 read only `out.md`; raw events, stderr, session ID, and exit code are persisted separately for debugging.
- **Session reuse**: `--resume` continues a session and `--fork` branches one (Devin does not support fork; Kimi resolves the forked session ID automatically).
- **Isolated runtime (partial)**: `codex`/`claude`/`qoder` skip user-global MCP/plugin configuration by default (auth is preserved); add `--include-user-config` to restore it. `kimi`/`devin` have no such isolation.
- **Process-group lifecycle**: SIGINT/SIGTERM/SIGHUP are forwarded to the whole process group; a cancel that outlives a 5-second grace (or a second signal) escalates to SIGKILL, and surviving descendants plus inherited pipes are reclaimed even after the group leader exits.
- **UTF-8 safe**: byte-level buffering with line splitting handles multibyte boundaries across 8192-byte chunks and EOF without a trailing newline.
- **Standard library only**.

## Requirements

- Python 3.9+ (stdlib only).
- **POSIX system** (macOS / Linux): process groups, POSIX signals, and `select`-based pipe supervision are required; native Windows is unsupported.
- At least one installed and authenticated backend CLI:
  - `codex` (Codex CLI)
  - `claude` (Claude Code)
  - `qodercli` (Qoder)
  - `kimi` (Kimi Code)
  - `devin` (the CLI bundled with the Devin desktop app)
- Common macOS install locations are probed automatically; elsewhere ensure the binary is on `PATH` or point to it via an environment variable.

## Quick start

```bash
git clone https://github.com/tt-a1i/agent-worker.git
d=$(mktemp -d /tmp/worker.codex.XXXXXX)
python3 agent-worker/scripts/run_worker.py codex -C /path/to/repo -d "$d" <<'EOF'
Fix the failing case in tests/test_parser.py: reproduce first, then make the minimal fix and run only the affected tests.
In out.md, report the exact verification commands you ran and anything left unverified.
EOF
cat "$d/out.md"
```

## Backends and default models

| Backend | Routine | Review |
| --- | --- | --- |
| `codex` | Codex built-in default when `-m` is omitted (`-e medium`) | `-e high` (pass an explicit flagship `-m` for hard architecture/high-stakes review) |
| `claude` | `sonnet` (`-e medium`) | `opus` (`-e high`) |
| `qoder` | `DeepSeek-Flash` (`-e high`) | `Ultimate` (`-e high`) |
| `kimi` | `-m` omitted → `default_model` from `~/.kimi-code/config.toml` | pass explicit `-m <alias>` |
| `devin` | `-m` omitted → Devin's configured default | `-m sonnet\|opus\|codex` |

Other Qoder model IDs: `DeepSeek-V4-Pro`, `Qwen4-Max`, `Kimi-K3.1-Pro`, `GLM-5.3`, `Sonus`. Codex keeps `service_tier=default`; do not use `fast`/`ultrafast` unless the user explicitly asks for it.

## Backend capability matrix

| Backend | `--ephemeral` | `--fork` | Config isolation (default) | Native diff review |
| --- | --- | --- | --- | --- |
| `codex` | ✅ | ✅ | ✅ `--ignore-user-config` | ✅ `--base`/`--commit`/`--uncommitted` |
| `claude` | ✅ (never with `--resume`/`--fork`) | ✅ | ✅ project-level config/MCP only | ❌ |
| `qoder` | ✅ (same) | ✅ | ✅ project-level config/MCP only | ❌ |
| `kimi` | ❌ | ✅ resolves the new session ID | ❌ user config retained | ❌ |
| `devin` | ❌ | ❌ | ❌ user config retained | ❌ |

`--ephemeral` exists only where the backend can truly disable persistence, and it is mutually exclusive with `--resume`/`--fork`; `kimi`/`devin` reject it up front. `sid` is written only after a new (or forked) session ID is confirmed. Devin's `events.jsonl` is derived from its SQLite session database (`sessions.db`) and may be empty when the DB is unreadable.

## Common commands

### Execute a task

```bash
d=$(mktemp -d /tmp/worker.claude.XXXXXX)
python3 scripts/run_worker.py claude -C "$repo" -d "$d" -m sonnet -e medium <<'EOF'
<goal, known repro/errors/paths, verification expectations, and git stopping point>
EOF
```

### Independent plan or diff review

```bash
# Codex native diff review
python3 scripts/run_worker.py codex -C "$repo" -d "$d2" -o "$d2/review.md" --review --base main

# Other backends: --review with a read-only prompt
python3 scripts/run_worker.py qoder -C "$repo" -d "$d3" -o "$d3/review.md" --review <<'EOF'
Read-only review (do not modify files): inspect `git diff main...HEAD` and report real bugs, regressions, and missing edge cases.
EOF
```

### Resume and fork

```bash
# Resume (always use a fresh -d so previous logs are not overwritten)
d2=$(mktemp -d /tmp/worker.resume.XXXXXX)
python3 scripts/run_worker.py codex -C "$repo" -d "$d2" --resume "$(cat "$d/sid")" <<'EOF'
Fix the verified findings below and re-run the checks...
EOF

# Branch a second approach (use the same backend that produced the sid)
python3 scripts/run_worker.py codex -C "$repo" -d "$d3" --fork "$(cat "$d/sid")" <<'EOF'
Try an alternative implementation...
EOF
```

## Output files

| File | Purpose |
| --- | --- |
| `out.md` | Final report (the only file to read on success) |
| `events.jsonl` | Raw backend event stream |
| `stderr.log` | Backend stderr (inspect on failure) |
| `sid` | New session ID (written only after confirmation; for `--resume`/`--fork`) |
| `exit` | Final exit code |
| `.prompt.*` | Oversized Kimi / Devin prompt files (mode `0600`, removed when the run ends) |

## Environment variables and local config

| Variable / Config | Purpose |
| --- | --- |
| `~/.config/agent-worker/config.json` | Optional local config, e.g. `{"allowed_backends": ["codex", "qoder"]}` to restrict enabled backends on a machine |
| `AGENT_WORKER_ALLOWED` | Comma-separated backend allowlist (e.g. `codex,qoder`, or `*` for all); overrides the config file |
| `AGENT_WORKER_CONFIG` | Override the config file path (set to empty string `""` to skip loading the default config) |
| `AGENT_WORKER_<BACKEND>_BIN` | Override the backend executable path, e.g. `AGENT_WORKER_CODEX_BIN` |
| `AGENT_WORKER_DEVIN_DB` | Override the Devin session database path (default `~/.local/share/devin/cli/sessions.db`) |

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/run_worker.py
```

Tests use temporary mock executables and a temporary SQLite database; no real model is called.

## Layout

```text
agent-worker/
├── SKILL.md                 # Skill instructions for host agents
├── scripts/
│   └── run_worker.py        # Unified runner (stdlib only)
├── tests/
│   └── test_run_worker.py   # End-to-end unit tests
├── README.md
├── README.en.md
├── LICENSE
└── .gitignore
```

## License

MIT, see [LICENSE](LICENSE).
