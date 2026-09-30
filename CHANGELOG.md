# Changelog

## 0.6.4 (2026-09-30)

The honest-copy release: selecting text in the conversation no longer
drags the status sidebar along, and todo planning keeps to itself.

- **in-app conversation selection + copy** — the conversation pane is
  now a selectable `BufferControl`: drag with the mouse and press
  `Ctrl+C` to copy exactly the selected conversation text (frame
  borders stripped) via the clipboard and OSC 52. `Ctrl+C` keeps its
  old cancel/exit meaning whenever no conversation selection is
  active. Requires lithe >= 0.9.11 for the paired kernel fixes.
- **native selection documented honestly** — `Shift+drag` is the
  terminal's own selection and can span both panes (Windows are
  rendering containers, not selection boundaries); F2 / `/sidebar`
  still hides the sidebar for a clean full-width copy. README, welcome
  text and `/help` now say so instead of promising isolation.
- **todo planning stays scoped** — the system prompt only asks for
  `update_todos` when the user wants a plan or the work has distinct
  trackable stages; ordinary questions and small edits create nothing.
- **todo storage is per user + workspace** — the task list file is now
  keyed by a hash of (user, resolved workspace) instead of a bare user
  name, so two projects (or two `--workspace` targets) no longer share
  or overwrite one list; the filename is always filesystem-safe.
- **layout hardening** — very short terminals (< 8 rows, narrow
  layout) drop the stacked sidebar instead of overbooking the screen;
  pane geometry tests now cover those boundaries.

## 0.6.3

- The bottom bar no longer duplicates the sidebar's usage numbers: it
  keeps the status chip and key hints only (tokens, cost, steps and the
  context gauge stay in the sidebar's 会话用量 block).
- File edits now land with coding-agent style line counts — `写入
  a.py（+2 行，新建）`, `编辑 a.py（+3 -1 行）`, `应用 2 项（+5 -2 行）` —
  computed by the lithe 0.9.10 kernel from the real before/after
  contents and shown in both the TUI feed and the plain renderer.

## 0.6.2

The independent-panes release: the conversation and the status sidebar
no longer share one rendering surface, and the conversation scrolls.

- **separate pane windows** — the full-screen layout is now a
  `DynamicContainer` of distinct `Window`s (header, conversation,
  sidebar, input, footer) instead of one pre-composited text frame, so
  terminal selection can no longer bleed from one pane into another.
- **scrollable conversation** — the pane keeps a scroll offset instead
  of only ever repainting the tail: mouse wheel (application mouse
  support enabled), PageUp/PageDown, Home/End. A new turn resumes
  following the output. Native copy still works with Shift+drag.
- **F2 / `/sidebar` toggles the status pane** — hiding it gives the
  conversation the full width, so copying a reply no longer picks up
  sidebar text even geometrically.
- **todo planning follows the work** — the CLI system prompt now tells
  the agent to size `update_todos` to the actual step count instead of
  defaulting to three items (pairs with lithe 0.9.9's tool-description
  fix).
- **tests** — 39 offline tests (was 37): pane geometry in wide, stacked
  and sidebar-hidden layouts; scroll windowing, clamping and
  per-turn reset; a PTY drive of the real full-screen app pressing
  PageUp/PageDown/F2/Ctrl+C.

## 0.6.1

- Show session-cumulative input, output, cached, total-token and cost usage, plus the latest context length and window utilization.
- Summarize tool calls instead of rendering full arguments, and guide edits toward `edit_file` or `apply_patch` for existing files.
- Refresh the full-screen palette and rounded panel borders; keep frame rows aligned to terminal width.

## 0.6.0

The full-screen release: `lithe chat` / `lithe run` now open a
persistent full-screen interface on interactive terminals — the screen
no longer falls back to the raw console between (or after) runs, which
the 0.5.0 one-shot dashboard did.

- **persistent full-screen session** (`lithe_cli.tui`) — one
  prompt_toolkit `Application` owns the terminal for the whole session:
  a conversation pane (task, tool calls and outcomes, streaming
  assistant text) on the left and a status sidebar on the right, over an
  input line and a colored status bar. A finished turn simply returns
  the input line; the panes keep the transcript, todos and totals.
- **colored, structured panels** — header bar (version · model ·
  workspace · live status chip), bordered panes with section rules
  (状态 / 工具调用 / 待办 / 用量), styled feed lines (user blue, assistant
  green, tools cyan, errors red, reasoning magenta, dim meta), and a
  footer status bar with step/token/cost counters and key hints.
- **Ctrl+C cancels a turn instead of exiting** — `execute()` now
  surfaces the kernel's `stop` handle; during a run Ctrl+C cancels the
  current turn and returns the input line, when idle it exits. In
  `lithe run` mode the result stays on screen until `q` / Enter.
- **CJK-safe layout** — panel widths adapt to the real terminal size
  (side-by-side at ≥76 columns, stacked below); all wrapping and
  alignment is East-Asian-width aware, so Chinese task text and tool
  arguments stay aligned in both layouts.
- **non-TTY unchanged** — pipes, CI and cron keep the plain per-line
  event format; the TUI engages only when stdout is a real terminal
  (`TERM=dumb` disables it too). The per-turn transcript, history replay
  and `/help` / `/tools` / `/new` / `/exit` slash commands work the same
  in both faces.
- **tests** — 35 offline tests (was 33): TUI state updates from kernel
  events, frame/footer composition fits wide and narrow terminals with
  CJK content, `execute(on_event=…)` stays silent while surfacing every
  event, CJK wrapping, plus a PTY end-to-end drive of the full-screen
  session.

## 0.5.0

The dashboard release: agent runs on an interactive terminal now render
into a live two-pane board instead of one-line event scribbles — the
single-line tool-call/todo output that was impossible to read becomes
two aligned panels that repaint on every event.

- **live run dashboard** (`lithe_cli.ui.RunDashboard`) — every `run`/
  `chat` turn on a TTY switches to an alternate-screen board: the left
  pane carries the task, an activity feed (tool invocations and their
  outcomes) and the assistant reply as it streams; the right sidebar
  shows status, model, step budget, elapsed time, recent tool calls with
  per-call duration, the agent's todo list (seeded from the todo store,
  updated live on `todo_change` events), and running token / context /
  cost totals. When the run ends the screen restores and the final
  answer prints as ordinary output, followed by the usual one-line
  footer.
