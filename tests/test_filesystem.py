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
    (board.root / ".state").mkdir()
    (board.root / ".state" / "garbage").write_text("ignored")
    assert board.find(t.id).column == "todo"
