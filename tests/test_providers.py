"""Commercial provider fallback (ZeroBounce / MillionVerifier / Hunter) against a SIMULATED API
(httpx.MockTransport). No real API is called and no credits are spent."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import fake_dns, make_settings, run

from app.engine.models import Certainty, Status
from app.engine.verifier import Verifier
from app import providers as P


def mv_transport(answers: dict, calls: list, credits=1000):
    """Simulated MillionVerifier: answers maps email -> (result, subresult, role) or 'fatal'/'boom'."""
    def handler(req: httpx.Request):
        q = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
        if req.url.path.endswith("/credits"):
            return httpx.Response(200, json={"credits": credits})
        email = q["email"]
        calls.append(email)
        a = answers.get(email, ("unknown", "unknown", False))
        if a == "fatal":
            return httpx.Response(200, json={"email": email, "result": "error", "error": "Insufficient credits"})
        if a == "boom":
            raise httpx.ConnectError("simulated network failure")
        res, sub, role = a
        return httpx.Response(200, json={"email": email, "result": res, "subresult": sub, "role": role,
                                         "free": False, "quality": "good", "livemode": True, "error": ""})
    return httpx.MockTransport(handler)


def mk(cluster, answers, calls, credits=1000, **kw):
    s = make_settings(cluster, smtp_enabled=False, **kw)      # like a laptop: no local SMTP
    p = P.MillionVerifier("simulated-key")
    p.transport = mv_transport(answers, calls, credits)
    v = Verifier(s, dns_backend=fake_dns(), provider=p)
    run(v.init())
    return v


def test_only_unsettled_addresses_go_to_provider_once(cluster):
    calls = []
    v = mk(cluster, {"alice@valid.mock": ("ok", "ok", False), "ghost@valid.mock": ("invalid", "no_mailbox", False),
                     "info@valid.mock": ("ok", "ok", True)}, calls)
    emails = ["alice@valid.mock", " ALICE@valid.mock ", "ghost@valid.mock", "info@valid.mock",
              "a@doesnotexist.mock", "x@mailinator.com", "bad@@x", "u@nullmx.mock"]
    res = run(v.verify_many(emails))
    assert sorted(calls) == ["alice@valid.mock", "ghost@valid.mock", "info@valid.mock"]   # each once
    a = res["alice@valid.mock"]
    assert a.email_status == Status.VALID and a.verified_by == "millionverifier"
    assert a.sub_status == "millionverifier:ok" and a.certainty == Certainty.CONFIRMED
    assert res[" ALICE@valid.mock "].email_status == Status.VALID
    g = res["ghost@valid.mock"]
    assert g.email_status == Status.INVALID and g.sub_status == "millionverifier:invalid/no_mailbox"
    assert res["info@valid.mock"].email_status == Status.ROLE_BASED
    # settled locally, for free
    for e, st in (("a@doesnotexist.mock", Status.INVALID), ("x@mailinator.com", Status.DISPOSABLE),
                  ("bad@@x", Status.INVALID), ("u@nullmx.mock", Status.INVALID)):
        assert res[e].email_status == st and res[e].verified_by == "emailish", e
    st = v.last_provider_stats
    assert st["candidates"] == 3 and st["checked"] == 3 and st["errors"] == 0


def test_provider_unknown_stays_unknown(cluster):
    calls = []
    v = mk(cluster, {"alice@valid.mock": ("unknown", "greylisted", False)}, calls)
    r = run(v.verify_one("alice@valid.mock"))
    assert r.email_status == Status.UNKNOWN and r.confidence == 0 and r.verified_by == "millionverifier"


def test_per_job_cap(cluster):
    calls = []
    v = mk(cluster, {}, calls, provider_max_checks=2)
    res = run(v.verify_many([f"user{i}@valid.mock" for i in range(5)]))
    assert len(calls) == 2
    skipped = [r for r in res.values() if r.verified_by == "emailish"]
    assert len(skipped) == 3 and all("per-job limit" in r.verification_reason for r in skipped)
    assert v.last_provider_stats["not_checked_cap"] == 3


def test_credit_balance_caps_checks(cluster):
    calls = []
    v = mk(cluster, {}, calls, credits=1)
    assert v.status.provider_credits == 1
    run(v.verify_many(["a1@valid.mock", "a2@valid.mock", "a3@valid.mock"]))
    assert len(calls) == 1


def test_fatal_error_stops_all_calls(cluster):
    calls = []
    answers = {f"u{i}@valid.mock": "fatal" for i in range(6)}
    v = mk(cluster, answers, calls)
    v.provider.concurrency = 1
    res = run(v.verify_many(list(answers)))
    assert len(calls) == 1, "after a key/credit error no further calls may be made"
    assert all(r.email_status == Status.UNKNOWN and r.verified_by == "emailish" for r in res.values())
    assert all("Insufficient credits" in r.verification_reason for r in res.values())
    assert "Insufficient credits" in v.last_provider_stats["stopped"]


def test_network_error_keeps_local_result(cluster):
    calls = []
    v = mk(cluster, {"alice@valid.mock": "boom", "bob@valid.mock": ("ok", "ok", False)}, calls)
    res = run(v.verify_many(["alice@valid.mock", "bob@valid.mock"]))
    assert res["alice@valid.mock"].email_status == Status.UNKNOWN
    assert "check failed" in res["alice@valid.mock"].verification_reason
    assert res["bob@valid.mock"].email_status == Status.VALID
    assert v.last_provider_stats["errors"] == 1


def test_bad_key_at_startup_disables_provider(cluster):
    p = P.MillionVerifier("bad")
    p.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"result": "error", "error": "apikey_not_found"}))
    v = Verifier(make_settings(cluster, smtp_enabled=False), dns_backend=fake_dns(), provider=p)
    run(v.init())
    assert v.provider is None and any("apikey_not_found" in w for w in v.status.warnings)
    assert run(v.verify_one("alice@valid.mock")).verified_by == "emailish"


def test_local_smtp_verdict_is_not_resent(cluster):
    """With working local SMTP, confirmed results are final: only inconclusive ones go out."""
    calls = []
    s = make_settings(cluster)
    p = P.MillionVerifier("k")
    p.transport = mv_transport({}, calls)
    v = Verifier(s, dns_backend=fake_dns(), provider=p)
    run(v.init())
    res = run(v.verify_many(["alice@valid.mock", "nobody@valid.mock", "x@blocked.mock"]))
    assert res["alice@valid.mock"].verified_by == "emailish" and res["alice@valid.mock"].email_status == Status.VALID
    assert res["nobody@valid.mock"].email_status == Status.INVALID
    assert calls == ["x@blocked.mock"]          # only the address our SMTP probe could not settle


def test_hunter_polls_202_and_handles_451(monkeypatch):
    seq = {"a@x.com": [202, 200], "b@x.com": [451]}

    def handler(req):
        email = parse_qs(urlparse(str(req.url)).query)["email"][0]
        code = seq[email].pop(0)
        if code == 200:
            return httpx.Response(200, json={"data": {"status": "valid", "score": 97, "result": "deliverable"}})
        return httpx.Response(code, json={})

    async def fast_sleep(_):
        return None
    monkeypatch.setattr(P.asyncio, "sleep", fast_sleep)
    h = P.Hunter("k")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await h.check(c, "a@x.com"), await h.check(c, "b@x.com")
    a, b = asyncio.run(go())
    assert a.status == Status.VALID and b.status == Status.UNKNOWN and "opted out" in b.sub_status.replace("_", " ")


@pytest.mark.parametrize("j,status", [
    ({"status": "valid", "sub_status": ""}, Status.VALID),
    ({"status": "valid", "sub_status": "accept_all"}, Status.VALID),
    ({"status": "invalid", "sub_status": "mailbox_not_found"}, Status.INVALID),
    ({"status": "catch-all", "sub_status": ""}, Status.CATCH_ALL),
    ({"status": "unknown", "sub_status": "greylisted"}, Status.UNKNOWN),
    ({"status": "do_not_mail", "sub_status": "disposable"}, Status.DISPOSABLE),
    ({"status": "do_not_mail", "sub_status": "role_based"}, Status.ROLE_BASED),
    ({"status": "do_not_mail", "sub_status": "toxic"}, Status.RISKY),
    ({"status": "spamtrap", "sub_status": ""}, Status.RISKY),
    ({"status": "abuse", "sub_status": ""}, Status.RISKY),
])
def test_zerobounce_mapping(j, status):
    v = P.map_zerobounce(j)
    assert v.status == status and v.sub_status.startswith("zerobounce:")


def test_zerobounce_accept_all_is_marked_uncertain():
    v = P.map_zerobounce({"status": "valid", "sub_status": "accept_all"})
    assert v.certainty == Certainty.UNCERTAIN and "history" in v.reason


@pytest.mark.parametrize("j,status", [
    ({"result": "ok", "role": False}, Status.VALID), ({"result": "ok", "role": True}, Status.ROLE_BASED),
    ({"result": "invalid", "subresult": "no_mailbox"}, Status.INVALID), ({"result": "catch_all"}, Status.CATCH_ALL),
    ({"result": "disposable"}, Status.DISPOSABLE), ({"result": "unknown"}, Status.UNKNOWN),
])
def test_millionverifier_mapping(j, status):
    assert P.map_millionverifier(j).status == status


def test_millionverifier_test_key_is_flagged():
    assert "TEST KEY" in P.map_millionverifier({"result": "ok", "livemode": False}).reason


@pytest.mark.parametrize("d,status", [
    ({"status": "valid"}, Status.VALID), ({"status": "invalid"}, Status.INVALID),
    ({"status": "accept_all"}, Status.CATCH_ALL), ({"status": "disposable"}, Status.DISPOSABLE),
    ({"status": "unknown"}, Status.UNKNOWN), ({"status": "webmail", "result": "deliverable"}, Status.VALID),
    ({"status": "webmail", "result": "undeliverable"}, Status.INVALID), ({"status": "webmail", "result": "risky"}, Status.RISKY),
])
def test_hunter_mapping(d, status):
    assert P.map_hunter({"data": d}).status == status


def test_provider_selection():
    from app.config import Settings
    assert P.provider_from_settings(Settings(provider="")) is None
    assert isinstance(P.provider_from_settings(Settings(millionverifier_api_key="k")), P.MillionVerifier)
    assert P.provider_from_settings(Settings(zerobounce_api_key="a", hunter_api_key="b")) is None   # ambiguous
    assert isinstance(P.provider_from_settings(Settings(zerobounce_api_key="a", hunter_api_key="b",
                                                        provider="hunter")), P.Hunter)
    assert P.provider_from_settings(Settings(millionverifier_api_key="k", provider="none")) is None
    with pytest.raises(ValueError):
        P.provider_from_settings(Settings(provider="nonsense"))


def test_dotenv_loader(tmp_path, monkeypatch):
    from app import config
    (tmp_path / ".env").write_text("# comment\nexport MILLIONVERIFIER_API_KEY = 'abc123'\nEMAILISH_PROVIDER=millionverifier\n")
    monkeypatch.delenv("EMAILISH_NO_DOTENV", raising=False)
    monkeypatch.delenv("MILLIONVERIFIER_API_KEY", raising=False)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    loaded = config.load_dotenv()
    assert loaded and config.os.environ["MILLIONVERIFIER_API_KEY"] == "abc123"
    monkeypatch.delenv("MILLIONVERIFIER_API_KEY")
    monkeypatch.delenv("EMAILISH_PROVIDER", raising=False)


def test_job_summary_reports_provider_usage(cluster, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    import time
    calls = []
    s = make_settings(cluster, smtp_enabled=False, data_dir=tmp_path / "d")
    p = P.MillionVerifier("k")
    p.transport = mv_transport({"alice@valid.mock": ("ok", "ok", False)}, calls)
    app = create_app(s, verifier=Verifier(s, dns_backend=fake_dns(), provider=p))
    with TestClient(app) as c:
        h = c.get("/api/health").json()
        assert h["provider"] == "MillionVerifier" and h["provider_credits"] == 1000
        up = c.post("/api/upload", files={"file": ("x.csv", b"email\nalice@valid.mock\nbad@@x\n", "text/csv")}).json()
        c.post(f"/api/jobs/{up['job_id']}/start", json={"email_column": "email"})
        for _ in range(100):
            j = c.get(f"/api/jobs/{up['job_id']}").json()
            if j["status"] == "done":
                break
            time.sleep(0.1)
        assert j["summary"]["provider"]["checked"] == 1
        assert j["summary"]["verified_by"] == {"millionverifier": 1, "emailish": 1}
        csv_text = c.get(f"/api/jobs/{up['job_id']}/download?format=csv").content.decode("utf-8-sig")
        assert "verified_by" in csv_text.splitlines()[0] and "millionverifier" in csv_text
