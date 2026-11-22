#!/usr/bin/env python3
"""Tests for the unified worker runner using mock CLI executables and a temporary Devin SQLite DB."""
from __future__ import annotations

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
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env[f"CLI_WORKER_{backend.upper()}_BIN"] = str(mock)
        if env_extra:
            env.update(env_extra)
        return env

    def run_cli(self, backend, mock, *args, prompt=None, stdin_text=None, env_extra=None,
                timeout=90, repo=None, cwd=None, outdir=None):
        if outdir is None:
            outdir = self.path("out")
        outdir_path = pathlib.Path(outdir)
        if not outdir_path.is_absolute():
            outdir_path = pathlib.Path(cwd or os.getcwd()) / outdir_path
        cmd = [sys.executable, str(SCRIPT), backend, "-C", str(repo or self.repo), "-d", str(outdir), *args]
        if prompt is not None:
            cmd.append(prompt)
        proc = subprocess.run(
            cmd,
            input=stdin_text if stdin_text is not None else "",
            capture_output=True,
            text=True,
            env=self.env_for(backend, mock, env_extra),
            timeout=timeout,
            cwd=cwd,
        )
        return proc, outdir_path

    def run_argv(self, argv, *, stdin_text="", env=None, cwd=None, timeout=90):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *argv],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=timeout,
        )

    def read(self, outdir, name):
        return (outdir / name).read_text(encoding="utf-8")

    def init_devin_db(self):
        db = self.dir / "sessions.db"
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
        self.assertIn("🚀 Codex · default (medium)", proc.stdout)
        self.assertIn("✏️ a.py", proc.stdout)
        self.assertIn("🧪", proc.stdout)
        self.assertIn("✅ 完成", proc.stdout)
        self.assertIn("2 步", proc.stdout)
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
        self.assertNotIn("FINAL ANSWER", proc.stdout)
        self.assertNotIn("💬", proc.stdout)

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
            print(json.dumps({"type": "result", "is_error": False, "result": "boom"}), flush=True)
            sys.exit(1)
        """)
        proc, _ = self.run_cli("claude", mock, "--fork", "parent-sid", prompt="go", outdir=outdir)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertFalse((outdir / "sid").exists())

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
        self.assertIn("🚀 Qoder · Ultimate (high · review)", proc.stdout)
        self.assertIn("1.25 credits", proc.stdout)
        self.assertIn("🧪", proc.stdout)
        argv = json.loads(argv_file.read_text())
        self.assertEqual(argv[argv.index("-m") + 1], "Ultimate")
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
            db = sqlite3.connect(os.environ["CLI_WORKER_DEVIN_DB"])
            repo = os.path.realpath(os.getcwd())
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("sess-devin", repo, time.time(), "n2"))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n1", None, "sess-devin", {node1!r}))
            db.commit()
            time.sleep(1.15)
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n2", "n1", "sess-devin", {node2!r}))
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
                "CLI_WORKER_DEVIN_DB": str(db),
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
            db = sqlite3.connect(os.environ["CLI_WORKER_DEVIN_DB"])
            repo = os.path.realpath(os.getcwd())
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("sess-new", repo, time.time(), "n1"))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n1", None, "sess-new", {new_node!r}))
            db.commit()
            db.close()
            sys.stdout.write("new report")
            sys.stdout.flush()
        """)
        proc, outdir = self.run_cli(
            "devin", mock, prompt="go", env_extra={"CLI_WORKER_DEVIN_DB": str(db)}, timeout=30
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
            db = sqlite3.connect(os.environ["CLI_WORKER_DEVIN_DB"])
            repo = os.path.realpath(os.getcwd())
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("sess-tree", repo, time.time(), "n3"))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n1", None, "sess-tree", json.dumps({{"role": "user", "content": "hi"}})))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n2", "n1", "sess-tree", {branch!r}))
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n3", "n1", "sess-tree", {main!r}))
            db.commit()
            db.close()
            sys.stdout.write("tree report")
            sys.stdout.flush()
        """)
        proc, outdir = self.run_cli(
            "devin", mock, prompt="go", env_extra={"CLI_WORKER_DEVIN_DB": str(db)}, timeout=30
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("aaa_main.py", proc.stdout)
        self.assertNotIn("zz_branch.py", proc.stdout)
        self.assertIn("1 步", proc.stdout)
        events = self.read(outdir, "events.jsonl")
        self.assertIn("aaa_main.py", events)
        self.assertNotIn("zz_branch.py", events)

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
            db = sqlite3.connect(os.environ["CLI_WORKER_DEVIN_DB"])
            db.execute("INSERT INTO message_nodes VALUES (?,?,?,?)", ("n2", "n1", {sid!r}, {new_node!r}))
            db.execute("UPDATE sessions SET main_chain_id = ? WHERE id = ?", ("n2", {sid!r}))
            db.commit()
            db.close()
            sys.stdout.write("resume report")
            sys.stdout.flush()
        """)
        proc, outdir = self.run_cli(
            "devin", mock, "--resume", sid, prompt="continue",
            env_extra={"CLI_WORKER_DEVIN_DB": str(db)}, timeout=30,
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
        grandchild = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
        mock = self.write_mock("claude", f"""
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            p = subprocess.Popen([sys.executable, "-c", {grandchild!r}])
            open({str(pid_file)!r}, "w").write(str(p.pid))
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
        try:
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "grandchild never started")
            start = time.monotonic()
            os.kill(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=20)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 12.0)
            self.assert_pid_reaped(int(pid_file.read_text()))
        finally:
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
            env_extra={"MOCK_PROMPT_COPY": str(copy_file), "CLI_WORKER_DEVIN_DB": str(self.dir / "none.db")},
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
            ["claude", "go", "-C", str(self.repo), "-d", str(outdir)],
            env=self.env_for("claude", mock),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(outdir, "out.md"), "intermixed ok")

    def test_review_target_mutex(self):
        missing = self.dir / "missing"
        proc, _ = self.run_cli("codex", missing, "--base", "main", "--commit", "abc123", prompt="x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not allowed with", proc.stderr)

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


if __name__ == "__main__":
    unittest.main()
