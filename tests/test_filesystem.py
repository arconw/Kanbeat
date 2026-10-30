from pathlib import Path

import pytest

from kanbeat.filesystem import Board


def test_new_board_has_plain_markdown_and_default_backlog(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure()
    ticket = board.create("Implement auth", "Validate tokens")
    assert ticket.id == "001"
    assert ticket.column == "backlog"
    assert ticket.path.name == "001-implement-auth.md"
    assert ticket.body.startswith("# Implement auth")
    assert "---" not in ticket.body
    assert (board.root / "INSTRUCTIONS.md").exists()


def test_status_is_directory_not_yaml(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure()
    original = board.create("API", "Some description", "todo")
    moved = board.move(original.id, "review")
    assert moved.column == "review"
    assert moved.body == original.body
    assert not original.path.exists()


def test_tickets_are_listed_by_numeric_id_and_keep_their_filename_on_move(
    tmp_path: Path,
):
    board = Board(tmp_path)
    board.ensure()
    first = board.create("First", column="todo")
    second = board.create("Second", column="todo")
    board.create("Third", column="todo")
    board.move(first.id, "review")
    moved = board.move(first.id, "todo")
    assert moved.path.name == first.path.name == "001-first.md"
    tickets, errors = board.scan()
    assert not errors
    assert [t.id for t in tickets if t.column == "todo"] == ["001", "002", "003"]
    assert second.path.name == "002-second.md"


def test_legacy_order_prefixed_filename_is_reported_as_invalid(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure()
    (board.root / "todo" / "20-001-old.md").write_text("# Old", encoding="utf-8")
    tickets, errors = board.scan()
    assert not tickets
    assert errors == ["Invalid ticket filename: todo/20-001-old.md"]


def test_archive_preserves_uniqueness(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure()
    t = board.create("Archive me")
    board.move(t.id, "archive")
    assert board.find(t.id).column == "archive"
    n = board.create("Next task")
    assert n.id != t.id


def test_invalid_filename_is_visible_error(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure()
    (board.root / "todo" / "bad.md").write_text("# Bad", encoding="utf-8")
    _, errors = board.scan()
    assert any("Invalid ticket" in message for message in errors)
    with pytest.raises(ValueError, match="invalid tickets"):
        board.create("Another")


def test_all_tickets_ignore_state(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure()
    t = board.create("Keep status", column="todo")
    (board.root / ".state").mkdir(exist_ok=True)
    (board.root / ".state" / "garbage").write_text("ignored")
    assert board.find(t.id).column == "todo"


def test_deleted_highest_id_is_not_reused_after_state_loss(tmp_path):
    import shutil

    board = Board(tmp_path)
    board.ensure()
    ticket = board.create("Delete me")
    board.move(ticket.id, "archive")
    board.remove_archived(ticket.id)
    shutil.rmtree(board.root / ".state")
    assert int(Board(tmp_path).create("Next").id) > int(ticket.id)


def test_deleting_legacy_ticket_remembers_imported_ids(tmp_path):
    board = Board(tmp_path)
    board.ensure()
    (board.root / "archive" / "999-imported.md").write_text("# Imported")
    board.remove_archived("999")
    assert board.create("Next").id == "1000"


def test_id_reservations_survive_failed_ticket_create(tmp_path, monkeypatch):
    board = Board(tmp_path)
    board.ensure()
    original = Path.open

    def fail_create(path, mode="r", *args, **kwargs):
        if mode == "x":
            raise OSError("Injected create failure")
        return original(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_create)
        with pytest.raises(OSError, match="Injected"):
            board.create("Fail")
    assert board.create("Next").id == "002"


def test_concurrent_ticket_creates_have_unique_ids(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    board = Board(tmp_path)
    board.ensure()
    with ThreadPoolExecutor(max_workers=4) as pool:
        tickets = list(
            pool.map(lambda i: Board(tmp_path).create(f"Task {i}"), range(8))
        )
    assert len({t.id for t in tickets}) == 8
    assert len(board.scan()[0]) == 8


def test_invalid_id_ledger_fails_closed(tmp_path):
    board = Board(tmp_path)
    board.ensure()
    (board.root / "ids.json").write_text('{"last_id": "corrupted"}')
    with pytest.raises(ValueError, match="last_id"):
        board.create("Next")
    assert not board.scan()[0]


def test_blocked_by_line_and_section_are_parsed_outside_code_fences():
    from kanbeat.filesystem import blocker_ids

    assert blocker_ids("# T\n\nBlocked by: #2, #003 and #0040.\n") == [
        "002",
        "003",
        "040",
    ]
    assert blocker_ids("# T\n- **Blocked by:** #002\n") == ["002"]
    assert blocker_ids(
        "# T\n## Blocked by\n- #002 Title 5\n- 3\n\n## Other\n- 77\n"
    ) == ["002", "003"]
    assert blocker_ids("# T\n```\nBlocked by: #9\n```\nFixed in 2024.\n") == []


@pytest.mark.parametrize(
    "line",
    [
        "Blocked by: the 2 auth tickets",
        "Blocked by: none, see 3 notes",
        "Blocked by: 002, 003",
        "**Blocked by:** none",
        "Blocked by: #1.2 of the spec",
    ],
)
def test_free_text_after_blocked_by_does_not_create_blockers(line):
    from kanbeat.filesystem import blocker_ids

    assert blocker_ids("# T\n\n" + line + "\n") == []


def test_dependencies_derive_blocks_and_treat_done_and_archive_as_satisfied(
    tmp_path: Path,
):
    from kanbeat.filesystem import dependencies

    board = Board(tmp_path)
    board.ensure()
    first = board.create("First", column="done")
    archived = board.create("Archived", column="done")
    board.move(archived.id, "archive")
    open_one = board.create("Open", column="todo")
    waiting = board.create(
        "Waiting",
        f"# Waiting\n\nBlocked by: #{first.id}, #{archived.id}, #{open_one.id}, #099\n",
        column="todo",
    )
    links = dependencies(board.scan()[0])
    assert [
        (row["id"], row["satisfied"]) for row in links[waiting.id]["blocked_by"]
    ] == [(first.id, True), (archived.id, True), (open_one.id, False), ("099", False)]
    assert links[waiting.id]["blocked"]
    assert [row["id"] for row in links[open_one.id]["blocks"]] == [waiting.id]
    assert not links[open_one.id]["blocked"]


def test_instructions_file_is_upgraded_only_when_it_is_a_known_old_default(
    tmp_path: Path, monkeypatch
):
    import hashlib

    from kanbeat import filesystem
    from kanbeat.filesystem import INSTRUCTION

    board = Board(tmp_path)
    board.ensure()
    path = board.root / "INSTRUCTIONS.md"
    assert "Blocked by:" in path.read_text(encoding="utf-8")
    assert "<order>" not in INSTRUCTION
    old_default = "# Kanbeat ticket-writing guide\n\nan old default\n"
    path.write_text(old_default, encoding="utf-8")
    board.ensure()
    assert (
        path.read_text(encoding="utf-8")
        == "# Kanbeat ticket-writing guide\n\nan old default\n"
    )
    monkeypatch.setattr(
        filesystem,
        "LEGACY_INSTRUCTION_HASHES",
        frozenset({hashlib.sha256(old_default.encode()).hexdigest()}),
    )
    board.ensure()
    assert path.read_text(encoding="utf-8") == INSTRUCTION
    path.write_text("custom", encoding="utf-8")
    board.ensure()
    assert path.read_text(encoding="utf-8") == "custom"


@pytest.mark.parametrize("order", [0, 20, 90, 100, 110, 1000])
def test_legacy_order_prefixed_filenames_are_never_misparsed(tmp_path: Path, order):
    board = Board(tmp_path)
    board.ensure()
    (board.root / "todo" / f"{order:03d}-001-old.md").write_text(
        "# Old", encoding="utf-8"
    )
    tickets, errors = board.scan()
    assert not tickets
    assert len(errors) == 1 and errors[0].startswith("Invalid ticket filename")


def test_title_that_starts_with_a_number_still_gets_a_valid_filename(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure()
    ticket = board.create("2024 roadmap", column="todo")
    assert ticket.path.name == f"{ticket.id}-ticket-2024-roadmap.md"
    assert not board.scan()[1]
    assert board.create("3d model").path.name.endswith("-3d-model.md")
