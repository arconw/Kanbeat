from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from .host_process import (
    HostError,
    RuntimeIdentity,
    identity_of,
    spawn_owned_group,
    stop_owned,
)

SESSION_ID = re.compile(r"^ses_[A-Za-z0-9]{1,100}$")
LISTENING = re.compile(rb"listening on (http://127\.0\.0\.1:([0-9]{1,5}))\s")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
USERNAME = "opencode"
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"
USERNAME_ENV = "OPENCODE_SERVER_USERNAME"
MAX_BODY_BYTES = 1024 * 1024
MAX_VALUE_CHARS = 200
MAX_PROMPT_BYTES = 64 * 1024
STARTUP_TAIL_BYTES = 4096


class OpenCodeHostError(HostError):
    pass


class OpenCodeTimeout(OpenCodeHostError, TimeoutError):
    pass


class OpenCodePromptTooLarge(OpenCodeHostError):
    pass


class OpenCodeHttpError(OpenCodeHostError):
    def __init__(self, status: int):
        super().__init__(f"OpenCode server returned HTTP {status}")
        self.status = status


def validate_prompt(prompt: str) -> None:
    if not prompt or "\x00" in prompt:
        raise OpenCodeHostError("Invalid prompt for the OpenCode worker")
    if len(prompt.encode()) > MAX_PROMPT_BYTES:
        raise OpenCodePromptTooLarge("Prompt is too large for the OpenCode worker")


