import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap

import pytest

from kanbeat.host_process import RuntimeIdentity
from kanbeat.native import process_identity
from kanbeat.opencode_host import (
    OpenCodeHostError,
    OpenCodeHttpError,
    OpenCodeModel,
    OpenCodeServeHost,
    OpenCodeTimeout,
    parse_session_metadata,
    parse_statuses,
    validate_session_id,
    viewer_argv,
    worker_argv,
)

SESSION = "ses_e79a02ed0ffec5zXTyfunEfD4S"
LEAK = "SECRET-RAW-LOG-LINE"

FAKE_OPENCODE = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys, threading, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from socketserver import TCPServer

    LOG = os.environ["FAKE_LOG"]
    MODE = os.environ.get("FAKE_MODE", "ok")
    PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
    SESSIONS = {{}}
    STATUS = {{}}

    def log(entry):
        with open(LOG, "a") as handle:
            handle.write(json.dumps(entry) + "\\n")

    class Server(ThreadingHTTPServer):
        def server_bind(self):
            TCPServer.server_bind(self)
            host, port = self.server_address[:2]
            self.server_name = host
            self.server_port = port

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body=None):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def handle_any(self, method):
            import base64
            expected = "Basic " + base64.b64encode(("opencode:" + PASSWORD).encode()).decode()
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            log({{"method": method, "path": self.path, "body": body,
                 "auth": self.headers.get("Authorization") == expected}})
            if MODE == "redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/steal")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.headers.get("Authorization") != expected:
                return self.reply(401, {{"error": "unauthorized"}})
            path = self.path.split("?")[0]
            if method == "GET" and path == "/session/status":
                return self.reply(200, STATUS)
            if method == "POST" and path == "/session":
                sid = "{session}"
                SESSIONS[sid] = {{"id": sid, "directory": "/work", "title": "TITLE", "cost": 1,
                                 "summary": {{"files": 1}}, "apiKey": "SECRET-KEY"}}
                if body and body.get("model"):
                    SESSIONS[sid]["model"] = body["model"]
                return self.reply(200, SESSIONS[sid])
            parts = path.strip("/").split("/")
            if method == "GET" and parts[0] == "session" and len(parts) == 2:
                return self.reply(200, SESSIONS[parts[1]]) if parts[1] in SESSIONS else self.reply(404, {{}})
            if method == "POST" and parts[:2] == ["api", "session"] and parts[-1] == "model":
                SESSIONS[parts[2]]["model"] = body["model"]
                return self.reply(204)
            if method == "POST" and parts[-1] == "abort":
                STATUS.pop(parts[1], None)
                return self.reply(200, True)
            return self.reply(404, {{}})

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

    def main():
        assert sys.argv[1:] == ["serve", "--hostname", "127.0.0.1", "--port", "0"], sys.argv
        log({{"pid": os.getpid(), "argv": sys.argv[1:], "pw_in_argv": PASSWORD in " ".join(sys.argv)}})
        if MODE == "exit":
            print("boom")
            sys.exit(2)
        if MODE == "silent":
            print("nothing useful", flush=True)
            time.sleep(3600)
        server = Server(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        if MODE == "noisy":
            sys.stdout.write("x" * 200000 + "\\n")
        print("{leak}", flush=True)
        print("opencode server listening on http://127.0.0.1:%d" % port, flush=True)
        def chatter():
            while True:
                print("{leak} chatter", flush=True)
                time.sleep(0.05)
        threading.Thread(target=chatter, daemon=True).start()
        server.serve_forever()

    main()
    """
)


@pytest.fixture
def short_dir():
    root = Path(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="kbo", dir=root))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def fake_opencode(short_dir):
    path = short_dir / "opencode"
    path.write_text(
        FAKE_OPENCODE.format(python=sys.executable, session=SESSION, leak=LEAK)
    )
    path.chmod(0o755)
    return path


def make_host(short_dir, fake_opencode, mode="ok", **kwargs):
    env = {
        "PATH": os.environ["PATH"],
        "FAKE_LOG": str(short_dir / "log"),
        "FAKE_MODE": mode,
    }
    return OpenCodeServeHost(
        short_dir, env=env, executable=str(fake_opencode), **kwargs
    )


def logged(short_dir):
    path = short_dir / "log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def alive(pid):
    try:
        state = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "stat="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(state) and not state.startswith("Z")


def command_line(pid):
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.exists():
        return proc.read_bytes().decode()
    return subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "command="],
        text=True,
        stderr=subprocess.DEVNULL,
    )


def test_models_validate_provider_variant_strings():
    model = OpenCodeModel("anthropic", "claude/sonnet-9", "max-thinking")
    assert model.flag == "anthropic/claude/sonnet-9"
    assert model.to_payload() == {
        "id": "claude/sonnet-9",
        "providerID": "anthropic",
        "variant": "max-thinking",
    }
    for args in [
        ("", "m"),
        ("p/x", "m"),
        ("p", "-m"),
        ("p", "m", "--x"),
        ("p", "m n"),
        ("p", "m\n"),
    ]:
        with pytest.raises(OpenCodeHostError):
            OpenCodeModel(*args)


def test_argv_builders_validate_ids_and_url():
    url = "http://127.0.0.1:5000"
    assert viewer_argv(url, SESSION) == [
        "opencode",
        "attach",
        url,
        "--session",
        SESSION,
    ]
    argv = worker_argv(
        url, SESSION, prompt="do it", model=OpenCodeModel("p", "m", "high")
    )
    assert argv == [
        "opencode",
        "run",
        "--attach",
        url,
        "--session",
        SESSION,
        "--format",
        "json",
        "--model",
        "p/m",
        "--variant",
        "high",
        "--",
        "do it",
    ]
    for bad_url in [
        "http://example.com:5000",
        "https://127.0.0.1:5",
        "http://user:pw@127.0.0.1:5",
        "http://127.0.0.1",
        "http://127.0.0.1:5/x",
    ]:
        with pytest.raises(OpenCodeHostError):
            viewer_argv(bad_url, SESSION)
    for bad in ["--evil", "ses_", "ses_a b", "abc", ""]:
        with pytest.raises(OpenCodeHostError):
            validate_session_id(bad)
    assert worker_argv(url, SESSION, prompt="--auto")[-2:] == ["--", "--auto"]
    with pytest.raises(OpenCodeHostError, match="too large"):
        worker_argv(url, SESSION, prompt="x" * (64 * 1024 + 1))


@pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode not installed")
def test_real_opencode_honors_prompt_separator():
    argv = worker_argv("http://127.0.0.1:9", SESSION, prompt="--auto")
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )
    output = result.stdout + result.stderr
    assert "Unknown argument" not in output
    assert result.returncode != 0


def test_parsers_drop_unlisted_fields():
    metadata = parse_session_metadata(
        {
            "id": SESSION,
            "directory": "/w",
            "title": "T",
            "apiKey": "K",
            "model": {"id": "m", "providerID": "p", "variant": "v", "extra": 1},
        }
    )
    assert metadata.to_dict() == {
        "session_id": SESSION,
        "directory": "/w",
        "provider": "p",
        "model": "m",
        "variant": "v",
    }
    assert "K" not in repr(metadata)
    assert (
        parse_session_metadata(
            {"id": SESSION, "model": {"id": "-bad", "providerID": "p"}}
        ).model
        is None
    )
    statuses = parse_statuses(
        {
            SESSION: {"type": "retry", "message": "secret", "attempt": 2},
            "junk": {"type": "busy"},
            "ses_x": 5,
        }
    )
    assert list(statuses) == [SESSION] and statuses[SESSION].status == "retry"
    with pytest.raises(OpenCodeHostError):
        parse_session_metadata({"id": "x"})
    with pytest.raises(OpenCodeHostError):
        parse_statuses([])


@pytest.mark.asyncio
async def test_lifecycle_credentials_and_filtering(short_dir, fake_opencode, capfd):
    host = make_host(short_dir, fake_opencode)
    identity = await host.start()
    password = host.child_env()["OPENCODE_SERVER_PASSWORD"]
    try:
        assert identity.kind == "opencode"
        assert identity.endpoint == host.url
        assert process_identity(identity.pid) == identity.identity
        assert os.getpgid(identity.pid) == identity.pid
        assert RuntimeIdentity.from_dict(identity.to_dict()) == identity
        cmdline = command_line(identity.pid)
        assert password not in cmdline
        assert logged(short_dir)[0]["pw_in_argv"] is False
        assert password not in json.dumps(identity.to_dict())
        for argv in (host.viewer_argv(SESSION), host.worker_argv(SESSION, prompt="p")):
            assert password not in " ".join(argv)
        env = host.child_env()
        assert env["OPENCODE_SERVER_USERNAME"] == "opencode"
        assert len(password) >= 32

        created = await host.create_session(
            directory=short_dir, model=OpenCodeModel("p", "m", "high")
        )
        assert created.session_id == SESSION
        assert created.model == OpenCodeModel("p", "m", "high")
        assert "TITLE" not in repr(created) and "SECRET-KEY" not in repr(created)
        create_call = [e for e in logged(short_dir) if e.get("method") == "POST"][0]
        assert create_call["path"].startswith("/session?directory=")
        assert create_call["body"] == {
            "model": {"id": "m", "providerID": "p", "variant": "high"}
        }

        changed = await host.set_session_model(SESSION, OpenCodeModel("q", "n"))
        assert changed.model == OpenCodeModel("q", "n")
        assert changed.to_dict()["variant"] is None
        assert await host.session_status(SESSION) == "idle"
        await host.abort(SESSION)
        with pytest.raises(OpenCodeHttpError) as error:
            await host.read_session("ses_missing")
        assert error.value.status == 404

        calls = logged(short_dir)[1:]
        assert all(call["auth"] for call in calls)
        paths = {call["path"].split("?")[0] for call in calls}
        assert not any(
            p.startswith(("/config", "/provider", "/auth"))
            or "message" in p
            or "export" in p
            for p in paths
        )
        await asyncio.sleep(0.3)
        captured = capfd.readouterr()
        assert LEAK not in captured.out + captured.err
    finally:
        await host.close()
    assert not alive(identity.pid)
    with pytest.raises(OpenCodeHostError):
        host.url
    await host.close()


@pytest.mark.asyncio
async def test_noisy_startup_output_is_drained(short_dir, fake_opencode):
    async with make_host(short_dir, fake_opencode, mode="noisy") as host:
        assert host.url.startswith("http://127.0.0.1:")
        for _ in range(5):
            await host.statuses()
            await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_startup_timeout_exit_and_bad_auth_clean_up(short_dir, fake_opencode):
    host = make_host(short_dir, fake_opencode, mode="silent", startup_timeout=0.5)
    with pytest.raises(OpenCodeTimeout):
        await host.start()
    pid = logged(short_dir)[0]["pid"]
    assert not alive(pid)

    (short_dir / "log").unlink()
    host = make_host(short_dir, fake_opencode, mode="exit")
    with pytest.raises(OpenCodeHostError, match="exited during startup"):
        await host.start()
    assert not alive(logged(short_dir)[0]["pid"])

    (short_dir / "log").unlink()
    host = make_host(short_dir, fake_opencode, mode="redirect")
    with pytest.raises(OpenCodeHttpError) as error:
        await host.start()
    assert error.value.status == 302
    assert not alive(logged(short_dir)[0]["pid"])
    with pytest.raises(OpenCodeHostError):
        await host.start()


@pytest.mark.asyncio
async def test_host_ignores_preexisting_credentials_in_env(short_dir, fake_opencode):
    env = {
        "PATH": os.environ["PATH"],
        "FAKE_LOG": str(short_dir / "log"),
        "OPENCODE_SERVER_PASSWORD": "user-set",
        "OPENCODE_SERVER_USERNAME": "someone",
    }
    host = OpenCodeServeHost(short_dir, env=env, executable=str(fake_opencode))
    assert host.child_env()["OPENCODE_SERVER_PASSWORD"] != "user-set"
    assert host.child_env()["OPENCODE_SERVER_USERNAME"] == "opencode"
    async with host:
        await host.statuses()
