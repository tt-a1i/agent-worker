#!/usr/bin/env python3
"""Unified worker runner: stream clean progress for codex/claude/qoder/kimi/devin and save reports to out.md."""
from __future__ import annotations

import argparse
import codecs
import errno
import json
import os
import pathlib
import re
import selectors
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid

TEST_BUILD_RE = re.compile(
    r"\b(vitest|jest|pytest|unittest|ctest|cargo\s+(?:test|check|clippy|build)|go\s+test|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|build|check|lint|typecheck)|"
    r"swift\s+(?:build|test)|xcodebuild|tsc\b|run-tests|just\s+(?:t|l|test|check)|make\s+(?:test|check|build))\b"
)
PROGRESS_HINT = "工具调用之间的过渡进度说明必须使用一句简短中文（不带 Markdown 标题或列表）；最终报告语言按用户任务要求。"
RULE_LINE = "─" * 50
BODY_WIDTH = 58
TIMELINE_RAIL = "      │"
TOKEN_RE = re.compile(r"[A-Za-z0-9_./:@+-]+| +|[^A-Za-z0-9_./:@+-]")

BACKENDS = ("codex", "claude", "qoder", "kimi", "devin")
TITLES = {"codex": "Codex", "claude": "Claude", "qoder": "Qoder", "kimi": "Kimi", "devin": "Devin"}
BIN_NAMES = {"codex": "codex", "claude": "claude", "qoder": "qodercli", "kimi": "kimi", "devin": "devin"}
FALLBACK_BINS = {
    "codex": [],
    "claude": ["~/.npm-global/bin/claude", "~/.local/bin/claude"],
    "qoder": ["~/.qoder/entry/qodercli", "~/.local/bin/qodercli"],
    "kimi": ["~/.kimi-code/bin/kimi", "~/.local/bin/kimi"],
    "devin": [
        "/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin",
        "~/.local/bin/devin",
    ],
}

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "StrReplaceFile", "WriteFile")
BASH_TOOLS = ("Bash", "Shell")
TASK_TOOLS = ("Task", "Agent", "Subagent")

CANCEL_GRACE = 5.0
DRAIN_DEADLINE = 0.5
KIMI_FORK_TIMEOUT = 30.0
KIMI_PROMPT_FILE_THRESHOLD = 96_000
DEVIN_POLL_INTERVAL = 1.0
THINK_INTERVAL = 12.0
DEFAULT_CONFIG_PATH = "~/.config/agent-worker/config.json"


def fmt_elapsed(t0: float) -> str:
    sec = max(0, int(time.monotonic() - t0))
    minutes, seconds = divmod(sec, 60)
    if minutes < 100:
        return f"{minutes:02d}:{seconds:02d}"
    hours, remaining_minutes = divmod(minutes, 60)
    if hours < 10:
        return f"{hours}h{remaining_minutes:02d}m"
    return f"{min(hours, 99):02d}h+ "


def safe_print(text: str = "") -> float:
    try:
        print(text, flush=True)
    except BrokenPipeError:
        pass
    return time.monotonic()


def short_path(p: str, root: str) -> str:
    try:
        real_p = os.path.realpath(p)
        real_r = os.path.realpath(root)
        rp = os.path.relpath(real_p, real_r)
        if not rp.startswith(".."):
            parts = pathlib.Path(rp).parts
            return str(pathlib.Path(*parts[-3:])) if len(parts) > 4 else rp
    except Exception:
        pass
    parts = pathlib.Path(p).parts
    if len(parts) <= 3:
        return p
    if parts[:2] == ("/", "tmp") or (len(parts) >= 3 and parts[1] == "tmp"):
        return "/tmp/" + "/".join(parts[-2:]) if len(parts) > 3 else p
    return "/".join(parts[-3:])


def char_width(ch: str) -> int:
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    cp = ord(ch)
    if cp >= 0x1F300 or (0x2600 <= cp <= 0x27BF) or (0x2300 <= cp <= 0x23FF):
        return 2
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def disp_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def _split_long_token(tok: str, max_w: int) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    w = 0
    for ch in tok:
        cw = char_width(ch)
        if cur and w + cw > max_w:
            out.append("".join(cur))
            cur = [ch]
            w = cw
        else:
            cur.append(ch)
            w += cw
    if cur:
        out.append("".join(cur))
    return out


def wrap_display_lines(text: str, max_w: int) -> list[str]:
    out: list[str] = []
    for para in text.splitlines():
        para = para.strip()
        if not para:
            continue
        cur = ""
        cur_w = 0
        for tok in TOKEN_RE.findall(para):
            if tok.isspace():
                if cur and cur_w + 1 <= max_w:
                    cur += " "
                    cur_w += 1
                continue
            tw = disp_width(tok)
            if tw > max_w:
                if cur.rstrip():
                    out.append(cur.rstrip())
                    cur, cur_w = "", 0
                pieces = _split_long_token(tok, max_w)
                out.extend(pieces[:-1])
                cur = pieces[-1]
                cur_w = disp_width(cur)
            elif cur and cur_w + tw > max_w:
                out.append(cur.rstrip())
                cur = tok
                cur_w = tw
            else:
                cur += tok
                cur_w += tw
        if cur.rstrip():
            out.append(cur.rstrip())
    return out


