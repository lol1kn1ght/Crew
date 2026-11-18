"""Internal recovery on a native kernel board; only CLI process transport is replaced."""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))
import crew_card as card
import crew_coordinator as cc
import kernel_board as K


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CREW_PROFILE_PREFIX", "crew-")
    monkeypatch.setenv("KANBAN_DB", str(home / "kanban.db"))
    for role in ("worker", "content", "verifier", "coordinator"):
        profile = home / "profiles" / ("crew-" + role)
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("crew:\n  role: %s\n" % role, encoding="utf-8")
    kb, conn, db = K.open_board(str(home))
    cli = K.kernel_cli(db)
    monkeypatch.setattr(card.subprocess, "run", cli)
    script = home / "landing" / ".crew" / "recovery" / "verify.py"
    script.parent.mkdir(parents=True)
    script.write_text("raise ValueError('anchored Steps parser')\n", encoding="utf-8")
    command = '%s "%s"' % (card.PYTHON, script.as_posix())
    body = ("Role: content\nCoordinator: owner/s\nBudget: 200000 tokens\nVerify: independent\n"
            "GOAL: g\nDone when: d\nproof command: %s\n" % command)
    result = SimpleNamespace(home=home, kb=kb, conn=conn, db=db, cli=cli, body=body,
                             command=command, script=script, ctx=cc.Ctx(db, say=lambda *_: None))
    yield result
    conn.close()


def stopped(b, status="blocked", reviewed=True, body=None):
    cid = K.add_card(b.conn, body=b.body if body is None else body, assignee="crew-content")
    if reviewed:
        assert b.kb.claim_task(b.conn, cid, claimer="writer") is not None
        assert b.kb.request_review(b.conn, cid, reviewer="crew-verifier", summary="artifact ready")
        assert b.kb.claim_review_task(b.conn, cid, claimer="verifier") is not None
    assert b.kb.block_task(b.conn, cid, kind="needs_input", reason="proof script parser failed")
    if status == "triage":
        assert b.kb.unblock_task(b.conn, cid)
        if reviewed:
            assert b.kb.claim_review_task(b.conn, cid, claimer="verifier-again") is not None
        assert b.kb.block_task(b.conn, cid, kind="needs_input", reason="second parser failure")
    assert b.kb.get_task(b.conn, cid).status == status
    card.record_origin(cid, env={"origin": "", "session": "synthetic"}, proof_cmd=b.command)
    card.record_script_hashes(cid, {str(b.script): "old-hash"}, by="verifier")
    for i, root in enumerate((b.home, b.home / "profiles" / "crew-content", b.home / "profiles" / "crew-verifier")):
        ledger = root / "crew" / "budget" / (cid + ".json")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_bytes(('{ "used": %d, "budget": 200000, "evidence": "active" }\n' % (73000 + i)).encode())
        Path(str(ledger) + ".spent").write_bytes(b'{"used":45000,"budget":190000,"evidence":"retained"}\n')
    return cid


def ledgers(b, cid):
    return {str(p.relative_to(b.home)): p.read_bytes() for p in b.home.rglob(cid + ".json*")}


def decision(kind="delegate", **kw):
    if kind == "delegate":
        return dict(decision="revise_script", why="parser repair; retained prose usage 202014", delegate=True, **kw)
    if kind == "accept":
        return dict(decision="revise_script", why="accept script as is", delegate=False, **kw)
    if kind == "rescope":
        return dict(decision="rescope", done_when="d2", fix="repair artifact", **kw)
    return dict(decision="retry", fix="repair artifact", failure_kind="artifact", **kw)


def apply(b, cid, dec, history=()):
    return cc.apply_decision(b.ctx, cc.get_card(b.ctx, cid), list(history), dec)


def evidence(b, cid, before):
    after = ledgers(b, cid)
    task = b.kb.get_task(b.conn, cid)
    print("PRESERVE_RECEIPT " + json.dumps({"status": task.status, "assignee": task.assignee,
          "budget": card.field(task.body, "Budget"), "before": {p: hashlib.sha256(v).hexdigest() for p, v in before.items()},
          "after": {p: hashlib.sha256(v).hexdigest() for p, v in after.items()}}, sort_keys=True))
    assert after == before, "internal recovery must not move active ledgers or overwrite retained .spent bytes"
    assert card.field(task.body, "Budget") == "200000 tokens"
    assert card.close_proof_command(cid) == b.command
    return task


@pytest.mark.parametrize("kind", ["delegate", "accept", "retry", "rescope"])
@pytest.mark.parametrize("status", ["blocked", "triage"])
@pytest.mark.parametrize("budget", [None, 200000], ids=["absent-budget", "same-budget"])
def test_nested_recovery_preserves_budget_ledgers_and_role(board, kind, status, budget):
    b = board
    cid = stopped(b, status)
    before = ledgers(b, cid)
    kwargs = {} if budget is None else {"budget": budget}
    ok, note = apply(b, cid, decision(kind, **kwargs))
    assert ok, note
    task = evidence(b, cid, before)
    if kind == "delegate":
        assert (task.status, task.assignee) == ("review", "crew-verifier")
        assert "Proof script: verifier to revise" in task.body
        assert card.pending_script_authorization(cid)
        assert card.proof_script_hashes(cid) == {str(b.script): "old-hash"}
    else:
        assert (task.status, task.assignee) == ("ready", "crew-content")
        assert "Coordinator fix 1:" in task.body
        assert not card.pending_script_authorization(cid)
        if kind == "accept":
            assert card.proof_script_hashes(cid) == {str(b.script): hashlib.sha256(b.script.read_bytes()).hexdigest()}
        else:
            assert card.proof_script_hashes(cid) == {str(b.script): "old-hash"}


