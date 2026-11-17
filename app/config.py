"""Settings, read from environment variables (prefix EMAILISH_)."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_dotenv() -> list[Path]:
    """Read KEY=VALUE lines from a .env file in the emailish folder (or the current folder)
    into the environment, without overriding variables that are already set. This is the
    easy way to store provider API keys on Windows. .env is git-ignored."""
    if os.environ.get("EMAILISH_NO_DOTENV"):
        return []
    loaded = []
    for p in dict.fromkeys([ROOT / ".env", Path.cwd() / ".env"]):
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k.startswith("export "):
                k = k[7:].strip()
            if k and k not in os.environ:
                os.environ[k] = v
        loaded.append(p)
    return loaded


DOTENV_FILES = load_dotenv()


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
    # Comma-separated domains. When set, SMTP is ONLY used for these domains (and their
    # subdomains); every other address is reported UNKNOWN / smtp_not_permitted without any
    # connection. Use it for controlled testing with domains you own.
    smtp_allowed_domains: str = field(default_factory=lambda: _env("SMTP_ALLOWED_DOMAINS", ""))
    # --- commercial provider for addresses the local engine cannot settle -------------
    # EMAILISH_PROVIDER: zerobounce | millionverifier | hunter | none | empty = auto (the one key set)
    provider: str = field(default_factory=lambda: _env("PROVIDER", ""))
    zerobounce_api_key: str = field(default_factory=lambda: os.environ.get("ZEROBOUNCE_API_KEY", ""))
    millionverifier_api_key: str = field(default_factory=lambda: os.environ.get("MILLIONVERIFIER_API_KEY", ""))
    hunter_api_key: str = field(default_factory=lambda: os.environ.get("HUNTER_API_KEY", ""))
    # Safety cap: at most this many paid provider checks per job (0 = no provider checks).
    provider_max_checks: int = field(default_factory=lambda: _env("PROVIDER_MAX_CHECKS", 5000))
    # Also send CATCH_ALL results to the provider (some providers resolve part of them; costs credits).
    provider_include_catch_all: bool = field(default_factory=lambda: _env("PROVIDER_INCLUDE_CATCH_ALL", False))
    allow_private_mx: bool = False   # tests only

    def __post_init__(self):
        if not self.mail_from:
            self.mail_from = f"verify@{self.helo_host}"

    def provider_key(self, name: str) -> str:
        return {"zerobounce": self.zerobounce_api_key, "millionverifier": self.millionverifier_api_key,
                "hunter": self.hunter_api_key}.get(name, "").strip()

    @property
    def smtp_allowlist(self) -> tuple[str, ...]:
        return tuple(d.strip().lower().rstrip(".") for d in self.smtp_allowed_domains.split(",") if d.strip())

    def smtp_permitted(self, domain: str) -> bool:
        allow = self.smtp_allowlist
        d = domain.lower().rstrip(".")
        return not allow or any(d == a or d.endswith("." + a) for a in allow)