def clean_progress_text(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""
    cleaned = cleaned.replace("…", "...")
    cleaned = re.sub(r"`([^`\n]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", cleaned)
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    return "\n".join(lines)


def format_timeline_block(icon: str, text: str, ts: str | None = None) -> str:
    lines = wrap_display_lines(clean_progress_text(text), BODY_WIDTH) or [""]
    if len(lines) > 4:
        lines = lines[:3] + [lines[3][: max(1, len(lines[3]) - 3)].rstrip() + "..."]

    icon_prefix = f"{icon} "
    indent = " " * disp_width(icon_prefix)
    rail = TIMELINE_RAIL if ts is None else f"{ts} │"
    rendered = [f"{rail} {icon_prefix}{lines[0]}"]
    rendered.extend(f"{TIMELINE_RAIL} {indent}{line}" for line in lines[1:])
    return "\n".join(rendered)


def truncate_display_width(text: str, max_w: int) -> str:
    if disp_width(text) <= max_w:
        return text
    limit = max(1, max_w - 3)
    out: list[str] = []
    w = 0
    for ch in text:
        cw = char_width(ch)
        if w + cw > limit:
            break
        out.append(ch)
        w += cw
    return "".join(out).rstrip() + "..."


def summarize_thought(text: str, max_w: int = BODY_WIDTH) -> str:
    cleaned = clean_progress_text(text)
    if not cleaned:
        return ""
    first = cleaned.splitlines()[0].strip()
    first = re.sub(r"^(?:#+\s*|[-*•]\s*|\d+[.)]\s*)", "", first).strip()
    if not first:
        return ""
    if disp_width(first) > max_w:
        for sep in ("。", "！", "？", ". ", "; ", "；"):
            idx = first.find(sep)
            if idx > 0:
                cand = first[: idx + (1 if sep in "。！？；" else 0)].strip()
                if 10 <= disp_width(cand) <= max_w:
                    return cand
        return truncate_display_width(first, max_w)
    return first


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def extract_thinking(msg_or_ev: dict) -> str:
    rc = msg_or_ev.get("reasoning_content")
    if isinstance(rc, str) and rc.strip():
        return rc
    content = msg_or_ev.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("thinking", "think", "reasoning"):
                val = block.get("thinking") or block.get("think") or block.get("text") or ""
                if isinstance(val, str) and val.strip():
                    parts.append(val)
        return "\n".join(parts)
    return ""


def parse_tool_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def resolve_bin(backend: str) -> str:
    env = (
        os.environ.get(f"AGENT_WORKER_{backend.upper()}_BIN")
        or os.environ.get(f"CLI_WORKER_{backend.upper()}_BIN")
    )
    if env:
        return os.path.expanduser(env)
    found = shutil.which(BIN_NAMES[backend])
    if found:
        return found
    for cand in FALLBACK_BINS[backend]:
        p = os.path.expanduser(cand)
        if os.path.exists(p):
            return p
    return BIN_NAMES[backend]


def fmt_cost(total_cost_usd, total_credits) -> str:
    if isinstance(total_cost_usd, (int, float)) and total_cost_usd > 0:
        return f" · ${total_cost_usd:.2f}"
    if isinstance(total_credits, (int, float)):
        return f" · {total_credits:.2f} credits"
    return ""


def handle_tools(tools, ctx, ui, steps_ref: dict, seen: set) -> None:
    for idx, tc in enumerate(tools or []):
        if not isinstance(tc, dict):
            continue
        tid = str(tc.get("id") or "")
        key = tid or f"anon:{idx}:{json.dumps(tc, sort_keys=True, ensure_ascii=False)}"
        if key in seen:
            continue
        seen.add(key)
        steps_ref["steps"] += 1
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        tname = fn.get("name") or tc.get("name") or ""
        args = parse_tool_args(fn.get("arguments", tc.get("arguments", {})))
        if tname in EDIT_TOOLS:
            fp = args.get("path") or args.get("file_path") or ""
            if fp:
                ui.sub("✏️", short_path(str(fp), ctx.repo))
        elif tname in BASH_TOOLS:
            cmdstr = args.get("command", "")
            if isinstance(cmdstr, str) and TEST_BUILD_RE.search(cmdstr):
                ui.sub("🧪", "运行测试/检查中…")
        elif tname in TASK_TOOLS:
            desc = args.get("description") or args.get("subagent_type") or "subagent"
            ui.sub("🤝", f"子代理: {desc}")


class UI:
    def __init__(self, thinking_mode: str = "auto"):
        self.t0 = time.monotonic()
        self.last_print = self.t0
        self.last_narrative = self.t0
        self.thinking_mode = thinking_mode
        self.last_thought = ""
        self.has_body = False
        self.last_sub = None

    def note(self, msg: str) -> None:
        self.last_narrative = time.monotonic()
        self.last_thought = ""
        self.status("💬", msg)

    def thought(self, raw: str) -> None:
        if self.thinking_mode == "off":
            return
        summary = summarize_thought(raw)
        if not summary or summary == self.last_thought:
            return
        now = time.monotonic()
        if self.thinking_mode == "auto" and (now - self.last_narrative) < THINK_INTERVAL:
            return
        self.last_thought = summary
        self.last_narrative = now
        self.status("💭", summary)

    def sub(self, icon: str, msg: str) -> None:
        clean_msg = msg.replace("…", "...")
        if self.last_sub == (icon, clean_msg):
            return
        if self.has_body:
            self.last_print = safe_print(format_timeline_block(icon, clean_msg))
        else:
            self.status(icon, clean_msg)
        self.last_sub = (icon, clean_msg)

    def status(self, icon: str, msg: str) -> None:
        if self.has_body:
            safe_print(TIMELINE_RAIL)
        self.last_print = safe_print(format_timeline_block(icon, msg, self.elapsed()))
        self.has_body = True
        self.last_sub = None

    def heartbeat(self, seconds: int, steps: int) -> None:
        if seconds > 0 and (time.monotonic() - self.last_print) >= seconds:
            self.status("⏳", f"思考/执行中 ({steps} 步)...")

    def elapsed(self) -> str:
        return fmt_elapsed(self.t0)


class LineSink:
    def __init__(self, process_line):
        self.process_line = process_line
        self.buf = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.buf.extend(chunk)
        while True:
            idx = self.buf.find(b"\n")
            if idx < 0:
                return
            line = bytes(self.buf[:idx])
            del self.buf[: idx + 1]
            self.process_line(line)

    def finish(self) -> None:
        if self.buf:
            self.process_line(bytes(self.buf))
            self.buf.clear()


class RawSink:
    def __init__(self):
        self.data = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.data.extend(chunk)

    def finish(self) -> None:
        pass


def jsonl_sink(events_file, handle_event) -> LineSink:
    """Decode one JSONL line at a time, log it verbatim, and hand dict events to the backend."""

    def process_line(line: bytes) -> None:
        raw = line.decode("utf-8", errors="replace").strip()
        if not raw:
            return
        events_file.write(raw + "\n")
        events_file.flush()
        try:
            ev = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(ev, dict):
            handle_event(ev)

    return LineSink(process_line)


class Ctx:
    def __init__(self, args, is_review: bool, fixed_review_target: bool):
        self.a = args
        self.backend = args.backend
        self.title = TITLES[self.backend]
        self.repo = os.path.abspath(args.repo)
        self.repo_name = os.path.basename(self.repo.rstrip("/")) or self.repo
        if args.out_dir:
            self.out_dir = pathlib.Path(os.path.abspath(args.out_dir))
        else:
            self.out_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"worker.{self.backend}.", dir="/tmp"))
        self.out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.out_file = (
            pathlib.Path(os.path.abspath(args.out_file)) if args.out_file else self.out_dir / "out.md"
        )
        self.events_file = self.out_dir / "events.jsonl"
        self.stderr_file = self.out_dir / "stderr.log"
        self.sid_file = self.out_dir / "sid"
        self.exit_file = self.out_dir / "exit"
        # Never let a reused -d leak the previous run's terminal state.
        self.exit_file.unlink(missing_ok=True)
        self.sid_file.unlink(missing_ok=True)
        self.is_review = is_review
        self.fixed_review_target = fixed_review_target
        self.prompt = ""
        self.missing_bin = False
        self.canceled = False
        self.cancel_sig = int(signal.SIGTERM)
        self.model, self.effort = resolve_model_effort(args, is_review)


