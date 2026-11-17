"""SMTP engine against a REAL mail server: Postfix 3.8 + postgrey on 127.0.0.1:25, configured
by lab/setup_lab_mta.sh with test domains we control (*.mock). No third-party server is
contacted. Postfix's own log is used as independent evidence of what the engine sent.

  sudo bash lab/setup_lab_mta.sh
  EMAILISH_LAB_MTA=1 pytest tests/test_lab_mta.py -v -s
"""
import asyncio
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from app.engine.models import Certainty, SmtpStatus, Status
from app.engine.verifier import Verifier
from lab.lab_env import lab_dns, lab_settings

pytestmark = pytest.mark.skipif(os.environ.get("EMAILISH_LAB_MTA") != "1",
                                reason="needs the Postfix lab: sudo bash lab/setup_lab_mta.sh; EMAILISH_LAB_MTA=1")
LOG = Path("/var/log/postfix-lab.log")


def lab_verifier(**kw) -> Verifier:
    v = Verifier(lab_settings(**kw), dns_backend=lab_dns())
    asyncio.run(v.init())
    return v


def log_lines(settle: float = 1.5) -> list[str]:
    time.sleep(settle)          # Postfix writes its log asynchronously (postlogd)
    return LOG.read_text(errors="replace").splitlines() if LOG.exists() else []


def smtpd_sessions(lines: list[str]) -> list[str]:
    """SMTP sessions that issued commands. The engine's egress check (TCP connect + close,
    logged by Postfix as 'commands=0/0') is not a session."""
    return [l for l in lines if "postfix/smtpd" in l and "disconnect from" in l and "commands=0/0" not in l]


def rcpt_total(session_line: str) -> int:
    m = re.search(r"rcpt=(\d+)(?:/(\d+))?", session_line)   # Postfix logs rcpt=accepted/total
    return int(m.group(2) or m.group(1)) if m else 0


def run(v, emails):
    return asyncio.run(v.verify_many(emails))


def test_lab_is_up():
    v = lab_verifier()
    assert v.status.smtp_available, v.status.smtp_note


def test_real_postfix_classifications():
    v = lab_verifier()
    before = len(log_lines())
    emails = ["alice@lab-valid.mock", "bob@lab-valid.mock", "info@lab-valid.mock", "nobody@lab-valid.mock",
              "full@lab-valid.mock", "disabled@lab-valid.mock", "anyone@lab-catchall.mock",
              "sales@lab-catchall.mock", "carol@lab-grey.mock", "dave@lab-temp.mock", "erin@lab-blocked.mock",
              "frank@lab-down.mock"]
    t0 = time.monotonic()
    res = run(v, emails)
    took = time.monotonic() - t0
    for e in emails:
        r = res[e]
        print(f"{e:28} {r.email_status.value:10} {r.sub_status:28} {r.smtp_code:10} conf={r.confidence:3} "
              f"| {r.verification_reason[:110]}")
    exp = {
        "alice@lab-valid.mock": (Status.VALID, "accepted"),
        "bob@lab-valid.mock": (Status.VALID, "accepted"),
        "info@lab-valid.mock": (Status.ROLE_BASED, "role_based"),
        "nobody@lab-valid.mock": (Status.INVALID, "mailbox_not_found"),
        "full@lab-valid.mock": (Status.RISKY, "mailbox_quota_exceeded"),
        "disabled@lab-valid.mock": (Status.INVALID, "mailbox_disabled"),
        "anyone@lab-catchall.mock": (Status.CATCH_ALL, "catch_all"),
        "sales@lab-catchall.mock": (Status.ROLE_BASED, "role_based_catch_all"),
        "dave@lab-temp.mock": (Status.UNKNOWN, "mail_server_temporary_error"),
        "erin@lab-blocked.mock": (Status.UNKNOWN, "antispam_system"),
        "frank@lab-down.mock": (Status.UNKNOWN, "failed_smtp_connection"),
    }
    for e, (st, sub) in exp.items():
        assert (res[e].email_status, res[e].sub_status) == (st, sub), (e, res[e].verification_reason)
    # greylisted by real postgrey on the first pass, accepted on the retry pass
    g = res["carol@lab-grey.mock"]
    assert g.email_status == Status.VALID and "after greylist retry" in g.verification_reason, g.verification_reason
    assert took >= 8, "the greylist retry pass must wait before retrying"
    assert res["alice@lab-valid.mock"].certainty == Certainty.CONFIRMED
    assert res["alice@lab-valid.mock"].catch_all is False
    assert res["anyone@lab-catchall.mock"].catch_all is True
    new = log_lines()[before:]
    sessions = smtpd_sessions(new)
    assert sessions, "postfix saw no sessions"
    assert not any("data=" in s for s in sessions), "DATA must never be sent"
    assert any("Greylisted" in l for l in new), "postgrey should have greylisted the first attempt"