def validate_session_id(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_ID.match(value):
        raise OpenCodeHostError("Invalid OpenCode session id")
    return value


def _validate_token(value: Any, label: str, *, allow_slash: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_VALUE_CHARS
        or value.startswith("-")
        or CONTROL.search(value)
        or any(char.isspace() for char in value)
        or (not allow_slash and "/" in value)
    ):
        raise OpenCodeHostError(f"Invalid OpenCode {label}")
    return value


def _clean(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and value
        and len(value) <= MAX_VALUE_CHARS
        and not CONTROL.search(value)
    ):
        return value
    return None


@dataclass(frozen=True)
class OpenCodeModel:
    provider_id: str
    model_id: str
    variant: str | None = None

    def __post_init__(self) -> None:
        _validate_token(self.provider_id, "provider", allow_slash=False)
        _validate_token(self.model_id, "model", allow_slash=True)
        if self.variant is not None:
            _validate_token(self.variant, "variant", allow_slash=False)

    @property
    def flag(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    def to_payload(self) -> dict[str, str]:
        payload = {"id": self.model_id, "providerID": self.provider_id}
        if self.variant is not None:
            payload["variant"] = self.variant
        return payload


@dataclass(frozen=True)
class OpenCodeSessionMetadata:
    session_id: str
    directory: str | None
    model: OpenCodeModel | None

    def to_dict(self) -> dict[str, Any]:
        model = self.model
        return {
            "session_id": self.session_id,
            "directory": self.directory,
            "provider": model.provider_id if model else None,
            "model": model.model_id if model else None,
            "variant": model.variant if model else None,
        }


@dataclass(frozen=True)
class OpenCodeSessionStatus:
    session_id: str
    status: str


def parse_session_metadata(payload: Any) -> OpenCodeSessionMetadata:
    if not isinstance(payload, dict):
        raise OpenCodeHostError("OpenCode session response is malformed")
    raw = payload.get("model")
    model = None
    if isinstance(raw, dict):
        provider, model_id, variant = (
            _clean(raw.get("providerID")),
            _clean(raw.get("id")),
            _clean(raw.get("variant")),
        )
        if provider and model_id:
            try:
                model = OpenCodeModel(provider, model_id, variant)
            except OpenCodeHostError:
                model = None
    return OpenCodeSessionMetadata(
        validate_session_id(payload.get("id")), _clean(payload.get("directory")), model
    )


def parse_statuses(payload: Any) -> dict[str, OpenCodeSessionStatus]:
    if not isinstance(payload, dict):
        raise OpenCodeHostError("OpenCode status response is malformed")
    statuses: dict[str, OpenCodeSessionStatus] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not SESSION_ID.match(key):
            continue
        kind = _clean(value.get("type")) if isinstance(value, dict) else None
        if kind:
            statuses[key] = OpenCodeSessionStatus(key, kind)
    return statuses


def validate_server_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.port is None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise OpenCodeHostError("OpenCode server URL must be a plain loopback http URL")
    return f"http://127.0.0.1:{parts.port}"


def viewer_argv(url: str, session_id: str, executable: str = "opencode") -> list[str]:
    return [
        executable,
        "attach",
        validate_server_url(url),
        "--session",
        validate_session_id(session_id),
    ]


def worker_argv(
    url: str,
    session_id: str,
    *,
    prompt: str | None = None,
    model: OpenCodeModel | None = None,
    executable: str = "opencode",
) -> list[str]:
    argv = [
        executable,
        "run",
        "--attach",
        validate_server_url(url),
        "--session",
        validate_session_id(session_id),
        "--format",
        "json",
    ]
    if model is not None:
        argv += ["--model", model.flag]
        if model.variant is not None:
            argv += ["--variant", model.variant]
    if prompt is not None:
        if not prompt or "\x00" in prompt:
            raise OpenCodeHostError("Invalid prompt for the OpenCode worker")
        if len(prompt.encode()) > MAX_PROMPT_BYTES:
            raise OpenCodePromptTooLarge("Prompt is too large for the OpenCode worker")
        argv += ["--", prompt]
    return argv


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class OpenCodeServeHost:
    def __init__(
        self,
        cwd: Path,
        *,
        env: dict[str, str] | None = None,
        executable: str = "opencode",
        startup_timeout: float = 20.0,
        request_timeout: float = 15.0,
    ):
        self.cwd = Path(cwd)
        self.executable = executable
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self._base_env = {
            key: value
            for key, value in (os.environ if env is None else env).items()
            if key not in (PASSWORD_ENV, USERNAME_ENV)
        }
        self._password = secrets.token_urlsafe(32)
        self._authorization = (
            "Basic "
            + base64.b64encode(f"{USERNAME}:{self._password}".encode()).decode()
        )
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect
        )
        self._process: asyncio.subprocess.Process | None = None
        self._drain: asyncio.Task[None] | None = None
        self._identity: RuntimeIdentity | None = None
        self._url: str | None = None
        self._closed = False

    @property
    def url(self) -> str:
        if self._url is None:
            raise OpenCodeHostError("OpenCode host is not running")
        return self._url

    @property
    def identity(self) -> RuntimeIdentity | None:
        return self._identity

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def child_env(self) -> dict[str, str]:
        return {
            **self._base_env,
            PASSWORD_ENV: self._password,
            USERNAME_ENV: USERNAME,
        }

    def viewer_argv(self, session_id: str) -> list[str]:
        return viewer_argv(self.url, session_id, self.executable)

    def worker_argv(
        self,
        session_id: str,
        *,
        prompt: str | None = None,
        model: OpenCodeModel | None = None,
    ) -> list[str]:
        return worker_argv(
            self.url,
            session_id,
            prompt=prompt,
            model=model,
            executable=self.executable,
        )

    async def start(self) -> RuntimeIdentity:
        if self._process is not None or self._closed:
            raise OpenCodeHostError("OpenCode host already started")
        process = await spawn_owned_group(
            [self.executable, "serve", "--hostname", "127.0.0.1", "--port", "0"],
            env=self.child_env(),
            cwd=self.cwd,
            capture_output=True,
        )
        self._process = process
        try:
            url = await asyncio.wait_for(
                self._read_listening_url(process), self.startup_timeout
            )
            self._drain = asyncio.create_task(self._drain_output(process))
            self._url = validate_server_url(url)
            self._identity = identity_of("opencode", process, self._url)
            await self._wait_authorized()
        except asyncio.TimeoutError as error:
            await self._teardown()
            raise OpenCodeTimeout("OpenCode server did not become ready") from error
        except BaseException:
            await self._teardown()
            raise
        return self._identity

    async def _read_listening_url(self, process: asyncio.subprocess.Process) -> str:
        assert process.stdout is not None
        tail = b""
        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
                await process.wait()
                raise OpenCodeHostError(
                    f"OpenCode server exited during startup ({process.returncode})"
                )
            tail = (tail + chunk)[-STARTUP_TAIL_BYTES:]
            match = LISTENING.search(tail)
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

    async def _wait_authorized(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.startup_timeout
        while True:
            try:
                await self.statuses()
                return
            except OpenCodeHttpError:
                raise
            except OpenCodeHostError:
                pass
            if loop.time() >= deadline:
                raise OpenCodeTimeout("OpenCode server did not accept requests")
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        self._closed = True
        await self._teardown()

    async def _teardown(self) -> None:
        drain, self._drain = self._drain, None
        process, self._process = self._process, None
        self._url = None
        await stop_owned(process)
        if drain is not None:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    async def __aenter__(self) -> "OpenCodeServeHost":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def create_session(
        self, *, directory: Path | None = None, model: OpenCodeModel | None = None
    ) -> OpenCodeSessionMetadata:
        query = {"directory": os.fspath(directory)} if directory is not None else None
        body = {"model": model.to_payload()} if model is not None else {}
        payload = await self._call("POST", "/session", body, query=query)
        return parse_session_metadata(payload)

    async def read_session(self, session_id: str) -> OpenCodeSessionMetadata:
        payload = await self._call("GET", f"/session/{validate_session_id(session_id)}")
        return parse_session_metadata(payload)

    async def set_session_model(
        self, session_id: str, model: OpenCodeModel
    ) -> OpenCodeSessionMetadata:
        session_id = validate_session_id(session_id)
        await self._call(
            "POST", f"/api/session/{session_id}/model", {"model": model.to_payload()}
        )
        return await self.read_session(session_id)

    async def statuses(self) -> dict[str, OpenCodeSessionStatus]:
        return parse_statuses(await self._call("GET", "/session/status"))

    async def session_status(self, session_id: str) -> str:
        found = (await self.statuses()).get(validate_session_id(session_id))
        return found.status if found else "idle"

    async def abort(self, session_id: str) -> None:
        await self._call("POST", f"/session/{validate_session_id(session_id)}/abort")

    async def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: dict[str, str] | None = None,
    ) -> Any:
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._blocking_call, method, url, body),
                self.request_timeout + 1,
            )
        except asyncio.TimeoutError as error:
            raise OpenCodeTimeout(
                f"OpenCode request {method} {path} timed out"
            ) from error

    def _blocking_call(self, method: str, url: str, body: Any) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", self._authorization)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=self.request_timeout) as response:
                raw = response.read(MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as error:
            error.close()
            raise OpenCodeHttpError(error.code) from None
        except (urllib.error.URLError, OSError) as error:
            raise OpenCodeHostError(
                f"OpenCode server unreachable: {type(error).__name__}"
            ) from None
        if len(raw) > MAX_BODY_BYTES:
            raise OpenCodeHostError("OpenCode response is too large")
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise OpenCodeHostError("OpenCode response is not JSON") from None
