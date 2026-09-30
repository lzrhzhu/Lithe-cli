# lithe-cli

[![PyPI](https://img.shields.io/pypi/v/lithe-cli.svg)](https://pypi.org/project/lithe-cli/)
[![Python](https://img.shields.io/pypi/pyversions/lithe-cli.svg)](https://pypi.org/project/lithe-cli/)
[![License: MIT](https://img.shields.io/pypi/lithe-cli.svg)](https://pypi.org/project/lithe-cli/)

**lithe-cli** is the command-line interface for
[lithe](https://pypi.org/project/lithe/) — the storage-free ReAct agent
kernel. Installing it pulls in `lithe` automatically and gives you a `lithe`
command: chat with an agent that reads and writes files in your workspace,
runs code, searches the web through MCP tools, inspects stored runs, and
undoes a run's mutations.

```bash
pip install lithe-cli
```

## Configure

There is no default endpoint — the CLI refuses to run rather than silently
hitting some third-party URL. The easiest way in is the wizard: on the
first `lithe chat` / `lithe run` (or any time, via `lithe config`) an
interactive terminal prompts for the three essentials and saves them to
`$LITHE_HOME/config.json` (mode 0600 — it holds the key). An optional
connectivity probe catches typos before your first turn.

To configure by hand instead, point the CLI at any OpenAI-compatible
endpoint:

```bash
export LITHE_API_KEY=sk-...
export LITHE_BASE_URL=https://your-endpoint/api/v1
export LITHE_MODEL=your-model
```

Resolution order is flag (`--api-key`, `--base-url`, `--model`) >
environment > saved config file, per key. The wizard never runs without
a TTY on both ends, so pipes and CI keep the hard refusal; `--no-setup`
restores that fail-fast behavior on terminals too. `lithe config --show`
peeks at the saved values with a masked key.

`LITHE_HOME` (default `~/.lithe`) locates the run store; the agent's
workspace defaults to the current directory (`--workspace` to change).

`lithe doctor` prints the effective configuration — endpoint (with a
masked key), config file, store, workspace, sandbox backend, skills, MCP
servers — so you can see what a run would use before starting one.

## Use

### One-shot task

```bash
$ cd my-project
$ lithe run "总结 README.md 的要点，存到 SUMMARY.md"
  ⚒ read_file 读取 README.md
  ✓ 已读取 README.md（120 行） 0.1s
  ⚒ write_file 写入 SUMMARY.md
  ✓ 写入 SUMMARY.md（+12 行，新建） 0.0s
已把要点写入 SUMMARY.md。
── done · steps 3 · tokens 2100 · cost 0.0042
```

修改已有文件时，助手优先调用 `edit_file` 或 `apply_patch` 做局部更新；只有新建文件或需要整体替换时才使用 `write_file`。工具调用界面只显示工具名、文件路径等摘要，文件写入/编辑的结果带 `+N -M 行` 的行数变化提示（lithe 0.9.10 起由内核统计）。

`--stream` streams tokens as they generate; `-v` adds per-call
usage/context gauges; `--max-steps` caps the tool loop;
`--context-window` enables fullness gauges.

### Full-screen interface

On an interactive terminal, `lithe chat` and `lithe run TASK` open a
persistent full-screen interface instead of scribbling one-line events
into the console:

```text
 lithe 0.6.2 · your-model · /home/me/my-project      ● 完成 · 0.2s
╭─ 对话 ──────────────────────────────╮╭─ 运行状态 ─────────╮
│ 把 a.txt 改成三行待办清单            ││ ● 完成 · 0.2s      │
│ ◆ update_todos                      ││ 步骤 2/35 · 0.2s   │
│ ✓ 任务清单已更新（3 条） 0.0s        ││ ✓ update_todos 0.0s│
 │ ◆ edit_file · 局部修改 a.txt        ││ ✓ edit_file 0.0s   │
│ ✓ 编辑 a.txt（+3 -1 行） 0.0s       ││ ◆ 待办 1/3         │
│ 已完成。                             ││ [~] 修改 a.txt     │
│                                     ││ ◆ 会话用量         │
│                                     ││ 输入 1,024 · 输出 216│
│                                     ││ 缓存输入 768       │
│                                     ││ 合计 1,240 · $0.0021│
│                                     ││ 上下文 1,024 / 128,000 (0.8%)│
╰─────────────────────────────────────╯╰────────────────────╯
lithe ❯ _
 ● 完成 · 0.2s          Enter 发送 · F2 侧栏 · /help 命令
```

The left pane is the conversation (task, compact tool-call summaries
and outcomes — file edits land with `+N -M 行` counts — and the
streaming assistant reply). The right sidebar shows run status, recent
tools, todos, session-wide cumulative input/output/cached tokens and
cost, plus the latest model-call context length and window percentage;
usage numbers live only there, the bottom bar keeps status and key
hints. The conversation has an application-managed mouse selection: drag across
its text and press `Ctrl+C` to copy only the selected conversation, without
sidebar content. `Shift+drag` bypasses the application and uses the terminal's
native selection, which can cross both panes; hide the sidebar with F2 or
`/sidebar` before using native selection. The conversation scrolls with the
mouse wheel, PageUp/PageDown and Home/End (a new turn jumps back to the output).
`Ctrl+C` cancels the current turn when there is no conversation selection and
exits when idle. In `lithe run` mode the result stays on screen until you press
`q`. Layout, wrapping and alignment are East-Asian-width aware; narrower
terminals stack the two panes vertically. Pipes and CI keep the plain per-line
output unchanged.

History carries across turns within a session (each turn is its own run
in the store, replayed as context for the next). `/new` clears the
conversation history and session usage totals; `/tools` lists the
registered tools, `/help` lists the commands.

### Extra capabilities

The kernel ships these as bundles; the CLI grants them per flag:

| Flag | Tools granted | Notes |
| --- | --- | --- |
| `--code` | `run_code` / `run_file` | Python under bubblewrap when installed (passthrough otherwise); `doctor` shows which |
| `--shell` | `run_command` | Native bash/sh on Linux and macOS, PowerShell/cmd on Windows; runs with the current user's host permissions, is not sandboxed, and cannot be undone |
| `--skills DIR` | `load_skill` | markdown skill library; defaults to `$LITHE_HOME/skills` when it exists, `--skills ""` disables |
| `--download` | `download_file` | SSRF-guarded, size-capped network fetch |
| `--vision` | `image_info` / `analyze_image` | image probe is stdlib-only; analysis routes one vision call to the main endpoint |
| `--mcp SPEC` | whatever the servers expose | JSON array/object or `@file` (env `LITHE_MCP`); stdio and streamable-http; failed servers degrade gracefully |

```bash
$ lithe run --code "用 run_code 验证 results.csv 的行数"
$ lithe run --shell "检查当前目录的项目状态"
$ lithe chat --skills ~/my-skills --mcp @~/mcp.json
```

### Undo a run

The bundled file and todo tools register reverters, so undo works with zero
configuration:

```bash
$ lithe runs                     # find the run
run           status  steps  cost  task
────────────  ──────  ─────  ────  ────────────────
abc123def456  done    3      0.01  总结 README.md …
$ lithe undo abc123def456
✓ 已撤销 2 个操作（run abc123def456）
```

A file the run created is deleted; a file it overwrote is restored.
(`run_code` side effects are not revertible — its tool description warns
the model.)

### Inspect

```bash
$ lithe runs                     # stored runs (newest last)
$ lithe log abc123def456         # messages + actions of one run
$ lithe doctor                   # config + capability status
```

### Tools

```bash
$ lithe tools                    # what the agent can do
tool          category  description
────────────  ────────  ──────────────────────────────
apply_patch   WRITE     以行级 patch 一次修改多个文件…
read_file     READ      读取文件内容…
...
```

The default tool set is the workspace bundle (`read_file` / `write_file` /
`edit_file` / `list_files` / `search_files` / `glob_files` /
`apply_patch`) plus todos (`update_todos` / `list_todos`); the flags above
add capabilities. (MCP tools attach at run time, so `lithe tools` lists
them only after a session has started the servers.)

### Colors

Output is colored when stdout is a TTY and `NO_COLOR` is unset; `--color`
/ `--no-color` force either way. Output rendering itself stays
dependency-free — plain text degrades cleanly through pipes and cron.
Interactive *input* (the chat prompt and the setup wizard) is
[prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/):
wide-character-safe editing, bracketed-paste handling, star-echoed API
keys, inline wizard validation, and persistent history.

## Where things land

| Path | Contents |
| --- | --- |
| current dir (or `--workspace`) | the agent's sandboxed workspace — every tool path resolves strictly inside it |
| `~/.lithe/runs` (or `--store`) | JSONL run store: messages, actions, undo records |
| `~/.lithe/runs/todos-<scope-hash>.json` | the agent's task list, isolated by user and resolved workspace |
| `~/.lithe/skills` (or `--skills`) | the markdown skill library, when enabled |
| `~/.lithe/history` | chat input history (prompt_toolkit `FileHistory`; Up/Ctrl+R recall) |

## Design notes

- The CLI is a thin host: it supplies tools, a system prompt, and the
  kernel's `JsonlRunStore`; everything else (ReAct loop, streaming, budgets,
  replay, undo engine) is reused from lithe.
- End-to-end behavior is tested offline against a scripted transport — no
  test spends tokens.
- `python -m lithe_cli` works alongside the `lithe` console script.

## License

MIT
