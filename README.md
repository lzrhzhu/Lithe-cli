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

The config file holds **named profiles** — several endpoints, one active:

```json
{
  "version": 2,
  "active": "zhipu",
  "profiles": {
    "zhipu": {"base_url": "https://…", "api_key": "…", "model": "glm-4.6",
               "context_window": 128000, "cached_models": ["glm-4.6"]},
    "zai-preset": {"provider": "zai", "api_key": "…", "model": "glm-4.6"}
  }
}
```

A profile may set `"provider"` instead of (or alongside) `base_url`: the
vendor preset from `lithe.bundles.providers` fills `base_url` when unset
and contributes `LLMConfig` defaults (transport, `extra_body`,
`default_headers`) **under** your explicit values — you write the key and
model, the preset knows the endpoint shape. `LITHE_PROVIDER` overrides the
profile field; `config --list` / `--show` display it; an unknown name fails
loudly with the available presets (openai / zai / deepseek / openrouter /
qwen / moonshot).

```bash
lithe config                 # wizard (edits the active profile)
lithe config --list          # profiles, key masked
lithe config --use openrouter   # switch active profile (script-friendly)
lithe config --model glm-4.5    # set the active profile's default model
lithe models                 # GET {base_url}/models, cached into the profile
```

To configure by hand instead, point the CLI at any OpenAI-compatible
endpoint:

```bash
export LITHE_API_KEY=sk-...
export LITHE_BASE_URL=https://your-endpoint/api/v1
export LITHE_MODEL=your-model
```

Resolution order is flag (`--api-key`, `--base-url`, `--model`) >
environment > the selected profile's fields, per key; `--profile NAME`
(or `LITHE_PROFILE`) selects a profile for one invocation. The wizard
never runs without a TTY on both ends, so pipes and CI keep the hard
refusal; `--no-setup` restores that fail-fast behavior on terminals
too. `lithe config --show` peeks at the saved values with a masked key.

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
`--context-window` enables fullness gauges; `--reasoning-effort LEVEL`
sets the reasoning intensity for one invocation (flag > profile field;
`/reasoning` adjusts it in-session).

### Full-screen interface

