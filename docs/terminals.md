# Interactive terminals

The supervisor owns each native PTY process. TUI clients display its current screen
and forward keys/paste/resize through the private Unix socket. Detaching the modal
or exiting a TUI never stops the process. Multiple clients can reopen the same
terminal; the latest resize determines its shared dimensions.

In Sessions, expand Tasks and select a Developer or Reviewer terminal. Stop the
board first: structured worker turns and an interactive process must not use the
same slot concurrently. Saved native session IDs resume only that ticket/role/slot.
A fresh slot starts a fresh native CLI with its role instructions and ticket path.
No permissions, authentication, model or sandbox overrides are passed.

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
Viewport dimensions are bounded to 240 columns and 100 rows. Native mouse reporting
and forwarding clipboard OSC requests are not enabled by Kanbeat's display client.

Implemented for [Issue #2](https://github.com/arconw/Kanbeat/issues/2).
Custom groups and session-management controls are implemented in
[Issue #3](https://github.com/arconw/Kanbeat/issues/3).

## Independent sessions and groups

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
