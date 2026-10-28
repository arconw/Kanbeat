from pathlib import Path

from taskbeat.dispatcher import Dispatcher
from taskbeat.sessions import SessionError
from taskbeat.storage import TicketStore


class FakeTmux:
    def __init__(self):
        self.alive: set[str] = set()
        self.prompts: list[tuple[str, str]] = []
        self.started: list[str] = []
        self.fail = False

    def target(self, ticket_id, role_id, harness):
        return f"{ticket_id}-{role_id}-{harness}"

    def is_alive(self, target):
        return target in self.alive

    def load_registry(self):
        return {}

    def start(self, target, harness, command, **kwargs):
        if self.fail:
            raise SessionError("no executable")
        created = target not in self.alive
        self.alive.add(target)
        self.started.append(target)
        return created

    def send(self, target, message):
        self.prompts.append((target, message))

    def capture(self, target, lines=180):
        return "previous output"


def test_only_first_todo_is_started(tmp_path: Path):
    store = TicketStore(tmp_path)
    first = store.create("First", status="todo")
    second = store.create("Second", status="todo")
    engine = Dispatcher(store)
    fake = FakeTmux()
    engine.tmux = fake
    engine.tick()
    assert store.read(first.id).status == "progress"
    assert store.read(second.id).status == "todo"
    assert len(fake.prompts) == 1
    engine.tick()
    assert len(fake.prompts) == 1


def test_never_dispatch_multiple_active_tickets(tmp_path: Path):
    store = TicketStore(tmp_path)
    store.create("First", status="progress")
    store.create("Second", status="review")
    notices = []
    engine = Dispatcher(store, lambda message, severity: notices.append(message))
    fake = FakeTmux()
    engine.tmux = fake
    engine.tick()
    assert not fake.prompts
    assert any("Multiple active" in notice for notice in notices)


def test_three_failed_launches_mark_ticket_failed(tmp_path: Path):
    store = TicketStore(tmp_path)
    ticket = store.create("Cannot launch", status="todo")
    engine = Dispatcher(store)
    fake = FakeTmux()
    fake.fail = True
    engine.tmux = fake
    for _ in range(3):
        engine.tick()
    assert store.read(ticket.id).status == "failed"


def test_no_reviewer_autocompletes_review(tmp_path: Path):
    store = TicketStore(tmp_path)
    ticket = store.create("No review configured", status="review")
    engine = Dispatcher(store)
    engine.tmux = FakeTmux()
    engine.tick()
    assert store.read(ticket.id).status == "done"
