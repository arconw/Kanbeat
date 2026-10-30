from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
from typing import Any, AsyncIterator, Iterable

from websockets.asyncio.client import connect as ws_connect, unix_connect
from websockets.exceptions import WebSocketException

from .host_process import (
    HostError,
    RuntimeIdentity,
    identity_of,
    spawn_owned_group,
    stop_owned,
)
from .ipc import MAX_SOCKET_BYTES, prepare_socket_directory

THREAD_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_VALUE_CHARS = 200
MAX_LOADED_PAGES = 20
CONNECT_ATTEMPT_SECONDS = 3.0
METHOD_NOT_FOUND = -32601
TOKEN_ENV = "KANBEAT_CODEX_REMOTE_TOKEN"
WS_LISTENING = re.compile(rb"listening on: (ws://127\.0\.0\.1:([0-9]{1,5}))\s")
WS_URL = re.compile(r"^ws://127\.0\.0\.1:([0-9]{1,5})$")
STARTUP_TAIL_BYTES = 8192
MISSING_THREAD = re.compile(
    r"^(?:thread not found(?:: [0-9a-f-]{36})?"
    r"|no rollout found for thread id [0-9a-f-]{36})$",
    re.I,
)


class CodexHostError(HostError):
    pass


class CodexDisconnected(CodexHostError):
    pass


class CodexTimeout(CodexHostError, TimeoutError):
    pass


class CodexRpcError(CodexHostError):
    def __init__(self, code: Any, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def thread_missing(error: BaseException | str) -> bool:
    text = error.message if isinstance(error, CodexRpcError) else str(error)
    return bool(MISSING_THREAD.search(text))


def validate_thread_id(value: Any) -> str:
    if not isinstance(value, str) or not THREAD_ID.match(value):
        raise CodexHostError("Invalid Codex thread id")
    return value.lower()


def remote_url(target: Path | str) -> str:
    if isinstance(target, str) and target.startswith("ws://"):
        match = WS_URL.match(target)
        if match is None or not 0 < int(match.group(1)) < 65536:
            raise CodexHostError("Codex remote must be ws://127.0.0.1:PORT")
        return target
    text = os.fspath(target)
    if (
        not os.path.isabs(text)
        or CONTROL.search(text)
        or any(c.isspace() for c in text)
    ):
        raise CodexHostError("Codex socket path must be absolute without whitespace")
    if len(os.fsencode(text)) > MAX_SOCKET_BYTES:
        raise CodexHostError("Codex socket path exceeds the Unix socket limit")
    return "unix://" + text


def remote_argv(executable: str, target: Path | str, auth_env: str | None) -> list[str]:
    argv = [executable, "--remote", remote_url(target)]
    if auth_env is not None:
        argv += ["--remote-auth-token-env", auth_env]
    return argv


def viewer_argv(
    target: Path | str,
    thread_id: str,
    executable: str = "codex",
    auth_env: str | None = None,
) -> list[str]:
    return remote_argv(executable, target, auth_env) + [
        "resume",
        validate_thread_id(thread_id),
    ]


def tui_argv(
    target: Path | str,
    *,
    thread_id: str | None = None,
    prompt: str | None = None,
    executable: str = "codex",
    auth_env: str | None = None,
) -> list[str]:
    argv = remote_argv(executable, target, auth_env)
    if thread_id is not None:
        argv += ["resume", validate_thread_id(thread_id)]
    if prompt is not None:
        if not prompt or prompt.startswith("-") or "\x00" in prompt:
            raise CodexHostError("Invalid initial prompt for the Codex TUI")
        argv.append(prompt)
    return argv


def _clean(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) > MAX_VALUE_CHARS or CONTROL.search(value):
        return None
    return value


@dataclass(frozen=True)
class CodexThreadMetadata:
    thread_id: str
    model: str | None
    model_provider: str | None
    reasoning_effort: str | None
    status: str | None
    active_flags: tuple[str, ...]
    cwd: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "model": self.model,
            "model_provider": self.model_provider,
            "reasoning_effort": self.reasoning_effort,
            "status": self.status,
            "active_flags": list(self.active_flags),
            "cwd": self.cwd,
        }


