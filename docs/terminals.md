# Interactive terminals

The supervisor owns each native PTY process. TUI clients display its current screen
and forward keys/paste/resize through the private Unix socket. Detaching the modal
or exiting a TUI never stops the process. Multiple clients can reopen the same
terminal; the latest resize determines its shared dimensions.

In Sessions, expand Tasks and select a Developer or Reviewer terminal, or use
Terminal from the ticket details or its saved slot configuration. Unsaved slot
edits must be saved before opening. Stop the board first: structured worker turns
and an interactive process must not use the same slot concurrently. This is not
yet a live attach to a running structured worker. The button is disabled while
the board is running. Saved native session IDs resume only that ticket/role/slot.
A fresh slot starts a fresh native CLI with its role instructions and ticket path.
No permissions, authentication, model or sandbox overrides are passed.

Sessions Tasks displays the recorded lifecycle of each structured worker and
its supervisor-owned task PTY separately, including available exit codes and
errors. Independent-session rows also show available exit codes and errors.
These are process and execution states, not inferred native busy/idle/waiting
signals; semantic agent activity still requires supported vendor events.

Use Detach or Ctrl+backslash to close the full-screen view. Stop requires explicit
confirmation and interrupts/terminates the process group. Stop task terminals
before Play resumes structured dispatch. Board Stop interrupts board-owned PTYs;
independent sessions are outside its scope. Moving/deleting the associated ticket
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
Viewport dimensions are bounded to 240 columns and 100 rows. The mouse wheel is forwarded using the application's active mouse protocol (including SGR) or alternate-scroll mode. Full-screen applications without mouse reporting receive Page Up/Down. Shift+wheel always opens local primary-screen output history, and the Live button returns to current output. Plain output history retains up to 2,000 scrolled lines while the supervisor lives, survives detaching clients, and stays anchored while new output arrives. Alternate-screen applications own their dialogue history; their redraws are not copied into primary scrollback. History is a read-only text view; the live native screen retains its ANSI styles. Clipboard OSC requests are not forwarded.

Implemented for [Issue #2](https://github.com/arconw/Kanbeat/issues/2).
Custom groups and session-management controls are implemented in
[Issue #3](https://github.com/arconw/Kanbeat/issues/3).

## Independent sessions and groups

Sessions shows each named group and Ungrouped as a collapsible section, matching Tasks, with its session count in the heading. Collapse choices, including Tasks, survive status refreshes, tab navigation and restarting the client. Choices are stored locally per project in `.kanbeat/.state/ui.json`; currently open clients keep their own view, while a newly opened client loads the last saved choices.

Sessions provides New group and New session. Choose a native harness, a group,
an optional initial prompt and, if available, an explicit native session ID.
Independent sessions can start while the board is running. Board Play/Stop never
stops them, and removing a board ticket does not affect them.

Actions lets you rename or move a session, explicitly stop it, restart it, or
remove a stopped local record. Stop and removal require confirmation. Group
removal requires an empty group; first move or remove its session records.
Removing records does not delete native CLI history or retained local output logs.

Groups, session IDs, titles, prompts, ownership and known native IDs persist in
`.state`. Restart after supervisor recovery reuses the same local session ID;
a known native ID resumes vendor history, otherwise a fresh native context starts.
No active process is silently restarted. Unresolved crash-orphan identities block
restart/removal until manually resolved. Deleting `.state` intentionally discards
this optional organization and local session metadata, not ticket status.

## Input and rendering latency

The terminal waits for screen revisions instead of polling at 150 ms. Ordered input batches use a bounded queue with backpressure; a full native input buffer delays sending rather than dropping keys. Malformed batches are rejected before any bytes reach the PTY. Detach drains queued input for up to two seconds and reports an unconfirmed remainder if the process is unresponsive. Connection failures are reported rather than retried with potentially duplicated input.

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
