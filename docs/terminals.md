# Interactive terminals

The supervisor owns each native PTY process. TUI clients display its current screen
and forward keys/paste/resize through the private Unix socket. Detaching the modal
or exiting a TUI never stops the process. Multiple clients can reopen the same
terminal; the latest resize determines its shared dimensions.

In Sessions, expand Tasks and select a Developer or Reviewer terminal, or use
Terminal from the ticket details or its saved slot configuration. Unsaved slot
edits must be saved before opening. OpenCode, Codex and Claude task slots attach
to the same live shared runtime while the board is running. During an active
structured turn that view is monitor-only: Kanbeat disables input so one native
session never gets two writers. Pi structured workers remain one-shot and require
Board Stop before opening their task terminal. Saved native session IDs resume
only that ticket/role/slot; a missing session is never silently replaced while a
known one exists. A fresh slot starts a fresh native CLI with its role
instructions and ticket path. No permissions, authentication or sandbox overrides
are passed.

Only structured workers with a confirmed running dispatch receive a gentle
color pulse in ticket, board and Sessions status labels. Native PTY existence
alone does not imply active inference, so PTYs never pulse without vendor
activity events. Textual reduced-motion mode disables the pulse; it updates
label styles in place without rebuilding the board.

Sessions Tasks displays the recorded lifecycle of each structured worker and
its supervisor-owned task PTY separately, including available exit codes and
errors. Independent-session rows also show available exit codes and errors.
These are process and execution states, not inferred native busy/idle/waiting
signals; semantic agent activity still requires supported vendor events.

Use Detach or Ctrl+backslash to close the full-screen view. Stop requires explicit
confirmation and reports `stopping` until the child exits. Start/Resume,
removal, and reopening a task terminal are blocked while stopping; a second
client sees the same lifecycle. Stop interrupts/terminates the process group.
Stop task terminals before Play resumes structured dispatch. Board Stop
interrupts board-owned PTYs;
independent sessions are outside its scope. Teardown signals the child-led process
group only while it is still verifiably the owned child's group. If the OS
denies group signaling after a PTY hangup, Kanbeat falls back to terminating
its tracked child; this cannot guarantee that every descendant also exits.
Moving/deleting the associated ticket
also stops its old interactive process. Interactive edits do not themselves emit
structured completion/verdicts or change ticket status; Play rechecks the work.

Identity, ownership and known native IDs live in `.kanbeat/.state/supervisor.json`.
Terminal output logs are private local data under `.state/logs/terminal-<id>.log`.
Screen state survives client disconnects while the supervisor lives. PTY file
descriptors cannot survive supervisor restart: stale processes are reconciled by
PID/start identity, the board starts stopped, and a new terminal may resume a known
native ID. Fresh interactive CLI session IDs are not universally exposed by vendor
TUIs and cannot always be discovered automatically.

The built-in terminal supports ANSI colors, cursor movement, alternate screens,
Unicode, named/function/control keys and bracketed paste through textual-tty/bittty.
Viewport dimensions are bounded to 240 columns and 100 rows. The mouse wheel is forwarded using the application's active mouse protocol (including SGR) or alternate-scroll mode. Full-screen applications without mouse reporting receive Page Up/Down. Shift+wheel always opens local primary-screen output history, and the Live button returns to current output. Plain output history retains up to 2,000 scrolled lines while the supervisor lives, survives detaching clients, and stays anchored while new output arrives. Alternate-screen applications own their dialogue history; their redraws are not copied into primary scrollback. A live worker monitor disables keys and paste, but its wheel and Shift+wheel still scroll the attached view; attached slot terminals without local primary-screen history use the application's own scrollback for both gestures. History is a read-only text view; the live native screen retains its ANSI styles. Clipboard OSC requests are not forwarded.

