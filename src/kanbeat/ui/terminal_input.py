from __future__ import annotations

import asyncio
import uuid

MAX_QUEUED_INPUTS = 512
MAX_INPUT_BATCH = 64
MAX_BATCH_BYTES = 32768
LARGE_PASTE_BYTES = 32768
PASTE_CHUNK_BYTES = 32768


def split_paste(text: str, limit: int = PASTE_CHUNK_BYTES):
    index = 0
    length = len(text)
    while index < length:
        end = min(length, index + limit)
        chunk = text[index:end]
        data = chunk.encode("utf-8")
        if len(data) > limit:
            end = index + max(1, (limit * len(chunk)) // len(data))
            chunk = text[index:end]
            data = chunk.encode("utf-8")
            while len(data) > limit and end > index + 1:
                end -= 1
                chunk = text[index:end]
                data = chunk.encode("utf-8")
        yield chunk
        index = end


def is_large_paste(event: dict) -> bool:
    if event.get("kind") != "paste":
        return False
    text = event.get("text")
    return isinstance(text, str) and len(text.encode("utf-8")) > LARGE_PASTE_BYTES


class TerminalInput:
    def __init__(self, send, report):
        self.send = send
        self.report = report
        self.queue: asyncio.Queue[dict] = asyncio.Queue(MAX_QUEUED_INPUTS)
        self.error = ""
        self.client_id = uuid.uuid4().hex

    async def send_paste_part(
        self, phase: str, paste_id: str, text: str | None = None
    ) -> None:
        values = {
            "paste": paste_id,
            "phase": phase,
            "client": self.client_id,
        }
        if text is not None:
            values["text"] = text
        rejected_since = None
        backoff = 0.01
        warned = False
        while True:
            result = await self.send("terminal_paste", **values)
            if result.get("accepted", True):
                return
            if rejected_since is None:
                rejected_since = asyncio.get_running_loop().time()
            if (
                not warned
                and asyncio.get_running_loop().time() - rejected_since >= 1
            ):
                self.report("Terminal is not accepting input; waiting for space")
                warned = True
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 1)

    async def abort_paste(self, paste_id: str) -> None:
        try:
            await self.send(
                "terminal_paste",
                paste=paste_id,
                phase="abort",
                client=self.client_id,
            )
        except (OSError, ValueError, RuntimeError):
            pass

    async def deliver_paste(self, event: dict) -> None:
        text = event.get("text") or ""
        paste_id = uuid.uuid4().hex
        try:
            await self.send_paste_part("begin", paste_id)
            for chunk in split_paste(text):
                await self.send_paste_part("chunk", paste_id, chunk)
            await self.send_paste_part("end", paste_id)
        except (OSError, ValueError, RuntimeError):
            await self.abort_paste(paste_id)
            raise

    async def put(self, event: dict) -> None:
        while self.queue.full():
            if self.error:
                raise RuntimeError("Terminal input unavailable: " + self.error)
            await asyncio.sleep(0.005)
        if self.error:
            raise RuntimeError("Terminal input unavailable: " + self.error)
        self.queue.put_nowait(event)

    @staticmethod
    def size(event: dict) -> int:
        return len(str(event.get("text") or event.get("character") or "").encode()) + 64

    async def run(self) -> None:
        carry = None
        while True:
            first = carry if carry is not None else await self.queue.get()
            carry = None
            if is_large_paste(first):
                try:
                    await self.deliver_paste(first)
                except (OSError, ValueError, RuntimeError) as exc:
                    count = 1 + self.queue.qsize()
                    self.error = str(exc)
                    self.report(
                        "Terminal input failed; delivery of "
                        f"{count} queued events is unconfirmed: {exc}"
                    )
                    self.queue.task_done()
                    while not self.queue.empty():
                        self.queue.get_nowait()
                        self.queue.task_done()
                    return
                self.queue.task_done()
                continue
            batch = [first]
            size = self.size(first)
            while len(batch) < MAX_INPUT_BATCH and not self.queue.empty():
                item = self.queue.get_nowait()
                amount = self.size(item)
                if size + amount > MAX_BATCH_BYTES:
                    carry = item
                    break
                batch.append(item)
                size += amount
            try:
                rejected_since = None
                backoff = 0.01
                warned = False
                while True:
                    result = await self.send("terminal_input", events=batch)
                    rejected = result.get("rejected") or []
                    if rejected:
                        indices = {row["index"] for row in rejected}
                        for row in rejected:
                            self.report("Terminal input rejected: " + row["error"])
                        for _ in indices:
                            self.queue.task_done()
                        batch = [
                            event
                            for index, event in enumerate(batch)
                            if index not in indices
                        ]
                        if not batch:
                            break
                        continue
                    if result.get("accepted", True):
                        break
                    if rejected_since is None:
                        rejected_since = asyncio.get_running_loop().time()
                    if (
                        not warned
                        and asyncio.get_running_loop().time() - rejected_since >= 1
                    ):
                        self.report(
                            "Terminal is not accepting input; waiting for space"
                        )
                        warned = True
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 1)
            except (OSError, ValueError, RuntimeError) as exc:
                self.error = str(exc)
                count = len(batch) + int(carry is not None) + self.queue.qsize()
                self.report(
                    f"Terminal input failed; delivery of {count} queued events is unconfirmed: {exc}"
                )
                for _ in batch:
                    self.queue.task_done()
                if carry is not None:
                    self.queue.task_done()
                while not self.queue.empty():
                    self.queue.get_nowait()
                    self.queue.task_done()
                return
            for _ in batch:
                self.queue.task_done()

    async def drain(self) -> None:
        try:
            await asyncio.wait_for(self.queue.join(), timeout=2)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "Terminal input is still queued; some keys may not be delivered"
            ) from exc