def write_private_text(path: pathlib.Path, text: str) -> None:
    path.unlink(missing_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def resolve_model_effort(args, is_review: bool) -> tuple[str, str]:
    backend = args.backend
    if backend == "codex":
        return args.model or "", args.effort or ("high" if is_review else "medium")
    if backend == "claude":
        return (args.model or ("opus" if is_review else "sonnet"),
                args.effort or ("high" if is_review else "medium"))
    if backend == "qoder":
        return args.model or "DeepSeek-Flash", args.effort or "high"
    return args.model or "", ""


def log_error(ctx: Ctx, msg: str, mode: str = "a") -> None:
    try:
        with ctx.stderr_file.open(mode, encoding="utf-8") as log:
            log.write(msg + "\n")
    except OSError as exc:
        print(f"❌ 无法写入日志 {ctx.stderr_file}: {exc}", file=sys.stderr, flush=True)
    print(msg, file=sys.stderr, flush=True)


def write_report(ctx: Ctx, body: str, rc: int, ui: UI) -> int:
    try:
        ctx.out_file.parent.mkdir(parents=True, exist_ok=True)
        ctx.out_file.write_text(body or "", encoding="utf-8")
    except OSError as exc:
        log_error(ctx, f"❌ 无法写入报告 {ctx.out_file}: {exc}")
        return rc or 2
    if not body and rc == 0:
        ui.status("⚠️", "报告内容为空")
    return rc


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def execute(ctx: Ctx, cmd, *, cwd=None, feed_bytes=None, sink, tick=None, heartbeat=None,
            select_timeout: float = 5.0, timeout=None, stderr_mode: str = "w") -> int:
    rc = 0
    start = time.monotonic()
    with ctx.stderr_file.open(stderr_mode, encoding="utf-8") as err_out:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE if feed_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err_out,
                start_new_session=True,
            )
        except FileNotFoundError:
            msg = f"❌ 找不到可执行文件 {cmd[0]}，请确认已安装并在 PATH 中"
            err_out.write(msg + "\n")
            print(msg, file=sys.stderr, flush=True)
            ctx.missing_bin = True
            return 127
        except PermissionError as exc:
            msg = f"❌ 无权限执行 {cmd[0]}: {exc}"
            err_out.write(msg + "\n")
            print(msg, file=sys.stderr, flush=True)
            return 126
        except OSError as exc:
            msg = f"❌ 启动失败 {cmd[0]}: {exc}"
            err_out.write(msg + "\n")
            print(msg, file=sys.stderr, flush=True)
            return 127

        pgid = proc.pid
        cancel = {"deadline": None, "killed": False}
        timed_out = {"hit": False}

        def forward_sig(signum: int, _frame: object) -> None:
            ctx.canceled = True
            ctx.cancel_sig = int(signum)
            if cancel["deadline"] is not None:
                cancel["killed"] = True
                _killpg(pgid, signal.SIGKILL)
                return
            cancel["deadline"] = time.monotonic() + CANCEL_GRACE
            _killpg(pgid, signum)

        old_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                old_handlers[sig] = signal.signal(sig, forward_sig)
            except (ValueError, OSError):
                pass

        sel = None
        eof = {"done": False}
        try:
            sel = selectors.DefaultSelector()
            if feed_bytes is not None:
                def feed_stdin() -> None:
                    assert proc.stdin is not None
                    try:
                        proc.stdin.write(feed_bytes)
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                    finally:
                        try:
                            proc.stdin.close()
                        except (BrokenPipeError, OSError):
                            pass

                threading.Thread(target=feed_stdin, daemon=True).start()

            assert proc.stdout is not None
            fd = proc.stdout.fileno()
            os.set_blocking(fd, False)
            sel.register(proc.stdout, selectors.EVENT_READ)

            def pump() -> None:
                for _ in range(16):
                    if eof["done"]:
                        return
                    try:
                        chunk = os.read(fd, 65536)
                    except (BlockingIOError, InterruptedError):
                        return
                    except OSError:
                        eof["done"] = True
                        return
                    if not chunk:
                        eof["done"] = True
                        return
                    sink.feed(chunk)

            drain_deadline = None
            while True:
                now = time.monotonic()
                if cancel["deadline"] is not None and not cancel["killed"] and now >= cancel["deadline"]:
                    cancel["killed"] = True
                    _killpg(pgid, signal.SIGKILL)
                if timeout is not None and not timed_out["hit"] and (now - start) >= timeout:
                    timed_out["hit"] = True
                    _killpg(pgid, signal.SIGKILL)

                if not eof["done"]:
                    try:
                        # While draining after leader exit, poll the inherited pipe briefly.
                        wait = 0.1 if drain_deadline is not None else min(select_timeout, 0.2)
                        sel.select(timeout=wait)
                    except (OSError, ValueError):
                        eof["done"] = True
                    else:
                        pump()
                else:
                    # stdout closed but the leader may still be running: keep polling.
                    time.sleep(0.05)

                if tick is not None:
                    tick()
                if heartbeat is not None:
                    heartbeat()

                exited = proc.poll() is not None
                if exited and eof["done"]:
                    break
                if exited and not eof["done"]:
                    # A grandchild inherited stdout: drain briefly, then stop waiting.
                    if drain_deadline is None:
                        drain_deadline = time.monotonic() + DRAIN_DEADLINE
                    elif time.monotonic() >= drain_deadline:
                        eof["done"] = True
                        break
                if timed_out["hit"] and exited:
                    break

            wrc = proc.wait()
            rc = 124 if timed_out["hit"] else wrc
        finally:
            try:
                try:
                    sink.finish()
                except Exception:
                    pass
                if sel is not None:
                    sel.close()
            finally:
                try:
                    # The leader may already be gone while a descendant is still running.
                    _killpg(pgid, signal.SIGTERM)
                    grace_until = time.monotonic() + 1.0
                    while not cancel["killed"] and time.monotonic() < (cancel["deadline"] or grace_until):
                        try:
                            os.killpg(pgid, 0)
                        except ProcessLookupError:
                            break
                        except PermissionError:
                            pass
                        time.sleep(0.05)
                    _killpg(pgid, signal.SIGKILL)
                    proc.wait()
                finally:
                    for sig, old in old_handlers.items():
                        try:
                            signal.signal(sig, old)
                        except (ValueError, OSError):
                            pass
                    if proc.stdout is not None:
                        try:
                            proc.stdout.close()
                        except OSError:
                            pass

    if rc < 0:
        rc = 128 + abs(rc)
    if tick is not None:
        tick()
    return 128 + ctx.cancel_sig if ctx.canceled else rc