Implemented for [Issue #2](https://github.com/arconw/Kanbeat/issues/2).
Custom groups and session-management controls are implemented in
[Issue #3](https://github.com/arconw/Kanbeat/issues/3).

## Independent sessions and groups

Sessions shows each named group and Ungrouped as a collapsible section, matching Tasks, with its session count in the heading. Collapse choices, including Tasks, survive status refreshes, tab navigation and restarting the client. Choices are stored locally per project in `.kanbeat/.state/ui.json`; currently open clients keep their own view, while a newly opened client loads the last saved choices.

Sessions provides New group and New session. Choose a native harness or a plain
**Terminal**, a group, and for a harness an optional initial prompt and, if available,
an explicit native session ID. Independent sessions can start while the board is
running. Board Play/Stop never stops them, and removing a board ticket does not
affect them.

A **Terminal** session runs your `$SHELL` (or `/bin/sh`) in the project directory.
The supervisor tracks the shell's current directory (from `/proc` on Linux, `lsof`
on macOS) and stores it in `.kanbeat/.state/supervisor.json`, so after Stop or a
supervisor restart, clicking the session starts a fresh shell in the directory you
last `cd`-ed to. Shell history, variables and running programs are not restored.
A saved directory that no longer exists falls back to the project directory. Stop
hangs up the shell first, then escalates like any other session. Terminals exist only
in Sessions: board task slots and hooks always use a native harness.

Clicking a stopped session resumes it. The session menu offers Edit (title, group,
for harnesses the native session ID, and for an independent agent session a harness
selector), Stop, **Remove from Kanbeat** (removes the record; every conversation
stays in its harness) and **Delete session and history** (also deletes every native
session Kanbeat recorded for it, in each harness it has used, plus private transfer
files). Stop interrupts the process at once without confirmation; removing and
deleting ask for confirmation. Group removal requires an empty group; first move or
remove its session records. Removing a record does not delete native CLI histories
unless you choose Delete session and history, and retained local output logs stay.

Harness switching is available only for an independent agent session that is
stopped; the selector is disabled while the session runs and its tooltip asks you to
stop it first. See
[Switching a session's harness](guide.md#switching-a-sessions-harness) for the
confirmation, the privacy rule, what moves between harnesses, limits and the
fresh-session fallback.

Groups, session IDs, titles, prompts, ownership and known native IDs persist in
`.state`. A harness switch clears the session's stored initial launch prompt only
after the user event that carries it is already in the target's own history or
has been delivered to the target. An empty source history, a prompt that is not
found in the readable history, or a reduced transfer that excludes the prompt
keeps it, because it may not have reached the target. Restart after supervisor
recovery reuses the same local session ID;
a known native ID resumes vendor history, otherwise a fresh native context starts.
No active process is silently restarted. Unresolved crash-orphan identities block
restart/removal until manually resolved. Deleting `.state` intentionally discards
this optional organization and local session metadata, not ticket status.

## Input and rendering latency

The terminal waits for screen revisions instead of polling at 150 ms. Ordered input batches use a bounded queue with backpressure; a full native input buffer delays sending rather than dropping keys. Malformed batches are rejected before any bytes reach the PTY. A large paste is split into ordered chunks at character boundaries and streamed to the supervisor, which keeps one bracketed-paste start and end marker for the whole text. An unfinished paste is discarded after 60 seconds without client progress or when its terminal stops; while a bracketed paste is closing, its state is kept until the end marker can be queued. Detach drains queued input for up to two seconds and reports an unconfirmed remainder if the process is unresponsive. Connection failures are reported rather than retried with potentially duplicated input.

Only changed screen rows are fed to the display. The header and detach hint are at the top; the terminal fills the remaining viewport to the bottom.

A local deterministic benchmark using a real PTY, Unix RPC and the Textual terminal display, with a fixture that delays each output flush by 30 ms, measured:

| Measurement | v0.6.1 baseline | Updated transport |
| --- | --- | --- |
| Single-key median / p95 | 119.0 / 120.5 ms | 35.3 / 36.6 ms |
| 40-key burst, all visible | 419.5 ms | 37.4 ms |

These are 60-sample local observations with a fixture CLI, not a guarantee for vendor CLIs or terminal emulators. Native CLI rendering, machine load and the display tick still contribute latency. Reproduce the updated measurement with:

```sh
python tools/benchmark_terminal.py --work .tmp/terminal-benchmark --delay 0.03
```

The work directory must be fresh. The benchmark calls no vendor CLI or model.
