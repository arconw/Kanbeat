from pathlib import Path

import pytest

from kanbeat.filesystem import Board


def test_new_board_has_plain_markdown_and_default_backlog(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure()
    ticket = board.create("Implement auth", "Validate tokens")
    assert ticket.id == "001"
    assert ticket.column == "backlog"
    assert ticket.path.name.startswith("0-001-implement-auth")
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


def test_numeric_order_and_stable_id(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure()
    a = board.create("First", column="todo")
    b = board.create("Second", column="todo")
    board.reorder(b.id, -1)
    tickets, errors = board.scan()
    assert not errors
    ordered = [t.id for t in tickets if t.column == "todo"]
    assert ordered == [b.id, a.id]


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


@pytest.mark.parametrize("fail_at", [1, 2])
def test_interrupted_reorder_keeps_all_tickets_visible(tmp_path, monkeypatch, fail_at):
    board = Board(tmp_path)
    board.ensure()
    tickets = [
        board.create(title, column="todo") for title in ("First", "Second", "Third")
    ]
    original = {t.id: t.body for t in tickets}
    rename = Path.rename
    calls = 0

    def failing_rename(source, destination):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("Injected rename failure")
        result = rename(source, destination)
        visible, errors = board.scan()
        assert not errors
        assert {t.id: t.body for t in visible} == original
        return result

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(OSError, match="Injected"):
        board.reorder(tickets[1].id, -1)
    visible, errors = Board(tmp_path).scan()
    assert not errors
    assert {t.id: t.body for t in visible} == original


def test_reorder_handles_equal_order_numbers(tmp_path):
    board = Board(tmp_path)
    board.ensure()
    first = board.create("First", column="todo")
    second = board.create("Second", column="todo")
    second.path.rename(second.path.with_name("0-002-second.md"))
    board.reorder(second.id, -1)
    tickets, errors = board.scan()
    assert not errors
    assert [t.id for t in tickets] == [second.id, first.id]


def test_legacy_interrupted_reorder_is_reported(tmp_path):
    board = Board(tmp_path)
    board.ensure()
    ticket = board.create("First", column="todo")
    stranded = ticket.path.with_name(".kanbeat-rename-001")
    ticket.path.rename(stranded)
    _, errors = board.scan()
    assert any("Interrupted legacy reorder" in error for error in errors)
    assert stranded.read_text() == ticket.body
    with pytest.raises(ValueError, match="invalid tickets"):
        board.create("Next")


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
    (board.root / "archive" / "0-999-imported.md").write_text("# Imported")
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


def test_reorder_only_renames_swapped_tickets(tmp_path):
    board = Board(tmp_path)
    board.ensure()
    tickets = [
        board.create(title, column="todo") for title in ("First", "Second", "Third")
    ]
    last = tickets[-1].path.with_name("1000-003-third.md")
    tickets[-1].path.rename(last)
    board.reorder(tickets[1].id, -1)
    assert last.exists()
    assert [t.id for t in board.scan()[0]] == ["002", "001", "003"]


def test_external_move_during_reorder_does_not_lose_tickets(tmp_path, monkeypatch):
    board = Board(tmp_path)
    board.ensure()
    first = board.create("First", column="todo")
    second = board.create("Second", column="todo")
    rename = Path.rename
    moved = False

    def move_during_rename(source, target):
        nonlocal moved
        result = rename(source, target)
        if not moved:
            moved = True
            rename(first.path, board.root / "archive" / first.path.name)
        return result

    monkeypatch.setattr(Path, "rename", move_during_rename)
    with pytest.raises(FileNotFoundError):
        board.reorder(second.id, -1)
    tickets, errors = board.scan()
    assert not errors
    assert {t.id: t.body for t in tickets} == {
        first.id: first.body,
        second.id: second.body,
    }
    assert board.find(first.id).column == "archive"
