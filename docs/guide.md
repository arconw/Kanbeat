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
- The first numeric filename segment is order, the second is a unique stable ticket ID.
- Tickets are plain Markdown with **no frontmatter**, transcript or runtime metadata.
- Status = directory. Order = first filename number. ID = second number. `archive/` is out of scope.
- Sequential tickets; 0..N parallel reviewers on **one active ticket only**. Separate reviewer slots have isolated contexts.
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
kanbeat --project /path/to/project   # select another project root
kanbeat --version
kanbeat shutdown             # exit the supervisor after cleaning up workers
```

The project root is the current working directory by default. `--project` is a top-level option and goes before the subcommand. `kanbeat run` is the explicit form of plain `kanbeat`. `kanbeat supervisor` runs a foreground supervisor for debugging; the normal CLI starts a detached supervisor automatically.

Open a second terminal in the **same** project and launch `kanbeat` again: it connects to the same supervisor. A different project directory starts an independent supervisor. `kanbeat new`, `move`, `play` and `stop` also start the supervisor when none is running.

`kanbeat shutdown` waits for cleanup without starting an absent supervisor. Add `--yes` to confirm stopping independent sessions. See [Releases](releases.md#update) before changing an installed binary.

TUI keys: `1` Board, `2` Sessions, `3` Archive, `4` Settings, `Ctrl+R` refresh, `Ctrl+Q` quit the TUI (the supervisor keeps running), `Ctrl+\` detach from an interactive terminal. Board, Sessions, Archive and Settings are also clickable. The minimum supported size is 80 × 24 characters; roughly 130 columns show all six fixed-width board lanes, and narrower terminals can scroll them horizontally. Dialogs and Settings scroll vertically in shorter terminals. Slot labels, controls and runtime badges have separate space, and invalid saved harness choices show a warning before Save.

Clicking the visible backdrop outside a dialog closes that dialog without saving. A confirmation is cancelled. Clicks inside controls and drags that start inside the dialog do not dismiss it; nested dialogs close one at a time. Full-screen views use their Close/Detach controls, and detaching a terminal keeps its process running.

Background snapshots update existing board cards and session rows in place. Runtime/notice changes preserve focus, scrolling and selection; moving or removing a ticket changes only its affected card.

## On-disk layout

```text
.kanbeat/
├── INSTRUCTIONS.md            # only read when explicitly asked
├── config.json                # project defaults, Git-trackable
├── ids.json                   # persistent ticket ID high-water mark, Git-trackable
├── backlog/
│   └── 0-001-future-search.md
├── todo/
│   ├── 0-002-authentication.md
│   └── 10-003-token-refresh.md
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

The file `0-002-authentication.md` contains only:

```md
# Implement authentication

Add token validation to the API.

## Acceptance criteria

- Validate access tokens.
- Handle expiration.
```

Creating `.kanbeat` automatically creates `INSTRUCTIONS.md` for a separately instructed agent to author tickets. It is **not** injected into coding agents automatically.

The first number is **numeric sort order**. The second, e.g. `002`, is the ticket ID and must be unique across all column and archive directories. The slug is readable; changing order and status never changes ID. No metadata is added to Markdown by Kanbeat.

`ids.json` reserves monotonically increasing IDs before ticket creation. Keep it
with the board in Git: deleting archived tickets or disposable `.state` must not
reset ticket identity. Kanbeat records imported IDs before deleting archived
tickets too. Failed creates can leave gaps, which are intentional. External ticket
authors must still choose unused IDs; history deleted outside Kanbeat before the
ID ledger exists cannot be reconstructed automatically.

Reordering normally renames only the two exchanged tickets; equal-order ties may
require normalizing the column's numeric prefixes. Every individual rename keeps
the `.md` suffix, ID and body. An interruption may leave a partial order change,
but tickets remain visible. Leftover `.kanbeat-rename-*` files from older versions
are reported as errors for manual filename restoration; they are never silently
ignored or rewritten.

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

Only one ticket may be in Progress or Review/QA for the dispatcher to start work. If there are multiple, the supervisor suspends dispatch and reports an error. `failed/` is a manual-recovery column.

## Live ticket updates and agent prompts

**Pass the ticket path, not a copy of its Markdown contents.** On a fresh Developer or Reviewer turn, the supervisor resolves the current ticket path from its stable ID and sends a short instruction, for example:

```text
Read .kanbeat/progress/0-002-authentication.md and complete the task described
there. This ticket is read-only for you: never edit, rename or move it.
Kanbeat alone manages ticket lifecycle and runtime.
```

The same rule applies in Review/QA, using its current `.kanbeat/review/...` path. Agents may change project code, but they **must not modify ticket files or technical Kanbeat state**. Tickets remain user-editable in an external editor at any time; Kanbeat's TUI is read-only, and an open ticket viewer updates its body and location as changes are detected.

