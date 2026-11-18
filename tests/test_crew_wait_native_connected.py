"""Connected source-bound kernel/watcher controls; no live gateway/model/worker launch."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_crew_owner_wait_stop import board, row, reviewed, ask, card, cc, heal, stop, K, dispatch


def ledger_bytes(b, cid):
    return {str(p.relative_to(b.home)): p.read_bytes() for p in b.home.rglob(cid+'.json*')}


def test_connected_recovery_wait_native_watcher_and_owner_continue(board, monkeypatch):
    b = board
    cid = reviewed(b)
    assert b.kb.claim_review_task(b.conn, cid, claimer='review-blocker')
    assert b.kb.block_task(b.conn, cid, reason='first artifact blocker', kind='needs_input')
    for root in (b.home, b.home/'profiles/crew-worker', b.home/'profiles/crew-verifier'):
        ledger = root/'crew/budget'/(cid+'.json')
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_bytes(b'{"budget":200000,"used":73000}\n')
        Path(str(ledger)+'.spent').write_bytes(b'{"budget":190000,"used":45000}\n')
    before_ledgers = ledger_bytes(b, cid)
    assert cc.apply_retry(b.ctx, row(b, cid), {'decision':'retry', 'fix':'synthetic artifact repair'}, [])[0]
    assert ledger_bytes(b, cid) == before_ledgers
    assert b.kb.claim_task(b.conn, cid, claimer='repair-writer')
    assert b.kb.request_review(b.conn, cid, reviewer='crew-verifier', summary='repaired')
    assert b.kb.claim_review_task(b.conn, cid, claimer='review-blocker-2')
    assert b.kb.block_task(b.conn, cid, reason='second artifact blocker', kind='needs_input')
    assert b.kb.get_task(b.conn, cid).status == 'triage'
    parent = K.add_card(b.conn)
    b.kb.link_tasks(b.conn, parent, cid)  # False means no ready->todo demotion, not link refusal.
    assert b.conn.execute('select 1 from task_links where parent_id=? and child_id=?', (parent, cid)).fetchone()
    assert b.kb.set_model_override(b.conn, cid, 'synthetic-model', provider='synthetic-provider')
    before = b.kb.get_task(b.conn, cid)
    links = list(b.conn.execute('select * from task_links'))
    assert ask(b, cid)[0]
    held = b.kb.get_task(b.conn, cid)
    assert (held.status, held.assignee) == ('blocked', 'crew-verifier')
    assert (held.body, held.block_recurrences, held.consecutive_failures, held.model_override, held.provider_override) == (
        before.body, before.block_recurrences, before.consecutive_failures, before.model_override, before.provider_override)
    assert list(b.conn.execute('select * from task_links')) == links
    assert card.close_proof_command(cid) == 'true'
    healthy_ready = K.add_card(b.conn)
    healthy_review = reviewed(b)
    ordinary_triage = K.add_card(b.conn, 'triage')
    triage_body = b.kb.get_task(b.conn, ordinary_triage).body
    held_events = K.events(b.conn, cid)
    spawned, configs, auxiliary = [], [], []
    # All settings are task-owned; real resolver and per-board tick consume this synthetic config.
    config = {'kanban': {'auto_decompose': False, 'review_dispatch': True, 'max_in_progress': 8,
                         'reconcile_orphans': False, 'dispatch_profiles': ['crew-worker', 'crew-verifier']}}
    cfg_path = b.home/'config.yaml'
    cfg_path.write_text('kanban:\n  auto_decompose: false\n  review_dispatch: true\n  reconcile_orphans: false\n  max_in_progress: 8\n  dispatch_profiles: [crew-worker, crew-verifier]\n')
    from gateway import kanban_watchers as watchers
    from hermes_cli import kanban_decompose as decompose
    from hermes_cli import config as native_config
    def load():
        configs.append(True)
        return config
    monkeypatch.setattr(native_config, 'load_config', load)
    def aux(*a, **kw):
        auxiliary.append((a, kw))
        pytest.fail('disabled auxiliary/model work must not run')
    monkeypatch.setattr(decompose, 'decompose_task', aux)
    real_dispatch = dispatch.dispatch_once
    tick_results = []
    def tick(conn, **kw):
        # Only spawn transport is inert. Native lane selection, claims, promotion and guards run.
        result = real_dispatch(conn, spawn_fn=lambda task, workspace, **kw: spawned.append((task.id, task.assignee)), **kw)
        tick_results.append(result)
        return result
    monkeypatch.setattr(dispatch, 'dispatch_once', tick)
    class Watch(watchers.GatewayKanbanWatchersMixin):
        _running = True
        ticks = 0
        def _kanban_dispatcher_boot(self):
            return load, b.kb, config['kanban']
        async def _sleep_between_ticks(self, interval):
            self.ticks += 1
            if self.ticks == 2:
                self._running = False
    async def immediate(_seconds):
        pass
    monkeypatch.setattr(watchers.asyncio, 'sleep', immediate)
    watch = Watch()
    asyncio.run(watch._kanban_dispatcher_watcher())
    assert watch.ticks == 2 and len(tick_results) == 2 and len(configs) >= 2
    assert auxiliary == []
    assert set(spawned) == {(healthy_ready, 'crew-worker'), (healthy_review, 'crew-verifier'), (parent, 'crew-worker')}
    assert cid not in [c for c, _ in spawned]
    assert K.events(b.conn, cid) == held_events
    assert b.kb.get_task(b.conn, ordinary_triage).body == triage_body
    assert b.kb.get_task(b.conn, ordinary_triage).status == 'triage'
    assert ledger_bytes(b, cid) == before_ledgers
    assert not b.kb.claim_task(b.conn, cid, claimer='denied-writer')
    assert not b.kb.claim_review_task(b.conn, cid, claimer='denied-reviewer')
    assert heal.heal_card(row(b, cid)) is None
    assert not heal.release_block(cid)
    assert 'waiting for the owner' in cc.handle_card(b.ctx, cid, [{'kind':'completed','id':999}])['detail']
    # Native unblocked re-gates the open parent, retaining reviewer phase; no specify/writer claim.
    assert card.unstuck_card(cid)[0]
    assert b.kb.get_task(b.conn, cid).status == 'todo'
    assert b.kb.get_task(b.conn, cid).assignee == 'crew-verifier'
    b.kb.recompute_ready(b.conn)
    assert b.kb.get_task(b.conn, cid).status == 'todo'
    assert b.kb.get_task(b.conn, parent).status == 'running'  # healthy parent was also natively dispatched
    assert b.kb.complete_task(b.conn, parent, result='parent done')
    b.kb.recompute_ready(b.conn)
    assert (b.kb.get_task(b.conn, cid).status, b.kb.get_task(b.conn, cid).assignee) == ('review', 'crew-verifier')
    assert b.kb.claim_review_task(b.conn, cid, claimer='same-reviewer')
    assert ledger_bytes(b, cid) == before_ledgers
    assert card.close_proof_command(cid) == 'true'
    print('CONNECTED_RECEIPT '+json.dumps({'held': cid, 'ticks':watch.ticks, 'healthy_spawned':spawned,
          'auxiliary_calls':len(auxiliary), 'continued_phase':'review', 'reviewer':'crew-verifier',
          'ledger_hashes':{p:hashlib.sha256(v).hexdigest() for p,v in before_ledgers.items()}}))


def test_real_allowed_heal_audit_and_decider_without_running_identity(board):
    b = board
    assert not os.environ.get('HERMES_KANBAN_TASK')
    cid = K.add_card(b.conn, 'blocked')
    card.record_origin(cid, env={'origin':'', 'session':'synthetic'}, proof_cmd='true')
    # Actual proof safety + harmless shell command and native lift, no fake running identity.
    got = heal.heal_stale_verify(row(b, cid), False)
    assert got and got['fixed'], got
    assert b.kb.get_task(b.conn, cid).status == 'ready'
    assert b.kb.claim_task(b.conn, cid, claimer='synthetic-writer')
    verdict = card.run_verdict(cid, by='crew-worker', no_hand_back=True)
    assert verdict['rc'] == 0, verdict
    assert b.kb.complete_task(b.conn, cid, result='harmless synthetic proof passed')
    ev = cc.card_events(b.ctx, cid, ('completed',))[-1]
    audit = cc.handle_card(b.ctx, cid, [ev])
    assert audit['action'] == 'audit pass', audit
    assert any(d.get('decision') == 'audit' and d.get('outcome') == 'pass' for d in cc.decisions_of(b.ctx, cid))
    another = K.add_card(b.conn, 'blocked')
    called = []
    def decider(ctx, facts):
        assert Path(facts).is_file()
        called.append(True)
        return '{"decision":"verify"}', 0
    b.ctx.decider = decider
    dec, problem = cc.ask_model(b.ctx, row(b, another), [])
    assert dec == {'decision':'verify'} and not problem and called == [True]
    print('ALLOWED_RECEIPT '+json.dumps({'heal':got['fixed'], 'verdict':verdict['verdict'], 'audit':audit['action'],
                                        'decider_calls':len(called), 'running_identity':False}))


def test_decider_rechecks_wait_before_each_retry(board):
    b = board
    cid = K.add_card(b.conn, 'blocked')
    called = []
    def decider(ctx, facts):
        called.append(True)
        card._append_card_event(cid, 'crew_decision', {'decision':'ask_owner', 'question':'interleaved'})
        return 'invalid', 0
    b.ctx.decider = decider
    assert cc.ask_model(b.ctx, row(b, cid), [])[0] is None
    assert called == [True]


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('brave', [False, True])
def test_real_owner_proof_continuation_and_safe_proof(board, legacy, brave):
    b = board
    cid = K.add_card(b.conn, 'triage' if legacy else 'blocked')
    card.record_origin(cid, env={'origin':'', 'session':'s'}, proof_cmd='true')
    card._append_card_event(cid, 'crew_decision', {'decision':'ask_owner', 'question':'confirm?',
                            'proof_ask': {'kind':'blocked', 'command':'true'} if brave else {'kind':'rescope','proposed':'true'}})
    assert not card.unstuck_card(cid)[0]
    text = card.owner_proof_answer(cid, brave=brave)
    assert 'confirmed' in text and 'still held' not in text, text
    assert b.kb.get_task(b.conn, cid).status == 'ready'
    assert not cc.awaiting_owner(b.ctx, cid, cc.decisions_of(b.ctx, cid))
    assert card.run_verdict(cid, by='crew-worker', no_hand_back=True)['rc'] == 0
    assert 'specified' not in K.events(b.conn, cid)


def test_read_error_does_not_authorize_automatic_work(board, monkeypatch):
    b = board
    bad = str(b.home/'missing.db')
    assert card.pending_owner_question(bad, 't_unknown')
    monkeypatch.setattr(heal, 'KANBAN_DB', bad)
    assert heal.heal_stale_verify({'id':'t_unknown'}, False) is None
    assert not heal.release_block('t_unknown')


def test_owner_stop_blocks_direct_audit_and_decider(board):
    b = board
    cid = reviewed(b)
    assert stop.stop_card(row(b, cid))[1]
    def forbidden(*a):
        pytest.fail('owner-stopped decider called')
    b.ctx.decider = forbidden
    assert cc.ask_model(b.ctx, row(b, cid), [])[0] is None
    assert 'owner' in cc.audit_proof(b.ctx, row(b, cid), {'id':1})['detail']
