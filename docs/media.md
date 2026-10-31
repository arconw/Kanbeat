# Demo recordings

The GIFs in `docs/media/` are captured from the real Kanbeat TUI by [`tools/record_demos.py`](../tools/record_demos.py). They are not mock-ups, hand-drawn UI or generated imagery.

## What is real

- A real `kanbeat supervisor` process runs for each demo project, started the same way the CLI starts it, with its normal Unix socket, lock and `.kanbeat/` directory.
- The TUI is the real `KanbeatApp`, driven headlessly with Textual's `run_test` pilot: real clicks and key presses, real IPC requests to the supervisor, real polling for changes. Each frame is the screen the Textual compositor produced at that moment (the same source `App.export_screenshot` uses), rasterised cell by cell with a monospace font.
- Tickets are real Markdown files in `.kanbeat/`. Every step is asserted against the supervisor or the filesystem while recording, for example that the ticket file landed in the expected column or that the terminal's status is `stopped`.
- Two clients in one recording are two separate `KanbeatApp` instances attached to the same supervisor.

## What is not real, and why

- **Four of the five recordings start no coding CLI and no model.** The supervisor of each of those demos runs with a restricted `PATH` that contains only a plain shell script named `codex`, `claude`, `opencode` and `pi`. That script prints a banner and echoes the lines you type. The banner is visible in the terminal frames. Those recordings are not evidence of live vendor CLI execution.
- **Tickets are moved by hand.** In [`board.gif`](media/board.gif) a person moves the card through Progress and Review/QA to Done with the card buttons (each manual step asks for confirmation) while the board is stopped. No agent does that work.
- **Play/Stop never dispatches.** In [`play-stop.gif`](media/play-stop.gif) the Todo column is empty, so pressing Play starts nothing. The recorder verifies that no worker log or runtime entry exists afterwards.
- **Time is compressed.** Frames are taken after each action settles; waiting times such as the 1.5 s refresh poll are not shown in real time.
- **One path is rewritten.** The status bar shows the project path. In the frames it is replaced by `~/demo-project` so the recording machine's directories are not published. The recorder also refuses to render any frame containing the home directory, the user name or the demo work directory.

## The recordings

| File | Shows |
| --- | --- |
| `board.gif` | Creating tickets from the TUI and from `kanbeat new` in a second terminal, the card strip and column menu, dependencies, manual moves to Done. |
| `play-stop.gif` | Play and Stop, task terminals disabled while the board runs, a second client joining, the first client closing without stopping anything. |
| `agents.gif` | **Real** Claude Code, OpenCode, Pi and Codex sessions, one per tiny buggy module, each fixing its bug and running its test in a throwaway git project, switching between them in Sessions and detaching. |
| `changes.gif` | The changes panel in a throwaway Git project: the files button counts, the panel opening beside a session, jumping to a file from the list, opening a new file, `[ ask ]` putting `@src/shapes.py` into the terminal, the source menu and Staged, and closing. |
| `sessions.gif` | Session groups, a new independent session, the supervisor-owned terminal, detaching, Board Play/Stop not touching the session, a second client reopening it, confirmed stop and record removal. |

## The real-agent recording

`agents.gif` is different: it runs the installed `claude`, `opencode`, `pi` and `codex` CLIs with the recording machine's own logins and models, so it costs real usage and its content is not deterministic. The recorder creates a throwaway git project with four small modules that have a one-line bug and a unit test, opens one session per agent, pastes a short request ("fix the bug and run the test"), waits until the module has changed and the screen is quiet, and asserts that every module was changed. Trust prompts are answered automatically.

Because real UIs print account details, the recorder rewrites and polices frames:

- `--mask TEXT=REPLACEMENT` rewrites text in every frame (the project path and the home directory form of it are always replaced by `~/demo-project`).
- `--forbid TEXT` makes the recorder refuse to render any frame that still contains `TEXT`. The home directory, the user name and the work directory are always forbidden.
- The published GIF masks the account email, the plan name, the usage-limit banner and the list of installed skills, and forbids the author's name and email tokens. Every frame was also inspected by eye before publishing.
- `--agent claude` (repeatable) records only some of the agents.

```sh
python tools/record_demos.py --workdir .tmp/demo --output .tmp/media --only agents \
    --mask "you@example.org=dev@example.com" --forbid you --forbid example.org
```

Real recordings can stall on an unexpected prompt or fail if a model does not change the file; the recorder then saves the partial recording as `agents-failed.gif` in the work directory.

## Reproducing them

The recorder runs from a development checkout and needs Pillow, a working `fc-match` (fontconfig) and the fonts Adwaita Mono and Noto Sans, or replacements passed with `--mono-font` and `--sans-font` (the mono font must cover box drawing, block and arrow glyphs).

```sh
python -m pip install -e . pillow
python tools/record_demos.py --workdir .tmp/demo --output docs/media
```

`--only board`, `--only play-stop`, `--only sessions`, `--only changes` or `--only agents` records a single GIF (the default run records every scenario except `agents`, which needs the real CLIs). The work directory must not exist yet or must have been created by the recorder (it refuses to reuse any other directory); demo projects inside it are replaced on each run. The recorder stops every supervisor it started and fails if any supervisor it started is still running. Session Stop is also asserted against its runtime status.

The recorder unsets `NO_COLOR` for its own process; with `NO_COLOR` set, Textual would render the frames without colours.

Recordings use a 128 by 30 cell terminal (196 by 32 for the board demo, because the six board columns are at least 30 columns wide each, 128 by 46 for the sessions demo and 160 by 40 for the changes demo, where the panel needs the extra width). In the play/stop demo the board is narrower than six columns and scrolls sideways.
