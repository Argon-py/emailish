"""Command-line use of the same engine.

  python -m app.cli check someone@example.com other@example.org
  python -m app.cli file contacts.xlsx [--column Email] [--out results]
  python -m app.cli health
  python -m app.cli diagnose [--domain yourdomain.com] [--target host] [--no-public-ip]
  python -m app.cli provider-check
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import files
from .config import Settings
from .engine.verifier import Verifier


async def _check(emails: list[str]) -> int:
    v = Verifier(Settings())
    st = await v.init()
    print(f"# DNS: {st.dns_backend}\n# SMTP: {st.smtp_note}", file=sys.stderr)
    res = await v.verify_many(emails)
    for e in emails:
        print(json.dumps(res[e].to_json()))
    return 0


async def _file(path: Path, column: str | None, out: Path | None) -> int:
    df = files.read_table(path)
    col = column or files.detect_email_column(df)[0]
    if not col:
        print("Could not detect an email column; pass --column", file=sys.stderr)
        return 2
    v = Verifier(Settings())
    st = await v.init()
    print(f"# DNS: {st.dns_backend}\n# SMTP: {st.smtp_note}\n# column: {col}", file=sys.stderr)

    def progress(done, total, phase):
        print(f"\r{phase}: {done}/{total}", end="", file=sys.stderr)

    raws = [str(x) for x in df[col].tolist()]
    res = await v.verify_many(list(dict.fromkeys(raws)), progress=progress)
    print(file=sys.stderr)
    table = files.build_output(df, col, res)
    out = out or path.with_name(path.stem + "_verified")
    files.write_csv(table, out.with_suffix(".csv"))
    files.write_xlsx(table, out.with_suffix(".xlsx"))
    print(f"wrote {out.with_suffix('.csv')} and {out.with_suffix('.xlsx')}", file=sys.stderr)
    return 0


async def _provider_check() -> int:
    from .config import DOTENV_FILES
    from .providers import PROVIDERS, ProviderFatal, client_for, provider_from_settings
    s = Settings()
    print("settings file(s) read:", ", ".join(str(p) for p in DOTENV_FILES) or "none (.env not found)")
    for name, cls in PROVIDERS.items():
        print(f"  {cls.key_env:26} {'set' if s.provider_key(name) else 'not set'}")
    try:
        p = provider_from_settings(s)
    except ValueError as e:
        print(e)
        return 2
    if not p:
        print("\nNo provider in use. Put ONE key in .env (e.g. MILLIONVERIFIER_API_KEY=...), or several keys plus "
              "EMAILISH_PROVIDER=zerobounce|millionverifier|hunter.")
        return 1
    try:
        async with client_for(p) as c:
            cr = await p.credits(c)
    except ProviderFatal as e:
        print(f"\n{p.label}: KEY REJECTED ({e})")
        return 1
    except Exception as e:
        print(f"\n{p.label}: could not reach the API ({type(e).__name__}: {e})")
        return 1
    print(f"\nProvider in use: {p.label}. Credits available: {cr:g}" if cr is not None else
          f"\nProvider in use: {p.label}. Key accepted.")
    print(f"Per-job limit (EMAILISH_PROVIDER_MAX_CHECKS): {s.provider_max_checks}")
    return 0


async def _health() -> int:
    v = Verifier(Settings())
    st = await v.init()
    print(json.dumps(st.__dict__, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="emailish")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("emails", nargs="+")
    f = sub.add_parser("file")
    f.add_argument("path", type=Path)
    f.add_argument("--column")
    f.add_argument("--out", type=Path)
    sub.add_parser("health")
    sub.add_parser("provider-check", help="check the provider API key and credit balance (free, no credits used)")
    d = sub.add_parser("diagnose", help="can this machine run SMTP diagnostics? (sends no MAIL/RCPT/DATA)")
    d.add_argument("--domain", help="your own domain: test its mail server (banner + EHLO only)")
    d.add_argument("--target", help="host to test port 25 against (default: neutral portquiz.net)")
    d.add_argument("--no-public-ip", action="store_true", help="skip public IP / reverse DNS check")
    a = ap.parse_args(argv)
    if a.cmd == "provider-check":
        return asyncio.run(_provider_check())
    if a.cmd == "diagnose":
        from .diagnose import diagnose
        return asyncio.run(diagnose(a.domain, a.target, not a.no_public_ip))
    if a.cmd == "check":
        return asyncio.run(_check(a.emails))
    if a.cmd == "file":
        return asyncio.run(_file(a.path, a.column, a.out))
    return asyncio.run(_health())


if __name__ == "__main__":
    sys.exit(main())