def parse_thread_metadata(payload: Any) -> CodexThreadMetadata:
    if not isinstance(payload, dict) or not isinstance(payload.get("thread"), dict):
        raise CodexHostError("Codex response has no thread")
    thread = payload["thread"]

    def pick(key: str) -> str | None:
        value = _clean(thread.get(key))
        return value if value is not None else _clean(payload.get(key))

    status = thread.get("status")
    status_type = _clean(status.get("type")) if isinstance(status, dict) else None
    flags = status.get("activeFlags") if isinstance(status, dict) else None
    active_flags = tuple(
        flag for flag in (flags if isinstance(flags, list) else []) if _clean(flag)
    )
    return CodexThreadMetadata(
        thread_id=validate_thread_id(thread.get("id")),
        model=pick("model"),
        model_provider=pick("modelProvider"),
        reasoning_effort=pick("reasoningEffort"),
        status=status_type,
        active_flags=active_flags,
        cwd=pick("cwd"),
    )


@dataclass(frozen=True)
class CodexNotification:
    method: str
    params: dict[str, Any]
    thread_id: str | None


def notification_thread_id(method: str, params: dict[str, Any]) -> str | None:
    direct = params.get("threadId")
    if isinstance(direct, str):
        return direct.lower()
    thread = params.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("id"), str):
        return thread["id"].lower()
    return None


_CLOSED = object()


