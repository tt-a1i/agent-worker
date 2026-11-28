<div align="center">

<p align="center"><img src="assets/cover.png" alt="agent-worker cover" width="640"></p>

# agent-worker

**让任意宿主 AI Agent（Claude Code、Codex、Antigravity、Cursor 等）统一调度本机 `codex` / `claude` / `qoder` / `kimi` / `devin` 作为受控后台 Worker 与交叉审查员。**

*统一 CLI 入口 · 实时可读进度流 · 上下文零污染 · 进程组级干净回收 · 零第三方依赖*

<p>
  <a href="https://github.com/tt-a1i/agent-worker/actions/workflows/ci.yml"><img src="https://github.com/tt-a1i/agent-worker/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Skill-Agent%20Skills%20Standard-2563EB?style=flat-square" alt="Agent Skills Standard">
  <img src="https://img.shields.io/badge/Python-3.9%2B%20(Stdlib)-10B981?style=flat-square" alt="Python 3.9+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-64748B?style=flat-square" alt="MIT License"></a>
</p>

<p>
  <strong>简体中文</strong> · <a href="README.en.md">English</a>
</p>

</div>

---

## 安装与快速开始

```bash
# 方式 1：通过 skills CLI 安装到宿主 Agent
npx skills add tt-a1i/agent-worker

# 方式 2：直接克隆到任意 Agent 的 skills 目录（如 ~/.claude/skills 或 ~/.agents/skills）
git clone https://github.com/tt-a1i/agent-worker.git ~/.claude/skills/agent-worker
```

安装后直接在宿主对话中自然语言调用（如 *“让 Codex 修复这个模块的测试”*、*“用 Qoder 做一轮独立代码审查”*）或输入 `/agent-worker`。

### 实时进度展示效果

在宿主后台任务面板中实时显示干净的人话进展（自动隐藏底层繁杂命令与长篇结项报告，完整报告单独写入 `out.md`）：

```text
🚀 Qoder · DeepSeek-Flash (high) · my-repo
──────────────────────────────────────────────────
00:02 │ 💬 我先检查这两个文件的内容。
      │
00:04 │ 💬 mul 返回了和，我会修正乘法并补充零值测试。
      │ ✏️ src/math_ops.py
      │ ✏️ tests/test_math.py
      │
00:06 │ 💬 修复完成，现在运行测试验证。
      │ 🧪 运行测试/检查中...
──────────────────────────────────────────────────
00:09 │ ✅ 完成 · 5 步 · 0.27 credits
      │    报告: /tmp/worker.qoder.A1b2C3/out.md
```

## 特性

- **统一入口**：`scripts/run_worker.py <backend> ...`，五个后端共享同一套参数与输出约定。
- **实时进度**：把后端原始流式事件翻译成 `💬` 说明、`✏️` 文件改动、`🧪` 测试线索、`🤝` 子代理和 40 秒心跳，避免刷屏的原始日志。
- **干净上下文**：退出码为 0 时只需读取摘要中显示的报告路径；原始事件、stderr、会话 ID、退出码分别落盘便于排障。
- **会话复用**：支持 `--resume` 续跑与 `--fork` 分叉（Devin 不支持 fork，Kimi 会自动解析分叉后新会话 ID）。
- **隔离运行（部分后端）**：`codex`/`claude`/`qoder` 默认跳过用户全局 MCP/插件配置（保留登录态），需要时用 `--include-user-config` 恢复；`kimi`/`devin` 无此隔离。
- **进程组生命周期**：SIGINT/SIGTERM/SIGHUP 转发到整个进程组，取消 5 秒未退出或收到二次信号即 SIGKILL；即使组长先退出也会回收仍存活的后代进程与继承的管道。
- **UTF-8 安全**：按字节缓冲、按行切分，跨 8192 字节块的多字节中文边界与末尾无换行均可正确处理。
- **仅标准库**：零第三方依赖。

## 环境要求

- Python 3.9+（仅标准库）。
- **POSIX 系统**（macOS / Linux）：依赖进程组、POSIX 信号与 `select` 管道监督，不支持原生 Windows。
- 至少安装并登录一个后端 CLI：
  - `codex`（Codex CLI）
  - `claude`（Claude Code）
  - `qodercli`（Qoder）
  - `kimi`（Kimi Code）
  - `devin`（Devin 桌面版内置 CLI）
- macOS 上会自动探测常见安装路径；其它平台请确保对应命令在 `PATH` 中，或用环境变量指定二进制。

## 快速开始

```bash
git clone https://github.com/tt-a1i/agent-worker.git
d=$(mktemp -d /tmp/worker.codex.XXXXXX)
python3 agent-worker/scripts/run_worker.py codex -C /path/to/repo -d "$d" <<'EOF'
修复 tests/test_parser.py 中的失败用例；先复现，再最小改动修复，并只运行相关测试。
在最终回复中报告实际执行的验证命令、特殊的工具链或环境路径、未验证项和超出任务范围的改动。运行器会自动保存最终回复，请勿在仓库中创建报告文件。
EOF
cat "$d/out.md"
```

## 后端与默认模型

| 后端 | 日常任务 | 评审任务 |
| --- | --- | --- |
| `codex` | 省略 `-m` 时使用本机 Codex 默认模型（`-e medium`） | `-e high`（高难度架构/高风险评审可显式指定旗舰 `-m`） |
| `claude` | `sonnet`（`-e medium`） | `opus`（`-e high`） |
| `qoder` | `DeepSeek-Flash`（`-e high`） | `DeepSeek-Flash`（`-e high`） |
| `kimi` | 省略 `-m` 时读取 `~/.kimi-code/config.toml` 的 `default_model` | 显式传 `-m <alias>` |
| `devin` | 省略 `-m` 时使用 Devin 已配置的默认模型 | `-m sonnet\|opus\|codex` |