- **layout adapts to the terminal** — narrower than 76 columns the
  panes stack vertically; all rendering is display-width-aware, so CJK
  task text and tool args stay aligned and inside the terminal in both
  layouts.
- **non-TTY output unchanged** — pipes, CI and cron keep the per-line
  event format; the dashboard engages only when stdout is a real
  terminal (`TERM=dumb` disables it as well), so scripted consumers see
  byte-identical output to 0.4.1.
- **tests** — 33 offline tests (was 31): dashboard rendering
  (tools/todos/usage visible, wide and narrow terminals each fit their
  width) on top of the existing suite.

## 0.4.1

Pins the fixed kernel: no CLI code changes, but the dependency floor moves
to `lithe>=0.9.8` so upgrading lithe-cli pulls the workspace fix — an
agent pointed at a project directory containing `venv`/`node_modules`/
`.git` (or any large repo) previously had every new-file `write_file`
refused by the kernel's total-tree file-count guard, and `undo` restores
failed the same way. 0.9.8 moves that guard to a per-run created-files
budget and ignores dependency/VCS directories in listings. `--shell` and
the layout work from 0.4.0 are unchanged.

## 0.4.0

The shell + layout release: the agent can now run native host commands
when explicitly granted, and the terminal output adapts to the terminal
it is rendered in.

- **`--shell` flag** — grants `run_command` (the new lithe 0.9.7
  `command` bundle): native bash/sh on Linux and macOS, PowerShell/cmd
  on Windows, executed in the workspace with timeout and output caps.
  Off by default like every capability flag — `lithe doctor` shows the
  state, and the system prompt tells the model the command runs with
  current-user host permissions and cannot be undone. Requires
  `lithe>=0.9.7`.
- **width-aware rendering** — `UI` now knows the terminal width
  (`shutil.get_terminal_size`): tables shrink column-by-column (last
  column first) and ellipsize instead of wrapping, the chat banner and
  section rules never exceed the terminal, and `runs` task text is
  clipped by display width — CJK task text no longer occupies more
  columns than a bare character slice assumed.
- **clearer banner** — the chat banner shows `lithe <version> · command
  agent` with labeled model/workspace rows, truncated to fit.
- **tests** — 31 offline tests (was 29): narrow-terminal table and
  banner fit assertions plus `--shell` opt-in coverage.

## 0.3.0

The prompt_toolkit release: every interactive prompt now runs on
[prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/) instead
of hand-rolled readers — the first runtime dependency beyond the lithe
kernel, traded for input handling the field had proven we couldn't get
right with `input()`/`getpass`/readline.

- **chat input** (`lithe_cli.prompts.chat_line`) — the REPL prompt gets
  prompt_toolkit's editor: wide-character-safe rendering (the long-CJK
  corruption class is structurally gone), native bracketed-paste
  handling, Ctrl+R search, and Up-recall backed by a persistent
  `FileHistory` at `~/.lithe/history`.
