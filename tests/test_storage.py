from pathlib import Path

import pytest

from taskbeat.storage import TicketStore


def test_unstructured_markdown_defaults_to_backlog(tmp_path: Path):
    store = TicketStore(tmp_path)
    (store.directory / "external.md").write_text(
        "# External idea\nThis was authored outside TaskBeat.\n", encoding="utf-8"
    )
    ticket = store.read("external")
    assert ticket.status == "backlog"
    assert ticket.title == "External idea"
    assert "authored outside" in ticket.body


def test_developer_cannot_skip_review(tmp_path: Path):
    store = TicketStore(tmp_path)
    ticket = store.create("Introduce auth", status="progress")
    changed, note = store.change_status(ticket.id, "done")
    assert changed.status == "review"
    assert note and "Review" in note


def test_parallel_reviewers_vote_without_overwriting(tmp_path: Path):
    store = TicketStore(tmp_path)
    ticket = store.create("Search endpoint", status="review")
    ticket.meta["reviewers"] = [
        {"id": "r1", "harness": "claude"},
        {"id": "r2", "harness": "codex"},
    ]
    store.save(ticket)
    begun = store.begin_review(ticket.id)
    iteration = begun.meta["review_iteration"]["id"]
    result = store.report_review(ticket.id, "r1", "approve", iteration)
    assert result.status == "review"
    assert result.meta["review_iteration"]["results"] == {"r1": "approve"}
    with pytest.raises(ValueError, match="different verdict"):
        store.report_review(ticket.id, "r1", "changes_requested", iteration)
    final = store.report_review(ticket.id, "r2", "changes_requested", iteration)
    assert final.status == "progress"
    assert "review_iteration" not in final.meta


def test_approval_and_stale_iteration_guard(tmp_path: Path):
    store = TicketStore(tmp_path)
    ticket = store.create("Documentation", status="review")
    ticket.meta["reviewers"] = [{"id": "r1", "harness": "pi"}]
    store.save(ticket)
    begun = store.begin_review(ticket.id)
    with pytest.raises(ValueError, match="Stale"):
        store.report_review(ticket.id, "r1", "approve", "wrong-iteration")
    final = store.report_review(
        ticket.id, "r1", "approve", begun.meta["review_iteration"]["id"]
    )
    assert final.status == "done"


def test_archive_is_physical_and_reversible(tmp_path: Path):
    store = TicketStore(tmp_path)
    ticket = store.create("Old idea", status="todo")
    store.archive(ticket.id)
    assert not store.path_for(ticket.id).exists()
    assert store.path_for(ticket.id, archived=True).exists()
    assert not any(t.id == ticket.id for t in store.list())
    store.restore(ticket.id, "backlog")
    assert store.read(ticket.id).status == "backlog"
    assert not store.path_for(ticket.id, archived=True).exists()


def test_unknown_metadata_is_preserved(tmp_path: Path):
    store = TicketStore(tmp_path)
    ticket = store.create("Something")
    ticket.meta["external_tool_field"] = {"custom": 42}
    store.save(ticket)
    store.change_status(ticket.id, "todo", manual=True)
    assert store.read(ticket.id).meta["external_tool_field"]["custom"] == 42


def test_path_traversal_rejected(tmp_path: Path):
    store = TicketStore(tmp_path)
    with pytest.raises(ValueError, match="Unsafe"):
        store.read("../outside")