def run_codex(ctx: Ctx, ui: UI) -> tuple[int, int, str]:
    a = ctx.a
    stage = ctx.out_dir / f".out.stage.{os.getpid()}.md"
    stage.unlink(missing_ok=True)

    cmd = [resolve_bin("codex"), "exec", "-C", ctx.repo]
    if not a.include_user_config:
        cmd.append("--ignore-user-config")
    cmd.append("--dangerously-bypass-approvals-and-sandbox")
    if a.skip_git_repo_check:
        cmd.append("--skip-git-repo-check")
    if a.ephemeral:
        cmd.append("--ephemeral")
    if ctx.model:
        cmd.extend(["-m", ctx.model])
    cmd.extend([
        "-c", f"model_reasoning_effort={ctx.effort}",
        "-c", f"service_tier={a.tier}",
    ])
    if a.thinking != "off":
        cmd.extend(["-c", "model_reasoning_summary=concise"])
    if not ctx.is_review:
        cmd.extend(["-c", f"developer_instructions={PROGRESS_HINT}"])

    if a.resume is not None:
        cmd.extend(["resume", "--json", "-o", str(stage), a.resume, "-"])
    elif a.fork is not None:
        cmd.extend(["fork", "--json", "-o", str(stage), a.fork, "-"])
    elif ctx.is_review:
        cmd.extend(["review", "--json", "-o", str(stage)])
        if a.base:
            cmd.extend(["--base", a.base])
        elif a.commit:
            cmd.extend(["--commit", a.commit])
        elif a.uncommitted:
            cmd.append("--uncommitted")
        elif ctx.prompt.strip():
            cmd.append("-")
    else:
        cmd.extend(["--json", "-o", str(stage), "-"])

    st = {
        "sid": "", "steps": 0, "last_agent_text": "", "pending_note": "",
        "last_files_sig": "", "last_test_announce": -1e9, "active_tests": set(),
        "is_error": False,
    }

    def flush_pending_note() -> None:
        if st["pending_note"]:
            cleaned = clean_progress_text(st["pending_note"])
            st["pending_note"] = ""
            if cleaned:
                ui.note(cleaned)
                st["last_files_sig"] = ""

    def handle_event(ev: dict) -> None:
        etype = ev.get("type", "")
        if etype == "thread.started":
            st["sid"] = ev.get("thread_id", "")
            if st["sid"] and not a.ephemeral:
                ctx.sid_file.write_text(st["sid"] + "\n", encoding="utf-8")
        elif etype == "item.started":
            flush_pending_note()
            item = ev.get("item") or {}
            if item.get("type") == "command_execution":
                cstr = item.get("command", "")
                if isinstance(cstr, str) and TEST_BUILD_RE.search(cstr):
                    st["active_tests"].add(item.get("id", ""))
                    if (time.monotonic() - st["last_test_announce"]) >= 15.0:
                        ui.sub("🧪", "运行测试/检查中…")
                        st["last_test_announce"] = time.monotonic()
        elif etype == "item.completed":
            item = ev.get("item") or {}
            itype = item.get("type", "")
            if itype == "agent_message":
                txt = item.get("text", "")
                if txt.strip():
                    flush_pending_note()
                    st["last_agent_text"] = txt
                    st["pending_note"] = txt
            elif itype == "reasoning":
                if not st["pending_note"]:
                    rtxt = item.get("text") or ""
                    if not rtxt and isinstance(item.get("summary"), list):
                        rtxt = "\n".join(
                            s.get("text", "") if isinstance(s, dict) else str(s)
                            for s in item["summary"]
                        )
                    if isinstance(rtxt, str) and rtxt.strip():
                        ui.thought(rtxt)
            elif itype == "file_change":
                flush_pending_note()
                st["steps"] += 1
                paths = []
                for ch in item.get("changes") or []:
                    p = ch.get("path", "")
                    if p:
                        paths.append(short_path(p, ctx.repo))
                if paths:
                    sig_str = ", ".join(dict.fromkeys(paths))
                    if sig_str != st["last_files_sig"]:
                        ui.sub("✏️", sig_str)
                        st["last_files_sig"] = sig_str
            elif itype == "command_execution":
                flush_pending_note()
                st["steps"] += 1
                iid = item.get("id", "")
                rc_cmd = item.get("exit_code")
                if iid in st["active_tests"]:
                    st["active_tests"].discard(iid)
                    if rc_cmd not in (0, None):
                        ui.sub("⚠️", f"测试未通过 (exit={rc_cmd})，继续修复…")
                        st["last_test_announce"] = -1e9
            elif itype in ("mcp_tool_call", "web_search"):
                flush_pending_note()
                st["steps"] += 1
        elif etype == "turn.completed":
            st["pending_note"] = ""
            st["is_error"] = False
        elif etype == "turn.failed":
            st["is_error"] = True
            err = ev.get("error")
            if isinstance(err, dict):
                emsg = err.get("message") or json.dumps(err, ensure_ascii=False)
            else:
                emsg = ev.get("message") or err or str(ev)
            ui.status("⚠️", f"错误: {str(emsg)[:160]}")
        elif etype == "error":
            emsg = ev.get("message") or str(ev)
            ui.status("⚠️", f"重试/提示: {str(emsg)[:160]}")

    def heartbeat() -> None:
        ui.heartbeat(a.heartbeat, st["steps"])

    try:
        with ctx.events_file.open("w", encoding="utf-8") as ev_out:
            rc = execute(
                ctx,
                cmd,
                feed_bytes=None if ctx.fixed_review_target else ctx.prompt.encode("utf-8"),
                sink=jsonl_sink(ev_out, handle_event),
                heartbeat=heartbeat,
            )
        if rc == 0 and st["is_error"]:
            rc = 1
        if stage.exists() and stage.stat().st_size > 0:
            try:
                try:
                    stage.replace(ctx.out_file)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    shutil.copy2(stage, ctx.out_file)
            except OSError as exc:
                log_error(ctx, f"❌ 无法写入报告 {ctx.out_file}: {exc}")
                rc = rc or 2
        elif st["last_agent_text"]:
            rc = write_report(ctx, st["last_agent_text"], rc, ui)
        elif rc == 0:
            rc = write_report(ctx, "", rc, ui)
    finally:
        stage.unlink(missing_ok=True)
    return rc, st["steps"], ""


