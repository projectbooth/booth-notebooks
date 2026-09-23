"""workload.py: minting against booth-core's endpoint (ADR 0058 wire contract)."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from booth_notebooks.identity import hub_username
from booth_notebooks.workload import MintRefused, MintUnavailable, WorkloadMinter, subject_for

URL = "http://core.test/api/internal/workload-tokens"


def minter(handler) -> WorkloadMinter:
    return WorkloadMinter("bwmc.notebooks.abc", URL, "http://core.test", transport=httpx.MockTransport(handler))


async def test_a_mint_sends_the_contract_body_and_credential():
    seen = {}

    def h(req: httpx.Request):
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"token": "tok", "tokenType": "Bearer", "expiresAt": "2026-09-22T10:00:00Z", "role": "viewer"})

    t = await minter(h).mint("acme", "notebook:acme.abc", "user-1")
    assert seen["auth"] == "Bearer bwmc.notebooks.abc"
    assert seen["body"] == {"workspace": "acme", "subject": "notebook:acme.abc", "roleCeiling": "editor", "owner": "user-1"}
    assert (t.token, t.role, t.expires_at.year, t.expires_at.tzinfo is not None) == ("tok", "viewer", 2026, True)


@pytest.mark.parametrize(
    ("status", "exc", "text"),
    [(403, MintRefused, "signed in"), (401, MintRefused, "operator"), (500, MintUnavailable, "500"), (400, MintUnavailable, "400")],
)
async def test_refusals_are_classified(status, exc, text):
    with pytest.raises(exc, match=text):
        await minter(lambda r: httpx.Response(status)).mint("acme", "notebook:x", "u")


async def test_network_failure_and_garbage_are_unavailable_not_refused():
    def boom(r):
        raise httpx.ConnectError("nope")

    with pytest.raises(MintUnavailable):
        await minter(boom).mint("acme", "notebook:x", "u")
    with pytest.raises(MintUnavailable, match="unreadable"):
        await minter(lambda r: httpx.Response(200, json={"nope": 1})).mint("acme", "notebook:x", "u")


def test_the_subject_always_matches_core_grammar_and_never_a_person():
    grammar = re.compile(r"^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9._:-]{1,200}$")  # ADR 0058
    for ws, sub in [("acme", "f81d4fae-7dec-11d0-a765-00a0c91e6bf6"), ("a-b-c", "auth0|xyz"), ("x", "me@example.com")]:
        assert grammar.match(subject_for(hub_username(ws, sub)))


def test_from_dir_reads_the_core_secret_and_is_absent_without_it(tmp_path):
    assert WorkloadMinter.from_dir(str(tmp_path)) is None
    (tmp_path / "credential").write_text("bwmc.notebooks.x\n")
    (tmp_path / "url").write_text(URL)
    m = WorkloadMinter.from_dir(str(tmp_path))
    assert m is not None and m.issuer == ""
    (tmp_path / "issuer").write_text("http://core.test")
    assert WorkloadMinter.from_dir(str(tmp_path)).issuer == "http://core.test"
