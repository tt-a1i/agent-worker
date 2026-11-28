#!/usr/bin/env python3
"""Tests for the unified worker runner using mock CLI executables and a temporary Devin SQLite DB."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_worker.py"

MOCK_HEADER = """#!/usr/bin/env python3
import json, os, signal, sqlite3, subprocess, sys, time
_argv = os.environ.get("MOCK_ARGV")
if _argv:
    with open(_argv, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps(sys.argv[1:]))
"""

DEVIN_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT, last_activity_at REAL, main_chain_id TEXT);
CREATE TABLE message_nodes (node_id TEXT, parent_node_id TEXT, session_id TEXT, chat_message TEXT);
"""


def tool_node(path, name="Edit", text="正在处理"):
    return json.dumps(
        {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {"id": "t-" + path, "function": {"name": name, "arguments": json.dumps({"path": path})}}
            ],
        },
        ensure_ascii=False,
    )


class WorkerTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        self._counter = 0

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, name):
        self._counter += 1
        return self.dir / f"{name}-{self._counter}"

    def write_mock(self, backend, body):
        path = self.path(f"mock-{backend}")
        path.write_text(MOCK_HEADER + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def env_for(self, backend, mock, env_extra=None):
        env = {
            key: value for key, value in os.environ.items()
            if not (key.startswith(("AGENT_WORKER_", "CLI_WORKER_")) and key.endswith("_BIN"))
            and key not in ("AGENT_WORKER_DEVIN_DB", "CLI_WORKER_DEVIN_DB")
        }
        env["PYTHONIOENCODING"] = "utf-8"
        env["AGENT_WORKER_CONFIG"] = ""
        env.pop("AGENT_WORKER_ALLOWED", None)
        env["AGENT_WORKER_DEVIN_DB"] = str(self.dir / "isolated-devin.db")
        if env_extra:
            env.update(env_extra)
        env[f"AGENT_WORKER_{backend.upper()}_BIN"] = str(mock)
        return env

    def run_cli(self, backend, mock, *args, prompt=None, stdin_text=None, env_extra=None,
                timeout=90, repo=None, cwd=None, outdir=None):
        if outdir is None:
            outdir = self.path("out")
        outdir_path = pathlib.Path(outdir)
        if not outdir_path.is_absolute():
            outdir_path = pathlib.Path(cwd or os.getcwd()) / outdir_path
        argv = ["-C", str(repo or self.repo), "-d", str(outdir), *args]
        if prompt is not None:
            argv.append(prompt)
        proc = self.run_argv(
            backend, mock, argv, stdin_text=stdin_text or "",
            env_extra=env_extra, cwd=cwd, timeout=timeout,
        )
        return proc, outdir_path

    def run_argv(self, backend, mock, argv, *, stdin_text="", env_extra=None, cwd=None, timeout=90):
        return subprocess.run(
            [sys.executable, str(SCRIPT), backend, *argv],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=self.env_for(backend, mock, env_extra),
            cwd=cwd,
            timeout=timeout,
        )

    def read(self, outdir, name):
        return (outdir / name).read_text(encoding="utf-8")

    def init_devin_db(self):
        db = self.path("sessions.db")
        conn = sqlite3.connect(str(db))
        conn.executescript(DEVIN_SCHEMA)
        conn.commit()
        conn.close()
        return db

    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        stat = out.stdout.strip()
        return bool(stat) and not stat.startswith("Z")

    def assert_pid_reaped(self, pid, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                return
            time.sleep(0.05)
        self.assertFalse(self._pid_alive(pid), f"process {pid} still alive")

    # ------------------------------------------------------------------ codex

    def test_codex_exec_stream_and_report(self):
        argv_file = self.dir / "codex-argv.json"
        mock = self.write_mock("codex", """
            events = [
                {"type": "thread.started", "thread_id": "th-123"},
                {"type": "item.started", "item": {"id": "c1", "type": "command_execution", "command": "pytest -q"}},
                {"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "pytest -q", "exit_code": 0}},
                {"type": "item.completed", "item": {"id": "f1", "type": "file_change", "changes": [{"path": "a.py"}]}},
                {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "Final report body"}},
                {"type": "turn.completed"},
            ]
            for ev in events:
                print(json.dumps(ev), flush=True)
        """)
        proc, outdir = self.run_cli("codex", mock, prompt="do it", env_extra={"MOCK_ARGV": str(argv_file)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "Final report body")
        self.assertEqual(self.read(outdir, "sid").strip(), "th-123")
        header, top, first, sub, bottom, summary, report = proc.stdout.splitlines()
        self.assertEqual(header, "🚀 Codex · default (medium) · repo")
        self.assertEqual(top, "─" * 50)
        self.assertRegex(first, r"^[0-9]{2}:[0-9]{2} │ 🧪 运行测试/检查中\.\.\.$")
        self.assertEqual(sub, "      │ ✏️ a.py")
        self.assertEqual(bottom, top)
        self.assertRegex(summary, r"^[0-9]{2}:[0-9]{2} │ ✅ 完成 · 2 步$")
        self.assertEqual(report, f"      │    报告: {outdir / 'out.md'}")
        argv = json.loads(argv_file.read_text())
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("model_reasoning_effort=medium", argv)
        self.assertIn("service_tier=default", argv)
        self.assertEqual(list(outdir.glob(".out.stage.*")), [])

    def test_codex_review_base_native(self):
        argv_file = self.dir / "codex-review-argv.json"
        mock = self.write_mock("codex", """
            for ev in [
                {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "native review"}},
                {"type": "turn.completed"},
            ]:
                print(json.dumps(ev), flush=True)
        """)
        proc, outdir = self.run_cli("codex", mock, "--base", "main", env_extra={"MOCK_ARGV": str(argv_file)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "native review")
        self.assertIn("🚀 Codex · default (high · review)", proc.stdout)
        argv = json.loads(argv_file.read_text())
        self.assertIn("review", argv)
        self.assertEqual(argv[argv.index("--base") + 1], "main")

    def test_codex_review_with_prompt_uses_stdin_dash(self):
        argv_file = self.dir / "codex-review-prompt-argv.json"
        mock = self.write_mock("codex", """
            print(json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "reviewed"}}), flush=True)
            print(json.dumps({"type": "turn.completed"}), flush=True)
        """)
        proc, outdir = self.run_cli("codex", mock, "--review", prompt="please review", env_extra={"MOCK_ARGV": str(argv_file)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "reviewed")
        argv = json.loads(argv_file.read_text())
        self.assertEqual(argv[-1], "-")
        self.assertIn("model_reasoning_effort=high", argv)

    def test_codex_turn_failed_forces_nonzero(self):
        mock = self.write_mock("codex", """
            print(json.dumps({"type": "turn.failed", "error": {"message": "quota exhausted"}}), flush=True)
        """)
        proc, outdir = self.run_cli("codex", mock, prompt="do it")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.read(outdir, "exit").strip(), "1")
        self.assertIn("⚠️", proc.stdout)
        self.assertIn("quota exhausted", proc.stdout)

    def test_codex_final_note_not_echoed_as_progress(self):
        mock = self.write_mock("codex", """
            print(json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "FINAL ANSWER"}}), flush=True)
            print(json.dumps({"type": "turn.completed"}), flush=True)
        """)
        proc, outdir = self.run_cli("codex", mock, "--heartbeat", "0", prompt="do it")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "FINAL ANSWER")
        header, rule, summary, report = proc.stdout.splitlines()
        self.assertEqual(header, "🚀 Codex · default (medium) · repo")
        self.assertEqual(rule, "─" * 50)
        self.assertRegex(summary, r"^[0-9]{2}:[0-9]{2} │ ✅ 完成 · 0 步$")
        self.assertEqual(report, f"      │    报告: {outdir / 'out.md'}")

    def test_codex_failure_without_progress_has_one_rule(self):
        mock = self.write_mock("codex", "sys.exit(3)\n")
        proc, outdir = self.run_cli("codex", mock, "--heartbeat", "0", prompt="do it")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        header, rule, summary, log = proc.stdout.splitlines()
        self.assertEqual(header, "🚀 Codex · default (medium) · repo")
        self.assertEqual(rule, "─" * 50)
        self.assertRegex(summary, r"^[0-9]{2}:[0-9]{2} │ ❌ 失败 \(exit 3\) · 0 步$")
        self.assertEqual(log, f"      │    日志: {outdir / 'stderr.log'}")

    def test_codex_stale_out_dir_is_not_reused(self):
        outdir = self.path("reused")
        outdir.mkdir()
        (outdir / "out.md").write_text("STALE REPORT", encoding="utf-8")
        (outdir / "sid").write_text("stale-sid\n", encoding="utf-8")
        (outdir / "exit").write_text("9\n", encoding="utf-8")
        mock = self.write_mock("codex", """
            print(json.dumps({"type": "thread.started", "thread_id": "th-new"}), flush=True)
            print(json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "fresh report"}}), flush=True)
            print(json.dumps({"type": "turn.completed"}), flush=True)
        """)
        proc, _ = self.run_cli("codex", mock, prompt="x", outdir=outdir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "fresh report")
        self.assertEqual(self.read(outdir, "sid").strip(), "th-new")
        self.assertEqual(self.read(outdir, "exit").strip(), "0")

    def test_codex_stale_out_without_output_is_not_kept(self):
        outdir = self.path("reused-empty")
        outdir.mkdir()
        (outdir / "out.md").write_text("STALE REPORT", encoding="utf-8")
        mock = self.write_mock("codex", "pass\n")
        proc, _ = self.run_cli("codex", mock, prompt="x", outdir=outdir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotEqual(self.read(outdir, "out.md"), "STALE REPORT")

    # ----------------------------------------------------------------- claude

    def test_claude_stream_cost_and_sid(self):
        argv_file = self.dir / "claude-argv.json"
        mock = self.write_mock("claude", """
            events = [
                {"type": "assistant", "session_id": "sess-c", "message": {"id": "m1", "stop_reason": "tool_use", "content": [
                    {"type": "text", "text": "先看代码"},
                    {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "a.py"}},
                ]}},
                {"type": "result", "session_id": "sess-c", "is_error": False, "result": "all done", "total_cost_usd": 0.05},
            ]
            for ev in events:
                print(json.dumps(ev, ensure_ascii=False), flush=True)
        """)
        proc, outdir = self.run_cli("claude", mock, prompt="fix it", env_extra={"MOCK_ARGV": str(argv_file)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "all done")
        self.assertEqual(self.read(outdir, "sid").strip(), "sess-c")
        self.assertIn("🚀 Claude · sonnet (medium)", proc.stdout)
        self.assertIn("$0.05", proc.stdout)
        self.assertIn("先看代码", proc.stdout)
        self.assertIn("✏️ a.py", proc.stdout)
        argv = json.loads(argv_file.read_text())
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertIn("--session-id", argv)
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_claude_review_defaults_to_opus_high(self):
        argv_file = self.dir / "claude-review-argv.json"
        mock = self.write_mock("claude", """
            print(json.dumps({"type": "result", "session_id": "s", "is_error": False, "result": "ok"}), flush=True)
        """)
        proc, _ = self.run_cli("claude", mock, "--review", prompt="review", env_extra={"MOCK_ARGV": str(argv_file)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("🚀 Claude · opus (high · review)", proc.stdout)
        argv = json.loads(argv_file.read_text())
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_claude_include_user_config_keeps_global_settings(self):
        argv_file = self.dir / "claude-user-argv.json"
        mock = self.write_mock("claude", """
            print(json.dumps({"type": "result", "session_id": "s", "is_error": False, "result": "ok"}), flush=True)
        """)
        proc, _ = self.run_cli("claude", mock, "--include-user-config", prompt="go", env_extra={"MOCK_ARGV": str(argv_file)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = json.loads(argv_file.read_text())
        self.assertNotIn("--strict-mcp-config", argv)

    def test_claude_fork_does_not_publish_parent_sid(self):
        outdir = self.path("fork-sid")
        mock = self.write_mock("claude", """
            print(json.dumps({"type": "result", "session_id": "parent-sid", "result": "boom"}), flush=True)
            sys.exit(1)
        """)
        proc, _ = self.run_cli("claude", mock, "--fork", "parent-sid", prompt="go", outdir=outdir)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertFalse((outdir / "sid").exists())

    def test_claude_and_qoder_do_not_publish_sid_on_immediate_crash(self):
        for backend in ("claude", "qoder"):
            with self.subTest(backend=backend):
                mock = self.write_mock(backend, "sys.exit(23)\n")
                proc, outdir = self.run_cli(backend, mock, prompt="go")
                self.assertEqual(proc.returncode, 23, proc.stderr)
                self.assertFalse((outdir / "sid").exists())

    def test_inherited_backend_override_cannot_replace_mock(self):
        mock = self.write_mock("claude", """
            print(json.dumps({"type": "result", "result": "mock used"}))
        """)
        with patch.dict(os.environ, {"AGENT_WORKER_CLAUDE_BIN": str(self.dir / "unexpected-bin")}):
            proc, outdir = self.run_cli("claude", mock, prompt="go")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "mock used")

    # ------------------------------------------------------------------ qoder

    def test_qoder_review_credits_and_tools(self):
        argv_file = self.dir / "qoder-argv.json"
        mock = self.write_mock("qoder", """
            events = [
                {"type": "assistant", "session_id": "sess-q", "message": {"id": "m1", "stop_reason": "tool_use", "content": [
                    {"type": "text", "text": "评审中"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}},
                ]}},
                {"type": "result", "session_id": "sess-q", "is_error": False, "result": "review ok", "total_credits": 1.25},
            ]
            for ev in events:
                print(json.dumps(ev, ensure_ascii=False), flush=True)
        """)
        proc, outdir = self.run_cli("qoder", mock, "--review", prompt="review this", env_extra={"MOCK_ARGV": str(argv_file)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "review ok")
        self.assertEqual(self.read(outdir, "sid").strip(), "sess-q")
        self.assertIn("🚀 Qoder · DeepSeek-Flash (high · review)", proc.stdout)
        self.assertIn("1.25 credits", proc.stdout)
        self.assertIn("🧪", proc.stdout)
        argv = json.loads(argv_file.read_text())
        self.assertEqual(argv[argv.index("-m") + 1], "DeepSeek-Flash")
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "high")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("project,local", argv)

    def test_qoder_default_model_is_deepseek_flash(self):
        argv_file = self.dir / "qoder-default-argv.json"
        mock = self.write_mock("qoder", """
            print(json.dumps({"type": "result", "session_id": "s", "is_error": False, "result": "ok"}), flush=True)
        """)
        proc, _ = self.run_cli("qoder", mock, prompt="go", env_extra={"MOCK_ARGV": str(argv_file)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("🚀 Qoder · DeepSeek-Flash (high)", proc.stdout)
        argv = json.loads(argv_file.read_text())
        self.assertEqual(argv[argv.index("-m") + 1], "DeepSeek-Flash")

    # ------------------------------------------------------------------- kimi

    def test_kimi_stream_tools_and_resume_hint(self):
        argv_file = self.dir / "kimi-argv.json"
        mock = self.write_mock("kimi", """
            events = [
                {"role": "assistant", "content": [{"type": "text", "text": "检查中"}],
                 "tool_calls": [{"id": "tc1", "function": {"name": "Bash", "arguments": json.dumps({"command": "pytest -q"})}}]},
                {"role": "assistant", "content": "kimi final answer", "tool_calls": []},
                {"role": "meta", "type": "session.resume_hint", "session_id": "session_abc"},
            ]
            for ev in events:
                print(json.dumps(ev, ensure_ascii=False), flush=True)
        """)
        proc, outdir = self.run_cli("kimi", mock, prompt="do kimi", env_extra={"MOCK_ARGV": str(argv_file)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "kimi final answer")
        self.assertEqual(self.read(outdir, "sid").strip(), "session_abc")
        self.assertIn("🚀 Kimi · default", proc.stdout)
        self.assertIn("检查中", proc.stdout)
        self.assertIn("🧪", proc.stdout)
        argv = json.loads(argv_file.read_text())
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertNotIn("--auto", argv)
        self.assertNotIn("-m", argv)
        self.assertIn("-p", argv)

    def test_kimi_fork_resolves_new_session(self):
        argv_file = self.dir / "kimi-fork-argv.json"
        mock = self.write_mock("kimi", """
            if "fork" in sys.argv:
                print("Forked to session_forked_1", flush=True)
                sys.exit(0)
            events = [
                {"role": "assistant", "content": "forked run ok", "tool_calls": []},
                {"role": "meta", "type": "session.resume_hint", "session_id": "session_forked_1"},
            ]
            for ev in events:
                print(json.dumps(ev, ensure_ascii=False), flush=True)
        """)
        proc, outdir = self.run_cli("kimi", mock, "--fork", "session_old", prompt="continue", env_extra={"MOCK_ARGV": str(argv_file)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "forked run ok")
        self.assertEqual(self.read(outdir, "sid").strip(), "session_forked_1")
        argv = json.loads(argv_file.read_text())
        self.assertIn("-S", argv)
        self.assertEqual(argv[argv.index("-S") + 1], "session_forked_1")

    def test_kimi_fork_failure_preserves_exit_and_logs(self):
        mock = self.write_mock("kimi", """
            if "fork" in sys.argv:
                print("fatal: cannot fork", file=sys.stderr, flush=True)
                sys.exit(23)
        """)
        proc, outdir = self.run_cli("kimi", mock, "--fork", "session_old", prompt="continue")
        self.assertEqual(proc.returncode, 23, proc.stderr)
        self.assertEqual(self.read(outdir, "exit").strip(), "23")
        log = self.read(outdir, "stderr.log")
        self.assertIn("fatal: cannot fork", log)
        self.assertIn("Kimi fork 失败", log)
        self.assertFalse((outdir / "sid").exists())

    def test_kimi_large_prompt_uses_file(self):
        copy_file = self.dir / "kimi-prompt-copy.md"
        mock = self.write_mock("kimi", """
            argv = sys.argv[1:]
            prompt = argv[argv.index("-p") + 1]
            path = prompt.split(" from ", 1)[1].split(" and execute", 1)[0]
            try:
                data = open(path, encoding="utf-8").read()
            except Exception as exc:
                data = "READ-ERROR:" + str(exc)
            open(os.environ["MOCK_PROMPT_COPY"], "w", encoding="utf-8").write(data)
            print(json.dumps({"role": "assistant", "content": "large prompt ok", "tool_calls": []}), flush=True)
        """)
        proc, outdir = self.run_cli(
            "kimi", mock, stdin_text="长" * 64000,
            env_extra={"MOCK_PROMPT_COPY": str(copy_file)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "large prompt ok")
        copied = copy_file.read_text(encoding="utf-8")
        self.assertNotIn("READ-ERROR", copied)
        self.assertIn("长", copied)
        self.assertGreater(len(copied.encode("utf-8")), 96_000)
        self.assertEqual(list(outdir.glob(".prompt.*")), [])

    def test_kimi_error_event_forces_failure(self):
        mock = self.write_mock("kimi", """
            print(json.dumps({"role": "error", "message": "boom"}), flush=True)
        """)
        proc, outdir = self.run_cli("kimi", mock, prompt="go")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.read(outdir, "exit").strip(), "1")
        self.assertIn("❌ 失败 (exit 1)", proc.stdout)

    # ------------------------------------------------------------------ devin

    def test_devin_sqlite_polling_and_raw_stdout(self):
        db = self.init_devin_db()
        argv_file = self.dir / "devin-argv.json"
        copy_file = self.dir / "devin-prompt-copy.txt"
        node1 = tool_node("a.py", text="检查中")
        node2 = tool_node("b.py", text="继续")
        mock = self.write_mock("devin", f"""
            argv = sys.argv[1:]
            prompt_path = argv[argv.index("--prompt-file") + 1]
            open(os.environ["MOCK_PROMPT_COPY"], "w", encoding="utf-8").write(open(prompt_path, encoding="utf-8").read())
            db = sqlite3.connect(os.environ["AGENT_WORKER_DEVIN_DB"])
            repo = os.path.realpath(os.getcwd())
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("sess-devin", repo, time.time(), "n1"))
            user = json.dumps({{"role": "user", "content": prompt_path}})
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("u", None, "sess-devin", user))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n1", "u", "sess-devin", {node1!r}))
            db.commit()
            sid_path = os.path.join(os.path.dirname(prompt_path), "sid")
            deadline = time.monotonic() + 5
            while not os.path.exists(sid_path) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert os.path.exists(sid_path), "prompt-file reference did not confirm the session live"
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n2", "n1", "sess-devin", {node2!r}))
            db.execute("UPDATE sessions SET main_chain_id = ? WHERE id = ?", ("n2", "sess-devin"))
            db.commit()
            db.close()
            sys.stdout.write("devin final")
            sys.stdout.flush()
        """)
        proc, outdir = self.run_cli(
            "devin",
            mock,
            prompt="do devin",
            env_extra={
                "AGENT_WORKER_DEVIN_DB": str(db),
                "MOCK_ARGV": str(argv_file),
                "MOCK_PROMPT_COPY": str(copy_file),
            },
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "devin final")
        self.assertEqual(self.read(outdir, "sid").strip(), "sess-devin")
        events = self.read(outdir, "events.jsonl")
        self.assertIn("a.py", events)
        self.assertIn("b.py", events)
        self.assertIn("💬 检查中", proc.stdout)
        self.assertIn("✏️ a.py", proc.stdout)
        self.assertIn("✏️ b.py", proc.stdout)
        self.assertIn("2 步", proc.stdout)
        self.assertIn("[注：", copy_file.read_text(encoding="utf-8"))
        self.assertEqual(list(outdir.glob(".prompt.*")), [])
        argv = json.loads(argv_file.read_text())
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dangerous")
        self.assertIn("--prompt-file", argv)
        self.assertNotIn("--model", argv)

    def test_devin_ignores_preexisting_sessions(self):
        db = self.init_devin_db()
        repo = os.path.realpath(str(self.repo))
        old_node = tool_node("old_file.py", text="旧会话")
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("sess-old", repo, time.time(), "o1"))
        conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("o1", None, "sess-old", old_node))
        conn.commit()
        conn.close()
        new_node = tool_node("new_file.py", text="新会话")
        mock = self.write_mock("devin", f"""
            db = sqlite3.connect(os.environ["AGENT_WORKER_DEVIN_DB"])
            repo = os.path.realpath(os.getcwd())
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("sess-new", repo, time.time(), "n1"))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("u", None, "sess-new", json.dumps({{"role": "user", "content": "go"}})))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n1", "u", "sess-new", {new_node!r}))
            db.commit()
            db.close()
            sys.stdout.write("new report")
            sys.stdout.flush()
        """)
        proc, outdir = self.run_cli(
            "devin", mock, prompt="go", env_extra={"AGENT_WORKER_DEVIN_DB": str(db)}, timeout=30
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "sid").strip(), "sess-new")
        self.assertIn("new_file.py", proc.stdout)
        self.assertNotIn("old_file.py", proc.stdout)
        self.assertNotIn("旧会话", proc.stdout)
        self.assertIn("1 步", proc.stdout)

    def test_devin_main_chain_branch_excluded(self):
        db = self.init_devin_db()
        branch = tool_node("zz_branch.py", text="分支")
        main = tool_node("aaa_main.py", text="主链")
        mock = self.write_mock("devin", f"""
            db = sqlite3.connect(os.environ["AGENT_WORKER_DEVIN_DB"])
            repo = os.path.realpath(os.getcwd())
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("sess-tree", repo, time.time(), "n3"))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n1", None, "sess-tree", json.dumps({{"role": "user", "content": "go"}})))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n2", "n1", "sess-tree", {branch!r}))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n3", "n1", "sess-tree", {main!r}))
            db.commit()
            db.close()
            sys.stdout.write("tree report")
            sys.stdout.flush()
        """)
        proc, outdir = self.run_cli(
            "devin", mock, prompt="go", env_extra={"AGENT_WORKER_DEVIN_DB": str(db)}, timeout=30
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("aaa_main.py", proc.stdout)
        self.assertNotIn("zz_branch.py", proc.stdout)
        self.assertIn("1 步", proc.stdout)
        events = self.read(outdir, "events.jsonl")
        self.assertIn("aaa_main.py", events)
        self.assertNotIn("zz_branch.py", events)

    def test_devin_discards_unconfirmed_events_when_second_session_appears(self):
        db = self.init_devin_db()
        first = tool_node("other.py")
        second = tool_node("local.py")
        mock = self.write_mock("devin", f"""
            conn = sqlite3.connect(os.environ["AGENT_WORKER_DEVIN_DB"])
            repo = os.path.realpath(os.getcwd())
            user = json.dumps({{"role": "user", "content": "same prompt"}})
            for sid, chat in (("other", {first!r}), ("local", {second!r})):
                conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", (sid, repo, time.time(), "tool"))
                conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("user", None, sid, user))
                conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("tool", "user", sid, chat))
                conn.commit()
                if sid == "other":
                    time.sleep(1.2)
            conn.close()
            print("stdout-only report", end="")
        """)
        proc, outdir = self.run_cli(
            "devin", mock, prompt="same prompt", env_extra={"AGENT_WORKER_DEVIN_DB": str(db)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "stdout-only report")
        self.assertFalse((outdir / "sid").exists())
        self.assertEqual(self.read(outdir, "events.jsonl"), "")
        self.assertNotIn("other.py", proc.stdout)
        self.assertNotIn("local.py", proc.stdout)

    def test_devin_rejects_mismatched_prompt_or_stale_session(self):
        for age, user in ((0, "someone else's prompt"), (60, "go"), (0, None)):
            with self.subTest(age=age, user=user):
                db = self.init_devin_db()
                mock = self.write_mock("devin", f"""
                    conn = sqlite3.connect(os.environ["AGENT_WORKER_DEVIN_DB"])
                    with conn:
                        conn.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                                     ("other", os.path.realpath(os.getcwd()), time.time() - {age}, "tail"))
                        if {user is not None}:
                            u = json.dumps({{"role": "user", "content": {user!r}}})
                            conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("tail", None, "other", u))
                        else:
                            a = json.dumps({{"role": "assistant", "content": "orphan", "tool_calls": []}})
                            conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("tail", None, "other", a))
                    print("stdout-only report", end="")
                """)
                proc, outdir = self.run_cli(
                    "devin", mock, prompt="go", env_extra={"AGENT_WORKER_DEVIN_DB": str(db)},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.read(outdir, "out.md"), "stdout-only report")
                self.assertFalse((outdir / "sid").exists())
                self.assertEqual(self.read(outdir, "events.jsonl"), "")

    def test_devin_locked_prompt_file_survives_later_concurrent_session(self):
        db = self.init_devin_db()
        node1 = tool_node("first.py", text="第一阶段")
        node2 = tool_node("second.py", text="第二阶段")
        other_node = tool_node("intruder.py", text="隔壁会话")
        mock = self.write_mock("devin", f"""
            argv = sys.argv[1:]
            prompt_path = argv[argv.index("--prompt-file") + 1]
            conn = sqlite3.connect(os.environ["AGENT_WORKER_DEVIN_DB"])
            repo = os.path.realpath(os.getcwd())
            with conn:
                conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("mine", repo, time.time(), "n1"))
                conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?)",
                             ("u", None, "mine", json.dumps({{"role": "user", "content": prompt_path}})))
                conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n1", "u", "mine", {node1!r}))
            sid_path = os.path.join(os.path.dirname(prompt_path), "sid")
            deadline = time.monotonic() + 5
            while not os.path.exists(sid_path) and time.monotonic() < deadline:
                time.sleep(0.02)
            with conn:
                conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("other", repo, time.time(), "o1"))
                conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("o1", None, "other", {other_node!r}))
                conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n2", "n1", "mine", {node2!r}))
                conn.execute("UPDATE sessions SET main_chain_id = ? WHERE id = ?", ("n2", "mine"))
            conn.close()
            print("locked ok", end="")
        """)
        proc, outdir = self.run_cli("devin", mock, prompt="work", env_extra={"AGENT_WORKER_DEVIN_DB": str(db)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "sid").strip(), "mine")
        self.assertIn("first.py", proc.stdout)
        self.assertIn("second.py", proc.stdout)
        self.assertNotIn("intruder.py", proc.stdout)

    def test_devin_resume_skips_baseline_nodes(self):
        db = self.init_devin_db()
        sid = "sess-resume-123456"
        repo = os.path.realpath(str(self.repo))
        old_node = tool_node("history.py", text="历史")
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", (sid, repo, time.time() - 60, "n1"))
        conn.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n1", None, sid, old_node))
        conn.commit()
        conn.close()
        new_node = tool_node("followup.py", text="新步骤")
        mock = self.write_mock("devin", f"""
            db = sqlite3.connect(os.environ["AGENT_WORKER_DEVIN_DB"])
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n2", "n1", {sid!r}, {new_node!r}))
            db.execute("UPDATE sessions SET main_chain_id = ? WHERE id = ?", ("n2", {sid!r}))
            db.commit()
            db.close()
            sys.stdout.write("resume report")
            sys.stdout.flush()
        """)
        proc, outdir = self.run_cli(
            "devin", mock, "--resume", sid, prompt="continue",
            env_extra={"AGENT_WORKER_DEVIN_DB": str(db)}, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "sid").strip(), sid)
        self.assertIn("followup.py", proc.stdout)
        self.assertNotIn("history.py", proc.stdout)
        self.assertIn("1 步", proc.stdout)
        events = self.read(outdir, "events.jsonl")
        self.assertIn("followup.py", events)
        self.assertNotIn("history.py", events)

    # ------------------------------------------------------------ stream edges

    def test_utf8_multibyte_boundary_integrity(self):
        text = "多字节中文边界" * 3000
        line = json.dumps(
            {"type": "result", "session_id": "sess-u", "is_error": False, "result": text, "total_cost_usd": 0.0},
            ensure_ascii=False,
        ).encode("utf-8")
        mock = self.write_mock("claude", f"sys.stdout.buffer.write({line!r})\nsys.stdout.buffer.flush()\n")
        proc, outdir = self.run_cli("claude", mock, prompt="utf8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), text)
        self.assertGreater(len(line), 8192 * 4)

    def test_eof_line_without_trailing_newline(self):
        mock = self.write_mock("claude", """
            sys.stdout.write(json.dumps({"type": "result", "session_id": "s", "is_error": False, "result": "eof ok"}))
            sys.stdout.flush()
        """)
        proc, outdir = self.run_cli("claude", mock, prompt="eof")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "eof ok")

    def test_is_error_with_zero_exit_becomes_failure(self):
        mock = self.write_mock("claude", """
            print(json.dumps({"type": "result", "session_id": "s", "is_error": True, "result": "boom"}), flush=True)
        """)
        proc, outdir = self.run_cli("claude", mock, prompt="fail")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.read(outdir, "exit").strip(), "1")
        self.assertIn("❌ 失败 (exit 1)", proc.stdout)

    def test_large_prompt_to_failing_child_no_broken_pipe_crash(self):
        mock = self.write_mock("claude", "sys.exit(3)\n")
        proc, outdir = self.run_cli("claude", mock, stdin_text="A" * 120_000)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.read(outdir, "exit").strip(), "3")
        self.assertNotIn("Traceback", proc.stderr)

    # ------------------------------------------------------- process lifecycle

    def test_grandchild_holding_stdout_does_not_hang(self):
        pid_file = self.dir / "grandchild.pid"
        grandchild = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(15)\n"
        mock = self.write_mock("claude", f"""
            p = subprocess.Popen([sys.executable, "-c", {grandchild!r}])
            open({str(pid_file)!r}, "w").write(str(p.pid))
            sys.stdout.write("leader done")
            sys.stdout.flush()
        """)
        start = time.monotonic()
        proc, _ = self.run_cli("claude", mock, prompt="go", timeout=20)
        elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 10.0)
        self.assert_pid_reaped(int(pid_file.read_text()))

    def test_sigterm_kills_grandchild_surviving_leader(self):
        pid_file = self.dir / "grandchild.pid"
        cleanup_file = self.dir / "cleanup"
        grandchild = textwrap.dedent(f"""
            import os, signal, time
            signals = 0
            def term(*args):
                global signals
                signals += 1
                if signals == 2:
                    open({str(cleanup_file)!r}, "w").close()
            signal.signal(signal.SIGTERM, term)
            with open({str(pid_file)!r}, "w") as fh:
                fh.write(str(os.getpid()))
            time.sleep(60)
        """)
        mock = self.write_mock("claude", f"""
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            subprocess.Popen([sys.executable, "-c", {grandchild!r}], stdout=subprocess.DEVNULL)
            time.sleep(60)
        """)
        outdir = self.path("sigterm-out")
        cmd = [sys.executable, str(SCRIPT), "claude", "-C", str(self.repo), "-d", str(outdir), "go"]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env_for("claude", mock),
        )
        reaped = False
        try:
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "grandchild never started")
            os.kill(proc.pid, signal.SIGTERM)
            deadline = time.monotonic() + 4
            while not cleanup_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(cleanup_file.exists(), "the runner never entered group teardown")
            os.kill(proc.pid, signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=4)
            self.assertEqual(proc.returncode, 143, stderr)
            self.assertEqual(self.read(outdir, "exit"), "143\n")
            self.assertIn("❌ 已取消 (exit 143) · 0 步", stdout)
            self.assertIn(f"      │    日志: {outdir / 'stderr.log'}", stdout)
            self.assert_pid_reaped(int(pid_file.read_text()))
            reaped = True
        finally:
            if not reaped and pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    # ------------------------------------------------------------------ paths

    def test_relative_out_dir_with_devin(self):
        copy_file = self.dir / "devin-relative-prompt.txt"
        mock = self.write_mock("devin", """
            argv = sys.argv[1:]
            path = argv[argv.index("--prompt-file") + 1]
            try:
                data = open(path, encoding="utf-8").read()
            except Exception as exc:
                data = "READ-ERROR:" + str(exc)
            open(os.environ["MOCK_PROMPT_COPY"], "w", encoding="utf-8").write(data)
            sys.stdout.write("relative ok")
            sys.stdout.flush()
        """)
        proc, outdir = self.run_cli(
            "devin", mock, prompt="rel", cwd=self.dir, outdir="relative-out",
            env_extra={"MOCK_PROMPT_COPY": str(copy_file), "AGENT_WORKER_DEVIN_DB": str(self.dir / "none.db")},
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.dir / "relative-out").is_dir())
        copied = copy_file.read_text(encoding="utf-8")
        self.assertNotIn("READ-ERROR", copied)
        self.assertIn("[注：", copied)

    def test_relative_out_dir_with_kimi(self):
        copy_file = self.dir / "kimi-relative-prompt.md"
        mock = self.write_mock("kimi", """
            argv = sys.argv[1:]
            prompt = argv[argv.index("-p") + 1]
            path = prompt.split(" from ", 1)[1].split(" and execute", 1)[0]
            try:
                data = open(path, encoding="utf-8").read()
            except Exception as exc:
                data = "READ-ERROR:" + str(exc)
            open(os.environ["MOCK_PROMPT_COPY"], "w", encoding="utf-8").write(data)
            print(json.dumps({"role": "assistant", "content": "rel kimi ok", "tool_calls": []}), flush=True)
        """)
        proc, outdir = self.run_cli(
            "kimi", mock, stdin_text="长" * 64000, cwd=self.dir, outdir="relative-kimi",
            env_extra={"MOCK_PROMPT_COPY": str(copy_file)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        copied = copy_file.read_text(encoding="utf-8")
        self.assertNotIn("READ-ERROR", copied)
        self.assertGreater(len(copied.encode("utf-8")), 96_000)

    # ------------------------------------------------------------- validation

    def test_intermixed_options_after_prompt(self):
        mock = self.write_mock("claude", """
            print(json.dumps({"type": "result", "session_id": "s", "is_error": False, "result": "intermixed ok"}), flush=True)
        """)
        outdir = self.path("intermixed")
        proc = self.run_argv(
            "claude", mock, ["go", "-C", str(self.repo), "-d", str(outdir)],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "intermixed ok")

    def test_review_target_mutex(self):
        missing = self.dir / "missing"
        proc, _ = self.run_cli("codex", missing, "--base", "main", "--commit", "abc123", prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not allowed with", proc.stderr)

    def test_fixed_codex_review_rejects_prompt_after_whitespace(self):
        text = (" " * 5000) + "extra prompt"
        mock = self.dir / "unused-codex"
        proc, _ = self.run_cli("codex", mock, "--base", "main", stdin_text=text)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("do not accept a prompt", proc.stderr)

        with tempfile.TemporaryFile() as stdin:
            stdin.write(text.encode("utf-8"))
            stdin.seek(0)
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "codex", "--base", "main"],
                stdin=stdin, capture_output=True, text=True, env=self.env_for("codex", mock),
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("do not accept a prompt", proc.stderr)

    def test_ephemeral_rejected_for_kimi_and_devin(self):
        missing = self.dir / "missing"
        proc, _ = self.run_cli("kimi", missing, "--ephemeral", prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("kimi backend does not support --ephemeral", proc.stderr)
        proc, _ = self.run_cli("devin", missing, "--ephemeral", prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("devin backend does not support --ephemeral", proc.stderr)

    def test_ephemeral_rejected_with_resume(self):
        missing = self.dir / "missing"
        proc, _ = self.run_cli("claude", missing, "--ephemeral", "--resume", "abc", prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--ephemeral cannot be combined with --resume or --fork", proc.stderr)

    def test_empty_resume_and_fork_rejected(self):
        missing = self.dir / "missing"
        proc, _ = self.run_cli("claude", missing, "--resume", "", prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("non-empty", proc.stderr)

        proc, _ = self.run_cli("claude", missing, "--fork", "", prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("non-empty", proc.stderr)

    def test_base_rejected_on_non_codex(self):
        missing = self.dir / "missing"
        proc, _ = self.run_cli("claude", missing, "--base", "main", prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("only supported by the codex backend", proc.stderr)

    def test_fork_rejected_on_devin(self):
        missing = self.dir / "missing"
        proc, _ = self.run_cli("devin", missing, "--fork", "abc", prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("does not support --fork", proc.stderr)

    def test_prompt_required(self):
        missing = self.dir / "missing"
        proc, _ = self.run_cli("claude", missing)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Prompt is required", proc.stderr)

    def test_tier_rejected_on_non_codex(self):
        missing = self.dir / "missing"
        proc, _ = self.run_cli("claude", missing, "--tier", "fast", prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--tier is only supported by the codex backend", proc.stderr)

    def test_missing_binary_exits_127(self):
        missing = self.dir / "definitely-not-here"
        proc, outdir = self.run_cli("claude", missing, prompt="x")
        self.assertEqual(proc.returncode, 127, proc.stderr)
        self.assertIn("找不到可执行文件", proc.stderr)
        self.assertEqual(self.read(outdir, "exit").strip(), "127")
        self.assertIn("找不到可执行文件", self.read(outdir, "stderr.log"))

    def test_non_executable_binary_exits_126(self):
        mock = self.write_mock("claude", "print('nope')\n")
        mock.chmod(0o644)
        proc, outdir = self.run_cli("claude", mock, prompt="x")
        self.assertEqual(proc.returncode, 126, proc.stderr)
        self.assertEqual(self.read(outdir, "exit").strip(), "126")
        self.assertIn("无权限执行", self.read(outdir, "stderr.log"))

    def test_repo_must_exist(self):
        mock = self.write_mock("claude", "print('unused')\n")
        missing_repo = self.dir / "no-such-repo"
        proc, outdir = self.run_cli("claude", mock, prompt="x", repo=missing_repo)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.read(outdir, "exit").strip(), "2")
        self.assertIn("目标目录不存在", self.read(outdir, "stderr.log"))

    def test_out_file_cannot_be_directory(self):
        mock = self.write_mock("codex", "print('unused')\n")
        existing_dir = self.dir / "some-dir"
        existing_dir.mkdir()
        proc, outdir = self.run_cli("codex", mock, "-o", str(existing_dir), prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.read(outdir, "exit").strip(), "2")
        self.assertIn("不能是目录", self.read(outdir, "stderr.log"))

    def test_output_parent_is_created_before_launch(self):
        report = self.dir / "nested" / "reports" / "custom.md"
        mock = self.write_mock("claude", f"""
            assert os.path.isdir({str(report.parent)!r})
            print(json.dumps({{"type": "result", "result": "custom report"}}))
        """)
        proc, outdir = self.run_cli("claude", mock, "-o", str(report), prompt="go")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(report.read_text(encoding="utf-8"), "custom report")
        self.assertTrue(proc.stdout.endswith(f"      │    报告: {report}\n"))
        self.assertFalse((outdir / "out.md").exists())

    def test_invalid_output_parent_prevents_backend_launch(self):
        parent = self.dir / "regular-file"
        parent.touch()
        argv = self.dir / "argv.json"
        mock = self.write_mock("claude", "sys.exit(23)\n")
        proc, outdir = self.run_cli(
            "claude", mock, "-o", str(parent / "out.md"), prompt="go",
            env_extra={"MOCK_ARGV": str(argv)},
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.read(outdir, "exit"), "2\n")
        self.assertIn("无法准备输出报告路径", self.read(outdir, "stderr.log"))
        self.assertFalse(argv.exists())

    def test_report_write_failure_has_matching_exit(self):
        report = self.dir / "report"
        mock = self.write_mock("claude", f"""
            os.mkdir({str(report)!r})
            print(json.dumps({{"type": "result", "result": "report body"}}))
        """)
        proc, outdir = self.run_cli("claude", mock, "-o", str(report), prompt="go")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.read(outdir, "exit"), "2\n")
        self.assertIn("无法写入报告", self.read(outdir, "stderr.log"))
        self.assertIn("❌ 失败 (exit 2)", proc.stdout)

    def test_prompt_file_cleaned_when_events_open_fails(self):
        for backend, prompt in (("devin", "go"), ("kimi", "x" * 97_000)):
            with self.subTest(backend=backend):
                outdir = self.path("invalid-events")
                (outdir / "events.jsonl").mkdir(parents=True)
                proc, _ = self.run_cli(
                    backend, self.dir / "unused-cli", stdin_text=prompt, outdir=outdir,
                )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(self.read(outdir, "exit"), "2\n")
                self.assertEqual(list(outdir.glob(".prompt.*")), [])

    def test_codex_transient_error_recovered(self):
        mock = self.write_mock(
            "codex",
            """
import json, pathlib, sys
sys.stdin.read()
args = sys.argv[1:]
out = pathlib.Path(args[args.index("-o") + 1])
out.write_text("recovered report\\n", encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "sid-rec"}))
print(json.dumps({"type": "error", "message": "Reconnecting... 1/5"}))
print(json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "recovered report"}}))
print(json.dumps({"type": "turn.completed"}))
""",
        )
        proc, outdir = self.run_cli("codex", mock, prompt="work")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Reconnecting", proc.stdout)
        self.assertEqual(self.read(outdir, "out.md").strip(), "recovered report")

    def test_allowed_backends_config_and_env_override(self):
        cfg = self.dir / "config.json"
        cfg.write_text(json.dumps({"allowed_backends": ["codex", "qoder"]}), encoding="utf-8")
        mock_claude = self.write_mock(
            "claude",
            """
            print(json.dumps({"type": "result", "session_id": "s1", "is_error": False, "result": "claude ok"}), flush=True)
            """,
        )
        mock_qoder = self.write_mock(
            "qoder",
            """
            print(json.dumps({"type": "result", "session_id": "s2", "is_error": False, "result": "qoder ok"}), flush=True)
            """,
        )

        # Blocked by config.json
        proc_blocked, _ = self.run_cli(
            "claude", mock_claude, prompt="hi", env_extra={"AGENT_WORKER_CONFIG": str(cfg)}
        )
        self.assertEqual(proc_blocked.returncode, 2)
        self.assertIn("backend 'claude' is disabled", proc_blocked.stderr)
        self.assertIn("codex, qoder", proc_blocked.stderr)

        # Allowed by config.json
        proc_ok, out_ok = self.run_cli(
            "qoder", mock_qoder, prompt="hi", env_extra={"AGENT_WORKER_CONFIG": str(cfg)}
        )
        self.assertEqual(proc_ok.returncode, 0, proc_ok.stderr)
        self.assertEqual(self.read(out_ok, "out.md"), "qoder ok")

        # AGENT_WORKER_ALLOWED=* overrides restrictive config.json
        proc_override, out_override = self.run_cli(
            "claude",
            mock_claude,
            prompt="hi",
            env_extra={"AGENT_WORKER_CONFIG": str(cfg), "AGENT_WORKER_ALLOWED": "*"},
        )
        self.assertEqual(proc_override.returncode, 0, proc_override.stderr)
        self.assertEqual(self.read(out_override, "out.md"), "claude ok")

        # AGENT_WORKER_ALLOWED restricts directly without config.json
        proc_env_block, _ = self.run_cli(
            "claude", mock_claude, prompt="hi", env_extra={"AGENT_WORKER_ALLOWED": "qoder"}
        )
        self.assertEqual(proc_env_block.returncode, 2)
        self.assertIn("disabled by AGENT_WORKER_ALLOWED", proc_env_block.stderr)

    def test_explicit_missing_config_is_rejected(self):
        missing = self.dir / "missing.json"
        proc, outdir = self.run_cli(
            "claude", self.dir / "unused-cli", prompt="go", env_extra={"AGENT_WORKER_CONFIG": str(missing)},
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn(f"config file not found: {missing}", proc.stderr)
        self.assertFalse(outdir.exists())


class RunnerUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("run_worker", SCRIPT)
        cls.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.worker)

    def test_line_sink_buffers_utf8_split_byte_by_byte(self):
        lines = []
        sink = self.worker.LineSink(lambda line: lines.append(line.decode("utf-8")))
        for byte in "测".encode("utf-8"):
            sink.feed(bytes([byte]))
            self.assertEqual(lines, [])
        sink.feed(b"\n")
        sink.finish()
        self.assertEqual(lines, ["测"])

    def test_elapsed_always_has_five_ascii_characters(self):
        for seconds, expected in (
            (-1, "00:00"),
            (2, "00:02"),
            (3599, "59:59"),
            (5999, "99:59"),
            (6000, "1h40m"),
            (9 * 3600 + 5 * 60, "9h05m"),
            (35999, "9h59m"),
            (36000, "10h+ "),
            (100 * 3600, "99h+ "),
        ):
            with self.subTest(seconds=seconds), patch.object(
                self.worker.time, "monotonic", return_value=100 + seconds
            ):
                timestamp = self.worker.fmt_elapsed(100)
                self.assertEqual(timestamp, expected)
                self.assertEqual(len(timestamp.encode("ascii")), 5)
                self.assertEqual(self.worker.format_timeline_block("💬", "消息", timestamp).index("│"), 6)

    def test_block_cleans_wraps_and_truncates_text(self):
        full_line = "测" * 29
        self.assertEqual(
            self.worker.format_timeline_block("✏️", f"**{full_line}**`代码`…", "00:02"),
            f"00:02 │ ✏️ {full_line}\n      │    代码...",
        )
        self.assertEqual(
            self.worker.format_timeline_block("💬", full_line * 5, "00:19").splitlines(),
            [
                f"00:19 │ 💬 {full_line}",
                f"      │    {full_line}",
                f"      │    {full_line}",
                f"      │    {'测' * 26}...",
            ],
        )

    def test_timeline_sub_wraps_and_caps_on_the_rail(self):
        ui = self.worker.UI()
        output = io.StringIO()
        line = "测" * 29
        with contextlib.redirect_stdout(output):
            ui.note("开始")
            ui.sub("✏️", line * 5)
        self.assertEqual(output.getvalue().splitlines()[1:], [
            f"      │ ✏️ {line}",
            f"      │    {line}",
            f"      │    {line}",
            f"      │    {'测' * 26}...",
        ])

    def test_groups_connect_and_consecutive_subs_are_deduplicated(self):
        ui = self.worker.UI()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
            ui, "elapsed", side_effect=["00:02", "00:19", "00:29", "00:30"]
        ):
            ui.sub("🧪", "检查中…")
            ui.sub("🧪", "检查中...")
            ui.sub("✏️", "scripts/run_worker.py")
            ui.note("检查结果已记录。")
            ui.sub("✏️", "scripts/run_worker.py")
            ui.sub("✏️", "scripts/run_worker.py")
            ui.status("⏳", "思考/执行中 (2 步)...")
            ui.note("测试通过。")
        self.assertEqual(output.getvalue().splitlines(), [
            "00:02 │ 🧪 检查中...",
            "      │ ✏️ scripts/run_worker.py",
            "      │",
            "00:19 │ 💬 检查结果已记录。",
            "      │ ✏️ scripts/run_worker.py",
            "      │",
            "00:29 │ ⏳ 思考/执行中 (2 步)...",
            "      │",
            "00:30 │ 💬 测试通过。",
        ])


if __name__ == "__main__":
    unittest.main()
