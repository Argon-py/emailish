"""Minimal async SMTP prober. Never issues DATA, so no message is ever sent.

Session:  connect -> 220 banner -> EHLO (HELO fallback) -> MAIL FROM ->
          RCPT TO x N targets -> RCPT TO <random> (catch-all probe) -> RSET -> QUIT
"""
from __future__ import annotations

import asyncio
import errno
from dataclasses import dataclass, field

# failure kinds (stage-level diagnostics, similar in spirit to MillionVerifier's subresults)
TIMEOUT = "timeout"
REFUSED = "connection_refused"
RESET = "connection_reset"
UNREACHABLE = "no_route"
PROTOCOL = "protocol_error"
REJECTED_SESSION = "session_rejected"   # banner/EHLO/MAIL FROM refused with a code


@dataclass
class Reply:
    code: int
    text: str

    def short(self) -> str:
        t = " ".join(self.text.split())
        return f"{self.code} {t[:200]}"


@dataclass
class SessionResult:
    mx_host: str = ""
    connected: bool = False
    failed_stage: str = ""          # connect | banner | ehlo | mail_from | rcpt | ""
    failure_kind: str = ""          # one of the kinds above
    failure_reply: Reply | None = None
    failure_detail: str = ""
    rcpt: dict[str, Reply] = field(default_factory=dict)   # address -> reply
    probe_reply: Reply | None = None                      # catch-all probe reply
    esmtp: bool = False


class ProtocolError(Exception):
    pass


async def _read_reply(reader: asyncio.StreamReader, timeout: float) -> Reply:
    lines, code = [], None
    while True:
        raw = await asyncio.wait_for(reader.readline(), timeout)
        if not raw:
            raise ConnectionResetError("server closed the connection")
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if len(line) < 3 or not line[:3].isdigit():
            raise ProtocolError(f"non-SMTP reply: {line[:120]!r}")
        c = int(line[:3])
        if code is None:
            code = c
        lines.append(line[4:] if len(line) > 3 else "")
        if len(line) == 3 or line[3] == " ":
            return Reply(code, "\n".join(lines))
        if line[3] != "-":
            raise ProtocolError(f"malformed reply line: {line[:120]!r}")
        if len(lines) > 200:
            raise ProtocolError("reply too long")


async def _cmd(reader, writer, line: str, timeout: float) -> Reply:
    writer.write((line + "\r\n").encode("utf-8"))
    await asyncio.wait_for(writer.drain(), timeout)
    return await _read_reply(reader, timeout)


def _conn_kind(e: BaseException) -> str:
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return TIMEOUT
    if isinstance(e, ConnectionRefusedError):
        return REFUSED
    if isinstance(e, (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError)):
        return RESET
    if isinstance(e, OSError) and e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return UNREACHABLE
    if isinstance(e, ProtocolError):
        return PROTOCOL
    return RESET


async def probe_session(
    host: str,
    ip: str,
    *,
    port: int,
    helo: str,
    mail_from: str,
    recipients: list[str],
    catchall_probe: str | None,
    connect_timeout: float,
    command_timeout: float,
) -> SessionResult:
    res = SessionResult(mx_host=host)
    stage = "connect"
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), connect_timeout)
        res.connected = True
        stage = "banner"
        banner = await _read_reply(reader, command_timeout)
        if banner.code != 220:
            res.failed_stage, res.failure_kind, res.failure_reply = stage, REJECTED_SESSION, banner
            return res
        stage = "ehlo"
        r = await _cmd(reader, writer, f"EHLO {helo}", command_timeout)
        if r.code == 250:
            res.esmtp = True
        else:
            r = await _cmd(reader, writer, f"HELO {helo}", command_timeout)
            if r.code != 250:
                res.failed_stage, res.failure_kind, res.failure_reply = stage, REJECTED_SESSION, r
                return res
        stage = "mail_from"
        r = await _cmd(reader, writer, f"MAIL FROM:<{mail_from}>", command_timeout)
        if r.code != 250:
            res.failed_stage, res.failure_kind, res.failure_reply = stage, REJECTED_SESSION, r
            return res
        stage = "rcpt"
        for addr in recipients:
            r = await _cmd(reader, writer, f"RCPT TO:<{addr}>", command_timeout)
            res.rcpt[addr] = r
            if r.code == 421:   # server is closing the channel
                return res
        if catchall_probe:
            res.probe_reply = await _cmd(reader, writer, f"RCPT TO:<{catchall_probe}>", command_timeout)
        try:
            await _cmd(reader, writer, "RSET", command_timeout)
            writer.write(b"QUIT\r\n")
            await asyncio.wait_for(writer.drain(), 2)
        except Exception:
            pass
        return res
    except Exception as e:  # network or protocol failure at `stage`
        res.failed_stage = stage
        res.failure_kind = _conn_kind(e)
        res.failure_detail = f"{type(e).__name__}: {e}"[:200]
        return res
    finally:
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 2)
            except Exception:
                pass


async def tcp_port_open(ip: str, port: int, timeout: float) -> tuple[bool, str]:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        w.close()
        return True, "connected"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:160]
