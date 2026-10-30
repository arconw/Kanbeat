# Kanbeat

A filesystem-native Kanban board for AI coding agents. A ticket is a Markdown file, its status is the directory it lives in, and one persistent local supervisor per project runs your own installed coding CLIs against the queue. Any number of terminal clients can attach to that supervisor.

> **Early release, not production-ready.** The board, supervisor, structured worker flow, transition hooks, persistent terminals and session groups are implemented and covered by automated tests. Codex, Claude and OpenCode protocol paths were exercised against deterministic local fake APIs, but not against real remote models or every vendor CLI version, and agents edit your working tree. Read [Limitations](#limitations) first.

![Kanbeat board: creating tickets and moving them between columns](docs/media/board.gif)

*Recorded from the real TUI against a real supervisor. The tickets are moved by hand; no agent ran. See [how the demos are made](docs/media.md).*

## What it does

- **Files are the board.** Tickets live in `.kanbeat/<column>/<id>-<slug>.md`. Moving a file moves the ticket, and you can edit tickets in any editor. No frontmatter, no hidden metadata.
- **One supervisor per project.** `kanbeat` starts or joins it. Closing a TUI never stops work, and several terminals can watch the same board.
- **Play and Stop.** Play lets the supervisor dispatch the Todo queue to your coding CLI. Stop interrupts task workers and leaves every ticket where it is; independent sessions keep running.
- **Developer, then Review/QA.** A developer turn moves a ticket to Review/QA, where zero or more reviewer slots with isolated contexts approve it or send it back with feedback.
- **Bring your own CLI.** Codex CLI, Claude Code, OpenCode and Pi run through their native modes with your own login, permissions and sandbox. Kanbeat embeds no agent and never overrides those settings. Model and effort choices from a project configuration session seed fresh Codex, Claude and OpenCode workers.
- **Native terminals.** The supervisor owns interactive terminals and shared harness runtimes. OpenCode, Codex and Claude task slots can be attached live while the board runs; Pi tasks require Board Stop. Independent sessions are organised into groups and survive detaching.
- **Transition hooks.** Optionally run a prompt in a fresh native session after each column change Kanbeat makes.
- **Scriptable.** `kanbeat new`, `list`, `move`, `play` and `stop` work from the shell.

## Demos

Both recordings use no coding CLI and no model: the Todo column stays empty, and terminals run a plain stand-in script, as the captions say.

![Play and Stop with two clients on one supervisor](docs/media/play-stop.gif)

*Play and Stop, and a second client joining the same supervisor. Closing the first client does not stop it.*

![Sessions: groups and a supervisor-owned terminal](docs/media/sessions.gif)

*Sessions: groups, a new independent session, a terminal that survives detaching, and the same session opened from a second client. The process in the terminal is a stand-in shell script, not a vendor CLI.*

## Supported platforms

| Platform | Status |
| --- | --- |
| Linux | Tested in CI on Python 3.11, 3.12, 3.13 and 3.14. |
| macOS | Tested in CI on Python 3.14. |
| WSL | A Linux runtime, but not yet tested on a Windows host. |
| Native Windows | Not supported: the supervisor uses POSIX APIs. A port is tracked in [#11](https://github.com/arconw/Kanbeat/issues/11). |

Kanbeat is not published on PyPI or Homebrew, and no prebuilt installers have been released yet. The source tree can build standalone Linux and macOS archives for a future release; see [Releases](docs/releases.md). Release and installer work is tracked in [#10](https://github.com/arconw/Kanbeat/issues/10).

## Prerequisites

- Python 3.11 or newer, and Git (the install command below fetches the release tag from GitHub).
- [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/) for an isolated install; plain `pip` in a virtual environment also works.
- At least one native CLI installed on your `PATH` and already authenticated: `codex`, `claude`, `opencode` or `pi`. Kanbeat does not log in for you.
- A terminal at least 80 × 24 characters; about 130 columns shows all six board columns without scrolling.

## Installation

Install the `v0.9.0` release tag from GitHub into an isolated environment:

```sh
uv tool install git+https://github.com/arconw/Kanbeat.git@v0.9.0
```

If the command is not on your PATH after installation, run `uv tool update-shell` and reopen your terminal.

Or, with pipx:

```sh
pipx install git+https://github.com/arconw/Kanbeat.git@v0.9.0
```

Use `pipx ensurepath` if needed. Without uv or pipx, use a virtual environment:

```sh
python -m venv ~/.venvs/kanbeat
~/.venvs/kanbeat/bin/python -m pip install git+https://github.com/arconw/Kanbeat.git@v0.9.0
~/.venvs/kanbeat/bin/kanbeat --help
```

Run `~/.venvs/kanbeat/bin/kanbeat`, or add that `bin` directory to your `PATH`. To work on Kanbeat itself, see [Development](docs/guide.md#development).

## First use

Run Kanbeat from the root of the project you want agents to work on. Use a clean Git branch or worktree: Kanbeat does not manage worktrees or undo changes.

```sh
cd /path/to/my-project
kanbeat
```

The first run creates `.kanbeat/` and starts a background supervisor for the project. Then:

1. Press **+ Ticket**, give it a title and put it in **Backlog** or **Todo**. Tickets are plain Markdown files; edit them in your editor to add detail.
2. Open **Settings** and check that the default harness is a CLI you have installed and logged in to.
3. Press **Play**. The supervisor picks the first Todo ticket and hands its path to the native CLI. **This starts a real agent that can edit your working tree and uses your CLI account.**
4. Press **Stop** at any time. Open `kanbeat` in another terminal in the same project to watch the same board.

The board starts stopped, so creating and moving tickets never starts an agent by itself. The same actions work from the shell:

```sh
kanbeat new "Implement auth" --column todo --description "Add token validation"
kanbeat list
kanbeat move 001 backlog
kanbeat play
kanbeat stop
```

Commit `.kanbeat/` with your project so the board travels with it, and add `.kanbeat/.state/` to your `.gitignore`: it holds local runtime data and logs.

## Limitations

- Early release. Behaviour, file layout and configuration may still change, and the old `.tasks` layout is not migrated.
- Only Linux and macOS are supported. WSL is untested and native Windows does not work.
- Live vendor protocols were exercised against local deterministic fake APIs, not against every real remote provider, login mode or CLI version. Event formats and session resume can change between vendor releases; check your installed CLI versions before running unattended.
- Live task attach is available for OpenCode, Codex and Claude. Pi task terminals still require Board Stop, and Pi model/effort capture is not implemented.
- Kanbeat does not manage Git: no worktrees, branches, resets or cleanups. Agents change your working tree directly.
- One ticket is active at a time. Only reviewers run in parallel.
- The TUI shows ticket Markdown read-only; edit the files externally.
- Attached views are monitor-only while a structured worker turn is active, so Kanbeat never runs two writers against one native session. Pi task terminals need a stopped board.
- Transition hooks are not exactly-once; make their actions safe to repeat.
- Native terminals do not forward mouse reporting or clipboard requests.

## Documentation

- [Guide](docs/guide.md): ticket format, flow, live updates, native CLI lifecycle, configuration, hooks and development.
- [Interactive terminals](docs/terminals.md): terminal and session lifecycle, groups and limits.
- [Design specification](docs/design/design.html): product and architecture design.
- [Demo recordings](docs/media.md): how the GIFs are captured and what they do and do not show.

## Community

Follow the author's channel, [Arco9 Lab on YouTube](https://www.youtube.com/@arco9-lab), which also covers [simpleVoiceover](https://github.com/arconw/simpleVoiceover). Bug reports and ideas go to [GitHub Issues](https://github.com/arconw/Kanbeat/issues).