Codex 的 `service_tier` 默认保持 `default`，不建议使用 `fast`/`ultrafast`，除非用户明确要求。

## 后端能力矩阵

| 后端 | `--ephemeral` | `--fork` | 配置隔离（默认） | 原生 diff 评审 |
| --- | --- | --- | --- | --- |
| `codex` | ✅ | ✅ | ✅ `--ignore-user-config` | ✅ `--base`/`--commit`/`--uncommitted` |
| `claude` | ✅（不可与 `--resume`/`--fork` 组合） | ✅ | ✅ 仅项目配置（默认关闭 MCP） | ❌ |
| `qoder` | ✅（同上） | ✅ | ✅ 仅项目配置（默认关闭 MCP） | ❌ |
| `kimi` | ❌ | ✅ 自动解析新会话 ID | ❌ 沿用用户配置 | ❌ |
| `devin` | ❌ | ❌ | ❌ 沿用用户配置 | ❌ |

`--ephemeral` 只在能真正关闭持久化的后端生效，且与 `--resume`/`--fork` 互斥。`kimi`/`devin` 会在启动前直接拒绝。`sid` 在确认新会话（或分叉后的新 ID）后写入。Devin 从 `sessions.db` 读取事件。新会话记录了本次运行的提示词文件时，运行器可实时显示数据库事件，否则会在结束时检查是否有唯一匹配的会话。无法确定事件归属时，仍会保存 Devin 的 stdout 作为报告。

Claude 和 Qoder 通过 `--setting-sources project,local --strict-mcp-config` 保留项目和本地设置，禁用未配置的 MCP 服务器。

## 常用命令

### 执行任务

```bash
d=$(mktemp -d /tmp/worker.claude.XXXXXX)
python3 scripts/run_worker.py claude -C "$repo" -d "$d" -m sonnet -e medium <<'EOF'
<目标、已知复现步骤/错误/路径、验证期望、提交停止点>
EOF
```

### 独立评审（计划或 diff）

```bash
# Codex 原生 diff 评审
d2=$(mktemp -d /tmp/worker.review.XXXXXX)
python3 scripts/run_worker.py codex -C "$repo" -d "$d2" -o "$d2/review.md" --review --base main

# 其它后端：--review + 只读提示词
d3=$(mktemp -d /tmp/worker.review.XXXXXX)
python3 scripts/run_worker.py qoder -C "$repo" -d "$d3" -o "$d3/review.md" --review <<'EOF'
检查 `git diff main...HEAD`，报告真实缺陷、回归和缺失边界。请勿创建或修改文件、快照及其他产物。
EOF
```

### 续跑与分叉

```bash
# 续跑（务必使用新的 -d，避免覆盖上次的日志）
d2=$(mktemp -d /tmp/worker.resume.XXXXXX)
python3 scripts/run_worker.py codex -C "$repo" -d "$d2" --resume "$(cat "$d/sid")" <<'EOF'
修复以下已确认问题并重新验证……
EOF

# 分叉出另一条路线（后端需与取得 sid 的后端一致）
python3 scripts/run_worker.py codex -C "$repo" -d "$d3" --fork "$(cat "$d/sid")" <<'EOF'
尝试另一种实现方案……
EOF
```

## 输出文件

| 文件 | 说明 |
| --- | --- |
| `out.md` | 最终报告（成功时只需读它） |
| `events.jsonl` | 后端原始事件流 |
| `stderr.log` | 后端 stderr（失败时查看） |
| `sid` | 新会话 ID（仅在确认后写入，用于 `--resume`/`--fork`） |
| `exit` | 最终退出码 |
| `.prompt.*` | Devin 提示词和超长 Kimi 提示词的临时文件（权限 `0600`，运行结束自动删除） |

## 环境变量与本地配置

| 变量 / 配置 | 作用 |
| --- | --- |
| `~/.config/agent-worker/config.json` | 可选本地配置，如 `{"allowed_backends": ["codex", "qoder"]}` 用于限制本机允许调用的后端 |
| `AGENT_WORKER_ALLOWED` | 逗号分隔的后端白名单（如 `codex,qoder`，传 `*` 放行全部），优先级高于配置文件 |
| `AGENT_WORKER_CONFIG` | 覆盖配置文件路径。显式指定的文件不存在时会报错，传空字符串可关闭配置加载。 |
| `AGENT_WORKER_<BACKEND>_BIN` | 覆盖后端可执行文件路径，如 `AGENT_WORKER_CODEX_BIN` |
| `AGENT_WORKER_DEVIN_DB` | 覆盖 Devin 会话数据库路径（默认 `~/.local/share/devin/cli/sessions.db`） |

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/run_worker.py
```

测试使用临时 mock 可执行文件与临时 SQLite 数据库，不会调用真实模型。

## 目录结构

```text
agent-worker/
├── SKILL.md                 # 技能说明（供主机 Agent 阅读）
├── assets/
│   └── cover.png            # README 封面（来自 LiuShengyu-Tech/chatgpt-desktop-mcp，MIT）
├── scripts/
│   └── run_worker.py        # 统一执行器（仅标准库）
├── tests/
│   └── test_run_worker.py   # 端到端单元测试
├── README.md
├── README.en.md
├── LICENSE
└── .gitignore
```

## 许可

MIT，见 [LICENSE](LICENSE)。

封面图片 `assets/cover.png` 来自 [LiuShengyu-Tech/chatgpt-desktop-mcp](https://github.com/LiuShengyu-Tech/chatgpt-desktop-mcp)（MIT）。