def run_claude_like(ctx: Ctx, ui: UI) -> tuple[int, int, str]:
    a = ctx.a
    backend = ctx.backend
    bin_path = resolve_bin(backend)
    if backend == "claude":
        cmd = [
            bin_path, "-p", "--verbose", "--output-format", "stream-json",
            "--dangerously-skip-permissions", "--model", ctx.model,
            "--effort", ctx.effort, "--append-system-prompt", PROGRESS_HINT,
        ]
    else:
        cmd = [
            bin_path, "-p", "-w", ctx.repo, "--dangerously-skip-permissions",
            "--output-format", "stream-json", "-m", ctx.model,
            "--reasoning-effort", ctx.effort, "--append-system-prompt", PROGRESS_HINT,
        ]
    if not a.include_user_config:
        cmd.extend(["--strict-mcp-config", "--setting-sources", "project,local"])

    fallback_sid = ""
    if a.resume is not None:
        fallback_sid = a.resume
        cmd.extend(["-r", a.resume])
    elif a.fork is not None:
        cmd.extend(["-r", a.fork, "--fork-session"])
    elif a.ephemeral:
        cmd.append("--no-session-persistence")
    else:
        new_sid = str(uuid.uuid4())
        cmd.extend(["--session-id", new_sid])
        fallback_sid = new_sid

    st = {
        "sid": "", "steps": 0, "seen_texts": set(), "buffered_texts": {},
        "flushed_mids": set(), "seen_tools": set(), "last_assistant_text": "",
        "final_result_text": "", "total_cost_usd": None, "total_credits": None,
        "last_files_sig": "", "last_test_announce": -1e9, "is_error": False,
    }

    def flush_mid(mid: str) -> None:
        chunks = st["buffered_texts"].pop(mid, [])
        st["flushed_mids"].add(mid)
        for t in chunks:
            cleaned = clean_progress_text(t)
            if cleaned:
                ui.note(cleaned)
                st["last_files_sig"] = ""

    def handle_event(ev: dict) -> None:
        etype = ev.get("type", "")
        ev_sid = ev.get("session_id")
        if ev_sid and ev_sid != a.fork and ev_sid != st["sid"]:
            st["sid"] = ev_sid
            if not a.ephemeral:
                ctx.sid_file.write_text(ev_sid + "\n", encoding="utf-8")

        if etype == "assistant":
            msg = ev.get("message") or {}
            mid = str(msg.get("id") or "default")
            stop_reason = msg.get("stop_reason")
            blocks = msg.get("content") or []
            has_tool = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
            has_text = any(
                isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).strip()
                for b in blocks
            )

            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                txt = block.get("text", "")
                key = (mid, txt.strip())
                if txt.strip() and key not in st["seen_texts"]:
                    st["seen_texts"].add(key)
                    st["last_assistant_text"] = txt
                    if mid in st["flushed_mids"] or has_tool or stop_reason == "tool_use":
                        cleaned = clean_progress_text(txt)
                        if cleaned:
                            ui.note(cleaned)
                            st["last_files_sig"] = ""
                        st["flushed_mids"].add(mid)
                    elif stop_reason != "end_turn":
                        st["buffered_texts"].setdefault(mid, []).append(txt)

            if not has_text:
                think_txt = extract_thinking(msg)
                tkey = ("think", mid, think_txt.strip())
                if think_txt.strip() and tkey not in st["seen_texts"]:
                    st["seen_texts"].add(tkey)
                    ui.thought(think_txt)

            if stop_reason == "tool_use" or has_tool:
                flush_mid(mid)
            elif stop_reason == "end_turn":
                st["buffered_texts"].pop(mid, None)

            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tid = str(block.get("id") or "")
                if tid and tid in st["seen_tools"]:
                    continue
                if tid:
                    st["seen_tools"].add(tid)
                st["steps"] += 1
                tname = block.get("name", "")
                tinp = block.get("input") or {}
                if tname in EDIT_TOOLS:
                    fp = tinp.get("file_path") or tinp.get("path") or ""
                    if fp:
                        rp = short_path(fp, ctx.repo)
                        if rp != st["last_files_sig"]:
                            ui.sub("✏️", rp)
                            st["last_files_sig"] = rp
                elif tname in BASH_TOOLS:
                    cstr = tinp.get("command", "")
                    if isinstance(cstr, str) and TEST_BUILD_RE.search(cstr) and (time.monotonic() - st["last_test_announce"]) >= 15.0:
                        ui.sub("🧪", "运行测试/检查中…")
                        st["last_test_announce"] = time.monotonic()
                elif tname in TASK_TOOLS:
                    desc = tinp.get("description") or tinp.get("subagent_type") or "subagent"
                    ui.sub("🤝", f"子代理: {desc}")

        elif etype == "result":
            st["buffered_texts"].clear()
            if isinstance(ev.get("result"), str) and ev["result"].strip():
                st["final_result_text"] = ev["result"]
            if isinstance(ev.get("total_cost_usd"), (int, float)):
                st["total_cost_usd"] = ev["total_cost_usd"]
            if ev.get("total_credits") is not None:
                st["total_credits"] = ev["total_credits"]
            if ev.get("is_error"):
                st["is_error"] = True
                detail = str(ev.get("subtype") or ev.get("result") or "unknown error")
                ui.status("⚠️", f"{ctx.title} 错误: {detail[:160]}")

    feed = f"{ctx.prompt.rstrip()}\n\n[注：{PROGRESS_HINT}]\n".encode("utf-8")
    with ctx.events_file.open("w", encoding="utf-8") as ev_out:
        rc = execute(
            ctx,
            cmd,
            cwd=ctx.repo if backend == "claude" else None,
            feed_bytes=feed,
            sink=jsonl_sink(ev_out, handle_event),
            heartbeat=lambda: ui.heartbeat(a.heartbeat, st["steps"]),
        )

    if rc == 0 and st["is_error"]:
        rc = 1
    if rc == 0 and not a.ephemeral and fallback_sid and not st["sid"]:
        ctx.sid_file.write_text(fallback_sid + "\n", encoding="utf-8")

    rc = write_report(ctx, st["final_result_text"] or st["last_assistant_text"], rc, ui)

    if backend == "qoder" and ctx.stderr_file.exists():
        for eline in ctx.stderr_file.read_text(errors="ignore").splitlines():
            if "not available right now" in eline:
                ui.status("⚠️", f"模型回退: {eline.strip()}")

    return rc, st["steps"], fmt_cost(st["total_cost_usd"], st["total_credits"])


