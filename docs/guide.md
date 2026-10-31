# Kanbeat guide

Detailed behavior reference. For what Kanbeat is, installation and a first run, start with the [README](../README.md). Interactive terminal details live in [terminals.md](terminals.md); the product and architecture design is in the [standalone design specification](design/design.html).

Kanbeat is an early release. Everything below describes the implemented behavior; items marked as not validated have not been exercised against live vendor CLIs.

- [Principles](#principles)
- [Command line and TUI](#command-line-and-tui)
- [On-disk layout](#on-disk-layout)
- [Flow](#flow)
- [Live ticket updates and agent prompts](#live-ticket-updates-and-agent-prompts)
- [Native CLI lifecycle](#native-cli-lifecycle)
- [Interactive terminals and sessions](#interactive-terminals-and-sessions)
- [Configuration](#configuration)
- [Transition hooks](#transition-hooks)
- [Development](#development)
- [Migration](#migration)

## Principles

- One supervisor per project, any number of TUI clients. Closing TUI does **not** stop execution.
- `kanbeat` starts or joins the project's supervisor.
- `Play` resumes dispatch. `Stop` interrupts board-owned CLI processes and terminates them, saving native session IDs when the CLI reports them. The supervisor stays alive.
- Stop does not change ticket columns. Play attempts native resume for unfinished slots; the CLI may need to recheck which changes were already made.
- The supervisor starts **stopped** if the state directory is absent, or when recovering from supervisor restart.
- The numeric filename prefix is the unique stable ticket ID.
- Tickets are plain Markdown with **no frontmatter**, transcript or runtime metadata.
- Status = directory. ID = filename number. `archive/` is out of scope.
- Independent tickets run in parallel (up to `max_parallel`); a ticket blocked by an unfinished ticket waits in Todo. Each ticket has 0..N parallel reviewers with isolated contexts.
- Default harness unavailable: global Stop with notification. Missing assigned harness: use default, fresh session. Missing native session: fresh session in same role.
- No special handling for intentional deletion of active runtime files.
- Native CLI flags never override user permissions, sandbox or authentication settings.
- No worktree management, automatic git reset, git cleanup or diff ownership.

## Command line and TUI

```sh
kanbeat                      # start or join this project's supervisor and open the TUI
kanbeat list
kanbeat new "Implement auth" --column todo --description "Add token validation"
kanbeat move 001 backlog
kanbeat play
kanbeat stop
kanbeat status [--json]      # execution state and the tickets per column, with blockers
kanbeat events [--since N]   # ticket events after number N, then exit
kanbeat watch [--json]       # keep printing new ticket events until interrupted
kanbeat config get [key]     # settings; set, history and revert are described below
kanbeat --project /path/to/project   # select another project root
kanbeat --version
kanbeat shutdown             # exit the supervisor after cleaning up workers
```

The project root is the current working directory by default. `--project` is a top-level option and goes before the subcommand. `kanbeat run` is the explicit form of plain `kanbeat`. `kanbeat supervisor` runs a foreground supervisor for debugging; the normal CLI starts a detached supervisor automatically.

Open a second terminal in the **same** project and launch `kanbeat` again: it connects to the same supervisor. A different project directory starts an independent supervisor. `kanbeat new`, `move`, `play` and `stop` also start the supervisor when none is running.

### Running the board from the command line

A ticket's status is the folder it sits in, so an agent or a script can run the board with plain files and these commands; there is no separate API or MCP server. [`INSTRUCTIONS.md`](#on-disk-layout) in every project tells agents the same.

- `kanbeat status` prints the execution state and the tickets per column; `--json` gives the same as one JSON object (`execution` and `columns`, each ticket with `id`, `title` and the `blocked_by` ids that are not done yet).
- **Events.** Kanbeat records every ticket change it sees in the files as a numbered event: `created`, `moved`, `deleted`, and `execution` when the board goes from stopped to running or back. Changes made by hand and by Kanbeat look the same. The supervisor checks the files once a second, so changes that are made and undone inside one check are not recorded. The last 200 events are kept in `.kanbeat/.state/supervisor.json`, and numbers never repeat while that file exists.
- `kanbeat events [--since N] [--limit N] [--json]` prints the events after number N and exits. `kanbeat watch [--since N] [--json]` keeps printing new ones, once per second at most (`--interval`, at least 0.5 seconds), starts from now unless `--since` says otherwise, and `--json` adds the event number (`seq`) and a `time` to every line. If the supervisor goes away it prints `supervisor disconnected`, retries for about a minute and prints `supervisor connected` and the events it missed when it is back. An agent that follows the board can run it as a background process.
- `kanbeat config get [key]`, `config set <key> <value>`, `config history [--limit N]` and `config revert <id>` read and change `config.json` through the supervisor. A change applies at once and the TUI Settings page follows. The keys are `default_harness`, `max_parallel` (1 to 32), `global_cp`, `developer_prompt`, `reviewer_prompt`, `transition_hook.harness` and `transition_hook.prompt`. A long prompt can come from `--file PATH`, or from standard input with the value `-`. Do not edit `config.json` by hand while the supervisor runs: it reads the file once at start and the Settings page rewrites it from memory.
- **History.** Every change to the settings, from the TUI, the command line or a revert, is recorded with its old and new values (tickets are not tracked). The last 100 changes are kept in `.kanbeat/.state/config-history.json`, which is written only when a setting changes. A change made from anywhere but the Settings page (the command line, or any other client of the supervisor that does not say it is the Settings page) shows as a notice in the TUI without asking for approval and names the command that undoes it. Reverting restores the recorded old values as they were, even if they would not pass today's checks. Settings has a **History** section that lists the last 5 changes collapsed, unfolds one to show the old and new values, loads 20 more at a time up to 100, and has a Revert button per change. A revert is recorded too, so it can be undone as well.
- Never edit `.kanbeat/.state/`. It is the supervisor's private runtime state, held in memory and rewritten from there; a hand edit is lost or corrupts it.

`kanbeat shutdown` waits for cleanup without starting an absent supervisor. Add `--yes` to confirm stopping independent sessions. See [Releases](releases.md#update) before changing an installed binary.

TUI keys: `1` Sessions, `2` Board, `3` Archive, `4` Settings, `Ctrl+R` refresh, `Ctrl+Q` quit the TUI (the supervisor keeps running), `Ctrl+\` detach from an interactive terminal. Board, Sessions, Archive and Settings are also clickable. The minimum supported size is 80 × 24 characters; roughly 130 columns show all six fixed-width board lanes, and narrower terminals can scroll them horizontally. Dialogs and Settings scroll vertically in shorter terminals. Slot labels, controls and runtime badges have separate space, and invalid saved harness choices show a warning before Save.

Clicking the visible backdrop outside a dialog closes that dialog without saving. A confirmation is cancelled. Clicks inside controls and drags that start inside the dialog do not dismiss it; nested dialogs close one at a time. Full-screen views use their Close/Detach controls, and detaching a terminal keeps its process running.

Background snapshots update existing board cards and session rows in place. Runtime/notice changes preserve focus, scrolling and selection; moving or removing a ticket changes only its affected card.

## On-disk layout

```text
.kanbeat/
├── INSTRUCTIONS.md            # only read when explicitly asked
├── config.json                # project defaults, Git-trackable
├── ids.json                   # persistent ticket ID high-water mark, Git-trackable
├── backlog/
│   └── 001-future-search.md
├── todo/
│   ├── 002-authentication.md
│   └── 003-token-refresh.md
├── progress/
├── review/
├── done/
├── failed/
├── archive/
└── .state/                    # local runtime, do not commit, optional, recreatable
    ├── supervisor.json        # Play/Stop, slots, session IDs, review verdicts
    ├── supervisor.sock        # local Unix IPC socket
    ├── supervisor.lock
    ├── supervisor.log
    └── logs/                  # native CLI output, not task content
```

The file `002-authentication.md` contains only:

```md
# Implement authentication

Add token validation to the API.

## Acceptance criteria

- Validate access tokens.
- Handle expiration.
```

Creating `.kanbeat` automatically creates `INSTRUCTIONS.md` for a separately instructed agent to author tickets. It is **not** injected into coding agents automatically.

The number, e.g. `002`, is the ticket ID and must be unique across all column and archive directories. The slug is readable; changing status never changes the ID or the filename. Files not named `<id>-<slug>.md` are reported as invalid filenames. No metadata is added to Markdown by Kanbeat.

`ids.json` reserves monotonically increasing IDs before ticket creation. Keep it
with the board in Git: deleting archived tickets or disposable `.state` must not
reset ticket identity. Kanbeat records imported IDs before deleting archived
tickets too. Failed creates can leave gaps, which are intentional. External ticket
authors must still choose unused IDs; history deleted outside Kanbeat before the
ID ledger exists cannot be reconstructed automatically.

Tickets in a column are listed by numeric ID. What starts when is decided by dependencies (see below).

If `.state` is absent at startup, board status still comes from directories and execution starts **stopped**. Without assignments, configured defaults are used after the user presses Play. Deleting `.state` while the system is actively running is not a supported operation.

Kanbeat does not create a `.gitignore`. Add `.kanbeat/.state/` to your project's `.gitignore`; tickets, column directories, `INSTRUCTIONS.md`, `ids.json` and `config.json` are meant to be tracked.

## Flow

```text
Backlog --manual--> Todo --Play--> Progress --Developer turn done-->
Review/QA --all Reviewer slots approve--> Done
          \--any changes_requested--> Progress
```

Each normal developer turn is executed through the native CLI's structured non-interactive mode. A successful completed turn moves its ticket to Review/QA. Developer can write tests but should not execute them. Reviewers test, prefer headless where suitable, and return a **verdict** in their final response: `KANBEAT_VERDICT: approve` or `KANBEAT_VERDICT: changes_requested`. Reviewer outcomes are collected per slot. One rejection returns the task to Progress with short feedback; all approvals advance to Done. Zero reviewers automatically advance Review/QA to Done.

A Reviewer receives the **path to the current canonical ticket Markdown file**, its own role/global/slot CP and workflow instructions. The Reviewer reads the ticket directly from disk, independently inspects the working tree, and must not edit, rename or move the ticket. It is never automatically given the Developer transcript, terminal history, CP, or other Reviewers' transcripts. A replacement **within the same reviewer slot** can receive that same slot's bounded prior output as untrusted context when harness is changed. Different reviewer slots never exchange histories.

Several tickets can be in Progress or Review/QA at once, up to `max_parallel` in `config.json` (default 3). `failed/` is a manual-recovery column.

### Dependencies

A ticket declares what it waits for with a line in its own Markdown, right after the title:

```md
Blocked by: #002, #003
```

In the inline form every ID needs the `#` prefix, so free text such as `Blocked by: the 2 auth tickets` creates no blocker. A `## Blocked by` section with one ticket ID per list item (the `#` is optional there) is read as well; lines inside code fences are ignored. The "blocks" side is derived and shown in the TUI, never written to files.

- A ticket in Todo starts only when every blocker is in `done/` or `archive/`. Otherwise it stays in Todo until the blockers finish, so a batch of tickets dropped into Todo works through itself in dependency order.
- Unblocked tickets start in ascending ID order, in parallel, up to `max_parallel`. In a chain of four tickets only the first is started.
- A blocker that is missing, in Backlog, Failed, Progress or Review does not release the ticket. Cycles never start.
- Moving a blocked ticket to Progress from the TUI is refused.


## Live ticket updates and agent prompts

**Pass the ticket path, not a copy of its Markdown contents.** On a fresh Developer or Reviewer turn, the supervisor resolves the current ticket path from its stable ID and sends a short instruction, for example:

```text
Read .kanbeat/progress/002-authentication.md and complete the task described
there. This ticket is read-only for you: never edit, rename or move it.
Kanbeat alone manages ticket lifecycle and runtime.
```

The same rule applies in Review/QA, using its current `.kanbeat/review/...` path. Agents may change project code, but they **must not modify ticket files or technical Kanbeat state**. Tickets remain user-editable in an external editor at any time; Kanbeat's TUI is read-only, and an open ticket viewer updates its body and location as changes are detected.

For every active worker turn, the supervisor stores a **SHA-256 hash of the ticket's Markdown contents** alongside the worker's runtime data in `.kanbeat/.state/`. After the turn ends, **before accepting completion, a Developer stage transition, or a Reviewer verdict**, it reads the current ticket by stable ID and compares content hashes. When the body changed:

- Do **not** interrupt a running turn, restart the harness, or discard its native session.
- Resolve the **current file path** again from the ticket ID (filename slug and status directory can have changed).
- Send a follow-up in the **same native worker session**: `Ticket #002 has changed. Re-read <current-ticket-path>, reconcile the updated requirements with your existing work, and continue. Do not modify the ticket.`
- Keep the ticket on the current stage until a subsequent successful turn has processed the latest content. Update the stored hash for that turn.
- In Review/QA, previously recorded verdicts for an older ticket revision **cannot approve the updated task**. Each affected Reviewer slot must re-read it and deliver a verdict for the current revision before moving to Done. Preserve Reviewer-to-Reviewer and Developer-to-Reviewer transcript isolation.

A filename-only rename that leaves the Markdown contents unchanged **does not** trigger a new agent turn. A stage change made by the supervisor changes the path, not the underlying requirements. Hashes detect **content changes**, not timestamps, filenames, or Git diffs.

**Implemented in the supervisor:** per-turn content hashes, native same-session re-read on change, stale Reviewer verdict invalidation, and re-resolving the canonical ticket path by stable ID. This implementation has not yet been validated against live vendor CLI processes.

## Native CLI lifecycle

No custom agent implementation is embedded. Kanbeat uses documented native commands to run each turn and parse structured events. Codex app-server turn events, Claude background hook events, OpenCode run JSON and Pi print JSON are used. A CLI's native session identifier is stored when reported and used for resume. If an assigned harness is gone, the slot switches to the configured default and starts fresh. If the **default** itself is unavailable, the whole board stops and must be restarted manually after configuration.

If a session no longer exists, the worker attempts a fresh run in the same slot. This is not equivalent to rolling back tools or Git modifications. Stop interrupts process groups, then terminates unresponsive workers. Custom non-board sessions must not be interrupted by board Stop. On an ordinary supervisor restart, the board starts stopped; previously launched board-owned processes are matched by PID and OS process-start identity before any orphan shutdown is attempted. If the process cannot be identified safely, Play remains blocked pending manual resolution. External moves of active tickets interrupt their old workers; failed parallel reviews interrupt remaining Reviewer processes.

**Native activity:** while a structured worker runs, Kanbeat shows `working`, `waiting`, `idle` or `error` only when the harness emits an explicit event for it: Codex turn, item and thread-status events, Claude hook and statusline events, OpenCode step and permission/question events, and Pi agent/message/tool events. Without such an event the UI falls back to process lifecycle and does not guess semantic busy or idle state from the PID. Waiting animation stops while a declared wait is active.

**Limitations:** JSON event shapes and session recovery vary by installed CLI version. The Sessions tab offers supervisor-owned interactive PTYs alongside structured worker logs; task terminals require a stopped board. See [terminal lifecycle and limits](terminals.md). Runtime session IDs are only available for CLIs/events that disclose them. You should verify your chosen CLI version before relying on the workflow unattended.

## Interactive terminals and sessions

The Sessions tab lists task terminals (a Developer or Reviewer slot of a ticket, available while the board is stopped) and independent sessions that you organise into your own groups. Independent sessions keep running when you detach and when the board is stopped. The supervisor owns every PTY; TUI clients only display and forward input, so several clients can open the same terminal.

### Changes panel

On the Sessions tab, a panel to the right of the terminal shows the project's uncommitted changes. It takes about 30% of the width and is closed until you open it with the button next to Detach; it then stays open until you close it (`×`, `Esc` or the same button). Kanbeat remembers that choice per project in `.kanbeat/.state/ui.json`. The button shows `3 files | +35 -11` (or just `files` when there are none). It blinks gently when the changes differ from the state you last saw with the panel open, and stops as soon as you open the panel; it does not blink while the panel is open, or after you have looked and closed it, until something changes again.

The panel is the first thing to give way when the window narrows: when the terminal would get less than 60 columns it hides itself, and returns when there is room again. If you press the button on a very narrow window, the panel covers the whole Sessions page instead, and closing it there does not change your saved choice.

The panel lists the changed files and, below the list, shows every file's diff as one stream with a line-number column, green added lines, red removed lines, highlighted changed words and syntax colours. The list and the diff scroll independently; clicking a file in the list scrolls the diff to it, and clicking a file's header folds or unfolds it. New (untracked) files and tests or generated files (`tests/`, `*.lock`, `dist/` and similar) start closed: new files unfold in place and the `N tests/generated (show)` row reveals the rest. `[ ask ]` types a reference to the file (`@path`, or the plain path in a Terminal session) into the open session without sending it. `source:` switches between Current (staged, unstaged and new files against `HEAD`), Unstaged, Staged and Last commit. The view refreshes by itself every two seconds; it only runs read-only Git commands, never takes Git's index lock and ignores `.kanbeat/.state/`.

Harness selectors in Settings, slot configuration and New session include an Open button. It starts or reattaches one persistent native TUI session per harness in the project. Use it to configure the native CLI, then start new sessions.

OpenCode, Codex and Claude workers run against a project-local shared runtime. OpenCode uses `opencode serve` on loopback with a random per-supervisor password passed only in child environments. Codex uses a private `codex app-server` WebSocket bound to loopback with a capability token that stays in memory and child environments. Claude workers run as `claude --bg` background sessions; Kanbeat installs a private hook and statusline bridge on the first launch and reuses that session stream when resuming. It never reads transcripts, credentials or native settings files. Their native session can be opened live from ticket and slot dialogs and Sessions Tasks while the board is running; the action attaches to that same thread, server session or background session. While a structured turn is active, the attached view is monitor-only so Kanbeat never runs two writers against one native session. Pi workers remain one-shot and require Board Stop before opening a task terminal. Shared hosts and background sessions are stopped on supervisor shutdown; recorded process identities and native session IDs are used for cleanup and recovery.

For OpenCode, Codex and Claude, the configuration session and worker sessions share the same runtime. A model, provider or effort selected on the configuration session is passed to newly started Developer and Reviewer worker sessions, transition hooks and fresh independent sessions; resumed sessions keep their own model. Native settings are project-level templates: there is no per-slot model, provider or effort override. Slot harness and custom prompt remain per-ticket. Codex does not copy sandbox or approval settings, and Claude does not copy permission mode, authentication or sandbox settings. Pi configuration sessions are still separate TUIs; their native settings are not captured or copied.

Harness capabilities:

| Harness | Configuration runtime | Live attach | Captured settings |
| --- | --- | --- | --- |
| Codex | Private owned `codex app-server`, named thread | Yes, same thread | Model, provider, reasoning effort from `thread/read` |
| Claude | Owned `claude --bg` background session | Yes, same background session | Model and effort from the per-invocation statusline bridge |
| OpenCode | Owned `opencode serve` and session | Yes, same server session | Model, provider and variant from the session API |
| Pi | Plain native TUI and one-shot worker | No, Board Stop required | Not captured; select settings per invocation |

These limits are intentional. Kanbeat does not treat a second `--resume` process as an attach, does not copy conversation history between slots, and does not silently replace a known native session.

The full lifecycle, stop rules, groups and persistence are documented in [terminals.md](terminals.md).

## Configuration

Edit project settings via Settings in TUI, or `.kanbeat/config.json`. Settings cover the default harness, a global custom prompt (CP), Developer and Reviewer/QA prompts, and the transition hook. Treat CP and logs as sensitive local data.

Short projects use `.kanbeat/.state/supervisor.sock`. Long project paths use a
stable SHA-256-derived socket name in `~/.tmp/kanbeat-sockets/`, shared by all
clients for that canonical project path. The socket directory is private (0700),
the socket is 0600, and the ownership lock stays inside the project's `.state`.
Distinct projects use distinct socket names; symlink aliases resolve to the same
project. No TCP listener is used. The home runtime path itself must fit the OS
Unix-socket limit. See [Issue #5](https://github.com/arconw/Kanbeat/issues/5).

## Transition hooks

The transition hook is a session that Kanbeat wakes whenever the board changes, so an agent can follow the tickets, keep a tracker in sync or manage the queue. It uses the same numbered events as `kanbeat events` and `kanbeat watch`. Configure it in Settings or with `kanbeat config set transition_hook.harness claude` and `transition_hook.prompt "..."`, or in `config.json`:

```json
{
  "transition_hook": {
    "harness": "claude",
    "prompt": "Update the GitHub issue linked in a ticket whenever it changes column."
  }
}
```

An empty prompt turns the hook off; an empty harness means the project default. The prompt is at most 8,000 characters.

- **One session.** Kanbeat keeps one independent session named **Transition hook**, in Sessions under **Ungrouped**. It starts the first time something changes after the hook is turned on, and its first prompt holds a short explanation, your standing instructions and the events so far. Events from before you turned the hook on are not delivered.
- **Waking it.** The session is told about new events as it becomes free. If it is running and has been quiet for about eight seconds (no terminal output), Kanbeat types a short message into it and presses Enter: the event numbers, one line per event, and how to read earlier ones. If it is stopped, Kanbeat resumes it under the same native session id, waits until it is ready and then types the message. If you removed the session, the next change starts a new one. Events that arrive while it is busy are delivered together, up to 30 at a time (fewer if they would not fit in one message), and at least 15 seconds apart. A session that is still starting is left alone. Ticket titles are cleaned of control characters and cut to 120 characters, and the message says they are data, not instructions.
- **Any board state.** Waking does not need Play. Changes made by hand, from the command line or by an agent count, and so do the board starting and stopping, so a hook that moves tickets can wake itself; keep the prompt specific and idempotent.
- **What it can do.** It is an ordinary session: your login, permissions and sandbox apply, and it may read `.kanbeat/INSTRUCTIONS.md` to manage tickets and settings with the commands described above. Stop or remove it from the session menu like any other session.
- **Limits.** Waking works by typing into the terminal, so it relies on the harness being at its input prompt when it is quiet; a harness that is waiting for an answer to a question may receive the message as that answer. The delivered position is kept in `.kanbeat/.state/supervisor.json` and an event is typed at most once, so after a crash or a refused write it may be missed rather than repeated. If the harness is unavailable or the prompt is too long to start the session, a notice says why and Kanbeat tries again after a minute.

Earlier versions started a fresh one-shot session for every column change and recorded them in a ledger. That mode is gone: pending events and the ledger from an older state file are dropped when the supervisor starts.

## Development

```sh
git clone https://github.com/arconw/Kanbeat.git
cd Kanbeat
python -m pip install -e '.[dev]'
pytest
```

CI runs regression tests and headless TUI checks on Python 3.11–3.14 on Linux,
plus Python 3.14 on macOS. It also checks Ruff diagnostics and formatting,
validates workflow syntax and shell snippets with actionlint, builds wheel/sdist,
validates distribution metadata and smoke-tests clean installs with bundled TUI
styles. Test and distribution artifacts are attached to Actions runs. Dependabot
tracks Python and Actions dependency updates. CodeQL scans Python on PRs,
main pushes and a weekly schedule. Tags matching `v*` run the same CI;
there is no automatic PyPI publication.

Local equivalent:

```sh
python -m ruff check src tests
python -m ruff format --check src tests
python -m pytest
python -m build --outdir .tmp/dist
python -m twine check .tmp/dist/*
```

Native harness event/resume compatibility still needs validation against live
installed vendor CLIs; CI uses deterministic fake harnesses and real local
subprocesses, without model calls.

The README demo GIFs are recorded from the real TUI by `tools/record_demos.py`; see [media.md](media.md).

## Migration

The old `.tasks` layout is not automatically migrated. Existing users must manually copy/reformat tickets to the new directory/filename convention. No destructive auto-conversion is performed.
