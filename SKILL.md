---
name: agent-worker
description: >-
  Delegate repository implementation, debugging, plan/architecture critique, or
  independent cross-model code review to local coding agents (`codex`, `claude`,
  `qoder`, `kimi`, `devin`) from any host agent (Claude Code, Codex,
  Antigravity, Cursor, etc.). Use when the user asks to delegate to
  Codex/Claude/Qoder/Kimi/Devin or when a task materially benefits from an
  autonomous edit-verify loop, parallel isolated git worktrees, or a fresh
  second-opinion review of a design plan or diff.
---

# Agent Worker

## 1. Delegation, Backend Choice & Quota Discipline

- **When to delegate vs. edit directly**:
  - **Delegate** iterative edit-test-fix loops, multi-file changes/refactors, parallel `git worktree` streams, or independent plan/diff review.
  - **Edit directly in the host**—even when acting as an "orchestrator" or following up on a Worker-authored PR/branch (unless the user explicitly forbids host edits)—when target lines and fix are already clear (5–10 line tweaks, constants/thresholds/copy, small review nits, or running one known command).
- **Avoid self-delegation**: default to a *different* CLI than the current host unless the user explicitly asks for a fresh isolated worker of the same CLI.
- **Backend defaults** (routine → review):
  - `codex`: `-m` omitted → Codex's built-in default model (`-e medium` routine; `-e high` for review, or pass `-m <flagship>` for hard architecture/high-stakes review). Keep `service_tier=default`—never offer/use `fast`/`ultrafast` unless the user explicitly asks.
  - `claude`: routine `sonnet` (`-e medium`); review `opus` (`-e high`).
  - `qoder`: routine `DeepSeek-Flash` (`-e high`); review `Ultimate` (`-e high`); other IDs: `DeepSeek-V4-Pro`, `Qwen4-Max`, `Kimi-K3.1-Pro`, `GLM-5.3`, `Sonus`.
  - `kimi`: `-m` omitted → `~/.kimi-code/config.toml` `default_model` (or pass explicit `-m <alias>`).
  - `devin`: `-m` omitted → Devin's configured default (or `-m sonnet|opus|codex`); `--fork` is unsupported.
- Confirm once per session in 1 line when **proposing** a worker yourself; skip the questionnaire if the user already named the worker. Dial routine follow-ups back to routine defaults.

## 2. Core Commands (Live Progress + Clean Context)

Resolve `<skill-dir>` to the directory containing this `SKILL.md`. Single entrypoint: `python3 "<skill-dir>/scripts/run_worker.py" <codex|claude|qoder|kimi|devin> ...`. It streams readable commentary, changed files, test cues, and 40s heartbeats to stdout while saving raw events to `"$d/events.jsonl"`, confirmed session ID to `"$d/sid"`, and the final report to `"$d/out.md"` (requires POSIX macOS/Linux).

- **Direct background process ownership**: launch the runner directly with the host's native background mode (`run_in_background: true`, or `&` + `wait` in one background script). **Never** detach with `(&)` + `while sleep`—direct ownership gives instant wakeup and reclaims the full worker process group on cancel.
- Read **only** `"$d/out.md"` on exit `0`; inspect `"$d/stderr.log"` / `"$d/events.jsonl"` only on failure.

### Execute Task
```bash
d=$(mktemp -d /tmp/worker.codex.XXXXXX)
python3 "<skill-dir>/scripts/run_worker.py" codex -C "$repo" -d "$d" -e "${effort:-medium}" <<'EOF'
<goal, known repro/errors/paths, scoped verification expectations, and git stopping point>
EOF
```
- Add `--skip-git-repo-check` only when `$repo` is outside Git; add `--ephemeral` only on `codex`/`claude`/`qoder` (never with `--resume`/`--fork`; `kimi`/`devin` reject it).

### Independent Plan / Diff Review
```bash
# Codex supports native diff review (--base / --commit / --uncommitted reject a prompt):
d2=$(mktemp -d /tmp/worker.review.XXXXXX)
python3 "<skill-dir>/scripts/run_worker.py" codex -C "$repo" -d "$d2" -o "$d2/review.md" --review --base "$base"

# Plan critique (all backends) or diff review on claude/qoder/kimi/devin — use quoted <<'EOF':
d3=$(mktemp -d /tmp/worker.review.XXXXXX)
python3 "<skill-dir>/scripts/run_worker.py" qoder -C "$repo" -d "$d3" -o "$d3/review.md" --review <<'EOF'
Read-only review (do not modify files): inspect git diff <base>...HEAD (or verify plan against codebase) and report real bugs, regressions, and missing edge cases.
EOF
```

### Resume / Fork
```bash
d2=$(mktemp -d /tmp/worker.resume.XXXXXX)  # fresh -d; reusing $d overwrites events/exit logs
python3 "<skill-dir>/scripts/run_worker.py" codex -C "$repo" -d "$d2" -o "$d2/fix.md" \
  --resume "$(cat "$d/sid")" <<'EOF'
<verified findings to fix and re-test; remind the worker to re-read files if edited externally>
EOF
# Branch an approach instead: `--fork "$(cat "$d/sid")"` with a fresh `-d` (supported on codex/claude/qoder/kimi).
```

## 3. High-Leverage Guardrails

- **Session reuse (`--resume` vs `--fork` vs fresh)**: `--resume` while investigation context is still lean. Once a session has stacked many rounds, **stop stacking `--resume`**—handle small follow-ups directly in the host or start fresh with a concise handoff instead of re-billing a bloated transcript.
- **Scoped verification & transparent report**: pass failing commands, key errors, relevant paths, ruled-out causes, and the stopping point (uncommitted diff vs commit vs PR). Instruct the worker to follow repo docs and run **fast, targeted non-interactive checks matching the change's impact** (updating affected generated artifacts/snapshots if touched, without running slow full-CI suites), and in `"$d/out.md"` report **exact verification commands run, any non-obvious toolchain/env paths used, unverified areas, and out-of-scope edits**.
- **One writer per checkout**: never run two write workers in the same directory concurrently; queue incoming feedback until the active run exits, and if a host wait times out, verify whether the background task is still alive before acting.
- **Parallel `git worktree`**: create checkouts **outside** the main repo root. Group edits touching shared lockfiles/generated artifacts into one stream; when landing sequentially, rebase, rebuild shared artifacts, re-run affected checks, then clean up temporary worktrees and `$d` dirs.