def run_kimi(ctx: Ctx, ui: UI) -> tuple[int, int, str]:
    a = ctx.a
    bin_path = resolve_bin("kimi")

    fork_ran = False
    resume_sid = ""
    if a.fork is not None:
        fork_sink = RawSink()
        fork_rc = execute(
            ctx,
            [bin_path, "fork", a.fork, "--yes", "--cwd", ctx.repo],
            sink=fork_sink,
            timeout=KIMI_FORK_TIMEOUT,
        )
        fork_ran = True
        combined = bytes(fork_sink.data).decode("utf-8", errors="replace")
        if ctx.stderr_file.exists():
            combined += ctx.stderr_file.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"Forked to (session_\S+)", combined)
        if ctx.missing_bin:
            return 127, 0, ""
        if ctx.canceled:
            return 128 + ctx.cancel_sig, 0, ""
        if fork_rc == 124:
            msg = "❌ Kimi fork 超时（30s）"
        elif fork_rc != 0 or not match:
            if fork_rc == 0:
                fork_rc = 1
            msg = f"❌ Kimi fork 失败 (exit {fork_rc}): {combined.strip()[:300]}"
        else:
            msg = ""
        if msg:
            with ctx.stderr_file.open("a", encoding="utf-8") as fh:
                fh.write(msg + "\n")
            print(msg, file=sys.stderr, flush=True)
            return fork_rc, 0, ""
        resume_sid = match.group(1)
        ctx.sid_file.write_text(resume_sid + "\n", encoding="utf-8")
    elif a.resume is not None:
        resume_sid = a.resume
        ctx.sid_file.write_text(resume_sid + "\n", encoding="utf-8")

    full_prompt = f"{ctx.prompt.rstrip()}\n\n[注：{PROGRESS_HINT}]\n"
    prompt_file = None
    if len(full_prompt.encode("utf-8")) > KIMI_PROMPT_FILE_THRESHOLD:
        prompt_file = ctx.out_dir / f".prompt.{os.getpid()}.md"
        arg_prompt = f"Read all instructions from {prompt_file} and execute them."
    else:
        arg_prompt = full_prompt

    cmd = [bin_path, "--output-format", "stream-json"]
    if resume_sid:
        cmd.extend(["-S", resume_sid])
    if ctx.model:
        cmd.extend(["-m", ctx.model])
    cmd.extend(["-p", arg_prompt])

    st = {"steps": 0, "last_assistant_text": "", "is_error": False, "tool_seen": set()}

    def handle_event(ev: dict) -> None:
        role = ev.get("role", "")
        etype = ev.get("type", "")
        if role == "meta" and etype == "session.resume_hint":
            new_sid = ev.get("session_id")
            if new_sid:
                ctx.sid_file.write_text(str(new_sid) + "\n", encoding="utf-8")
            return
        if role == "error" or etype == "error":
            st["is_error"] = True
            msg = ev.get("message") or ev.get("content") or str(ev)
            ui.status("⚠️", f"错误: {str(msg)[:160]}")
            return
        if role != "assistant":
            return

        txt = content_text(ev.get("content"))
        think_txt = extract_thinking(ev)
        tools = ev.get("tool_calls") or []
        if tools:
            if txt.strip():
                cleaned = clean_progress_text(txt)
                if cleaned:
                    ui.note(cleaned)
            elif think_txt.strip():
                ui.thought(think_txt)
            handle_tools(tools, ctx, ui, st, st["tool_seen"])
        elif txt.strip():
            st["last_assistant_text"] = txt
        elif think_txt.strip():
            ui.thought(think_txt)

    try:
        if prompt_file is not None:
            write_private_text(prompt_file, full_prompt)
        with ctx.events_file.open("w", encoding="utf-8") as ev_out:
            rc = execute(
                ctx,
                cmd,
                cwd=ctx.repo,
                sink=jsonl_sink(ev_out, handle_event),
                heartbeat=lambda: ui.heartbeat(a.heartbeat, st["steps"]),
                stderr_mode="a" if fork_ran else "w",
            )
    finally:
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)

    if rc == 0 and st["is_error"]:
        rc = 1
    rc = write_report(ctx, st["last_assistant_text"], rc, ui)
    return rc, st["steps"], ""