class CodexSubscription:
    def __init__(
        self,
        client: "CodexRpcClient",
        thread_id: str | None,
        methods: frozenset[str] | None,
        maxsize: int,
    ):
        self.thread_id = thread_id
        self.methods = methods
        self.dropped = 0
        self._client = client
        self._queue: deque[Any] = deque()
        self._maxsize = maxsize
        self._ready = asyncio.Event()
        self._closed = False

    def accepts(self, notification: CodexNotification) -> bool:
        if self.thread_id is not None and notification.thread_id != self.thread_id:
            return False
        return self.methods is None or notification.method in self.methods

    def push(self, item: Any) -> None:
        if self._closed:
            return
        if item is not _CLOSED and len(self._queue) >= self._maxsize:
            self._queue.popleft()
            self.dropped += 1
        self._queue.append(item)
        self._ready.set()

    def shutdown(self) -> None:
        self.push(_CLOSED)
        self._closed = True

    def close(self) -> None:
        self._client.unsubscribe(self)
        self._queue.clear()
        self._queue.append(_CLOSED)
        self._closed = True
        self._ready.set()

    async def get(self, timeout: float | None = None) -> CodexNotification:
        async def take() -> CodexNotification:
            while True:
                if self._queue:
                    item = self._queue[0]
                    if item is _CLOSED:
                        raise CodexDisconnected("Codex subscription closed")
                    self._queue.popleft()
                    return item
                self._ready.clear()
                await self._ready.wait()

        if timeout is None:
            return await take()
        try:
            return await asyncio.wait_for(take(), timeout)
        except asyncio.TimeoutError as error:
            raise CodexTimeout("Timed out waiting for a Codex notification") from error

    async def __aiter__(self) -> AsyncIterator[CodexNotification]:
        while True:
            try:
                yield await self.get()
            except CodexDisconnected:
                return

    def __enter__(self) -> "CodexSubscription":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class CodexRpcClient:
    def __init__(self, websocket: Any, *, default_timeout: float = 30.0):
        self._ws = websocket
        self._default_timeout = default_timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: set[CodexSubscription] = set()
        self._reader: asyncio.Task[None] | None = None
        self._outbound: set[asyncio.Task[None]] = set()
        self._closed = False
        self._released = False
        self.user_agent: str | None = None

    @classmethod
    async def connect(
        cls,
        socket_path: Path,
        *,
        client_name: str = "kanbeat",
        client_version: str = "0",
        timeout: float = 30.0,
    ) -> "CodexRpcClient":
        remote_url(socket_path)
        return await cls._open(
            unix_connect(
                os.fspath(socket_path),
                "ws://localhost/",
                max_size=MAX_FRAME_BYTES,
                ping_interval=None,
                open_timeout=timeout,
            ),
            client_name,
            client_version,
            timeout,
        )

    @classmethod
    async def connect_ws(
        cls,
        url: str,
        token: str,
        *,
        client_name: str = "kanbeat",
        client_version: str = "0",
        timeout: float = 30.0,
    ) -> "CodexRpcClient":
        remote_url(url)
        return await cls._open(
            ws_connect(
                url,
                additional_headers={"Authorization": f"Bearer {token}"},
                max_size=MAX_FRAME_BYTES,
                ping_interval=None,
                open_timeout=timeout,
                proxy=None,
            ),
            client_name,
            client_version,
            timeout,
        )

    @classmethod
    async def _open(
        cls, opening: Any, client_name: str, client_version: str, timeout: float
    ) -> "CodexRpcClient":
        try:
            websocket = await asyncio.wait_for(opening, timeout)
        except (OSError, WebSocketException, asyncio.TimeoutError) as error:
            raise CodexDisconnected(
                f"Cannot connect to the Codex app-server: {type(error).__name__}"
            ) from error
        client = cls(websocket, default_timeout=timeout)
        client._reader = asyncio.create_task(client._read_loop())
        try:
            result = await client.request(
                "initialize",
                {"clientInfo": {"name": client_name, "version": client_version}},
            )
            await client.notify("initialized")
        except BaseException:
            await client.close()
            raise
        if isinstance(result, dict):
            client.user_agent = _clean(result.get("userAgent"))
        return client

    @property
    def closed(self) -> bool:
        return self._closed

    def subscribe(
        self,
        thread_id: str | None = None,
        *,
        methods: Iterable[str] | None = None,
        maxsize: int = 256,
    ) -> CodexSubscription:
        if self._closed:
            raise CodexDisconnected("Codex connection is closed")
        subscription = CodexSubscription(
            self,
            validate_thread_id(thread_id) if thread_id is not None else None,
            frozenset(methods) if methods is not None else None,
            maxsize,
        )
        self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: CodexSubscription) -> None:
        self._subscriptions.discard(subscription)

    async def request(
        self, method: str, params: Any = None, *, timeout: float | None = None
    ) -> Any:
        if self._closed:
            raise CodexDisconnected("Codex connection is closed")
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(
                future, self._default_timeout if timeout is None else timeout
            )
        except asyncio.TimeoutError as error:
            raise CodexTimeout(f"Codex request {method} timed out") from error
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def close(self) -> None:
        if self._released:
            return
        self._released = True
        self._closed = True
        reader, self._reader = self._reader, None
        outbound, self._outbound = list(self._outbound), set()
        for task in outbound:
            task.cancel()
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        try:
            await self._ws.close()
        except (OSError, WebSocketException):
            pass
        if reader is not None and reader is not asyncio.current_task():
            await asyncio.gather(reader, return_exceptions=True)
        if outbound:
            await asyncio.gather(*outbound, return_exceptions=True)
        self._fail_all()

    async def _send(self, message: dict[str, Any]) -> None:
        try:
            await self._ws.send(json.dumps(message))
        except (OSError, WebSocketException) as error:
            self._fail_all()
            raise CodexDisconnected("Codex connection lost") from error

    def _fail_all(self) -> None:
        self._closed = True
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(CodexDisconnected("Codex connection lost"))
        self._pending.clear()
        for subscription in list(self._subscriptions):
            subscription.shutdown()
        self._subscriptions.clear()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message)
        except (OSError, WebSocketException):
            pass
        finally:
            self._fail_all()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        message_id = message.get("id")
        if isinstance(method, str):
            if message_id is not None:
                task = asyncio.create_task(self._reject_server_request(message_id))
                self._outbound.add(task)
                task.add_done_callback(self._outbound.discard)
                return
            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            notification = CodexNotification(
                method, params, notification_thread_id(method, params)
            )
            for subscription in list(self._subscriptions):
                if subscription.accepts(notification):
                    subscription.push(notification)
            return
        future = self._pending.get(message_id) if type(message_id) is int else None
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            future.set_exception(
                CodexRpcError(error.get("code"), str(error.get("message", ""))[:500])
            )
        else:
            future.set_result(message.get("result"))

    async def _reject_server_request(self, message_id: Any) -> None:
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "id": message_id,
                        "error": {
                            "code": METHOD_NOT_FOUND,
                            "message": "Not supported by this observer",
                        },
                    }
                )
            )
        except (OSError, WebSocketException):
            pass