On an interactive terminal, `lithe chat` and `lithe run TASK` open a
persistent full-screen interface (built on [Textual](https://textual.textualize.io))
instead of scribbling one-line events into the console. The left pane is
the conversation; the right sidebar is five fixed sections — 会话（current
+ recent ones, `●` marks sessions with a turn still running）、模型、运行、
工具/待办、用量:

```text
 lithe 0.8.9 · ▣ #12 重构计划 │ zhipu · glm-4.6 │ ~/myproj      ● 运行中
╭──────────────────────────────────────╮╭──────────────────────────╮
│ 把 a.txt 改成三行待办清单            ││ ◆ 会话                   │
│ ◆ edit_file · 局部修改 a.txt        ││ #12 重构计划 ●           │
│ ✓ 编辑 a.txt（+3 -1 行） 0.0s       ││ #11 bugfix ✓ 7轮         │
│ 已完成。                             ││ F3 切换 · /new 新建      │
│                                      ││ ◆ 模型                   │
│                                      ││ zhipu · glm-4.6          │
│                                      ││ ◆ 会话用量               │
│                                      ││ 输入 1,024 · 输出 216    │
╰──────────────────────────────────────╯╰──────────────────────────╯
 Tab 采纳 → /model  /models  /new
 lithe ❯ _
 ● 运行中 · 步骤 2/35   Enter 发送 · F2 侧栏 · F3 会话 · F4 模型 · F5 设置 · F6 推理 · /help
```

**Switch without leaving the screen**: `F3` opens the session picker
(`Enter` switch, `n` new, `d` delete, `r` rename), `F4` the model picker
(grouped by profile; `Enter` switch, `s` save as the profile's default,
`r` fetch `/models`), and `F5` the settings picker (`Enter` toggles a
capability; see below). Switching away from a running session does **not**
cancel it — its badge stays lit and the pane rebuilds from the store when
you come back. Typing `/` shows matching commands above the prompt (`Tab`
accepts; `/model`, `/profile`, `/set` complete their arguments too),
and `↑`/`↓` recall the persistent input history. `Ctrl+C` cancels the
current turn (a second press during the same turn force-exits) and exits
when idle. Typing plain text mid-turn returns it to the input box with a
note instead of dropping it; slash commands work while a turn runs. The
transcript follows new output only while you're at the bottom — scroll
up to re-read and your position sticks.

Text selection uses the terminal's native `Shift+drag` (hide the sidebar
with F2 first if it grabs both panes). Pipes and CI keep the plain
per-line output unchanged; the legacy prompt_toolkit full-screen was
removed — the Textual UI is the only full-screen front-end.

### Sessions

Every chat conversation persists as a kernel conversation (one run per
turn, messages written incrementally):

```bash
lithe chat -c                  # continue the most recent session
lithe chat --resume 12         # by id (or unique title prefix)
lithe sessions                 # list; newest activity first
lithe sessions --rename 12 规划 # rename
lithe sessions --delete 12     # soft delete (runs stay: log/undo work)
```

A session remembers its last profile and model and restores them on
resume (unless `--model`/env pinned). Inside chat, the same operations
are slash commands: `/sessions`, `/resume 12`, `/new [标题]`,
`/rename 标题`, `/model glm-4.5 [--save]`, `/profile zhipu`,
`/models`, `/undo`, `/tools`, `/sidebar`, `/help`, `/exit`.

### In-session settings

The per-turn knobs don't need a restart: `/set` (or `F5`) opens a picker
where `Enter` toggles a capability, and typed forms set anything —
`/set shell on`, `/set code off`, `/set vision`, `/set document on`,
`/set max-steps 50`, `/set timeout 240`, `/set attempts 3`,
`/set stream on`, `/set verbose`.
Settings apply to the **next turn** (the running turn keeps its own tool
set), stay session-scoped (nothing is written to the profile), and
turning `shell` on restates its trust warning. `/tools` re-derives the
registry after a capability flip, so the listing always matches what the
next turn will see.

**Reasoning intensity**: `/reasoning` (or `F6`) picks a level —
`off / minimal / low / medium / high`, or any value your model accepts
(`none` on some) typed directly; `--save` persists it to the profile. The
kernel maps it per protocol (`reasoning_effort` on chat-completions,
`reasoning.effort` on Responses) and combines it with
`extra_body["reasoning"]` siblings such as OpenRouter's
`max_tokens`/`exclude`. The sidebar shows the active level when set.

### Extra capabilities

The kernel ships these as bundles; the CLI grants them per flag:

| Flag | Tools granted | Notes |
| --- | --- | --- |
| `--code` | `run_code` / `run_file` | Python under bubblewrap when installed (passthrough otherwise); `doctor` shows which |
| `--shell` | `run_command` | Native bash/sh on Linux and macOS, PowerShell/cmd on Windows; runs with the current user's host permissions, is not sandboxed, and cannot be undone |
| `--skills DIR` | `load_skill` | markdown skill library; defaults to `$LITHE_HOME/skills` when it exists, `--skills ""` disables |
| `--download` | `download_file` | SSRF-guarded, size-capped network fetch |
| `--vision` | `image_info` / `analyze_image` | image probe is stdlib-only; analysis routes one vision call to the main endpoint |
| `--document` | `document_info` / `analyze_document` | PDF/DOCX/XLSX/PPTX reading on the main endpoint; `/set document` toggles in-session |
| `--document-format` | (dialect for `analyze_document`) | `inline-file` (OpenRouter family) / `files-api` (strict OpenAI upload) / `none` (probe only); default: profile field `document_format`, else provider preset |
| `--subagents` | `delegate` / `delegate_parallel` | kernel delegation on the default roster — `researcher` (read-only lookup, gains vision/document tools with those flags), `coder` (file edits + `run_code` with `--code`), `operator` (`run_command`/`download_file`, present only with `--shell`/`--download`); children inherit the endpoint and budgets, parallel workers share one live cost ceiling; `/set subagents` toggles in-session |
| `--mcp SPEC` | whatever the servers expose | JSON array/object or `@file` (env `LITHE_MCP`); stdio and streamable-http; failed servers degrade gracefully |

```bash
$ lithe run --code "用 run_code 验证 results.csv 的行数"
$ lithe run --document "总结 report.pdf 的结论，存到 NOTES.md"
$ lithe run --subagents "先让 researcher 摸清 src 结构，再让 coder 分头改"
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
$ lithe sessions                 # stored conversations (newest first)
$ lithe log abc123def456         # messages + actions of one run
$ lithe models                   # what the endpoint offers (cached)
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
| `~/.lithe/config.json` (or `LITHE_HOME`) | endpoint profiles: `{version, active, profiles}` (0600 — it holds keys) |
| `~/.lithe/runs` (or `--store`) | JSONL run store: conversations, runs, messages, actions, undo records |
| `~/.lithe/runs/todos-<scope-hash>.json` | the agent's task list, isolated by user and resolved workspace |
| `~/.lithe/skills` (or `--skills`) | the markdown skill library, when enabled |
| `~/.lithe/history` | chat input history (prompt_toolkit `FileHistory`; `↑`/`↓` recall in the TUI, Ctrl+R search in the plain REPL) |

## Design notes

- The CLI is a thin host over a `Workbench`: the workbench owns the
  endpoint view (profile + model), the session manager and the running
  turns; the plain REPL and the full-screen Textual app are both just
  subscribers to its event bus and callers of one dispatcher. Everything
  else (ReAct loop, streaming, budgets, replay, undo engine) is reused
  from lithe.
- The Textual screen is tested headlessly with `App.run_test()` pilots
  (turns, F2/F3/F4, history, completion) — real interaction tests that
  also run on Windows; its pure builders (sidebar/header/footer markup,
  completion suggestions) and the shared `TuiState` event folding have
  direct unit tests.
- `python -m lithe_cli` works alongside the `lithe` console script.

## License

MIT
