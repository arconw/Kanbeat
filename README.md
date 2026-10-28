# Arco9 TaskBeat

A Markdown-first, Kanban-driven TUI for native CLI coding agents.

**Status: early architectural scaffold, not production-ready.** See the standalone visual design document at [docs/design/design.html](docs/design/design.html).

## What it is

TaskBeat is intentionally a small **dispatcher**, not a new agent framework. Tickets are plain Markdown files in the current project's .tasks directory. A native Codex CLI, Claude Code CLI, OpenCode CLI or Pi CLI process lives in tmux, receives the ticket instructions and edits the project as usual. TaskBeat observes file-based status changes and hands the ticket to the next role. Ticket logs are never embedded in Markdown.

The first implementation includes:
- Textual mouse-clickable Kanban with Board, Sessions, Archive and Settings tabs.
- Full-viewport, borderless ticket editing (FMODAL) and small dialogs (SMODAL).
- Ticket ordering, manual status moves, select-mode bulk actions, archive and restore.
- Project-local YAML-frontmatter Markdown and atomic write / advisory lock.
- Sequential ticket dispatcher, parallel reviewers **within one ticket**, reviewer consensus, three launch/recovery attempts and notifications.
- Native tmux process lifecycle and durable session registry; optional experimental embedded PTY view.

## Requirements

- Python 3.11+
- tmux in PATH, on a Unix-like machine
- At least one installed, authenticated coding CLI: codex, claude, opencode, pi.
- A terminal emulator with mouse reporting; mouse *hover* is not required.

Install from the repository root:

    python -m pip install -e .
    taskbeat run

Optional experimental full-terminal FMODAL:

    python -m pip install -e '.[terminal]'

Use in another project by starting it from that project's directory:

    cd /path/to/project
    taskbeat

TaskBeat creates .tasks on first launch. It does **not** create a ticket automatically. TaskBeat's scheduler runs when the TUI is open, not as a background daemon.

## Ticket and agent CLI

Create a ticket and enqueue it:

    taskbeat new "Implement auth middleware" --status todo --description "Add auth validation"
    taskbeat list

An agent completes development by changing the ticket to Review:

    taskbeat status task-123 review

An agent trying to jump directly from Progress to Done is redirected to Review.

A reviewer uses the concurrency-safe verdict command:

    taskbeat review task-123 reviewer-1 approve --iteration <current-iteration-id>
    taskbeat review task-123 reviewer-2 changes_requested --iteration <current-iteration-id>

### Review/QA context boundary

**A Reviewer receives only the original ticket task as its task context**, together with its own reviewer/global prompt (if set) and minimal workflow instructions (reviewer ID, iteration, status/reporting command). TaskBeat does **not** feed it the Developer's session logs, transcript, messages, terminal output, Developer CP or a summary of the Developer's work. The Reviewer independently examines the actual repository changes and runs tests. Different Reviewer **slots** (for example, `reviewer-1` and `reviewer-2`) do not share histories.

**Exception: replacement inside the same Reviewer slot.** If `reviewer-1` was running Claude Code and its harness is changed to Codex, the new Codex session in `reviewer-1` may receive the bounded history of that slot's previous Claude session (when available). The same-slot reset may likewise reuse its own context. This is a continuation of one slot, **not** communication between different reviewers. `reviewer-2` still gets no history from `reviewer-1`.

The slot ID must remain the same for any such handoff. A newly created Reviewer slot receives the original ticket and its own applicable prompts only. A **Developer → Reviewer** transition never performs a log handoff.

Each review worker has an independent ID. After *all* reviewers submit, TaskBeat returns the ticket to Progress if anyone requested changes or sends it to Done if all approved. Zero reviewers means Review automatically reaches Done.

Use the global option to reference the project when running outside the project's directory:

    taskbeat --project /path/to/project list

### Ticket format

    ---
    id: task-001
    title: Implement auth middleware
    status: todo
    order: 100
    developer:
      harness: codex
    reviewers:
      - id: reviewer-1
        harness: claude
    ---
    ## Goal
    Validate auth and handle edge cases.

Optional role fields use **sparse frontmatter**: `cp` and `session` are only written when set. Clearing a value removes its field on the next save. Empty `developer` and `reviewers` sections and unset `review_iteration` are omitted too. Existing Markdown files are compacted the next time TaskBeat saves them.

Files without frontmatter are also recognized, **safely defaulting to Backlog** rather than running automatically. Fields not owned by TaskBeat are preserved when the file is rewritten. Never put secrets in ticket Markdown: Custom Prompts in tickets are visible as plain text.

Task status values: backlog, todo, progress, review, done, failed. Archive is a directory, not an execution status.

## Runtime model

- .tasks/*.md: tickets, editable externally
- .tasks/archive/*.md: archived tickets
- .tasks/config.json: project settings (gitignored by default)
- .tasks/.state/: private tmux socket, session registry, dispatcher ledger, lock (gitignored)
- One ticket active at once, selected from Todo by ascending order
- N reviewer processes *within* that one ticket are allowed
- Old tmux sessions remain alive after closing their viewer; leaving ticket scope via Backlog/Archive ends ticket-owned processes

The original harness's access permissions, sandbox and authentication remain untouched. The CLI is started as the configured command; no permission-altering command flags are added.

## Known gaps and intentionally experimental features

This is the **initial scaffold**, not a claim of feature completeness:
- Embedded PTY via textual-tty is optional and still requires hands-on compatibility checking across all four CLIs. A tmux screen capture / input fallback is provided.
- Native CLI session-ID discovery and vendor-specific resume after tmux dies are not implemented yet. Live tmux sessions can be reattached.
- Reset, customizable per-reviewer session configuration dialog, explicit handoff editor, session group management UI and independent hook execution are incomplete. Ticket data fields are ready for extension.
- Configured transition hook is saved but **not executed** yet. No fake GitHub sync or side effects.
- Hung-but-alive CLIs are not killed based on elapsed time. The three-attempt policy applies to launch / unexpected exit; timeout detection is an explicit follow-up.
- Files edited *simultaneously by external tools* without honoring the lock can still conflict. Agent-facing taskbeat commands use a file lock and atomic replacement.
- There is no remote daemon or database. Session registry and state are local to the project.

Tests are included as examples/coverage seeds but have **not been run** as part of this initial implementation.

## Development

    python -m pip install -e '.[dev]'
    pytest

Design decisions and implementation roadmap: [docs/design/design.html](docs/design/design.html).
