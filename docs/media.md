# Demo recordings

The GIFs in `docs/media/` are captured from the real Kanbeat TUI by [`tools/record_demos.py`](../tools/record_demos.py). They are not mock-ups, hand-drawn UI or generated imagery.

## What is real

- A real `kanbeat supervisor` process runs for each demo project, started the same way the CLI starts it, with its normal Unix socket, lock and `.kanbeat/` directory.
- The TUI is the real `KanbeatApp`, driven headlessly with Textual's `run_test` pilot: real clicks and key presses, real IPC requests to the supervisor, real polling for changes. Each frame is the screen the Textual compositor produced at that moment (the same source `App.export_screenshot` uses), rasterised cell by cell with a monospace font.
- Tickets are real Markdown files in `.kanbeat/`. Every step is asserted against the supervisor or the filesystem while recording, for example that the ticket file landed in the expected column or that the terminal's status is `stopped`.
- Two clients in one recording are two separate `KanbeatApp` instances attached to the same supervisor.

## What is not real, and why

- **No coding CLI and no model is ever started.** The supervisor of each demo runs with a restricted `PATH` that contains only a plain shell script named `codex`, `claude`, `opencode` and `pi`. That script prints a banner and echoes the lines you type. The banner is visible in the terminal frames. These recordings are not evidence of live vendor CLI execution; compatibility with real Codex CLI, Claude Code, OpenCode and Pi is still to be validated.
- **Tickets are moved by hand.** In [`board.gif`](media/board.gif) a person moves the card through Progress and Review/QA to Done with the ticket actions dialog while the board is stopped. No agent does that work.
- **Play/Stop never dispatches.** In [`play-stop.gif`](media/play-stop.gif) the Todo column is empty, so pressing Play starts nothing. The recorder verifies that no worker log or runtime entry exists afterwards.
- **Time is compressed.** Frames are taken after each action settles; waiting times such as the 1.5 s refresh poll are not shown in real time.
- **One path is rewritten.** The status bar shows the project path. In the frames it is replaced by `~/demo-project` so the recording machine's directories are not published. The recorder also refuses to render any frame containing the home directory, the user name or the demo work directory.
- The ticket viewer and a few cards show real layout quirks of the current TUI (for example the wrapped `···` card menu). They are left as they are.

## The recordings

| File | Shows |
| --- | --- |
| `board.gif` | Creating tickets from the TUI and from `kanbeat new` in a second terminal, ticket actions, reordering, manual moves to Done. |
| `play-stop.gif` | Play and Stop, task terminals disabled while the board runs, a second client joining, the first client closing without stopping anything. |
| `sessions.gif` | Session groups, a new independent session, the supervisor-owned terminal, detaching, Board Play/Stop not touching the session, a second client reopening it, confirmed stop and record removal. |

## Reproducing them

The recorder runs from a development checkout and needs Pillow, a working `fc-match` (fontconfig) and the fonts Adwaita Mono and Noto Sans, or replacements passed with `--mono-font` and `--sans-font` (the mono font must cover box drawing, block and arrow glyphs).

```sh
python -m pip install -e . pillow
python tools/record_demos.py --workdir .tmp/demo --output docs/media
```

`--only board`, `--only play-stop` or `--only sessions` records a single GIF. The work directory must not exist yet or must have been created by the recorder (it refuses to reuse any other directory); demo projects inside it are replaced on each run. The recorder stops every supervisor it started and fails if any supervisor it started is still running. Session Stop is also asserted against its runtime status.

The recorder unsets `NO_COLOR` for its own process; with `NO_COLOR` set, Textual would render the frames without colours.

Recordings use a 128 by 30 cell terminal (128 by 46 for the sessions demo). Smaller widths make the six board columns scroll horizontally.
