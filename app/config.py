"""Settings, read from environment variables (prefix EMAILISH_)."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default):
    v = os.environ.get(f"EMAILISH_{name}")
    if v is None or v == "":
        return default
    if isinstance(default, bool):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(v)
    if isinstance(default, float):
        return float(v)
    return v


def _default_helo() -> str:
    h = socket.getfqdn()
    return h if "." in h and not h.endswith(".localdomain") else "localhost.localdomain"


@dataclass
class Settings:
    helo_host: str = field(default_factory=lambda: _env("HELO_HOST", _default_helo()))
    mail_from: str = field(default_factory=lambda: _env("MAIL_FROM", ""))
    smtp_enabled: bool = field(default_factory=lambda: _env("SMTP_ENABLED", True))
    smtp_port: int = field(default_factory=lambda: _env("SMTP_PORT", 25))
    connect_timeout: float = field(default_factory=lambda: _env("SMTP_CONNECT_TIMEOUT", 10.0))
    command_timeout: float = field(default_factory=lambda: _env("SMTP_COMMAND_TIMEOUT", 15.0))
    global_concurrency: int = field(default_factory=lambda: _env("SMTP_GLOBAL_CONCURRENCY", 8))
    per_host_concurrency: int = field(default_factory=lambda: _env("SMTP_PER_HOST", 1))
    mx_min_interval: float = field(default_factory=lambda: _env("SMTP_MX_MIN_INTERVAL", 2.0))
    rcpt_per_session: int = field(default_factory=lambda: _env("SMTP_RCPT_PER_SESSION", 10))
    greylist_retry_delay: float = field(default_factory=lambda: _env("GREYLIST_RETRY_DELAY", 90.0))
    dns_mode: str = field(default_factory=lambda: _env("DNS_MODE", "auto"))
    egress_check_host: str = field(default_factory=lambda: _env("EGRESS_CHECK_HOST", "gmail-smtp-in.l.google.com"))
    skip_egress_check: bool = field(default_factory=lambda: _env("SKIP_EGRESS_CHECK", False))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "data")).resolve())
    allow_private_mx: bool = False   # tests only

    def __post_init__(self):
        if not self.mail_from:
            self.mail_from = f"verify@{self.helo_host}"
