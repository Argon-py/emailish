"""A scriptable mock SMTP server cluster for tests.

Each behaviour runs on its own loopback address (127.0.0.N) on a shared port, so a
fake DNS table can point each test domain's MX at a server with a specific behaviour.
The server records every command it receives so tests can assert that DATA is never sent.
"""
from __future__ import annotations

import asyncio
import re
import socket
import threading
from dataclasses import dataclass, field
from typing import Callable

RcptFn = Callable[[str, int], tuple[int, str]]   # (address, attempt_number) -> (code, text)


@dataclass
class Behaviour:
    rcpt: RcptFn
    banner: tuple[int, str] = (220, "mock ESMTP ready")
    banner_delay: float = 0.0
    garbage_banner: bool = False
    rcpt_delay: float = 0.0
    mail_reply: tuple[int, str] = (250, "2.1.0 Ok")
    drop_after_mail: bool = False
    ehlo_reply: tuple[int, str] | None = None
    log: list[str] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)


def mailbox_set(existing: set[str], unknown=(550, "5.1.1 <{a}>: Recipient address rejected: User unknown")) -> RcptFn:
    def fn(addr: str, attempt: int):
        if addr.split("@")[0].lower() in existing:
            return 250, "2.1.5 Ok"
        return unknown[0], unknown[1].format(a=addr)
    return fn


def accept_all(addr: str, attempt: int):
    return 250, "2.1.5 Ok"


def fixed(code: int, text: str) -> RcptFn:
    return lambda addr, attempt: (code, text)


def greylist_then(inner: RcptFn, times: int = 1) -> RcptFn:
    def fn(addr: str, attempt: int):
        if attempt <= times:
            return 450, "4.7.1 <%s>: Recipient address rejected: Greylisted, see http://postgrey.schweikert.ch/" % addr
        return inner(addr, attempt)
    return fn


class MockCluster:
    def __init__(self):
        self.port = self._free_port()
        self.servers: dict[str, Behaviour] = {}    # ip -> behaviour
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self._handles = []

    @staticmethod
    def _free_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def add(self, ip: str, b: Behaviour) -> None:
        self.servers[ip] = b
        fut = asyncio.run_coroutine_threadsafe(asyncio.start_server(
            lambda r, w, b=b: self._handle(r, w, b), ip, self.port), self.loop)
        self._handles.append(fut.result(5))

    async def _handle(self, reader, writer, b: Behaviour):
        async def send(code, text, more=None):
            lines = (more or []) + [f"{code} {text}"]
            writer.write(("\r\n".join(lines) + "\r\n").encode())
            await writer.drain()
        try:
            if b.banner_delay:
                await asyncio.sleep(b.banner_delay)
            if b.garbage_banner:
                writer.write(b"HELLO THERE FRIEND\r\n")
                await writer.drain()
            else:
                await send(*b.banner)
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode().strip()
                b.log.append(line)
                cmd = line.split(" ", 1)[0].upper()
                if cmd == "EHLO":
                    if b.ehlo_reply:
                        await send(*b.ehlo_reply)
                    else:
                        await send(250, "SIZE 10240000", ["250-mock.test"])
                elif cmd == "HELO":
                    await send(250, "mock.test")
                elif cmd == "MAIL":
                    await send(*b.mail_reply)
                    if b.drop_after_mail:
                        break
                elif cmd == "RCPT":
                    m = re.search(r"<([^>]*)>", line)
                    addr = m.group(1) if m else ""
                    b.attempts[addr] = b.attempts.get(addr, 0) + 1
                    if b.rcpt_delay:
                        await asyncio.sleep(b.rcpt_delay)
                    await send(*b.rcpt(addr, b.attempts[addr]))
                elif cmd == "RSET" or cmd == "NOOP":
                    await send(250, "2.0.0 Ok")
                elif cmd == "QUIT":
                    await send(221, "2.0.0 Bye")
                    break
                elif cmd == "DATA":
                    await send(554, "5.5.1 DATA is not allowed in tests")
                else:
                    await send(500, "5.5.2 command not recognized")
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def all_commands(self) -> list[str]:
        return [c for b in self.servers.values() for c in b.log]

    def close(self):
        async def _shutdown():
            for srv in self._handles:
                srv.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(_shutdown(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