- **wizard prompts** (`lithe_cli.prompts.ask`) — inline validation
  (Enter is refused with the message until the value passes), editable
  prefilled defaults, and `is_password` star echo for the API key —
  replacing the custom cbreak reader from 0.2.4.
- **removed** — `lithe_cli/ttyio.py` (the ~200-line cbreak reader) and
  its bespoke escape/paste state machines; upstream owns those concerns
  now. The UnicodeDecodeError safety nets stay: a line that arrives
  broken is still dropped with the UTF-8 hint, never a crash.
- **wizard guard** — `lithe-cli config` refuses early (message, not a
  ptk traceback) when stdin/stdout aren't TTYs.
- **tests** — 28 offline tests: the PTY suite now drives real
  prompt_toolkit (long-CJK line, paste wrappers, password stars,
  inline URL validation, cross-session history recall) against a
  harness that answers cursor-position requests like a real terminal —
  without that reply ptk degrades and history keys misbehave.

## 0.2.4

The readline divorce: chat input no longer passes through GNU readline,
whose redisplay could corrupt multibyte buffers on long wrapped lines
(a line the terminal echoed correctly came back from `input()` with a
byte lost mid-character, killing the turn with UnicodeDecodeError).

- **own cbreak reader (`lithe_cli.ttyio`)** — chat reads bytes straight
  off `/dev/tty` and echoes exactly what arrived; the terminal does the
  wrapping, so there is no column arithmetic to get wrong. Editing
  support: backspace (width-aware for CJK), Ctrl+W, Ctrl+U, Up/Down
  in-memory history, Ctrl+C/Ctrl+D. Bracketed-paste wrappers and
  unknown escape sequences (focus events, cursor replies, stray arrows)
  are swallowed instead of leaking into the line. Falls back to plain
  `input()` for pipes/tests; undecodable lines still raise for the
  caller to drop with the UTF-8 hint.
- **masked prompt decoding fix** — the API-key reader decoded byte by
  byte with `errors="replace"`, which turned every non-ASCII character
  into three U+FFFD; it now uses a strict incremental UTF-8 decoder
  (shared with the chat reader), so non-ASCII secrets round-trip.
- **tests** — 30 offline tests (was 29): PTY coverage replaying the
  exact long Chinese line from the field (IME-style bursts + injected
  focus events), history recall, CJK backspace, masked non-ASCII input,
  and paste-wrapper stripping.

## 0.2.3

The resilience release: a mangled input line no longer kills the process.

- **undecodable input never crashes** — typing/pasting a line that arrives
  as broken UTF-8 (readline/locale/IME mismatch, e.g. `LANG=C` sessions on
  cloud VMs) used to raise `UnicodeDecodeError` out of `input()` and take
  the whole `chat` loop down with a traceback. The chat loop and every
  wizard prompt now catch it, drop the line with a hint to set a UTF-8
  locale (`export LANG=C.UTF-8`), and keep going.
- **tests** — 29 offline tests (was 28): a broken-UTF-8 line in the chat
  loop and in wizard prompts is survived with a warning, plus the
  star-echo PTY coverage from 0.2.2.

## 0.2.2

The paste release: configuring an API key by pasting it now works, and
the hidden prompt answers with a `*` per character instead of silence.

- **paste-safe prompts** — pasted values are no longer corrupted by
  bracketed-paste wrappers (`\x1b[200~…\x1b[201~`, enabled by zsh/recent
  bash) that `input`/`getpass` pass through verbatim; a pasted API key
  used to be saved with them glued on and fail auth with 401. All three
  wizard answers are cleaned before validation and save.
- **star echo for the API key** — the hidden prompt now echoes one `*`
  per accepted character (custom cbreak reader over `/dev/tty`, with
  backspace/Ctrl+U support and a `getpass` fallback for non-TTY or
  non-POSIX), so a paste is visible feedback instead of silence.
- **tests** — 28 offline tests (was 27): PTY-level coverage that pastes
  a key wrapped in bracketed-paste markers and asserts both the clean
  saved value and the star feedback.

## 0.2.1

The first-run release: a missing endpoint is now a three-question wizard
on interactive terminals instead of a refusal with export lines.

- **first-run setup wizard** — `lithe run` / `lithe chat` with no endpoint
  configured, on a TTY, prompt for base URL / API key (hidden input) /
  model, validate the URL scheme, and save to
  `$LITHE_HOME/config.json` (mode 0600 — it holds the key). An optional
  connectivity probe (`GET {base_url}/models`) catches typos before the
  first turn; it warns, never blocks.
