"""Session04: native owner wait and park-first settlement, synthetic workers only."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO/'scripts'), str(REPO/'tests')]
import crew_card as card
import crew_coordinator as cc
import crew_heal as heal
import crew_stop as stop
import kernel_board as K
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path/'state'
    home.mkdir()
    for key, value in dict(HERMES_HOME=str(home), KANBAN_DB=str(home/'kanban.db'), CREW_PROFILE_PREFIX='crew-', CREW_STOP_HERMES_ROOT=str(home)).items():
        monkeypatch.setenv(key, value)
    for key in ('HERMES_KANBAN_TASK', 'CREW_COORDINATOR_TURN'):
        monkeypatch.delenv(key, raising=False)
    for role in ('worker', 'verifier', 'coordinator'):
        p = home/'profiles'/('crew-'+role)
        p.mkdir(parents=True)
        (p/'config.yaml').write_text('crew:\n  role: '+role+'\n')
    kb, conn, db = K.open_board(str(home))
    cli = K.kernel_cli(db)
    real_run = subprocess.run
    def routed(argv, **kw):
        return cli(argv, **kw) if 'kanban' in list(argv) else real_run(argv, **kw)
    monkeypatch.setattr(card.subprocess, 'run', routed)
    monkeypatch.setattr(stop, 'KANBAN_DB', db)
    monkeypatch.setattr(heal, 'KANBAN_DB', db)
    monkeypatch.setattr(stop, 'TERM_WAIT_S', .03)
    b = SimpleNamespace(home=home, kb=kb, conn=conn, db=db, cli=cli, ctx=cc.Ctx(db, say=lambda *_: None))
    yield b
    conn.close()


def row(b, cid):
    return cc.get_card(b.ctx, cid)


def reviewed(b):
    cid = K.add_card(b.conn, body=K.BODY+'Verify: independent\n')
    assert b.kb.claim_task(b.conn, cid, claimer='synthetic-writer')
    assert b.kb.request_review(b.conn, cid, reviewer='crew-verifier', summary='ready')
    card.record_origin(cid, env={'origin':'', 'session':'synthetic'}, proof_cmd='true')
    return cid


def ask(b, cid, **extra):
    ok, why = cc.apply_ask_owner(b.ctx, row(b, cid), 'continue?')
    card._append_card_event(cid, 'crew_decision', dict(decision='ask_owner', question='continue?', **extra))
    return ok, why


def test_review_ask_is_real_hold_and_same_reviewer_continues(board):
    b = board
    cid = reviewed(b)
    before = b.kb.get_task(b.conn, cid)
    assert ask(b, cid)[0]
    assert b.kb.get_task(b.conn, cid).status == 'blocked'
    for _ in range(2):
        b.kb.recompute_ready(b.conn)
        assert not b.kb.claim_task(b.conn, cid, claimer='inert-writer')
        assert not b.kb.claim_review_task(b.conn, cid, claimer='inert-reviewer')
    assert card.unstuck_card(cid)[0]
    now = b.kb.get_task(b.conn, cid)
    assert (now.status, now.assignee, now.body) == ('review', 'crew-verifier', before.body)
    assert b.kb.claim_review_task(b.conn, cid, claimer='same-reviewer')


@pytest.mark.parametrize('event', ['specified', 'claimed', 'spawned', 'completed', 'promoted_manual', 'hold_released', 'self_heal', 'comment'])
def test_auto_events_never_answer_owner(board, event):
    b = board
    cid = K.add_card(b.conn, 'blocked')
    ask(b, cid)
    card._append_card_event(cid, event, {})
    assert cc.awaiting_owner(b.ctx, cid, cc.decisions_of(b.ctx, cid))
    assert b.kb.unblock_task(b.conn, cid)
    assert not cc.awaiting_owner(b.ctx, cid, cc.decisions_of(b.ctx, cid))


def test_direct_heal_and_fallback_cannot_release_question(board, monkeypatch):
    b = board
    cid = K.add_card(b.conn, 'blocked')
    card.record_origin(cid, env={'origin':'', 'session':'s'}, proof_cmd='true')
    ask(b, cid)
    def forbidden(*a, **k):
        pytest.fail('proof/pin must not run under owner wait')
    monkeypatch.setattr(heal.crew_safety, 'run_proof', forbidden)
    monkeypatch.setattr(card, 'reroute_after_wall', forbidden)
    assert heal.heal_card(row(b, cid)) is None
    assert heal.heal_stale_verify(row(b, cid), False) is None
    assert heal.heal_dead_model(row(b, cid), False) is None
    assert not heal.release_block(cid)
    assert b.kb.get_task(b.conn, cid).status == 'blocked'


def test_direct_audit_and_decider_respect_wait(board, monkeypatch):
    b = board
    cid = K.add_card(b.conn, 'blocked')
    ask(b, cid)
    def forbidden(*a, **k):
        pytest.fail('automatic audit/decider ran under wait')
    b.ctx.decider = forbidden
    monkeypatch.setattr(cc, 'run_verdict', forbidden)
    card.record_origin(cid, env={'origin':'', 'session':'s'}, proof_cmd='true')
    assert cc.audit_proof(b.ctx, row(b, cid), {'id': 1}).get('action') != 'audit pass'
    assert cc.ask_model(b.ctx, row(b, cid), [])[0] is None


def test_unstuck_must_not_consent_to_proof(board):
    b = board
    cid = K.add_card(b.conn, 'blocked')
    ask(b, cid, proof_ask={'kind':'rescope', 'proposed':'true'})
    ok, text = card.unstuck_card(cid)
    assert not ok and '/crew-proof' in text
    assert b.kb.get_task(b.conn, cid).status == 'blocked'


def test_legacy_triage_continuation_is_not_specify(board):
    b = board
    cid = K.add_card(b.conn, 'triage')
    card._append_card_event(cid, 'crew_decision', {'decision':'ask_owner', 'question':'legacy'})
    old = K.events(b.conn, cid)
    assert card.unstuck_card(cid)[0]
    assert K.events(b.conn, cid)[len(old):] == ['blocked', 'unblocked']
    assert not cc.awaiting_owner(b.ctx, cid, cc.decisions_of(b.ctx, cid))


def test_proof_confirmation_is_not_claimed_release_when_lift_fails(board, monkeypatch):
    b = board
    cid = K.add_card(b.conn, 'blocked')
    ask(b, cid, proof_ask={'kind':'rescope', 'proposed':'true'})
    monkeypatch.setattr(card, 'lift_block', lambda *_: {'rc':1, 'out':'native refused', 'status':'blocked'})
    text = card.owner_proof_answer(cid)
    assert 'confirmed' in text and 'still held' in text and 'native refused' in text
    assert card.pending_proof_ask(cid) is None
    assert cc.awaiting_owner(b.ctx, cid, cc.decisions_of(b.ctx, cid))


@pytest.fixture
def own_worker(board):
    b = board
    cid = K.add_card(b.conn)
    assert b.kb.claim_task(b.conn, cid, claimer='synthetic')
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', 'work kanban task '+cid], start_new_session=True)
    assert os.getpgid(proc.pid) != os.getpgrp()
    fingerprint = dispatch._process_fingerprint(proc.pid)
    assert fingerprint
    task = b.kb.get_task(b.conn, cid)
    dispatch._set_worker_pid(b.conn, cid, proc.pid, run_id=task.current_run_id, claim_lock=task.claim_lock)
    from hermes_state import SessionDB
    sdb = SessionDB(db_path=b.home/'state.db')
    sdb.create_session('synthetic-stop-session', 'cli')
    card._append_card_event(cid, 'session', {'session':'synthetic-stop-session', 'profile':'default'})
    try:
        yield cid, proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        sdb.close()


def test_stop_parks_before_signal_and_preserves_terminal_identity(board, own_worker, monkeypatch):
    b = board
    cid, proc = own_worker
    original = stop.crew_proc.terminate_tree
    calls = []
    def terminate(pid, wait):
        assert b.kb.get_task(b.conn, cid).status == 'blocked'
        assert card.parked_by_owner(b.db, cid)
        assert b.conn.execute('select status from task_runs where task_id=?', (cid,)).fetchone()[0] != 'running'
        calls.append(pid)
        return original(pid, wait)
    monkeypatch.setattr(stop.crew_proc, 'terminate_tree', terminate)
    lines, ok, facts = stop.stop_card(row(b, cid))
    assert ok, lines
    assert calls == [proc.pid]
    assert facts['parked'] and facts['worker_settled']
    assert not stop.alive(proc.pid)


@pytest.mark.parametrize('refusal', ['cmdline', 'missing', 'unreadable', 'reused'])
def test_refused_terminalized_worker_is_not_down_or_session_closed(board, own_worker, monkeypatch, refusal):
    b = board
    cid, proc = own_worker
    if refusal == 'cmdline':
        monkeypatch.setattr(stop, 'cmdline_of', lambda *_: '')
    elif refusal in ('missing', 'reused'):
        b.conn.execute('update task_runs set worker_started_at=? where task_id=?', (None if refusal == 'missing' else 'other-boot|1', cid))
        b.conn.commit()
    else:
        monkeypatch.setattr(dispatch, '_process_fingerprint', lambda *_: None)
    mutation_signals = []
    real_kill, real_killpg = os.kill, os.killpg
    def kill(pid, sig):
        if sig:
            assert pid == proc.pid
            mutation_signals.append((pid, sig))
        return real_kill(pid, sig)
    def killpg(pgid, sig):
        assert pgid == proc.pid and pgid != os.getpgrp()
        mutation_signals.append((pgid, sig))
        return real_killpg(pgid, sig)
    monkeypatch.setattr(os, 'kill', kill)
    monkeypatch.setattr(os, 'killpg', killpg)
    def forbidden(*a, **k):
        pytest.fail('refused identity must not signal or close sessions')
    monkeypatch.setattr(stop.crew_proc, 'terminate_tree', forbidden)
    monkeypatch.setattr(stop, 'close_sessions', forbidden)
    lines, ok, facts = stop.stop_card(row(b, cid))
    assert not ok and facts['parked'] and not facts['worker_settled'], lines
    assert stop.alive(proc.pid)
    assert any('NOT DOWN' in line for line in lines)
    assert facts['sessions_closed'] == []
    from hermes_state import SessionDB
    sdb = SessionDB(db_path=b.home/'state.db')
    try:
        assert sdb.get_session('synthetic-stop-session')['ended_at'] is None
    finally:
        sdb.close()
    # Repeat park must retain the relevant closed native run, not lose its live PID.
    assert not stop.stop_card(row(b, cid))[1]
    assert mutation_signals == []
    print('REFUSAL_RECEIPT '+json.dumps({'case':refusal, 'parked':facts['parked'], 'worker_settled':facts['worker_settled'],
          'mutation_signals':mutation_signals, 'session_left_open':True, 'native_run_terminal':True}))


def test_foreign_schedule_is_not_owner_stop(board):
    b = board
    cid = K.add_card(b.conn)
    assert b.kb.schedule_task(b.conn, cid, reason='foreign scheduler')
    assert not stop.stop_card(row(b, cid))[1]


def test_stop_supports_naked_review(board):
    b = board
    cid = reviewed(b)
    assert stop.stop_card(row(b, cid))[1]
    assert card.unstuck_card(cid)[0]
    assert (b.kb.get_task(b.conn, cid).status, b.kb.get_task(b.conn, cid).assignee) == ('review', 'crew-verifier')


def test_stop_final_readback_must_still_be_owner_park(board, monkeypatch):
    b = board
    cid = K.add_card(b.conn)
    def raced(*a):
        assert b.kb.unblock_task(b.conn, cid)
        return [], []
    monkeypatch.setattr(stop, 'close_sessions', raced)
    lines, ok, facts = stop.stop_card(row(b, cid))
    assert not ok and not facts['parked'], lines


def test_explicit_archive_of_done_card_remains_supported(board):
    b = board
    cid = K.add_card(b.conn)
    assert b.kb.claim_task(b.conn, cid, claimer='writer')
    assert b.kb.complete_task(b.conn, cid, result='done')
    lines, ok, facts = stop.stop_card(row(b, cid), archive=True)
    assert ok and facts['after'][0] == 'archived', lines


def test_same_kind_ready_escalation_is_held_not_comment_success(board):
    b = board
    cid = K.add_card(b.conn, 'blocked')
    assert b.kb.unblock_task(b.conn, cid)
    assert ask(b, cid)[0]
    assert b.kb.get_task(b.conn, cid).status == 'blocked'
    assert 'block_loop_detected' in K.events(b.conn, cid)
    assert not b.kb.claim_task(b.conn, cid, claimer='no')


def test_failed_native_park_is_honest_and_does_not_comment(board, monkeypatch):
    b = board
    cid = reviewed(b)
    verbs = []
    def refused(*args, **kw):
        verbs.append(args[0])
        return 1, 'native refused'
    monkeypatch.setattr(cc, 'kanban', refused)
    ok, text = cc.apply_ask_owner(b.ctx, row(b, cid), 'q')
    assert not ok and 'pending' in text
    assert verbs == ['block']
    assert b.kb.get_task(b.conn, cid).status == 'review'


def test_dry_stop_has_no_mutation_or_signals_or_session_closure(board, own_worker, monkeypatch):
    b = board
    cid, proc = own_worker
    before = list(b.conn.execute('select * from task_events order by id'))
    def forbidden(*a, **k):
        pytest.fail('dry-run mutated worker/session')
    monkeypatch.setattr(stop.crew_proc, 'terminate_tree', forbidden)
    monkeypatch.setattr(stop, 'close_sessions', forbidden)
    lines, ok, facts = stop.stop_card(row(b, cid), dry=True)
    assert ok and not facts['parked'] and not facts['worker_settled']
    assert facts['runs_killed'] == []
    assert stop.alive(proc.pid)
    assert list(b.conn.execute('select * from task_events order by id')) == before


def test_missing_pid_is_unknown_not_settled(board, monkeypatch):
    b = board
    cid = K.add_card(b.conn)
    assert b.kb.claim_task(b.conn, cid, claimer='claimed-not-yet-published')
    def forbidden(*a, **k):
        pytest.fail('unknown worker signalled/session closed')
    monkeypatch.setattr(stop.crew_proc, 'terminate_tree', forbidden)
    monkeypatch.setattr(stop, 'close_sessions', forbidden)
    lines, ok, facts = stop.stop_card(row(b, cid))
    assert not ok and facts['parked'] and not facts['worker_settled']
    assert any('identity unknown' in line for line in lines)


def test_refused_park_never_signals_worker(board, own_worker, monkeypatch):
    b = board
    cid, proc = own_worker
    monkeypatch.setattr(stop, 'park_card', lambda *a: ('refused', 'test refusal'))
    def forbidden(*a, **k):
        pytest.fail('unfenced worker signalled')
    monkeypatch.setattr(stop.crew_proc, 'terminate_tree', forbidden)
    lines, ok, facts = stop.stop_card(row(b, cid))
    assert not ok and not facts['parked'] and not facts['worker_settled']
    assert stop.alive(proc.pid)


def test_worker_claimed_between_capture_and_park_is_settled(board, monkeypatch):
    b = board
    cid = K.add_card(b.conn)
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', 'work kanban task '+cid], start_new_session=True)
    assert os.getpgid(proc.pid) != os.getpgrp()
    original = stop.park_card
    def interleaved(cid, dry):
        assert b.kb.claim_task(b.conn, cid, claimer='interleaved-synthetic')
        task = b.kb.get_task(b.conn, cid)
        dispatch._set_worker_pid(b.conn, cid, proc.pid, run_id=task.current_run_id, claim_lock=task.claim_lock)
        return original(cid, dry)
    monkeypatch.setattr(stop, 'park_card', interleaved)
    try:
        lines, ok, facts = stop.stop_card(row(b, cid))
        assert ok and facts['worker_settled'], lines
        assert facts['runs_killed']
        assert not stop.alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_blocked_reviewer_stop_schedule_preserves_phase_and_family(board):
    b = board
    cid = reviewed(b)
    assert b.kb.claim_review_task(b.conn, cid, claimer='reviewer')
    assert b.kb.block_task(b.conn, cid, reason='artifact blocker', kind='needs_input')
    sibling = K.add_card(b.conn)
    b.kb.link_tasks(b.conn, cid, sibling)
    sibling_before = b.kb.get_task(b.conn, sibling)
    lines, ok, facts = stop.stop_card(row(b, cid))
    assert ok and facts['parked'] and facts['worker_settled'], lines
    assert b.kb.get_task(b.conn, cid).status == 'scheduled'
    assert facts['family'] and b.kb.get_task(b.conn, sibling) == sibling_before
    assert card.unstuck_card(cid)[0]
    assert (b.kb.get_task(b.conn, cid).status, b.kb.get_task(b.conn, cid).assignee) == ('review', 'crew-verifier')
