import hashlib
import json

import pytest

from kanbeat.filesystem import INSTRUCTION, LEGACY_INSTRUCTION_HASHES, Board
from kanbeat.supervisor import Supervisor

OLD_INSTRUCTION = """# Kanbeat ticket-writing guide

This file is **not** loaded automatically as an agent instruction.
Read it only when explicitly asked to create or modify Kanbeat tickets.

- Create tickets in `.kanbeat/backlog/` by default.
- Ticket filenames: `<unique-id>-<slug>.md`.
- The number is a stable unique ID across board **and archive**.
- The slug is descriptive. Preserve the ID when renaming or moving a ticket.
- Ticket content is plain Markdown: one `# Title`, then task description and
  optional acceptance criteria. No frontmatter or internal runtime metadata.
- Use unused IDs. Never modify `.state/`, `config.json` or files belonging
  to another active ticket. Do not write completion/status markers to tickets.
- To queue work, move the file to `todo/`.
- Existing ticket content is user-owned. Do not insert harness transcripts.

## Dependencies between tickets

Kanbeat runs independent tickets **in parallel** (up to `max_parallel` in
`config.json`). Tickets that have no dependencies are all started at the same
time, so declare a dependency whenever one ticket needs the result of another
(same file or module, an API another ticket introduces, required sequence).

- Declare it inside the ticket that has to wait, on its own line right after
  the `# Title`, as a list of ticket IDs:

      Blocked by: #002, #003

- The IDs are the unique IDs from the filenames. A ticket in `todo/` is started
  only when every blocker is in `done/` or `archive/`; until then it stays in
  `todo/` and starts by itself later. Queue a whole batch in `todo/` and it
  works through itself in dependency order.
- Write dependencies only in the blocked ticket. Never write a "blocks" line:
  Kanbeat derives it and shows it in the TUI.
- A chain (`003` blocked by `002`, `002` blocked by `001`) is picked one ticket
  at a time, from the first. Never create cycles.
- Prefer few, real dependencies. Unneeded ones remove parallelism.
"""


@pytest.fixture
def supervisor(tmp_path):
    return Supervisor(tmp_path)


def run(supervisor, **request):
    import asyncio

    return asyncio.run(supervisor.dispatch(request))


def test_a_change_is_validated_saved_applied_and_recorded(supervisor, tmp_path):
    state = supervisor.runtime
    entry = state.change_config({"max_parallel": "5", "global_cp": "Be brief"}, "cli")
    assert [row["key"] for row in entry["changes"]] == ["max_parallel", "global_cp"]
    assert state.config["max_parallel"] == 5 and supervisor.parallel_limit() == 5
    saved = json.loads((tmp_path / ".kanbeat" / "config.json").read_text())
    assert saved["max_parallel"] == 5 and saved["global_cp"] == "Be brief"
    assert state.change_config({"max_parallel": 5}, "cli") is None
    assert len(state.config_history()) == 1


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"nope": "x"}, "Unknown setting"),
        ({"default_harness": "gpt"}, "default_harness must be one of"),
        ({"max_parallel": "0"}, "between 1 and"),
        ({"max_parallel": "many"}, "whole number"),
        ({"max_parallel": "99"}, "between 1 and"),
        ({"transition_hook.harness": "gpt"}, "empty or one of"),
        ({"global_cp": "x" * 20001}, "longer than"),
        ({"global_cp": 5}, "must be text"),
    ],
)
def test_invalid_changes_are_refused_and_change_nothing(supervisor, changes, message):
    with pytest.raises(ValueError, match=message):
        supervisor.runtime.change_config(changes, "cli")
    assert supervisor.runtime.config_history() == []


def test_revert_restores_old_values_and_is_itself_recorded(supervisor):
    state = supervisor.runtime
    state.change_config({"global_cp": "one"}, "cli")
    second = state.change_config({"global_cp": "two", "max_parallel": 4}, "tui")
    undo, skipped = state.revert_config(second["id"], "tui revert")
    assert skipped == [] and undo["source"] == "tui revert"
    assert state.config["global_cp"] == "one" and state.config["max_parallel"] == 3
    with pytest.raises(ValueError, match="already undone"):
        state.revert_config(second["id"], "tui revert")
    with pytest.raises(ValueError, match="No config change #99"):
        state.revert_config(99, "tui revert")
    redo, _ = state.revert_config(undo["id"], "tui revert")
    assert redo["id"] == 4
    assert state.config["global_cp"] == "two" and state.config["max_parallel"] == 4


def test_history_is_newest_first_truncated_and_capped_at_a_hundred(supervisor):
    state = supervisor.runtime
    for number in range(130):
        state.change_config({"global_cp": f"{number} " + "x" * 700}, "cli")
    assert len(state.config_history()) == 100
    view = state.history_view(5)
    assert [row["id"] for row in view["history"]] == [130, 129, 128, 127, 126]
    assert view["total"] == 100
    assert len(view["history"][0]["changes"][0]["new"]) == 601
    assert len(state.history_view(500)["history"]) == 100
    assert len(state.config_history()[-1]["changes"][0]["new"]) > 700