- **`lithe config`** — re-run the wizard any time (re-editing, not
  retyping: saved values are the defaults); `--show` prints the saved
  config with a masked key.
- **config file layer** — endpoint resolution is now flag > env >
  config file, per key. Nothing that worked before changes: env-only and
  flag-only setups behave identically, and non-interactive contexts
  (pipes, CI) keep the hard refusal — the wizard never runs without a
  TTY on both ends.
- **`--no-setup`** — opt out of the wizard on `run`/`chat` for scripting
  that wants the old fail-fast behavior explicitly.
- **doctor** — new `config` line showing the file's path and whether an
  endpoint is saved; the missing-endpoint hint now points at
  `lithe config`.
- **tests** — 27 offline tests (was 20): the config-file layer (fills,
  env/flag precedence, corrupt-file tolerance, 0600 perms), the wizard
  (save round-trip, cancel, URL validation, TTY-only dispatch in
  `require_endpoint`), and the hermetic missing-endpoint refusal.

## 0.2.0

The capability release: the CLI now surfaces the bundles the lithe kernel
already ships, and the terminal output got a zero-dependency face-lift.

- **`--code`** — grant the agent `run_code` / `run_file` (lithe's sandbox
  bundle): Python execution under bubblewrap when available, passthrough
  otherwise; `lithe doctor` reports which backend is active.
- **`--skills DIR`** — a markdown skill library (`load_skill` tool).
  Defaults to `$LITHE_HOME/skills` when that directory exists;
  `--skills ""` disables.
- **`--mcp SPEC`** — external MCP servers (stdio or streamable-http),
  attached at run/chat time with graceful per-server degradation. SPEC is a
  JSON array/object or `@file`; env `LITHE_MCP` holds the same.
- **`--vision`** — `image_info` plus `analyze_image` (one vision call on
  the main endpoint, memoized per file hash).
- **`lithe doctor`** — one-screen configuration and capability status:
  endpoint (masked key), store + run count, workspace, sandbox backend,
  skills, MCP servers, color mode.
- **colored, aligned output** — a zero-dependency ANSI renderer
  (`lithe_cli.ui`): tables for `tools`/`runs`, colored roles/status/
  sections in `log`, per-tool-call elapsed time, and a one-line wrap-up
  after every run (`── done · steps 2 · tokens 120 · cost 0.0123`).
  Color auto-detects TTY and honors `NO_COLOR`/`TERM=dumb`;
  `--color`/`--no-color` force it either way.
- **chat polish** — a banner (version, model, workspace), `/help`,
  `/tools`, `/new` (clear session history) slash commands; unknown
  commands get a hint instead of being sent to the model.
- **streaming fix** — `--stream` no longer echoes the full answer after
  its tokens were already streamed.
- **tests** — 20 offline end-to-end tests (was 10): new coverage for the
  capability flags, doctor, MCP spec parsing (flag + env, failure modes),
  the run footer, dynamic system prompt, and the UI's plain/color
  degradation.

## 0.1.0

Initial release: the command-line interface for the
[lithe](https://pypi.org/project/lithe/) agent kernel (`lithe>=0.9.5`).

- **`lithe run TASK`** — one-shot task against any OpenAI-compatible
  endpoint; `--stream` for token streaming, `-v` for usage/context events,
  `--max-steps` / `--context-window` / `--timeout` / `--attempts` for run
  policy.
- **`lithe chat`** — interactive session; each turn is its own stored run,
  replayed as history for the next, so context carries across turns.
- **`lithe tools`** — list the CLI's registered tools (workspace file tools,
  `apply_patch`, todos; `--download` adds `download_file`).
- **`lithe runs` / `lithe log RUN_ID`** — inspect the JSONL run store:
  messages, tool calls, and undo-able actions of any run.
- **`lithe undo RUN_ID`** — zero-config undo of a run's file/todo mutations
  via the bundled tools' reverters (a created file is deleted, an
  overwritten file restored).
- **endpoint config** — `LITHE_API_KEY` / `LITHE_BASE_URL` / `LITHE_MODEL`
  (flags override env); no default endpoint on purpose. `LITHE_HOME`
  (default `~/.lithe`) locates the store; workspace defaults to cwd.
- **convenience** — both `lithe` and `lithe-cli` console scripts, plus
  `python -m lithe_cli`; `lithe --version` reports lithe-cli and kernel
  versions together.
- **tests** — 10 offline end-to-end tests against a scripted LLM transport
  (after lithe's `minimal_host` example): tool listing, offline runs that
  write files, undo round-trips, runs/log rendering, env/flag config
  precedence, missing-endpoint refusal.
