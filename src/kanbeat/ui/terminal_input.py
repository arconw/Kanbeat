from __future__ import annotations

import asyncio

MAX_QUEUED_INPUTS = 512
MAX_INPUT_BATCH = 64
MAX_BATCH_BYTES = 32768


class TerminalInput:
    def __init__(self, send, report):
        self.send = send
        self.report = report
        self.queue: asyncio.Queue[dict] = asyncio.Queue(MAX_QUEUED_INPUTS)
        self.error = ""

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
                    await asyncio.sleep(0.01)
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