@pytest.mark.parametrize("kind", ["delegate", "accept", "retry", "rescope"])
@pytest.mark.parametrize("budget", [200001, 10**12, 0, -1, True, "200000", 200000.0, None, "lots"])
def test_other_or_invalid_budget_refused_before_any_effect(board, kind, budget):
    b = board
    cid = stopped(b)
    before = ledgers(b, cid)
    task_before = b.kb.get_task(b.conn, cid)
    events_before = list(b.conn.execute("select * from task_events where task_id = ? order by id", (cid,)))
    b.cli.calls.clear()
    ok, note = apply(b, cid, decision(kind, budget=budget, model="candidate-model", provider="openai"))
    assert not ok, "internal recovery must refuse changed/invalid budget before authorization, hash, route or body effects"
    assert "budget" in note.lower()
    assert b.kb.get_task(b.conn, cid) == task_before
    assert list(b.conn.execute("select * from task_events where task_id = ? order by id", (cid,))) == events_before
    assert b.cli.calls == []
    assert ledgers(b, cid) == before


def test_resolve_script_preflights_before_accepting_hash_or_running_proof(board):
    b = board
    cid = stopped(b)
    before = card.proof_script_hashes(cid)
    b.ctx.decider = lambda *_: (json.dumps(decision("accept", budget=300000)), 0)
    dec, source, pending = cc.resolve_script(b.ctx, cc.get_card(b.ctx, cid), [], "script changed")
    assert card.proof_script_hashes(cid) == before
    assert dec["decision"] == "ask_owner"
    assert not pending
    assert all(argv[argv.index("kanban") + 1] == "log" for argv in b.cli.calls)


def test_delegation_supported_bare_claim_interleaving(board, monkeypatch):
    b = board
    cid = stopped(b, reviewed=False)
    before = ledgers(b, cid)
    unblock = b.kb.unblock_task
    claims = []

    def unblock_then_claim(conn, card_id, *args, **kwargs):
        ok = unblock(conn, card_id, *args, **kwargs)
        claims.append(b.kb.claim_task(conn, card_id, claimer="inert-dispatcher"))
        return ok

    monkeypatch.setattr(b.kb, "unblock_task", unblock_then_claim)
    ok, note = apply(b, cid, decision())
    assert ok, note
    assert len(claims) == 1 and claims[0] is not None
    task = evidence(b, cid, before)
    assert (task.status, task.assignee) == ("review", "crew-verifier")


def test_open_parent_stays_gated_without_reset(board):
    b = board
    cid = stopped(b, "triage", reviewed=False)
    parent = K.add_card(b.conn)
    b.kb.link_tasks(b.conn, parent, cid)
    before = ledgers(b, cid)
    ok, note = apply(b, cid, decision("retry"))
    assert ok, note
    task = evidence(b, cid, before)
    assert (task.status, task.assignee) == ("todo", "crew-content")


def test_public_retry_still_resets_active_ledgers_and_ceiling(board):
    b = board
    cid = stopped(b)
    before = ledgers(b, cid)
    result = card.retry_card(cid, budget=300000)
    assert result["ok"] and result["ledgers_moved"] == 3
    assert result["unblock"]["rc"] == 0
    task = b.kb.get_task(b.conn, cid)
    assert (task.status, task.assignee) == ("review", "crew-verifier")
    assert card.field(task.body, "Budget") == "300000 tokens"
    for rel, content in before.items():
        if rel.endswith(".json"):
            assert not (b.home / rel).exists()
            assert Path(str(b.home / rel) + ".spent").read_bytes() == content


def test_directive_replay_and_new_owner_era_identity_on_native_board(board):
    b = board
    cid = stopped(b)
    before = ledgers(b, cid)
    first = decision("retry")
    assert apply(b, cid, first)[0]
    original = b.kb.get_task(b.conn, cid).body
    assert apply(b, cid, decision("retry"))[0]
    assert b.kb.get_task(b.conn, cid).body == original
    card._append_card_event(cid, "crew_decision", first)
    card._append_card_event(cid, "crew_decision", {"decision": "ask_owner", "question": "continue?"})
    history = cc.decisions_of(b.ctx, cid)
    assert cc.fix_count(history) == 0
    assert apply(b, cid, decision("retry"), history)[0]
    task = evidence(b, cid, before)
    assert task.body.count("Coordinator fix 1:") == 1
    assert task.body.count("Coordinator fix 2:") == 1
