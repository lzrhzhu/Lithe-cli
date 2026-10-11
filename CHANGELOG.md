# Changelog

## Unreleased

## 0.3.1 (2026-10-11)

Renumbered from the retired 0.3.0: that filename had previously been
used by a deleted upload, and PyPI never reuses them.

## 0.3.0 (2026-10-11)

(never published — burned by PyPI's file-name reuse rule; see 0.3.1)

- **fix: profile model is optional end-to-end** — a blank model in the
  F7 form (or `config`) clears the stored default instead of saving an
  empty string; switching to such a profile keeps the current model,
  and `/models` listings fill the choice later. A profile deleted
  after its session ran no longer crashes the resume path.
- **fix: F4 model picker keeps profile groups complete** — the ★常用 /
  最近 quick lanes and the per-profile groups are complementary views,
  not a partition: a cached model that also sits in a favorite or a
  session-recent stays listed in its group (dedup now applies only
  inside a lane and inside a group), so a group never collapses to
  just the current model.

A conversation-first chrome round: run state moves out of the sidebar
into the conversation itself — a live `thinking` lane with a spinner,
the turn's duration under its output — the footer status bar goes away
entirely, the sidebar separates from the transcript by background
color instead of a divider line, polish passes tune the prompt band,
the user-message block, Markdown paragraph spacing, the welcome logo
is regenerated to actually spell "Lithe", the whole palette moves to a
violet family, and the capability defaults flip to secure-by-default:
analysis bundles on, execution surfaces off.

- **capability defaults: analysis on, execution off** — `--vision`,
  `--document` and `--download` now default to ON (their bundles —
  image/document probe+analysis, SSRF-guarded download — are read/ingress
  surfaces), each gaining a `--no-vision` / `--no-document` /
  `--no-download` opt-out (BooleanOptionalAction, like subagents) plus
  the existing in-session `/set` toggles. `--code` and `--shell` stay
  opt-in: they execute arbitrary code and host commands. `lithe doctor`
  gains a capabilities line showing the four toggles at a glance; the
  subagent operator joins the default roster (it only carries
  `download_file` under the new defaults — still no new powers).
- **welcome logo spells "Lithe"** — the ASCII-art logo was a garbled
  non-word; regenerated with the ANSI Shadow font it now reads Lithe.

- **polish: violet palette** — the screen recolors from slate/blue to
  violet and related hues: deep violet page (`#14101f`) with a darker
  sidebar band, violet prompt band and user-message panel, lavender
  tool/bullet/code/link/heading colors, purple thinking lane and
  welcome accents. Success/error/warn stay green/red/amber (semantic).
- **polish: user messages are a background block, not a frame** —
  quoted input reads as a solid panel (`#272040`, near-white
  violet-tinted text) spanning the pane, one breathing line above each
  message, no borders anywhere (`_sync_feed` groups contiguous user
  rows so multi-line messages form one band).
- **polish: Markdown paragraphs butt together** — blank lines between
  paragraphs render as nothing at all (even a single blank row read as
  a wide gap on tall terminal rows); code fences keep their blank
  lines verbatim.
- **polish: the prompt band gets a solid background** — the editing
  area drops the muddy `$surface 60%` overlay for a crisp violet band
  (`#1c1631`); the dim model/reasoning info line rides its bottom.
- **no footer status bar; endpoint info moves into the editing area** —
  the slim status line below the prompt (`● 完成 · /help 查看命令` /
  `● 思考中 · 12.3s · Ctrl+C 取消`) is gone: its hints duplicated the
  sidebar's folded 按键 section and its run state duplicated the
  conversation's thinking lane. In its place the prompt band's bottom
  carries a dim info line — `glm-4.6 · zhipu · 推理 high` — the
  effective model with its profile plus the reasoning effort when set
  (`prompt_info_text` replaces `footer_text`; the spinner heartbeat
  now resyncs the whole thinking lane, so one-shot runs keep streaming
  digests into it too).

- **run state lives in the conversation, not the sidebar** — the
  sidebar's 运行 section is gone. While a turn runs, the conversation
  shows a `⠋ thinking · 12.3s` lane (a rotating braille spinner —
  `ThinkingRow` ticks at ~8 fps on its own timer so long silent model
  calls still look alive — plus the live elapsed); a click unfolds the
  turn's accumulated REASONING digests. When the turn ends the lane
  settles under the output: `▸ thought · 4.2s` when the model reasoned
  (click re-folds/unfolds), a dim `model · 4.2s` meta line otherwise —
  the current turn's duration always sits below its message. The next
  `run_start` resets the lane (collapsed, spinner again); a failed run
  that never sees `done` shows no stale duration (`run_start` clears
  `last_turn_duration`).
- **no sidebar divider; panes separate by background** — the sidebar
  drops its one-column `border-left` and sits on a darker background
  band (`#0b1220` over the pinned `#0f172a` page color), kilo-style:
  color contrast + whitespace, no line.
- **fix: `FormModal` was no longer imported** — the app used it for
  F7's endpoint form but the name had fallen out of `ttui.py`'s import
  block, so `n`/`e` in the endpoint picker crashed with `NameError`.

A Kilo-style chrome round on top of the F7 work: the boxes come off,
the header strip goes away, the sidebar learns to fold, the newline key
works in real terminals, and a second polish pass moves the key map off
the footer, spaces the transcript, and folds the thinking.

- **polish: key map off the footer into a folded sidebar section** — the
  footer shrank to a slim status line (`● 思考中 · 12.3s · Ctrl+C 取消`
  while running, `● 完成 · /help 查看命令` idle, status-colored). The
  F2–F7 / Enter / Ctrl+J / Esc map now lives in a new 按键 sidebar
  section, folded by default with `F2–F7 · /help` as its summary.
- **polish: thinking renders as numbered click-to-expand folds** —
  REASONING digests mount as `▸ 思考 #N` rows (purple heading, click
  toggles the body) instead of one flat truncated line; the digest cap
  rose to 800 chars since a fold costs one line until opened.
  (`FoldRow`/`FoldHead` join the widget set — note its refresh method is
  `_sync_view`, because `_render` is Widget's internal Visual producer
  and overriding it hands the renderer a None visual.)
- **polish: transcript paragraph spacing + taller prompt** — feed rows
  get one breathing line at class transitions (user → tool → answer →
  notices; lines inside a block stay dense), and the prompt box grows to
  a minimum of 3 lines (max 10).
- **fix: Ctrl+Enter 换行 did nothing in real terminals** — most
  terminals cannot send a distinguishable Ctrl+Enter: Windows ones
  deliver a plain CR (indistinguishable from Enter, so the line
  submitted), and the LF others send arrives in Textual as `ctrl+j`;
  only enhanced (kitty/CSI-u) reporting emits a real `ctrl+enter`.
  The prompt now binds all three — `Ctrl+J`, `Ctrl+Enter` and
  `Shift+Enter` — and the footer names the one that works everywhere:
  Ctrl+J.

- **borderless chrome** — the conversation pane, sidebar and prompt lose
  their rounded box borders; the sidebar keeps only a one-column dim
  divider, the prompt is a bare `❯` line, the footer is a single dim
  line with no background, and assistant text renders in the default
  foreground instead of green (user lines stay blue, tools cyan).
- **no header strip** — the top bar (version · session · profile ·
  workspace) is gone; that truth already lives in the sidebar's 会话 /
  模型 sections. `header_text` is removed with it.
- **collapsible sidebar sections** — 会话 / 模型 / 运行 / 工具 / 待办 /
  用量 each render under a `▾`/`▸` heading; clicking the heading folds
  or unfolds the body. A folded section keeps a one-line summary in the
  heading (session id, model, status, todo count, and for 用量:
  输入/输出/费用/上下文百分比), so folding costs no information at a
  glance. 用量 starts folded — the verbose seven-line breakdown is one
  click away. `sidebar_markup` gains a `collapsed` set;
  `sidebar_sections` / `section_header` are the new pure builders.
- **fix: `e` 编辑 / `p` 改 provider did nothing** — the endpoint picker
  advertised both letters, but `PickerModal` carried no key bindings for
  them, so the press fell through. The bindings now exist (gated by each
  picker's `letter_actions`, inert elsewhere) and the edit form opens
  pre-filled for the focused profile.
- **`d` 删除档案** — a confirmation modal (y/n, its own title via
  `ConfirmModal`'s new `title` parameter), then `workbench.delete_profile`:
  favorites die with the profile; deleting the *current* one falls back
  to the store's next active profile (endpoint truth re-adopted), and
  when none remain the session keeps its endpoint fields env-style. The
  picker reopens with the refreshed list either way.
- **model is optional in the F7 form** — a blank value stores no default
  and explicitly clears one the profile had (`upsert` no longer keeps
  the old model when the form submits an empty one); validation now
  requires only 档案名 / base_url / API key. A bare profile prints the
  `F4 或 /models 拉取列表后选择` hint on save — the `/models` listing
  fills the choice, so the single-model field was never a real
  requirement. `set_profile`'s message says `未设置模型` instead of
  `None` for model-less profiles.
- **fix: resuming a session whose profile was deleted crashed** —
  `sync_session_pin` called `endpoint(name)` on a name the store no
  longer knew and let the SystemExit escape; deletion is now a UI
  action, so the miss degrades to "keep the current endpoint".

## 0.2.1 (2026-10-10)

The minor bump marks the TUI becoming a complete endpoint console: the
F7 round (below) plus corrected manual version references.

Why not 0.2.0: PyPI permanently retires the numbers of deleted
releases, and this package's pre-renumber line had already published
(and later deleted) 0.2.0 — that upload was rejected with
file-name-reuse, so the round lands on 0.2.1.

Endpoint management moves into the TUI, and the manual's version
references are corrected — the whole 0.9.x line was retired from PyPI
when the published line renumbered to 0.1.0, so the READMEs were
pointing at version numbers that no longer exist anywhere.

- **F7 端点管理** — a profile picker listing every saved endpoint with
  its `provider · transport` dialect, base_url and masked key. Enter
  switches (full endpoint truth, next turn); `n` opens an in-TUI form
  (档案名 → provider type cycled with Enter, base_url re-defaulting to
  each preset's official URL — overridable for custom routers →
  star-echoed API key → model); `e` edits the focused profile; `p`
  swaps a profile's provider type in two keystrokes. A newly created
  profile is switched to on save; edits to the current profile re-adopt
  its fields, so provider/base_url changes apply to the next turn.
- **`FormModal`** — a reusable labeled-field modal behind the form:
  Enter advances (last field submits), choice rows cycle, blank
  required fields are named in the hint instead of silently blocking.
  Enter is dispatched by the form itself — Button's two-phase key
  activation cycles erratically across Textual versions.
- **sidebar shows the endpoint truth** — the 模型 section gains the
  `provider · transport` dialect line (with the F7 hint) and the
  sampling knobs (temperature / output cap, `/set`-adjustable); the
  footer names F7 alongside F4–F6.
- **fix: preset-only profiles kept a blank base_url on in-session
  switches** — `_apply_endpoint` now fills the preset's official URL
  when the stored profile carries none, matching `load_config`'s
  layering (switching to a provider-only profile used to blank the
  endpoint for the rest of the session).

## 0.1.12 (2026-10-10)

The wizard catches up with providers. `lithe config` was a flat-triple
interview from before presets existed — profiles gained `provider`
fields that only hand-editing could set, and the changelog even claimed
a `--provider` flag that was never wired up.

- **provider-first wizard** — `lithe config` now asks profile name →
  provider type (Tab-completed across the seven presets, `none` keeps a
  hand-written endpoint; the preset's notes print under the prompt) →
  base_url (defaults to the preset's official URL — override it to point
  a provider's format at your own router) → API key → model, and saves
  the provider field with the profile. Re-running edits the named
  profile (name defaults to the active one); picking `none` on a profile
  that had a provider explicitly demotes it to hand-written.
- **protocol-aware probe** — the wizard's optional connectivity check
  builds its request via the new `profiles.models_request`: a
  messages-transport provider (anthropic) probes with `x-api-key` +
  `anthropic-version` (and `limit=1000`) instead of a Bearer header
  that would 401 against the real endpoint.
- **`--provider` flag** — `lithe run/chat/... --provider NAME` pins the
  preset for one invocation, flag > `LITHE_PROVIDER` > profile field,
  matching the other endpoint pins (and 0.1.11's changelog claim);
  `lithe config --provider NAME` sets the active profile's preset
  non-interactively (`none`/`off` clears). The wizard's first-run result
  now carries the provider too, so a wizard-configured anthropic profile
  takes effect in the same process instead of falling back to
  chat-completions until the next invocation re-read the profile.

## 0.1.11 (2026-10-10)

The multi-provider round, riding the kernel's new Messages transport
(lithe 0.1.9): profiles become first-class provider endpoints, models get
collision-proof qualified names, and a favorites lane makes switching
between a handful of daily drivers one keystroke.

- **qualified model refs `profile:model`** — the separator is `:` because
  model ids themselves contain `/` (OpenRouter's `vendor/model`) while
  profile names cannot contain either. `/model anthropic:claude-sonnet-4`
  switches profile *and* model in one step (profile fields, provider
  preset and all); an unknown prefix falls through as a bare model name
  so colon-y custom ids keep working.
- **profile switching is now endpoint truth** — `/profile` and session
  resume carry every profile field (provider, document_format,
  reasoning_effort, extra_body, default_headers, pricing, sampling), not
  just the credential triple. Previously an in-session switch to an
  anthropic profile kept the old vendor's preset layered over the new
  base_url — the wrong transport talking to the wrong dialect.
  `LITHE_PROVIDER` / `--provider` now pin the provider against in-session
  switching, like the other endpoint env pins.
- **`/models` is protocol-aware and can fan out** — listing uses the
  profile's provider preset to pick its auth: `messages` endpoints
  (anthropic) fetch with `x-api-key` + `anthropic-version` and a
  `limit=1000` page (Bearer + no limit elsewhere). `/models all` fetches
  every saved profile in parallel, caches each, and reports qualified
  names — same-name models across providers stop colliding.
- **`/model` listing is the same across frontends** — the plain REPL now
  matches the TUI picker: ★常用 section first, then 最近 (derived from
  session meta, zero sidecar state), then one group per profile with
  `provider · protocol` headers. Numbered picks follow the visible order
  and switch profiles as needed; `/profile` listings show the same
  dialect labels (inferred from base_url when the profile has no provider
  field — display only, never written back).
- **favorites (`/fav`)** — qualified refs stored in the config file's
  top-level `favorites` array; `/fav 档案:模型` toggles, the F4 picker's
  `a` toggles the focused row, `f` collapses the (possibly huge) grouped
  list to the favorites lane. Deleting a profile prunes its favorites;
  malformed refs are rejected, not saved.

## 0.1.10 (2026-10-06)

Multiline prompt input plus the environment-facts round, from a usage
audit of a real delegation session: the subagents' first ~15
`run_command` calls included ~6 pure path-assumption failures (git at a
workspace root that is not a repo, CI snippets with project-relative
paths, a husk `.venv` guessed usable, `.\`-less invocations,
ANSI-decoded UTF-8). The tools were blameless — the models simply had
no platform/workspace briefing and `run_command` had no
working-directory parameter. This release wires both, matching how
high-success-rate agents solve it (one tool, workdir, containment,
per-platform prompt facts).

- **the prompt area is multiline** — `HistoryInput` is now a
  `TextArea`: Enter sends, Ctrl+J / Ctrl+Enter insert a newline, history recall
  stays on ↑/↓ while the cursor is on the first/last line, and command
  completion hides for multiline drafts. Pasting multi-line text no
  longer drops everything after the first line.
- **`run_command` gains `cwd`** — commands target sub-projects via the
  new kernel parameter instead of stitching `Set-Location`/`cd` prefixes
  discovered by trial and error. The CLI passes its approver into
  `register_command_tools`, so a cwd outside the workspace gets the same
  human-confirmation channel as destructive commands (requires
  lithe >= 0.1.7).
- **environment facts in the system prompt** — `environment_facts()`
  appends host/shell identity and the workspace layout (top-level
  directories; which of them are independent git repos; whether the root
  itself is one) to every prompt. The shell line names the actual
  Windows shell and its chaining idiom; the layout line kills the
  nested-repo class of first-hour failures.
- **subagents see the same facts** — delegation prompts are roster spec
  prompts, not the orchestrator's system prompt; the engine now gets a
  `prompt_builder` that prepends the same environment facts, so the
  operator stops re-learning the workspace by failed guesses.

## 0.1.9 (2026-10-06)

Subagent cards move into the conversation: they now mount inline where the
delegation began (a `subagent:<instance>` marker row in the feed), scroll
with the transcript instead of squatting in a fixed region below it, and a
collapsed card is exactly one line — status glyph, identity, task truncated
to the pane width with an ellipsis — behind a slim left rail. The separate
`#subagents` scroll region is gone.

- **inline cards** — live `subagent_start` drops the marker at the moment
  of delegation; history rebuilds (`transcript_feed_lines`) emit one marker
  per instance at its first stored row, so a resumed session shows each
  card where the subtask actually ran. `/copy` never sees the markers.
- **compact collapsed card** — the heading is a 1-row `text-align: left`
  button (no borders/padding) whose label is verbatim `Content` (model task
  text with `[/]` can no longer raise `MarkupError`), truncated to the
  heading's own measured width.
- **no duplicated transcript after a turn** — `session_idle` no longer
  reloads the store feed on top of the live-mounted rows (every finished
  turn used to appear twice); the durable rebuild now happens on returning
  to a session (`_activate`), which also picks up turns that finished while
  another session was active.

## 0.1.8 (2026-10-06)

Fix a TUI crash when parallel subagents start back to back: the second
`subagent_start` synced the first card before its `compose()` children were
in the DOM, and `set_data`'s `query_one(".subagent-heading")` raised
`NoMatches`. `set_data` now stores the block and returns in that window;
`on_mount` renders the card once it is live.

- **hardened `SubagentCard.set_data`** — tolerate a mounted-but-not-yet-
  composed card (the window `_sync_feed`'s deferred first paint was written
  for, but sibling events could re-enter early); no behavior change once the
  card is live.

## 0.1.7 (2026-10-06)

- Add expandable, searchable subagent transcript cards to the TUI and restore subagent activity, conversation history, and usage totals when reopening a session.
- Require lithe >= 0.1.5 for persisted subagent lifecycle records.


## 0.1.6 (2026-10-06)

The same-agent-parallelism round, riding lithe 0.1.4: a session fanned three
read-only review tasks to one `researcher` and they ran serially — the
parallel delegation tool was only parallel for distinct agents.

- **live labels distinguish parallel instances** — subagent heartbeats,
  start/end records and TUI feed lines show the delegation instance suffix
  (``检索员·a3f2``), so several concurrent delegations of the same agent
  (now possible, kernel >= 0.1.4) stay tellable apart in the terminal.
- requires lithe >= 0.1.4.

## 0.1.5 (2026-10-06)

The transient-failure round, from the same session that exposed lithe
0.1.3's diagnosability gap: a momentary upstream error ended a whole
`delegate_parallel` turn because the CLI's retry policy was
`attempts=2` with zero backoff — the automatic retry fired immediately and
lost to the same throttle that had just refused the first request, twice in
a row (delegation, then the orchestrator's own next call).

- **real retry backoff by default** — new `sleep_429` (2.0s) / `sleep_err`
  (1.0s) bases between retryable failures, jittered and scaled by attempt
  (a gateway's `Retry-After` still wins); settable via `--sleep-429` /
  `--sleep-err`, the saved profile, or `/set`. An immediate duplicate
  request stays available with an explicit `0`.
- **`lithe log` shows why a run failed** — failed runs carry the kernel's
  error diagnostic on their stored final row (lithe ≥ 0.1.3); `lithe log`
  prints it as a red `error:` line ahead of `final:`, so a post-mortem no
  longer needs the process's stderr.

## 0.1.4 (2026-10-05)

The stability round: a crash in the destructive-command approval dialog,
task lists that leaked across conversations, and a missing release gate
in CI/publish — plus the widget-module extraction those fixes rode in on.

- **`ConfirmModal` duplicate-id crash fixed** — the destructive
  `run_command` approval dialog rendered two `Static` widgets with the
  same id (`picker-hint`), so opening it raised `MountError: Tried to
  insert 2 widgets with the same ID` and killed the app. The command line
  now has its own `picker-command` id (highlighted red); a regression
  test mounts the modal and answers y/esc.
- **todos are scoped per conversation** — the task list store keyed only
  on user+workspace, so every session in one workspace shared a single
  list and new conversations inherited stale todos. `todo_store_path`
  now folds in the conversation id; the workbench passes it through on
  every turn, the TUI reloads the right list on new/switch, and `undo`
  resolves the owning run's conversation before undoing todo changes.
- **Textual widgets extracted to `lithe_cli.ttui_widgets`** —
  `HistoryInput`, `WbEvent`, `selected_text`, `ConversationPane`,
  `PickerModal`, `ConfirmModal` and `completion_suggestions` move out of
  `ttui.py` (which keeps re-exporting them; module `__getattr__` forwards
  any straggler attribute), shrinking the front-end module by a third.
- **release gate** — `scripts/check_release.py` builds isolated wheel +
  sdist, verifies PEP 625 artifact names and Name/Version metadata
  against the source `__version__` (line-ending agnostic — setuptools
  writes CRLF METADATA on Windows), runs `twine check --strict`, and can
  clean stale `build/` / `dist/` / `.egg-info` (`--clean-only`); CI and
  the publish workflow run it before upload.

## 0.1.3 (2026-10-05)

The copy round: mouse reporting means the terminal's own drag-select,
context menu and Ctrl+C never reach a full-screen app, so getting text
out of `lithe chat` meant quitting (or Shift+drag) first. The app now
brings its own copy paths, and the feed renders as data, not markup.

- **markup-safe feed** — conversation lines, the banner, the footer and
  the streaming line all render with `markup=False`: an answer or tool
  output containing `[x]`-style brackets renders verbatim instead of
  raising `MarkupError` inside layout (and `/copy` sends the clipboard
  exactly what was displayed).
- **in-app selection + right-click copy menu** — the conversation pane
  handles right-click (mouse reporting keeps the terminal's own menu
  unreachable in here) and offers the Textual text selection plus the
  scope copies; right-click elsewhere in the app opens the same menu.
- **`/copy [last|all|user|tools]`** — one command lifts the requested
  slice off the feed (streaming included); the plain REPL rebuilds the
  same rows from the session transcript so both front-ends copy
  identical text.
- **Ctrl+C over a selection copies it** — a native Ctrl+C reflex over a
  drag selection copies instead of cancelling the turn; press again with
  nothing selected for the documented cancel/exit behaviour.
- **layered clipboard delivery** — platform clipboard command → OSC 52
  escape (works over SSH) → file under `$LITHE_HOME`, with the winning
  channel reported in the feed.
- **`lithe sessions --export ID|标题`** — prints a conversation as
  plain text on stdout for piping (`lithe sessions --export 12 | pbcopy`).

## 0.1.2 (2026-10-05)

The subagent-visibility round: a delegation used to be a black box —
the call line said `delegate_parallel` and nothing else, the workers'
tool calls were invisible, and the footer ignored the delegation
footprint. All five display gaps are closed; no kernel changes.

- **live subagent progress** — `register_subagents` now wires the
  kernel's `on_subagent_event` hook: every worker's tool calls,
  results, errors and answers render live with its display name
  (`[检索员] ⚒ search_files · …`), in both the plain line mode and the
  Textual feed. Thin events' capped JSON args are parsed back for the
  label; per-worker `step` chatter is skipped.
- **`subagent_start` / `subagent_end` records** — the events the
  delegate tools already returned in `ToolResult.ui` are rendered
  instead of dropped: after a delegation, the feed shows what each
  agent was tasked with (`▸ 检索员：…`) and how it ended
  (`▪ 检索员 · 完成 · 5 步 · 2 处改动`, status localized).
- **delegation call labels** — `tool_call_label` understands the
  delegation argument shapes: `delegate · researcher：任务摘要` and
  `delegate_parallel · researcher、coder（2 项）` instead of a bare
  tool name.
- **footer delegation footprint** — the run wrap-up appends
  `delegations N · sub-cost X.XXXX` from the kernel's done-event
  breakdown (subagent spend was already folded into the totals; now it
  is also visible as its own number).
- **footer context watermark** — the done footer carries
  `ctx P%` (or `ctx Ntk` without a declared window) by default; the
  TUI sidebar gauge is unchanged.

## 0.1.1 (2026-10-04)

Clean renumber: every earlier PyPI release was deleted at the
maintainer's request; PyPI permanently retires deleted version numbers
and filenames — 0.1.0 of this package was among them — so 0.1.1 is the
lowest available version. This release contains all development to date
(the rounds previously numbered 0.9.3/0.9.4/0.9.5). Requires lithe ≥ 0.1.0.

- **`--subagents` now defaults to on** — the default roster only carries
  already-registered tools (researcher filters to the read tools, coder to
  the file tools, operator exists only with `--shell`/`--download`), so
  delegation grants no power the run lacks: it is a token-spend selector,
  not a security gate, and no longer demands an opt-in. Workers inherit
  the same sandbox, command guard, budgets (parallel siblings share one
  live cost ceiling) and cancellation as before. `--no-subagents` (or
  `/set subagents off`) opts out; the enabling `/set` warning about token
  amplification is unchanged.
- **`SYSTEM_PROMPT_BASE` restructured** — the base prompt is now
  line-per-topic (role+workspace / paths / file editing / failure
  recovery / todos / output) instead of one run-on paragraph, and states
  two ground rules the model previously had to learn by failure: file
  paths are workspace-relative (no absolute paths, no `../` outside the
  root), and a failed tool call means read the returned error, fix the
  arguments and retry — not resend verbatim. The locked guidance
  (prefer `edit_file`/`apply_patch` for local edits, todo-planning
  limits, concise Chinese reporting) is unchanged.
- **capability extras get their own lines** — `build_system_prompt`
  concatenated enabled-capability sentences straight onto the base
  prompt's last sentence (`…汇报结果。可以用 run_code…`); each
  capability is now a separate line, so the model reads one rule per
  line and the "one line per capability" docstring is finally true.

## 0.9.3 (2026-10-04)

The command-guard round: destructive shell commands now need a human
yes. Requires lithe ≥ 0.9.24 (`classify_command` / `make_command_guard`).

- **dangerous-command guard on `run_command`** — catastrophic commands
  (`rm -rf /`, `mkfs`, `format c:` …) are refused outright;
  destructive-but-scoped ones (`rm -r <path>`, `git push --force`,
  `sudo`, `chmod -R` …) ask first, each frontend through its own
  channel: the Textual TUI shows a y/n modal (n/Esc or an abandoned
  modal refuses; 300 s cap so a forgotten dialog can't wedge the turn),
  the plain REPL asks y/N on stdin when it is a TTY, and non-TTY runs —
  with nobody to ask — deny with guidance, so piped agents never run
  destructive commands silently.
- **publish workflow** — `.github/workflows/publish.yml` (PyPI trusted
  publishing on release, mirroring the kernel repo's flow).

## 0.9.2 (2026-10-03)

- **live turn timer** — while a turn runs, the Textual footer and sidebar
  now tick in real time (`● 思考中 · 12.3s`, `已进行 12.3s`): a 0.5 s
  heartbeat repaints the chrome, so the clock advances even during long
  model calls that emit no events. On turn end the running line is
  replaced by the settled 本轮耗时. The plain (non-TTY) REPL keeps its
  turn-end footer only — piped output must stay free of rewrite escapes.

## 0.9.1 (2026-10-03)

The readability round: todos become visible where the work happens, turn
duration surfaces everywhere a turn ends, and failed tool calls explain
themselves. Requires lithe ≥ 0.9.22 (host `duration_s`).

- **todos are shown, not just counted** — `update_todos` emits a
  `todo_change` event, but the plain REPL dropped it silently and the
  Textual feed only printed "任务清单已更新（N 项）": the list itself lived
  solely in the sidebar (hideable, capped at the last 5 items). Both
  frontends now render the full checklist into the conversation —
  `▤ 任务清单（2/5 完成）` with one `[ ]/[~]/[x]/[-]` line per item — so
  the plan is visible exactly where the agent is working on it.
- **turn duration** — each run's wall-clock length (`duration_s` from the
  kernel's DONE event) now appears: in the plain REPL footer
  (`── done · steps 3 · tokens 360 · cost 0.0021 · 12.3s`), in the TUI
  status line and as a 本轮耗时 sidebar line, as a `time` column in
  `lithe runs` (created_at → finished_at, `—` for legacy rows) and as
  `耗时=` in `lithe log`.
- **failed tool results explain themselves** — a failed call's terminal
  line used to show only the terse summary ("参数错误"); the diagnostic
  from the event's `error` field is now appended (deduplicated when the
  summary is a prefix of the error), in both the plain REPL and the TUI.

## 0.9.0 (2026-10-03)

The budgets-and-steering release: run budgets arrive as first-class CLI
knobs, mid-turn input becomes real steering, and profiles round-trip the
sampling/vendor/pricing fields the kernel already understood. Requires
lithe ≥ 0.9.21 (steering persistence fix).

- **run budgets** — `--max-cost USD` / `--max-tokens N` (cumulative) end
  the turn with status `budget_exceeded` once crossed; `/set max-cost` /
  `/set max-tokens` adjust in-session (`off` clears), and the sidebar
  shows 成本预算 / token 预算 progress lines. On endpoints that report no
  `usage.cost`, a profile `pricing` table (`{"prompt": 3,
  "completion": 15}` per 1M tokens, `cached_prompt` optional) computes
  call cost — the thing that makes `--max-cost` enforceable there.
- **steering** — plain text submitted while a turn runs is queued into
  the kernel's steering inbox and injected as a user message at the next
  step boundary: the model incorporates it on its next call, the line is
  rendered in the transcript (`user_injected`) and persistently
  recorded. Texts that cannot be injected before the turn ends (e.g. the
  model call never yields a step boundary) are reported as dropped
  instead of vanishing. The plain REPL cannot steer (its input line is
  unavailable while a turn runs).
- **profile field passthrough** — hand-written profile fields now
  survive loading and reach `LLMConfig`: `pricing` (above),
  `temperature` / `max_tokens` (sampling; `--temperature` /
  `--max-output-tokens` flags win over the profile), and
  `extra_body` / `default_headers` (vendor request fields/headers for
  gateways no preset covers — a provider preset's dict fields merge
  key-wise underneath). `config --show` displays them all, and
  `/set temperature` / `/set max-output-tokens` adjust in-session
  (`off` = endpoint default).

## 0.8.9 (2026-10-03)

Subagent delegation arrives as an opt-in capability — the kernel's
delegation bundle wired onto the CLI's tool system.

- **`--subagents`** — grants `delegate` / `delegate_parallel` over the
  CLI's default roster, whose tool lists filter against the actually
  registered tools so capability flags shape the workers automatically:
  `researcher` (read-only lookup; gains `image_info`/`analyze_image` with
  `--vision` and `document_info`/`analyze_document` with `--document`),
  `coder` (file edits via edit_file/apply_patch plus `run_code`/`run_file`
  with `--code`), and `operator` (`run_command`/`download_file`, present
  only with `--shell`/`--download`). Children inherit the endpoint, retry
  policy and budgets from the host; their turns are recorded under the
  parent run tagged with the subagent id, and parallel workers share one
  live cost ceiling (kernel ≥ 0.9.20).
- **`/set subagents`（F5）** — in-session toggle with the next-turn
  semantics of every tool-affecting knob; enabling restates the
  token-spend warning, `/tools` lists the delegation pair, and the system
  prompt gains a delegation line when on.
- **`doctor`** reports the delegation state alongside the other
  capabilities.

## 0.8.8 (2026-10-03)

A one-fix follow-up to 0.8.7's scroll behavior, caught by CI timing on
a slow runner.

- **follow anchor lands after layout** — `_sync_feed`'s bottom-follow
  called `scroll_end` immediately after mounting new lines, when the
  pane's extent only grows on the next layout refresh: the scroll
  anchored one refresh short of the bottom, and the following event's
  at-bottom check then mis-read "the reader scrolled up" and stopped
  following for good. The follow now also anchors via
  `call_after_refresh`, so it lands on the true post-layout bottom. The
  regression test settles over several pauses instead of asserting
  after one (robust on slow CI).

## 0.8.7 (2026-10-03)

The interaction-safety round: one-shot runs become cancellable, profile
re-config stops destroying fields it doesn't know, the config file
survives a crash mid-write, mid-turn input is never silently dropped,
`#N` suggestions actually route, and the transcript follows your scroll
instead of fighting it.

- **Ctrl+C cancels a one-shot `lithe run`** — run mode drives no
  workbench turn, so `wb.cancel()` had nothing to stop and repeated
  Ctrl+C just reprinted （正在取消本轮…）. The stop now falls back to the
  run's own stop handle (the README promise), a second Ctrl+C during the
  same turn force-exits, and the cancel-asked state resets when the turn
  ends (both chat and run modes).
- **profile fields survive a re-config** — `upsert` previously rebuilt
  the endpoint from the wizard's triple only, silently erasing
  `provider`, `reasoning_effort`, `document_format` and a hand-tuned
  `context_window` when `lithe config` or the wizard re-saved an
  existing profile. The stored endpoint now forms the base and the
  caller's triple overwrites; `cached_models` still drops when the
  base_url changes.
- **atomic config saves** — `config.json` (which holds every saved API
  key) is now written via temp-file + `os.replace` with the previous
  version kept as `config.json.bak` (owner-only perms on both); a torn or
  corrupt main file falls back to the `.bak` at load time, so a crash
  mid-write costs the edit, not every profile.
- **`/resume #N` routes** — the TUI's completion suggests session ids as
  `#12`; `SessionManager.resolve` now accepts that shape instead of
  failing with 找不到会话 #12.
- **mid-turn input is not dropped** — plain text submitted while a turn
  runs goes back into the input box with a warning (resubmit when the
  turn ends) instead of vanishing; slash commands dispatch mid-turn as
  they always did in the plain REPL (`/model`, `/resume`, …).
- **the transcript follows only when you're at the bottom** — new feed
  lines no longer yank the pane back down on every event while you
  scrolled up to re-read earlier output; following resumes once you
  return to the bottom. The dead prompt_toolkit-era scroll bookkeeping in
  `TuiState` was removed with it.

## 0.8.6 (2026-10-03)

Document understanding arrives as an opt-in capability with the wire
dialect layered under the provider system, and the legacy prompt_toolkit
full-screen front-end is removed for good.

- **the legacy `--ui prompt` screen is gone** — the prompt_toolkit
  full-screen application (`run_screen` / `build_app`, its layout,
  lexers, pickers, mouse selection and OSC 52 copy) was deleted
  wholesale: `lithe chat` / `lithe run` on an interactive terminal now
  always launch the Textual front-end, non-TTY keeps the plain per-line
  REPL, and there is no flag or env var that can reach the old screen
  (`--ui` and `LITHE_UI` are no longer accepted). `tui.py` shrinks to
  what the Textual UI builds on — `TuiState` (kernel-event folding),
  `transcript_feed_lines`, `screen_supported`, the `/keys` help text.
  prompt_toolkit remains a dependency for what it still does: the setup
  wizard and the plain-REPL input line (CJK-safe editing, history).
- **`--document`** — grants `document_info` + `analyze_document`
  (PDF/DOCX/XLSX/PPTX reading on the main endpoint), alongside
  `--vision`. `/set document` (and the F5 picker) toggles it in-session.
- **`--document-format`** — the dialect selector
  (`inline-file` / `files-api` / `none`), resolved
  flag > profile `document_format` field > provider preset default
  (openrouter → `inline-file`, openai → `files-api`,
  deepseek/zai/qwen/moonshot → `none`). Without a resolved dialect the
  deterministic `document_info` probe still registers; an unknown value
  fails loudly at startup. `config --show` displays the saved field, and
  profiles round-trip it (hand-written config.json entries survive
  saves).

## 0.8.5 (2026-10-03)

The picker-polish release: F5 gains an in-place toggle so several
knobs flip in one visit, `/models` refreshes the session's model list
immediately, and the prompt_toolkit frontend's Enter-toggle is
repaired.

- **F5: 空格 in-place toggle** — Enter on a boolean row toggles *and
  dismisses* the settings picker, so flipping several knobs meant
  reopening F5 per knob. Space now flips the focused row in place —
  the modal stays open and the row's label re-renders (`○ off` →
  `● on`) — letting one visit set shell/code/vision/download/…
  together. Enter keeps its 切换并关闭 behavior; hint rows ignore
  space. Both frontends (Textual picker via a new `live_toggle`
  callback on `PickerModal`, prompt_toolkit overlay via a `space`
  binding), with updated hint and `/set` help texts.
- **`/models` refreshes immediately** — the fetch rewrites
  `cached_models` in the profile, but the in-session F4 list and
  `/model` completion stayed stale until an unrelated event happened
  to refresh the chrome. `command_output` events (what `/models` and
  F4-`r` report through) now trigger the metadata refresh in both
  frontends.
- **`--ui prompt` Enter-toggle repaired** — tui.py passed the literal
  token `"toggle"` to `set_setting` (the same 0.8.4 Textual bug),
  so every Enter on a settings row errored with 是开关：on / off;
  now it makes the same bare (toggle) call `/set` makes.

## 0.8.4 (2026-10-02)

The picker-repair release: the 0.8.2/0.8.3 pickers shipped with three
defects that made F5 partially unusable, F6 a hard crash, and every
Textual exit noisy. All found by headless F5/F6 regression tests.

- **F6 crash** — the reasoning picker read the module constant as a
  `Workbench` attribute (`wb.REASONING_LEVELS` → `AttributeError`) in
  both frontends; Textual's traceback then masked the exit bug below.
  Fixed by importing `REASONING_LEVELS` from `lithe_cli.workbench`
  where the picker builds its rows.
- **F5 crashes + dead toggles** — the settings picker formatted the
  `reasoning-effort` row's `None` value with `{value:<6}`
  (`TypeError`), so the modal never opened; and `Enter` on a boolean
  row passed the literal token `"toggle"` to `set_setting`, which its
  on/off parser rejects — every toggle errored with 是开关：on / off.
  `None` now renders as `off`, and `Enter` calls `set_setting` bare
  (empty value = toggle, same as `/set shell` without an argument).
- **invisible confirmations** — the F5/F6 pickers appended their
  应用/切换 messages to the feed but never called `_sync_feed()`, so
  nothing appeared in the conversation pane until the next event.
- **clean exit** — `main` wrapped the *sync* `run_textual_screen`
  (which drives its own `asyncio.run`) in another `asyncio.run`,
  raising `ValueError: a coroutine was expected, got 0` whenever the
  Textual app exited — crashing after every F6-style in-app error.
  Now called directly.

## 0.8.3 (2026-10-02)

The reasoning-effort release (paired with lithe 0.9.18): a first-class
推理强度 knob, switchable mid-session with the same picker UX as `/model`.

- **`/reasoning` + F6 picker** — `/reasoning` lists the levels
  (off / minimal / low / medium / high, ● marking the current one) and
  opens the modal picker both frontends share; `Enter` applies, `s` saves
  as the profile's default. Typed forms work too: `/reasoning high`,
  `/reasoning 3` (index), `/reasoning none --save` (values outside the
  offered set pass through verbatim — which levels a model accepts is the
  endpoint's call, and a rejected value surfaces through the kernel's 400
  diagnostics). `off` clears the knob (nothing is sent). Semantics match
  `/model` and `/set`: applies to the next turn, session-scoped unless
  `--save`.
- **profile field `reasoning_effort`** — persisted via `--save` /
  `set_reasoning_effort`; loading normalizes "off" to unset. The
  `--reasoning-effort LEVEL` flag sets it per invocation (flag > profile).
- **visibility** — the sidebar's 模型 section shows `推理 high · F6 切换`
  when set (both frontends); `/set`'s listing includes the knob with a
  pointer to `/reasoning`; slash completion covers the levels; footers
  mention F6.
- Depends on lithe 0.9.18: the kernel maps the knob per protocol
  (`reasoning_effort` on chat, `reasoning.effort` on Responses,
  deep-merged with `extra_body["reasoning"]` siblings like OpenRouter's
  `max_tokens`/`exclude`).

## 0.8.2 (2026-10-02)

The in-session settings release: the per-turn capability knobs no longer
need a restart, and they get the same picker UX as `/model`.

- **`/set` + F5 settings picker** — `/set` lists the session-adjustable
  knobs and opens the same modal picker `/model` uses (F5 opens it
  directly in both frontends): `Enter` toggles a boolean row, numeric
  items render as hints with their `/set 名称 值` usage. Typed forms set
  anything: `/set shell on`, `/set code off`, `/set vision` (bare =
  toggle), `/set max-steps 50`, `/set timeout 240`, `/set attempts 3`,
  `/set stream on`, `/set verbose on`. Keys accept 序号 and underscore
  aliases (`/set max_steps 50`); bad values get actionable errors.
  Semantics match `/model`: edits hit the shared `cfg` and apply to the
  **next turn** — turns already running keep their tool set; nothing is
  persisted to the profile (session-scoped, like `/model` without
  `--save`). Tool-affecting flips (`shell`/`code`/`vision`/`download`)
  invalidate the `/tools` cache so the listing re-derives; turning
  `shell` on restates its trust warning (current-user permissions, not
  sandboxed, not undoable). Slash completion covers `/set` keys and
  on/off. Deliberately not settable in-session: `--workspace`/`--store`/
  `--user` (session identity), `--ui`/`--mcp` (process-scoped).

## 0.8.1 (2026-10-02)

The provider-preset release (paired with lithe 0.9.17): a profile can name
a vendor instead of hand-writing its endpoint shape, and lithe's new
400 diagnostics surface automatically.

- **profile field `provider`** — a profile may set `"provider": "zai"`
  instead of (or alongside) `base_url`: the vendor preset from
  `lithe.bundles.providers` fills `base_url` when nothing explicit set it
  and contributes `LLMConfig` defaults (transport, `extra_body`,
  `default_headers`) under the profile's explicit values (dict fields merge
  per key). `LITHE_PROVIDER` overrides the profile field. `config --list`
  marks each profile with `[provider]`; `config --show` prints it. An
  unknown provider fails loudly with the available presets — same policy
  as an unknown `--profile`. Hand-editing config.json or `upsert(provider=
  ...)` sets it; the wizard is unchanged this release.
- **dependency floor `lithe>=0.9.17`** — the provider integration plus the
  kernel's extra_body 400 diagnostics (a rejected vendor field now names
  itself in the run's error event) come from that kernel.

## 0.8.0 (2026-10-02)

The Textual release: the full-screen interface moves onto the Textual
framework and becomes the default; the previous prompt_toolkit screen
stays available as `--ui prompt` / `LITHE_UI=prompt`.

- **Textual front-end by default** — layout, modals, scrolling and repaint
  are framework-owned (the F2 sidebar toggle that motivated the switch no
  longer goes through a hand-rolled diff over a swapped container tree).
  Same Workbench backend, same five-section sidebar (会话 / 模型 / 运行 /
  工具+待办 / 用量), same F3 session picker and F4 model picker (Enter /
  n / d / r / s keys), busy badges, store-backed rebuild on switch-back.
  `textual` is now a core dependency.
- **inline command completion** — typing `/` shows matching commands above
  the prompt; `Tab` accepts the first suggestion; `/model`, `/profile`
  and `/resume` complete their arguments (cached models, saved profiles,
  session ids).
- **persistent input history** — ↑/↓ recall previous lines across turns
  and processes, sharing the same FileHistory store as the plain REPL.
- **legacy screen kept** — `--ui prompt` restores the prompt_toolkit
  interface unchanged (including its in-app mouse selection + OSC 52
  copy, which the Textual screen does not have; use the terminal's native
  Shift+drag selection there).
- **interaction tests on Windows** — the Textual screen is tested
  headlessly via `App.run_test()` pilots (turn execution, F2/F3/F4,
  history, completion), closing the gap where the old PTY tests skipped
  on Windows.

## 0.7.0 (2026-10-02)

The workbench release (paired with lithe 0.9.14): profiles, sessions,
and a full-screen interface you can steer without leaving it.

- **provider profiles** — `$LITHE_HOME/config.json` grows from one flat
  endpoint into named profiles (`{"version": 2, "active": …,
  "profiles": {…}}`). A legacy flat file keeps working untranslated
  until the next save. `lithe config --list / --use NAME / --model
  NAME` switch non-interactively; `--profile` / `LITHE_PROFILE` pick a
  profile per run; per-key precedence stays flag > env > profile, so CI
  pins keep winning. `lithe models` lists (and caches) the endpoint's
  `GET /models`.
- **persistent sessions** — every `lithe chat` conversation is stored
  as a kernel conversation: `chat --continue` resumes the latest,
  `chat --resume ID|标题前缀` a specific one, `lithe sessions` lists /
  renames / deletes (soft delete — runs stay for `log`/`undo`).
  Sessions remember their last profile/model and restore them on
  resume unless flag/env pinned. Cancelled turns replay cleanly (the
  kernel already reconciles orphan tool calls).
- **the workbench** — a new orchestration layer (`Workbench`) behind
  both frontends: one dispatcher for every slash command, per-session
  turn tasks (switching away does not cancel a running turn), and an
  event bus both the plain REPL and the TUI subscribe to.
- **TUI pickers + sections** — F3 opens the session picker (Enter
  switch, `n` new, `d` delete, `r` rename), F4 the model picker
  (grouped by profile, `s` save as profile default, `r` fetch list).
  The sidebar is now five fixed sections (会话 / 模型 / 运行 / 工具+待办
  / 用量), busy sessions carry a `●` badge, the header shows
  `profile · model · #会话 标题`, and `/` commands complete inline
  (including `/model <name>` and `/profile <name>` arguments).
- **new commands in chat** — `/model [名称|--save]`, `/models`,
  `/profile [名称]`, `/sessions`, `/resume [ID]`, `/rename 标题`,
  `/undo [run]` plus the existing `/new /tools /sidebar /help /exit`.
- **one-shot `lithe run` unchanged** — no session, no picker; the TUI
  for it is still one turn and `q` to leave.

## 0.6.5 (2026-10-01)

The Windows release (paired with lithe 0.9.12): piped and redirected
chat sessions survive on Windows.

- **piped chat no longer crashes on Windows** — prompt_toolkit's
  Windows console backend needs a real screen buffer and raises
  `NoConsoleScreenBufferError` when either stdout or stdin is a pipe or
  redirect (POSIX degrades to plain-text output there). The chat line
  now detects that case and falls back to the builtin `input()` with
  ANSI stripped, so `lithe chat` works through pipes, `tee` and CI
  instead of dying on the first prompt. Real terminals keep the full
  prompt_toolkit editor.

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