def _resolve_devin_sid(conn: sqlite3.Connection, sid: str) -> str:
    row = conn.execute("SELECT id FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row:
        return str(row[0])
    rows = conn.execute("SELECT id FROM sessions WHERE id LIKE ? ORDER BY id", (sid + "%",)).fetchall()
    if len(rows) == 1:
        return str(rows[0][0])
    return ""


def run_devin(ctx: Ctx, ui: UI) -> tuple[int, int, str]:
    a = ctx.a
    bin_path = resolve_bin("devin")
    prompt_file = ctx.out_dir / f".prompt.{os.getpid()}.txt"
    full_prompt = f"{ctx.prompt.rstrip()}\n\n[注：{PROGRESS_HINT}]\n"
    prompt_refs = {str(prompt_file), f"@{prompt_file}"}
    prompt_texts = {ctx.prompt.strip(), full_prompt.strip(), *prompt_refs}

    cmd = [bin_path, "--permission-mode", "dangerous", "--respect-workspace-trust", "false"]
    if ctx.model:
        cmd.extend(["--model", ctx.model])
    if a.resume is not None:
        cmd.extend(["-r", a.resume])
    cmd.extend(["--prompt-file", str(prompt_file), "-p"])

    db_path = pathlib.Path(
        os.environ.get("AGENT_WORKER_DEVIN_DB")
        or os.environ.get("CLI_WORKER_DEVIN_DB")
        or os.path.expanduser("~/.local/share/devin/cli/sessions.db")
    )
    real_repo = os.path.realpath(ctx.repo)
    st = {
        "sid": a.resume or "", "steps": 0, "nodes_seen": set(), "tool_seen": set(),
        "sid_written": False, "sid_locked": a.resume is not None,
        "ambiguous": False, "db_warned": False, "last_poll": -1e9,
    }
    pre_sids = set()
    pending = {}

    def warn_db_once(exc: Exception) -> None:
        if not st["db_warned"]:
            st["db_warned"] = True
            ui.status("⚠️", f"Devin 会话数据库读取异常: {str(exc)[:160]}")

    def _matches_prompt_ref(conn: sqlite3.Connection, cand_sid: str) -> bool:
        for (chat,) in conn.execute(
            "SELECT chat_message FROM message_nodes WHERE session_id = ?", (cand_sid,)
        ):
            raw = chat if isinstance(chat, str) else json.dumps(chat, ensure_ascii=False)
            try:
                cm = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(cm, dict) and cm.get("role") == "user":
                if content_text(cm.get("content")).strip() in prompt_refs:
                    return True
        return False

    def poll_once(conn: sqlite3.Connection, ev_out, final: bool) -> None:
        conn.execute("BEGIN")
        if a.resume is None and not st["sid_locked"]:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE working_directory = ? AND last_activity_at >= ?",
                (real_repo, launch_epoch),
            )
            candidates = [str(r[0]) for r in rows if str(r[0]) not in pre_sids]
            if not candidates:
                return
            if len(candidates) > 1 or (st["sid"] and st["sid"] != candidates[0]):
                matched = [c for c in candidates if _matches_prompt_ref(conn, c)]
                if len(matched) == 1:
                    if st["sid"] and st["sid"] != matched[0]:
                        pending.clear()
                    st["sid"] = matched[0]
                    st["sid_locked"] = True
                else:
                    st["ambiguous"] = True
                    pending.clear()
                    ctx.sid_file.unlink(missing_ok=True)
                    ui.status("⚠️", "无法确定本次 Devin 会话 ID（多个新会话），仅保留 stdout 报告")
                    return
            else:
                st["sid"] = candidates[0]

        sid = st["sid"]
        row = conn.execute("SELECT main_chain_id FROM sessions WHERE id = ?", (sid,)).fetchone()
        main_id = row[0] if row else None
        rows = conn.execute(
            "SELECT node_id, parent_node_id FROM message_nodes WHERE session_id = ?", (sid,)
        )
        parents = {
            str(key): str(parent) if parent is not None else ""
            for key, parent in rows if key not in (None, "")
        }

        if main_id not in (None, ""):
            chain = []
            seen = set()
            cur = str(main_id)
            while cur in parents and cur not in seen:
                seen.add(cur)
                chain.append(cur)
                cur = parents[cur]
            chain.reverse()
        else:
            chain = list(parents)

        if final and not st["sid_written"]:
            # A buffered message may have been updated before Devin exited.
            pending.clear()
        unseen = [key for key in chain if key not in st["nodes_seen"]]
        missing = [key for key in unseen if key not in pending]
        # Keep each query under SQLite's historical limit of 999 parameters.
        for start in range(0, len(missing), 998):
            batch = missing[start:start + 998]
            params = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT node_id, chat_message FROM message_nodes WHERE session_id = ? AND node_id IN ({params})",
                (sid, *batch),
            )
            for key, chat in rows:
                raw = chat if isinstance(chat, str) else json.dumps(chat, ensure_ascii=False)
                try:
                    cm = json.loads(raw)
                except json.JSONDecodeError:
                    cm = None
                pending[str(key)] = (raw, cm if isinstance(cm, dict) else {})
        conn.rollback()

        if not st["sid_written"]:
            user_text = ""
            for key in unseen:
                _, cm = pending[key]
                if cm.get("role") == "user":
                    user_text = content_text(cm.get("content")).strip()
                    break
            if not user_text or user_text not in prompt_texts:
                return
            if user_text in prompt_refs:
                st["sid_locked"] = True
            # A plain prompt can be shared by concurrent runs. Hold its events
            # until the final poll unless the user message names our prompt file.
            if not final and not st["sid_locked"]:
                return
            ctx.sid_file.write_text(sid + "\n", encoding="utf-8")
            st["sid_written"] = True

        for key in unseen:
            raw, cm = pending.pop(key)
            ev_out.write(raw.rstrip("\n") + "\n")
            ev_out.flush()
            if cm.get("role") == "assistant":
                tools = cm.get("tool_calls") or []
                think_txt = extract_thinking(cm)
                if tools:
                    txt = content_text(cm.get("content"))
                    if txt.strip():
                        cleaned = clean_progress_text(txt)
                        if cleaned:
                            ui.note(cleaned)
                    elif think_txt.strip():
                        ui.thought(think_txt)
                    handle_tools(tools, ctx, ui, st, st["tool_seen"])
                elif think_txt.strip():
                    ui.thought(think_txt)
            st["nodes_seen"].add(key)

    def poll_db(final: bool = False) -> None:
        if st["ambiguous"] or not db_ready or not db_path.exists():
            return
        now = time.monotonic()
        if not final and (now - st["last_poll"]) < DEVIN_POLL_INTERVAL:
            return
        st["last_poll"] = now
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        except sqlite3.Error as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                warn_db_once(exc)
            return
        try:
            poll_once(conn, ev_out, final)
        except sqlite3.OperationalError as exc:
            text = str(exc)
            if "locked" not in text and "busy" not in text:
                warn_db_once(exc)
        except (sqlite3.DatabaseError, ValueError, TypeError, KeyError, OSError) as exc:
            warn_db_once(exc)
        finally:
            conn.close()

    db_ready = True
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
            try:
                conn.execute("BEGIN")
                pre_sids = {str(r[0]) for r in conn.execute("SELECT id FROM sessions")}
                if a.resume is not None:
                    st["sid"] = _resolve_devin_sid(conn, a.resume) or a.resume
                    st["nodes_seen"] = {
                        str(r[0]) for r in conn.execute(
                            "SELECT node_id FROM message_nodes WHERE session_id = ?", (st["sid"],)
                        )
                    }
            finally:
                conn.close()
        except sqlite3.Error as exc:
            db_ready = False
            warn_db_once(exc)

    if a.resume is not None:
        ctx.sid_file.write_text(st["sid"] + "\n", encoding="utf-8")
        st["sid_written"] = True

    sink = RawSink()
    try:
        write_private_text(prompt_file, full_prompt)
        with ctx.events_file.open("w", encoding="utf-8") as ev_out:
            launch_epoch = time.time() - 1.0
            rc = execute(
                ctx,
                cmd,
                cwd=ctx.repo,
                sink=sink,
                tick=poll_db,
                heartbeat=lambda: ui.heartbeat(a.heartbeat, st["steps"]),
                select_timeout=1.0,
            )
            if not ctx.missing_bin:
                poll_db(final=True)
    finally:
        prompt_file.unlink(missing_ok=True)

    raw_text = bytes(sink.data).decode("utf-8", errors="replace")
    rc = write_report(ctx, raw_text, rc, ui)
    return rc, st["steps"], ""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Run a local coding CLI (codex/claude/qoder/kimi/devin) with live progress and isolated logs."
    )
    ap.add_argument("backend", choices=BACKENDS, help="Worker backend to run")
    ap.add_argument("-C", "-w", "--cd", "--cwd", dest="repo", default=".", help="Target repository / working directory")
    ap.add_argument("-d", "--out-dir", default="", help="Output temp dir (created under /tmp/worker.<backend>.XXXXXX if omitted)")
    ap.add_argument("-o", "--out-file", default="", help="Override final report path (defaults to <out-dir>/out.md)")
    ap.add_argument("-m", "--model", default="", help="Model (backend-aware defaults when omitted)")
    ap.add_argument("-e", "--effort", default="", help="Reasoning effort (backend-aware defaults when omitted)")
    ap.add_argument("--tier", default="default", help="Codex service_tier (keep default unless user explicitly asks)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("-r", "--resume", default=None, metavar="SID", help="Resume an existing session ID")
    mode.add_argument("--fork", default=None, metavar="SID", help="Fork an existing session ID into a new session")
    mode.add_argument("--review", action="store_true", help="Review mode / review default model")
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--base", default="", help="Codex: review diff against base branch (implies --review)")
    target.add_argument("--commit", default="", help="Codex: review a specific commit SHA (implies --review)")
    target.add_argument("--uncommitted", action="store_true", help="Codex: review uncommitted changes (implies --review)")
    ap.add_argument("--skip-git-repo-check", action="store_true", help="Codex: allow running outside a Git repository")
    ap.add_argument("--include-user-config", action="store_true", help="Keep global user config/MCPs instead of isolated defaults")
    ap.add_argument("--ephemeral", "--no-session-persistence", dest="ephemeral", action="store_true",
                    help="Do not persist the session (codex/claude/qoder only; never with --resume/--fork)")
    ap.add_argument("--thinking", choices=("auto", "on", "off"), default="auto",
                    help="Single-line 💭 reasoning preview (codex/claude/qoder/devin): auto (after 12s without 💬), on, or off")
    ap.add_argument("--heartbeat", type=int, default=40, help="Seconds of silence before printing a heartbeat")
    ap.add_argument("prompt", nargs="?", default="", help="Prompt text (reads UTF-8 stdin if omitted)")
    return ap


def read_stdin_nonblocking() -> str:
    """Return non-whitespace prompt text if any is immediately available."""
    decode = codecs.getincrementaldecoder("utf-8")(errors="replace").decode
    try:
        fd = sys.stdin.fileno()
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        with selectors.DefaultSelector() as sel_in:
            if not regular:
                sel_in.register(fd, selectors.EVENT_READ)
            while regular or sel_in.select(timeout=0.0):
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                text = decode(chunk)
                if text.strip():
                    return text
    except OSError:
        pass
    return decode(b"", final=True)


def check_allowed_backend(ap: argparse.ArgumentParser, backend: str) -> None:
    env_allowed = os.environ.get("AGENT_WORKER_ALLOWED", "").strip()
    if env_allowed:
        if env_allowed in ("*", "all"):
            return
        allowed = [b.strip() for b in env_allowed.split(",") if b.strip()]
        unknown = [b for b in allowed if b not in BACKENDS]
        if unknown:
            ap.error(f"invalid backend(s) in AGENT_WORKER_ALLOWED: {', '.join(unknown)}")
        if backend not in allowed:
            ap.error(
                f"backend '{backend}' is disabled by AGENT_WORKER_ALLOWED "
                f"(allowed: {', '.join(allowed) or 'none'})"
            )
        return

    cfg_raw = os.environ.get("AGENT_WORKER_CONFIG")
    if cfg_raw == "":
        return
    cfg_path = pathlib.Path(cfg_raw or DEFAULT_CONFIG_PATH).expanduser()
    if not cfg_path.exists():
        if cfg_raw is not None:
            ap.error(f"config file not found: {cfg_path}")
        return
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        ap.error(f"failed to read config {cfg_path}: {exc}")
        return
    if not isinstance(data, dict):
        ap.error(f"config {cfg_path} must be a JSON object")
        return
    if "allowed_backends" not in data:
        return
    raw_list = data["allowed_backends"]
    if not isinstance(raw_list, list) or not all(isinstance(x, str) for x in raw_list):
        ap.error(f"allowed_backends in {cfg_path} must be a list of backend names")
        return
    allowed = [b.strip() for b in raw_list if b.strip()]
    unknown = [b for b in allowed if b not in BACKENDS]
    if unknown:
        ap.error(f"invalid backend(s) in {cfg_path} allowed_backends: {', '.join(unknown)}")
    if backend not in allowed:
        ap.error(
            f"backend '{backend}' is disabled by {cfg_path} "
            f"(allowed_backends: {', '.join(allowed) or 'none'})"
        )


def main() -> int:
    ap = build_parser()
    args = ap.parse_intermixed_args()
    check_allowed_backend(ap, args.backend)

    if args.resume is not None and not args.resume.strip():
        ap.error("--resume requires a non-empty session ID")
    if args.fork is not None and not args.fork.strip():
        ap.error("--fork requires a non-empty session ID")

    fixed_review_target = bool(args.base or args.commit or args.uncommitted)
    if fixed_review_target and args.backend != "codex":
        ap.error("--base/--commit/--uncommitted are only supported by the codex backend; pass a read-only git diff prompt via stdin instead")
    if (args.resume is not None or args.fork is not None) and fixed_review_target:
        ap.error("--resume/--fork cannot be combined with --base/--commit/--uncommitted (review mode)")
    if args.backend == "devin" and args.fork is not None:
        ap.error("devin backend does not support --fork; use --resume or a fresh run")
    if args.backend != "codex" and args.tier != "default":
        ap.error("--tier is only supported by the codex backend")
    if args.ephemeral and args.backend in ("kimi", "devin"):
        ap.error(f"{args.backend} backend does not support --ephemeral")
    if args.ephemeral and (args.resume is not None or args.fork is not None):
        ap.error("--ephemeral cannot be combined with --resume or --fork")

    is_review = bool(args.review or fixed_review_target)

    prompt_text = args.prompt
    if not prompt_text and not sys.stdin.isatty():
        if args.backend == "codex" and fixed_review_target:
            prompt_text = read_stdin_nonblocking()
        else:
            prompt_text = sys.stdin.buffer.read().decode("utf-8", errors="replace")

    if fixed_review_target and prompt_text.strip():
        ap.error("--base/--commit/--uncommitted do not accept a prompt")
    if not prompt_text.strip() and not fixed_review_target and not (args.backend == "codex" and is_review):
        ap.error("Prompt is required")

    ctx = Ctx(args, is_review, fixed_review_target)
    ctx.prompt = prompt_text

    if not os.path.isdir(ctx.repo):
        msg = f"❌ 目标目录不存在或不是目录: {ctx.repo}"
    else:
        msg = ""
        try:
            if ctx.out_file.is_dir():
                msg = f"❌ 输出报告路径不能是目录: {ctx.out_file}"
            else:
                ctx.out_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = f"❌ 无法准备输出报告路径 {ctx.out_file}: {exc}"
    if msg:
        log_error(ctx, msg, mode="w")
        ctx.exit_file.write_text("2\n", encoding="utf-8")
        return 2

    ui = UI(args.thinking)

    if args.resume is not None:
        mode_label = f"resume:{args.resume[:8]}"
    elif args.fork is not None:
        mode_label = f"fork:{args.fork[:8]}"
    else:
        mode_label = "review" if is_review else "exec"

    meta_parts = []
    if ctx.backend in ("codex", "claude", "qoder"):
        meta_parts.append(ctx.effort)
    if ctx.backend == "codex" and args.tier != "default":
        meta_parts.append(args.tier)
    if mode_label != "exec":
        meta_parts.append(mode_label)

    model_display = ctx.model or "default"
    if meta_parts:
        safe_print(f"🚀 {ctx.title} · {model_display} ({' · '.join(meta_parts)}) · {ctx.repo_name}")
    else:
        safe_print(f"🚀 {ctx.title} · {model_display} · {ctx.repo_name}")
    ui.last_print = safe_print(RULE_LINE)

    rc = 2
    steps = 0
    cost = ""
    try:
        if ctx.backend == "codex":
            rc, steps, cost = run_codex(ctx, ui)
        elif ctx.backend in ("claude", "qoder"):
            rc, steps, cost = run_claude_like(ctx, ui)
        elif ctx.backend == "kimi":
            rc, steps, cost = run_kimi(ctx, ui)
        else:
            rc, steps, cost = run_devin(ctx, ui)
    except OSError as exc:
        log_error(ctx, f"❌ 运行失败: {exc}")
    finally:
        if ctx.canceled:
            rc = 128 + ctx.cancel_sig
        try:
            ctx.out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            ctx.exit_file.write_text(f"{rc}\n", encoding="utf-8")
        except OSError:
            pass

    if ui.has_body:
        safe_print(RULE_LINE)
    if rc == 0:
        safe_print(format_timeline_block("✅", f"完成 · {steps} 步{cost}", ui.elapsed()))
        safe_print(f"{TIMELINE_RAIL}    报告: {ctx.out_file}")
    else:
        state = "已取消" if ctx.canceled else "失败"
        safe_print(format_timeline_block("❌", f"{state} (exit {rc}) · {steps} 步", ui.elapsed()))
        safe_print(f"{TIMELINE_RAIL}    日志: {ctx.stderr_file}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