def test_one_session_per_ten_recipients_and_one_catchall_probe():
    v = lab_verifier(mx_min_interval=1.0, greylist_retry_delay=0)
    before = len(log_lines())
    emails = [f"nobody{i:02d}@lab-valid.mock" for i in range(22)] + ["alice@lab-valid.mock"]
    t0 = time.monotonic()
    res = run(v, emails)
    took = time.monotonic() - t0
    sessions = smtpd_sessions(log_lines()[before:])
    assert len(sessions) == 3, sessions               # 23 addresses / 10 per session
    rcpts = [rcpt_total(s) for s in sessions]
    assert sum(rcpts) == 23 + 1, rcpts                # + exactly one catch-all probe
    assert took >= 2.0                                # >= 1 s between connections to the same MX
    assert sum(r.email_status == Status.INVALID for r in res.values()) == 22
    assert res["alice@lab-valid.mock"].email_status == Status.VALID


def test_non_fqdn_helo_is_rejected_by_postfix_and_reported_unknown():
    v = lab_verifier(helo_host="localhost", greylist_retry_delay=0)
    res = run(v, ["alice@lab-valid.mock", "nobody@lab-valid.mock"])
    for e, r in res.items():
        print(e, r.email_status.value, r.sub_status, r.verification_reason[:120])
        assert r.email_status == Status.UNKNOWN and r.sub_status == "antispam_system", e
        assert "Helo command rejected" in r.verification_reason


def test_allowlist_blocks_connections_to_other_domains():
    v = lab_verifier(smtp_allowed_domains="lab-valid.mock", greylist_retry_delay=0)
    before = len(log_lines())
    res = run(v, ["anyone@lab-catchall.mock", "dave@lab-temp.mock"])
    assert all(r.email_status == Status.UNKNOWN and r.sub_status == "smtp_not_permitted" for r in res.values())
    assert all(r.smtp_status == SmtpStatus.NOT_CHECKED for r in res.values())
    assert not smtpd_sessions(log_lines()[before:]), "no SMTP session may be opened for non-allowed domains"
    ok = run(v, ["alice@lab-valid.mock"])["alice@lab-valid.mock"]
    assert ok.email_status == Status.VALID


def test_provider_rate_limit_never_produces_invalid():
    """Postfix anvil rate limit (a real provider restriction): after the limit every RCPT gets
    450 4.7.1 'too many recipients'. Those addresses must be UNKNOWN, never INVALID."""
    script = Path(__file__).resolve().parent.parent / "lab" / "setup_lab_mta.sh"
    subprocess.run(["bash", str(script)], env=dict(os.environ, LAB_RCPT_RATE_LIMIT="6"), check=True,
                   capture_output=True)
    try:
        time.sleep(1)
        v = lab_verifier(greylist_retry_delay=0, mx_min_interval=0.2)
        emails = [f"nobody{i:02d}@lab-valid.mock" for i in range(12)]
        res = run(v, emails)
        limited = [r for r in res.values() if "too many recipients" in r.verification_reason.lower()]
        print("rate-limited:", len(limited), {(r.email_status.value, r.sub_status) for r in limited})
        assert limited, "expected Postfix to rate-limit some recipients"
        assert all(r.email_status == Status.UNKNOWN for r in limited)
        rejected = [r for r in res.values() if r.email_status == Status.INVALID]
        assert all(r.smtp_code.startswith("550") for r in rejected)
    finally:
        subprocess.run(["bash", str(script)], env=dict(os.environ, LAB_RCPT_RATE_LIMIT="0"), check=True,
                       capture_output=True)