class CodexAppServerHost:
    """Owned `codex app-server`.

    With no `socket_path` the server listens on an ephemeral 127.0.0.1 port
    behind a random capability token (private mode). The token lives only in
    memory and in `child_env()`; Codex creates nothing outside its CODEX_HOME.
    With a `socket_path`, Codex itself creates /tmp/codex-daemon-UID/<hash>
    behind that path regardless of TMPDIR.
    """

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        executable: str = "codex",
        startup_timeout: float = 20.0,
        request_timeout: float = 30.0,
    ):
        self.socket_path = Path(socket_path) if socket_path is not None else None
        self.cwd = Path(cwd)
        self.executable = executable
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        base = dict(os.environ if env is None else env)
        base.pop(TOKEN_ENV, None)
        self._base_env = base
        self._token = secrets.token_urlsafe(32) if socket_path is None else None
        self._url: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._drain: asyncio.Task[None] | None = None
        self._client: CodexRpcClient | None = None
        self._identity: RuntimeIdentity | None = None
        self._owned_link: tuple[Path, Path] | None = None
        self._closed = False

    @property
    def identity(self) -> RuntimeIdentity | None:
        return self._identity

    @property
    def running(self) -> bool:
        return (
            not self._closed
            and self._process is not None
            and self._process.returncode is None
        )

    @property
    def client(self) -> CodexRpcClient:
        if self._client is None or self._client.closed:
            raise CodexDisconnected("Codex app-server host is not connected")
        return self._client

    @property
    def remote(self) -> str:
        if self.socket_path is not None:
            return remote_url(self.socket_path)
        if self._url is None:
            raise CodexDisconnected("Codex app-server host is not started")
        return self._url

    @property
    def _target(self) -> Path | str:
        return self.socket_path if self.socket_path is not None else self.remote

    @property
    def _auth_env(self) -> str | None:
        return TOKEN_ENV if self._token is not None else None

    def child_env(self) -> dict[str, str]:
        env = dict(self._base_env)
        if self._token is not None:
            env[TOKEN_ENV] = self._token
        return env

    def viewer_argv(self, thread_id: str) -> list[str]:
        return viewer_argv(self._target, thread_id, self.executable, self._auth_env)

    def tui_argv(
        self, *, thread_id: str | None = None, prompt: str | None = None
    ) -> list[str]:
        return tui_argv(
            self._target,
            thread_id=thread_id,
            prompt=prompt,
            executable=self.executable,
            auth_env=self._auth_env,
        )

    async def connect_observer(
        self, client_name: str = "kanbeat-observer", *, timeout: float = 30.0
    ) -> CodexRpcClient:
        if self._token is not None:
            return await CodexRpcClient.connect_ws(
                self.remote, self._token, client_name=client_name, timeout=timeout
            )
        assert self.socket_path is not None
        return await CodexRpcClient.connect(
            self.socket_path, client_name=client_name, timeout=timeout
        )

    async def start(self) -> RuntimeIdentity:
        if self._process is not None or self._closed:
            raise CodexHostError("Codex app-server host already started")
        if self.socket_path is not None:
            await self._start_unix(self.socket_path)
        else:
            await self._start_private()
        assert self._identity is not None
        return self._identity

    async def _start_unix(self, socket_path: Path) -> None:
        listen = remote_url(socket_path)
        prepare_socket_directory(socket_path)
        remove_stale_socket(socket_path)
        process = await spawn_owned_group(
            [self.executable, "app-server", "--listen", listen],
            env=self._base_env,
            cwd=self.cwd,
            capture_output=False,
        )
        self._process = process
        try:
            self._identity = identity_of("codex", process, listen)
            self._client = await self._connect_when_ready(process, socket_path)
            self._owned_link = safe_recorded_link(socket_path)
        except BaseException:
            self._owned_link = safe_recorded_link(socket_path)
            await self._teardown()
            raise

    async def _start_private(self) -> None:
        assert self._token is not None
        digest = hashlib.sha256(self._token.encode()).hexdigest()
        process = await spawn_owned_group(
            [
                self.executable,
                "app-server",
                "--listen",
                "ws://127.0.0.1:0",
                "--ws-auth",
                "capability-token",
                "--ws-token-sha256",
                digest,
            ],
            env=self._base_env,
            cwd=self.cwd,
            capture_output=True,
        )
        self._process = process
        try:
            self._url = await asyncio.wait_for(
                self._read_listening_url(process), self.startup_timeout
            )
            self._drain = asyncio.create_task(self._drain_output(process))
            self._identity = identity_of("codex", process, self._url)
            self._client = await self._connect_when_ready(process, None)
        except asyncio.TimeoutError as error:
            await self._teardown()
            raise CodexTimeout("Codex app-server did not become ready") from error
        except BaseException:
            await self._teardown()
            raise

    @staticmethod
    async def _read_listening_url(process: asyncio.subprocess.Process) -> str:
        assert process.stdout is not None
        tail = b""
        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
                await process.wait()
                raise CodexHostError(
                    f"Codex app-server exited during startup ({process.returncode})"
                )
            tail = (tail + chunk)[-STARTUP_TAIL_BYTES:]
            match = WS_LISTENING.search(tail)
            if match and 0 < int(match.group(2)) < 65536:
                return match.group(1).decode()

    @staticmethod
    async def _drain_output(process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        try:
            while await process.stdout.read(65536):
                pass
        except (OSError, ValueError):
            pass

    async def _connect_when_ready(
        self, process: asyncio.subprocess.Process, socket_path: Path | None
    ) -> CodexRpcClient:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.startup_timeout
        while True:
            if process.returncode is not None:
                raise CodexHostError(
                    f"Codex app-server exited during startup ({process.returncode})"
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CodexTimeout("Codex app-server did not become ready")
            if socket_path is None or _is_socket(socket_path):
                try:
                    return await self.connect_observer(
                        "kanbeat", timeout=min(CONNECT_ATTEMPT_SECONDS, remaining)
                    )
                except (CodexDisconnected, CodexTimeout):
                    pass
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        self._closed = True
        await self._teardown()

    async def _teardown(self) -> None:
        client, self._client = self._client, None
        process, self._process = self._process, None
        drain, self._drain = self._drain, None
        owned, self._owned_link = self._owned_link, None
        if client is not None:
            await client.close()
        await stop_owned(process)
        if drain is not None:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
        if owned is None and self.socket_path is not None:
            owned = safe_recorded_link(self.socket_path)
        if owned is not None:
            try:
                remove_recorded_link(*owned)
            except (OSError, RuntimeError):
                pass

    async def __aenter__(self) -> "CodexAppServerHost":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def request(
        self, method: str, params: Any = None, *, timeout: float | None = None
    ) -> Any:
        return await self.client.request(
            method,
            params,
            timeout=self.request_timeout if timeout is None else timeout,
        )

    def subscribe(
        self,
        thread_id: str | None = None,
        *,
        methods: Iterable[str] | None = None,
        maxsize: int = 256,
    ) -> CodexSubscription:
        return self.client.subscribe(thread_id, methods=methods, maxsize=maxsize)

    async def start_thread(
        self,
        cwd: Path,
        *,
        model: str | None = None,
        model_provider: str | None = None,
    ) -> CodexThreadMetadata:
        params: dict[str, Any] = {"cwd": os.fspath(cwd)}
        if model is not None:
            params["model"] = model
        if model_provider is not None:
            params["modelProvider"] = model_provider
        result = await self.request("thread/start", params)
        return parse_thread_metadata(result)

    async def read_thread(self, thread_id: str) -> CodexThreadMetadata:
        result = await self.request(
            "thread/read",
            {"threadId": validate_thread_id(thread_id), "includeTurns": False},
        )
        return parse_thread_metadata(result)

    async def delete_thread(self, thread_id: str) -> None:
        await self.request("thread/delete", {"threadId": validate_thread_id(thread_id)})

    async def loaded_thread_ids(self) -> list[str]:
        ids: list[str] = []
        cursor: str | None = None
        for _page in range(MAX_LOADED_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self.request("thread/loaded/list", params)
            data = result.get("data") if isinstance(result, dict) else None
            ids.extend(
                value.lower()
                for value in (data if isinstance(data, list) else [])
                if isinstance(value, str) and THREAD_ID.match(value)
            )
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not isinstance(cursor, str) or not cursor:
                break
        else:
            raise CodexHostError("Codex loaded thread list exceeded its page limit")
        return ids

    async def observe_thread(self, thread_id: str) -> CodexThreadMetadata:
        result = await self.request(
            "thread/resume",
            {"threadId": validate_thread_id(thread_id), "excludeTurns": True},
        )
        return parse_thread_metadata(result)

    async def stop_observing(self, thread_id: str) -> None:
        await self.request(
            "thread/unsubscribe", {"threadId": validate_thread_id(thread_id)}
        )

    async def set_thread_name(self, thread_id: str, name: str) -> None:
        if not name or len(name) > MAX_VALUE_CHARS or CONTROL.search(name):
            raise CodexHostError("Invalid Codex thread name")
        await self.request(
            "thread/name/set",
            {"threadId": validate_thread_id(thread_id), "name": name},
        )

    async def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        if not text or len(text) > 8 * 1024 * 1024:
            raise CodexHostError("Invalid Codex turn input")
        params: dict[str, Any] = {
            "threadId": validate_thread_id(thread_id),
            "input": [{"type": "text", "text": text}],
        }
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        result = await self.request("turn/start", params)
        turn = result.get("turn") if isinstance(result, dict) else None
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise CodexHostError("Codex turn/start response has no turn")
        return turn

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self.request(
            "turn/interrupt",
            {
                "threadId": validate_thread_id(thread_id),
                "turnId": turn_id[:MAX_VALUE_CHARS],
            },
        )

    async def wait_for_new_thread(
        self,
        known_thread_ids: Iterable[str],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> CodexThreadMetadata:
        known = {validate_thread_id(value) for value in known_thread_ids}
        wanted = os.fspath(cwd) if cwd is not None else None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        with self.subscribe(methods=["thread/started"]) as started:
            while True:
                for thread_id in await self.loaded_thread_ids():
                    if thread_id in known:
                        continue
                    try:
                        metadata = await self.read_thread(thread_id)
                    except CodexRpcError:
                        continue
                    if wanted is None or _same_cwd(metadata.cwd, wanted):
                        return metadata
                    if metadata.cwd is not None:
                        known.add(thread_id)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise CodexTimeout("No new Codex thread appeared")
                try:
                    await started.get(min(poll_interval, remaining))
                except CodexTimeout:
                    pass


def _same_cwd(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left == right
    return os.path.realpath(left) == os.path.realpath(right)


def _is_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def _unlink_owned(path: Path, kinds: tuple[Any, ...]) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if info.st_uid != os.getuid() or not any(kind(info.st_mode) for kind in kinds):
        raise RuntimeError("Refusing to remove a non-owned or unexpected runtime path")
    path.unlink()


def _stale_socket(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX)
    probe.settimeout(1.0)
    try:
        probe.connect(os.fspath(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return True
    except OSError:
        raise RuntimeError("Cannot prove the existing socket is stale") from None
    finally:
        probe.close()
    return False


def _codex_daemon_root() -> Path:
    return Path("/tmp") / f"codex-daemon-{os.getuid()}"


def remove_stale_socket(path: Path) -> None:
    """Remove an owned dead socket or a Codex-created symlink to one."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        if info.st_uid != os.getuid():
            raise RuntimeError("Refusing to replace a foreign runtime link")
        target = Path(os.readlink(path))
        if not target.is_absolute():
            target = path.parent / target
        try:
            target_info = target.lstat()
        except FileNotFoundError:
            path.unlink(missing_ok=True)
            return
        if (
            target_info.st_uid != os.getuid()
            or not stat.S_ISSOCK(target_info.st_mode)
            or (
                target.parent != path.parent
                and _codex_daemon_root() not in target.parents
            )
        ):
            raise RuntimeError("Refusing to replace an unexpected runtime link")
        if not _stale_socket(target):
            raise RuntimeError(
                "Another server is already listening on this socket path"
            )
        target.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("Refusing to replace an existing non-socket or linked path")
    if _stale_socket(path):
        path.unlink(missing_ok=True)
        return
    raise RuntimeError("Another server is already listening on this socket path")


def safe_recorded_link(path: Path) -> tuple[Path, Path] | None:
    try:
        return recorded_link(path)
    except OSError:
        return None


def recorded_link(path: Path) -> tuple[Path, Path] | None:
    """Link and target Codex created for our own just-started server."""
    if not path.is_symlink():
        return (path, path) if _is_socket(path) else None
    target = Path(os.readlink(path))
    if not target.is_absolute():
        target = path.parent / target
    return (path, target)


def remove_recorded_link(link: Path, target: Path) -> None:
    if link == target:
        _unlink_owned(link, (stat.S_ISSOCK,))
        return
    if link.is_symlink() and Path(os.readlink(link)) in (
        target,
        Path(os.path.relpath(target, link.parent)),
    ):
        _unlink_owned(link, (stat.S_ISLNK,))
    _unlink_owned(target, (stat.S_ISSOCK,))