def test_tickets_are_not_part_of_the_history(supervisor):
    supervisor.board.create("A ticket")
    supervisor.board.create("Another")
    assert supervisor.runtime.config_history() == []


def test_saving_from_the_settings_page_is_recorded_without_a_notice(supervisor):
    run(
        supervisor,
        op="save_config",
        values={"global_cp": "From the TUI", "unknown": "ignored"},
    )
    [entry] = supervisor.runtime.config_history()
    assert entry["source"] == "tui"
    assert [row["key"] for row in entry["changes"]] == ["global_cp"]
    assert supervisor.runtime.data.get("notices", []) == []


def test_command_line_changes_show_a_notice_and_can_be_read_back(supervisor):
    result = run(
        supervisor,
        op="config_set",
        changes={"transition_hook.prompt": "Ping me"},
        source="cli",
    )
    assert result["entry"]["keys"] == ["transition_hook.prompt"]
    [notice] = supervisor.runtime.data["notices"]
    assert notice["level"] == "warning"
    assert "transition_hook.prompt" in notice["message"]
    assert f"kanbeat config revert {result['entry']['id']}" in notice["message"]
    assert "Ping me" not in notice["message"]
    assert (
        run(supervisor, op="config_get", key="transition_hook.prompt")["value"]
        == "Ping me"
    )
    assert (
        run(supervisor, op="config_get")["config"]["transition_hook"]["prompt"]
        == "Ping me"
    )
    assert run(supervisor, op="snapshot")["config_seq"] == 1
    with pytest.raises(ValueError, match="Unknown setting"):
        run(supervisor, op="config_get", key="nope")
    undo = run(supervisor, op="config_revert", id=result["entry"]["id"], source="cli")
    assert undo["entry"]["id"] == 2
    assert supervisor.runtime.config["transition_hook"]["prompt"] == ""
    assert supervisor.runtime.config_history()[-1]["source"] == "cli revert"
    assert len(supervisor.runtime.data["notices"]) == 2


def test_the_history_survives_a_restart(tmp_path):
    first = Supervisor(tmp_path)
    first.runtime.change_config({"max_parallel": 2}, "cli")
    again = Supervisor(tmp_path)
    assert again.runtime.history_view(5)["total"] == 1
    assert again.runtime.config["max_parallel"] == 2


def test_the_new_instructions_replace_the_previous_unedited_ones(tmp_path):
    assert (
        hashlib.sha256(OLD_INSTRUCTION.encode()).hexdigest()
        in LEGACY_INSTRUCTION_HASHES
    )
    board = Board(tmp_path)
    board.root.mkdir()
    (board.root / "INSTRUCTIONS.md").write_text(OLD_INSTRUCTION, encoding="utf-8")
    board.ensure()
    assert (board.root / "INSTRUCTIONS.md").read_text(encoding="utf-8") == INSTRUCTION
    assert "## Managing the board" in INSTRUCTION
    assert "kanbeat watch --json" in INSTRUCTION
    edited = OLD_INSTRUCTION + "\nMy own rule.\n"
    (board.root / "INSTRUCTIONS.md").write_text(edited, encoding="utf-8")
    board.ensure()
    assert (board.root / "INSTRUCTIONS.md").read_text(encoding="utf-8") == edited


def test_a_revert_keeps_settings_that_changed_again_later(supervisor):
    state = supervisor.runtime
    first = state.change_config({"developer_prompt": "B", "max_parallel": 4}, "cli")
    state.change_config({"developer_prompt": "C"}, "cli")
    entry, skipped = state.revert_config(first["id"], "cli revert")
    assert skipped == ["developer_prompt"]
    assert [row["key"] for row in entry["changes"]] == ["max_parallel"]
    assert state.config["developer_prompt"] == "C"
    assert state.config["max_parallel"] == 3
    only = state.change_config({"developer_prompt": "D"}, "cli")
    state.change_config({"developer_prompt": "E"}, "cli")
    with pytest.raises(ValueError, match="changed again later"):
        state.revert_config(only["id"], "cli revert")
    assert state.config["developer_prompt"] == "E"


def test_a_revert_that_keeps_settings_says_so(supervisor):
    first = run(
        supervisor, op="config_set", changes={"global_cp": "B", "max_parallel": 4}
    )
    run(supervisor, op="config_set", changes={"global_cp": "C"})
    result = run(supervisor, op="config_revert", id=first["entry"]["id"], source="cli")
    assert result["entry"]["skipped"] == ["global_cp"]
    assert any(
        "except global_cp" in note["message"]
        for note in supervisor.runtime.data["notices"]
    )


@pytest.mark.parametrize(
    "request_,message",
    [
        ({"op": "config_history", "limit": 0}, "limit must be between"),
        ({"op": "config_history", "limit": 101}, "limit must be between"),
        ({"op": "config_history", "limit": "many"}, "limit must be a whole number"),
        ({"op": "config_revert"}, "id must be a whole number"),
        ({"op": "config_revert", "id": "x"}, "id must be a whole number"),
        ({"op": "config_revert", "id": 0}, "id must be between"),
    ],
)
def test_bad_numbers_get_a_clear_error(supervisor, request_, message):
    with pytest.raises(ValueError, match=message):
        run(supervisor, **request_)
