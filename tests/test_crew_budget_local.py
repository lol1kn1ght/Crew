"""Profile-local budget regressions: real body/board/files, no shared-guard boolean mocks."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))
import crew_card as card
import kernel_board as K


@pytest.fixture
def bound(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    (home / "config.yaml").write_text("crew:\n  role: worker\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CREW_PROFILE_PREFIX", "crew-")
    monkeypatch.delenv("CREW_COORDINATOR_TURN", raising=False)
    monkeypatch.setenv("KANBAN_DB", str(home / "kanban.db"))
    kb, conn, db = K.open_board(str(home))
    body = K.BODY.replace("Budget: 200000 tokens", "Budget: 200000 tokens\nVerify: proof")
    cid = K.add_card(conn, body=body)
    assert kb.claim_task(conn, cid, claimer="synthetic-worker") is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", cid)
    card.record_origin(cid, env={"origin": "", "session": "synthetic"}, proof_cmd="true")
    spec = importlib.util.spec_from_file_location("crew_budget_local_test", str(REPO / "__init__.py"))
    plug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plug)
    ledger = Path(plug._budget_file(cid))
    b = SimpleNamespace(home=home, kb=kb, conn=conn, cid=cid, body=body, p=plug, ledger=ledger)
    yield b
    conn.close()


def set_budget(b, text):
    body = b.body.replace("Budget: 200000 tokens\n", text)
    assert b.kb.edit_task(b.conn, b.cid, body=body)
    return body


def ledger(b, value):
    b.ledger.parent.mkdir(parents=True, exist_ok=True)
    b.ledger.write_text(json.dumps(value), encoding="utf-8")


def guard(b, tool="terminal", args=None):
    return b.p.crew_tool_guard(tool_name=tool, args=args or {"command": "pwd"}, session_id="S", turn_id="T")


@pytest.mark.parametrize("token", ["200000", "200,000", "200_000", "1,234,567", "1_234_567"])
@pytest.mark.parametrize("suffix", ["", " tokens"])
def test_supported_full_budget_consistent_across_paths(bound, token, suffix):
    b = bound
    body = set_budget(b, "  bUdGeT: " + token + suffix + "\n")
    expected = int(token.replace(",", "").replace("_", ""))
    assert b.p._card_budget(b.cid) == expected
    assert card.parse_contract(body)["budget"] == expected
    assert card._recovery_budget(b.cid, {}) == (expected, "")
    assert guard(b) is None


BAD_BUDGETS = ["", "Budget:\n", "Budget: 0 tokens\n", "Budget: -200000 tokens\n",
               "Budget: +200000 tokens\n", "Budget: 200000oops\n", "Budget: 200000.5 tokens\n",
               "Budget: 2e5 tokens\n", "Budget: 200000 tokens typo\n", "Budget: about 200000 tokens\n",
               "Budget: 200,,000 tokens\n", "Budget: 20_00_00 tokens\n", "Budget: 200000_ tokens\n",
               "Budget: 1,234_567 tokens\n", "Budget: 200000 tokens\nBudget: 300000 tokens\n",
               "Budget: 200000 tokens\nBudget: typo\n"]


@pytest.mark.parametrize("text", BAD_BUDGETS)
def test_invalid_ceiling_is_refusal_not_guard_disable(bound, text):
    b = bound
    body = set_budget(b, text)
    assert b.p._card_budget(b.cid) is None
    assert card.parse_contract(body)["budget"] is None
    assert card._recovery_budget(b.cid, {})[1]
    result = guard(b)
    assert result and result["action"] == "block"
    assert "budget" in result["message"].lower()


def test_same_process_observes_ceiling_change_and_removal(bound):
    b = bound
    ledger(b, {"used": 150000})
    assert guard(b) is None
    set_budget(b, "Budget: 100000 tokens\n")
    assert guard(b)["action"] == "block"
    set_budget(b, "Budget: 300000 tokens\n")
    assert guard(b) is None
    set_budget(b, "")
    assert guard(b)["action"] == "block"


def test_crlf_contract_is_still_a_valid_contract(bound):
    b = bound
    body = b.body.replace("\n", "\r\n")
    assert b.kb.edit_task(b.conn, b.cid, body=body)
    assert b.p._card_budget(b.cid) == 200000
    assert card.parse_contract(body)["budget"] == 200000
    assert card._recovery_budget(b.cid, {}) == (200000, "")
    assert guard(b) is None


def test_observer_records_fresh_ceiling_after_prior_read(bound):
    b = bound
    assert b.p._card_budget(b.cid) == 200000
    set_budget(b, "Budget: 300000 tokens\n")
    b.p.crew_budget_account(usage={"total_tokens": 10})
    assert json.loads(b.ledger.read_text())["budget"] == 300000


DAMAGED = [b'{broken', b'[]', b'null', b'{}', b'{"calls": 1}', b'{"used": -1}',
           b'{"used": true}', b'{"used": false}', b'{"used": "many"}', b'{"used": "12"}',
           b'{"used": null}', b'{"used": []}', b'{"used": NaN}', b'{"used": Infinity}',
           b'{"used": -Infinity}', b'\xff']


@pytest.mark.parametrize("data", DAMAGED)
def test_existing_damaged_ledger_unknown_blocks_and_observer_preserves_bytes(bound, data):
    b = bound
    b.ledger.parent.mkdir(parents=True)
    b.ledger.write_bytes(data)
    assert b.p._budget_used(b.cid) is None, "existing invalid accounting must not turn into zero"
    result = guard(b)
    assert result and result["action"] == "block"
    assert "unknown" in result["message"].lower()
    b.p.crew_budget_account(usage={"total_tokens": 50})
    assert b.ledger.read_bytes() == data
    assert not Path(str(b.ledger) + ".tmp").exists()


@pytest.mark.parametrize("data", DAMAGED)
def test_observer_alone_never_replaces_existing_damage(bound, data):
    b = bound
    b.ledger.parent.mkdir(parents=True)
    b.ledger.write_bytes(data)
    b.p.crew_budget_account(usage={"total_tokens": 50})
    assert b.ledger.read_bytes() == data


def test_unreadable_ledger_is_unknown_and_untouched(bound):
    b = bound
    b.ledger.mkdir(parents=True)  # deterministic EISDIR, independent of uid/permission bypass
    (b.ledger / "evidence").write_bytes(b"retain")
    assert b.p._budget_used(b.cid) is None
    assert guard(b)["action"] == "block"
    b.p.crew_budget_account(usage={"total_tokens": 50})
    assert (b.ledger / "evidence").read_bytes() == b"retain"
    assert not Path(str(b.ledger) + ".tmp").exists()


@pytest.mark.parametrize("used", [0, 17, 17.75])
def test_valid_legacy_used_and_optional_fields_keep_arithmetic(bound, used):
    b = bound
    ledger(b, {"used": used, "evidence": "legacy"})
    assert b.p._budget_used(b.cid) == int(used)
    assert guard(b) is None
    usage = {"total_tokens": 1200, "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 1000, "cache_write_tokens": 40}
    b.p.crew_budget_account(usage=usage)
    got = json.loads(b.ledger.read_text())
    assert got["used"] == int(used) + round(100 + 50 + 1000 * 0.1 + 40 * 1.25)
    assert got["raw_total"] == 1200 and got["calls"] == 1
    assert got["evidence"] == "legacy"
    assert got["budget"] == 200000
    b.p.crew_budget_account(usage={"total_tokens": 10})
    updated = json.loads(b.ledger.read_text())
    assert updated["used"] == got["used"] + 10
    assert updated["calls"] == 2 and updated["raw_total"] == 1210


def test_missing_ledger_compatibility_and_first_response_creation(bound):
    b = bound
    assert not b.ledger.exists()
    assert b.p._budget_used(b.cid) == 0
    assert guard(b) is None
    b.p.crew_budget_account(usage={"total_tokens": 50})
    got = json.loads(b.ledger.read_text())
    assert got["used"] == 50 and got["budget"] == 200000 and got["calls"] == 1


@pytest.mark.parametrize("problem", ["ceiling", "ledger", "spent"])
def test_emergency_tools_remain_available(bound, problem):
    b = bound
    if problem == "ceiling":
        set_budget(b, "Budget: typo\n")
    else:
        ledger(b, {"used": -1 if problem == "ledger" else 200001})
    for tool in ("kanban_block", "kanban_comment", "kanban_show", "kanban_heartbeat"):
        assert guard(b, tool, {"task_id": b.cid, "reason": "stop"}) is None


def test_owner_and_unbound_sessions_are_unaffected(bound, monkeypatch):
    b = bound
    set_budget(b, "Budget: broken\n")
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert guard(b) is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", b.cid)
    b.p._ROLE = ""  # real owner profile has no crew role
    assert guard(b) is None


def test_budget_reader_exception_becomes_explicit_refusal(bound, monkeypatch):
    def broken(_card):
        raise OSError("synthetic read failure")
    monkeypatch.setattr(bound.p, "_card_budget", broken)
    assert guard(bound)["action"] == "block"


@pytest.mark.parametrize("problem", ["spent", "ledger", "ceiling"])
def test_existing_pass_close_stays_allowed_but_new_proof_is_not_close(bound, problem):
    b = bound
    ledger(b, {"used": 200001})
    if problem == "ledger":
        b.ledger.write_bytes(b'{broken')
    elif problem == "ceiling":
        set_budget(b, "Budget: typo\n")
    before = b.ledger.read_bytes()
    assert guard(b, "crew_verdict", {"card": b.cid})["action"] == "block"
    assert guard(b, "kanban_complete", {"task_id": b.cid, "result": "done"})["action"] == "block"
    # A real retained verdict row, not _close_ok=True. No proof subprocess is launched here.
    path = Path(card.verdict_path(b.cid))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"verdict": "PASS", "rc": 0, "command": "true",
                                "by": "crew-worker", "ts": time.time()}) + "\n")
    assert card.close_check(b.cid, b.body, card.claimed_at(b.cid))[0]
    result = guard(b, "kanban_complete", {"task_id": b.cid, "result": "done"})
    if problem == "spent":
        assert result["action"] == "modify" and "verdict already PASS" in result["args"]["result"]
        assert json.loads(b.ledger.read_text())["used"] == 200001
    else:
        assert result is None
        assert b.ledger.read_bytes() == before
    assert guard(b, "crew_verdict", {"card": b.cid})["action"] == "block"


@pytest.mark.parametrize("role", ["worker", "content", "verifier", "coordinator"])
def test_valid_budget_keeps_role_script_and_board_guards(bound, role, monkeypatch):
    b = bound
    (b.home / "config.yaml").write_text("crew:\n  role: %s\n" % role)
    b.p._ROLE = None
    ledger(b, {"used": 17})
    assert guard(b) is None
    ordinary = guard(b, "write_file", {"path": "/x/normal.md", "content": "ordinary"})
    if role == "verifier":
        assert ordinary["action"] == "block"
    else:
        assert ordinary is None
    result = guard(b, "write_file", {"path": "/x/.crew/verify.py", "content": "pass"})
    if role in ("worker", "content"):
        assert result["action"] == "block" and "proof files belong" in result["message"]
    else:
        assert result is None
    assert guard(b, "terminal", {"command": "sqlite3 kanban.db 'select 1'"})["action"] == "block"
    if role == "coordinator":
        monkeypatch.setenv("CREW_COORDINATOR_TURN", "1")
        assert guard(b, "write_file", {"path": "/x/normal.md", "content": "ordinary"})["action"] == "block"
        assert guard(b) is None


def test_valid_budget_does_not_weaken_rework_cap(bound):
    b = bound
    path = Path(card.verdict_path(b.cid))
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"verdict": "FAIL", "rc": 1, "command": "true", "by": "crew-worker", "ts": time.time()}
    path.write_text(json.dumps(row) + "\n" + json.dumps(dict(row, ts=row["ts"] + 1)) + "\n")
    assert card.rework_fails(b.cid) == 2
    result = guard(b, "kanban_complete", {"task_id": b.cid, "result": "done"})
    assert result["action"] == "block"
    # Use the real independent-review contract to reach the preserved rework cap.
    assert b.kb.edit_task(b.conn, b.cid, body=b.body.replace("Verify: proof", "Verify: independent"))
    result = guard(b, "kanban_request_review", {"task_id": b.cid, "reviewer": "crew-verifier"})
    assert result["action"] == "block" and "rework cap" in result["message"]


@pytest.mark.parametrize("changes", [{"command": "false"}, {"by": "not-a-crew-role"},
                                      {"verdict": "FAIL", "rc": 1}, {"ts": 1}])
def test_unknown_accounting_close_exception_requires_valid_existing_pass(bound, changes):
    b = bound
    b.ledger.parent.mkdir(parents=True)
    b.ledger.write_bytes(b'{broken')
    path = Path(card.verdict_path(b.cid))
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict({"verdict": "PASS", "rc": 0, "command": "true", "by": "crew-worker", "ts": time.time()}, **changes)
    path.write_text(json.dumps(row) + "\n")
    assert guard(b, "kanban_complete", {"task_id": b.cid, "result": "done"})["action"] == "block"
    assert b.ledger.read_bytes() == b'{broken'