For every active worker turn, the supervisor stores a **SHA-256 hash of the ticket's Markdown contents** alongside the worker's runtime data in `.kanbeat/.state/`. After the turn ends, **before accepting completion, a Developer stage transition, or a Reviewer verdict**, it reads the current ticket by stable ID and compares content hashes. When the body changed:

- Do **not** interrupt a running turn, restart the harness, or discard its native session.
- Resolve the **current file path** again from the ticket ID (filename, order, slug and status directory can have changed).
- Send a follow-up in the **same native worker session**: `Ticket #002 has changed. Re-read <current-ticket-path>, reconcile the updated requirements with your existing work, and continue. Do not modify the ticket.`
- Keep the ticket on the current stage until a subsequent successful turn has processed the latest content. Update the stored hash for that turn.
- In Review/QA, previously recorded verdicts for an older ticket revision **cannot approve the updated task**. Each affected Reviewer slot must re-read it and deliver a verdict for the current revision before moving to Done. Preserve Reviewer-to-Reviewer and Developer-to-Reviewer transcript isolation.

A filename-only rename or reorder that leaves the Markdown contents unchanged **does not** trigger a new agent turn. A stage change made by the supervisor changes the path, not the underlying requirements. Hashes detect **content changes**, not timestamps, filenames, or Git diffs.

**Implemented in the supervisor:** per-turn content hashes, native same-session re-read on change, stale Reviewer verdict invalidation, and re-resolving the canonical ticket path by stable ID. This implementation has not yet been validated against live vendor CLI processes.

## Native CLI lifecycle

No custom agent implementation is embedded. Kanbeat uses documented native commands to run each turn and parse structured events. Codex exec JSON, Claude print stream-json, OpenCode run JSON and Pi print JSON are used. A CLI's native session identifier is stored when reported and used for resume. If an assigned harness is gone, the slot switches to the configured default and starts fresh. If the **default** itself is unavailable, the whole board stops and must be restarted manually after configuration.

If a session no longer exists, the worker attempts a fresh run in the same slot. This is not equivalent to rolling back tools or Git modifications. Stop interrupts process groups, then terminates unresponsive workers. Custom non-board sessions must not be interrupted by board Stop. On an ordinary supervisor restart, the board starts stopped; previously launched board-owned processes are matched by PID and OS process-start identity before any orphan shutdown is attempted. If the process cannot be identified safely, Play remains blocked pending manual resolution. External moves of active tickets interrupt their old workers; failed parallel reviews interrupt remaining Reviewer processes.

**Limitations:** JSON event shapes and session recovery vary by installed CLI version. The Sessions tab offers supervisor-owned interactive PTYs alongside structured worker logs; task terminals require a stopped board. See [terminal lifecycle and limits](terminals.md). Runtime session IDs are only available for CLIs/events that disclose them. You should verify your chosen CLI version before relying on the workflow unattended.

## Interactive terminals and sessions

The Sessions tab lists task terminals (a Developer or Reviewer slot of a ticket, available while the board is stopped) and independent sessions that you organise into your own groups. Independent sessions keep running when you detach and when the board is stopped. The supervisor owns every PTY; TUI clients only display and forward input, so several clients can open the same terminal.

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

Configure an optional native harness and prompt in Settings or `config.json`:

```json
{
  "transition_hook": {
    "harness": "claude",
    "prompt": "Update the GitHub issue linked in the ticket after this transition."
  }
}
```

An empty prompt disables hooks; an empty harness selects the project default at
queue time. Every column change made through Kanbeat (including manual moves,
archive/restore and failure transitions) queues one hook. Creating, reordering,
deleting or externally moving a file does not queue a hook. Pending hooks run
serially on Play, before the next board turn. Each starts a fresh native session
with the transition event ID and current canonical ticket path, without worker
history, ticket body copies or worker CP. The configured hook prompt alone defines
its automation; hooks retain native CLI permissions and authentication.

Hook failures notify the user and preserve the ticket's new column. Missing hook
harnesses fail that hook without default fallback or stopping the board. Stop
interrupts running hooks too. The local ledger is in `.kanbeat/.state/supervisor.json`
under `hooks`, and output is in `.kanbeat/.state/logs/hook-<event-id>.log`.

Completed, failed and interrupted events are never replayed automatically. On
restart the board stays stopped, pending events wait for Play, and interrupted
hooks require manual inspection of their external effects. A running hook's
outcome can be uncertain after a crash. The ledger and file rename are separate
writes: a crash between them can leave a moved ticket without a queued hook.
Deleting `.state` loses the ledger. This is not an exactly-once guarantee for
external services; make hook operations idempotent and inspect before retrying.

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
