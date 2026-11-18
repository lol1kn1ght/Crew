#!/usr/bin/env python3
"""Stop one card (or all open Crew cards), retaining work and reporting worker settlement honestly.

  crew_stop.py [<card id>] [--archive] [--dry-run] [--json] [--quiet]

ORDER: capture relevant native run identities; PARK and verify the owner hold; only then terminate
exact supported owned workers through the existing cmdline/card guard + native start fingerprint and
crew_proc. On POSIX the worker must own an isolated process group, never the caller's group. Re-read
captured identities even after native park terminalizes their run rows; include a run claimed before
the winning park. Do not signal arbitrary historical terminal PIDs. Unknown/reused/unreadable identity
is refusal, not permission to use another killer. Dead/zombie workers need no signal.

Running/ready use a different typed block kind to avoid recurrence-to-triage; runless review/triage
use native operator needs_input park; todo/blocked use phase-preserving schedule. A foreign scheduled
state without the owner marker is NOT a successful owner park. Repeat own park is idempotent.

parked=true can coexist with worker_settled=false: the board fence is not OS settlement. Overall stop
is NOT DOWN until relevant workers settle. Sessions are conservatively left open until all relevant
workers settle. --archive is an explicit drop after park + settlement (or an already-terminal card); never recursive.
--dry-run has no board changes, signals or session closure. Open family is reported, not followed.

Continue through /crew-unstuck <id> (pending proof consent requires /crew-proof instead). The native
unblock retains review phase, reviewer and parent gates. Coordinator/heal/notify leave owner parks
alone. This is a supported-native-consumer fence and observed-identity check, not a universal lease,
all-writer barrier or proof of quiescence against arbitrary later writes/PID races.

Exit: 0 success (or dry-run report), 1 NOT DOWN, 2 no board/bad arguments, 3 unknown card id.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import crew_card  # noqa: E402
import crew_proc  # noqa: E402

# The board this acts on: the same resolution the rest of the crew uses (crew_card.kanban_db: a pinned
# HERMES_KANBAN_DB / KANBAN_DB, else the board under this HERMES_HOME). A hard-coded ~/.hermes/kanban.db ignored
# a routed profile and a non-default board, so stop could park cards on the wrong one.
KANBAN_DB = (crew_card.kanban_db() or os.environ.get("KANBAN_DB") or os.environ.get("HERMES_KANBAN_DB")
             or os.path.join(crew_card.base_home(), "kanban.db"))
# A card in one of these is still somebody's work in progress; done and archived are not.
OPEN_STATUSES = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review")
CLOSED_STATUSES = ("done", "archived")
TERM_WAIT_S = 1.5


def q(sql, args=()):
    conn = sqlite3.connect("file:%s?mode=ro" % KANBAN_DB, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def is_crew_card(card):
    """A card is crew work only when its body carries the crew Coordinator:/Role: line."""
    return crew_card.is_crew_body(card.get("body"))


# ------------------------------------------------------------------ decisions (pure)

def stop_targets(cards, card_id=None):
    """The cards this run stops: the named one, or every open crew card.

    A named card is returned whether or not it is open - the caller decides what to say about a card
    that is already done or archived, and a stop that silently ignored the id would read as success.
    """
    if card_id:
        return [c for c in cards if c.get("id") == card_id]
    return [c for c in cards if (c.get("status") or "") not in CLOSED_STATUSES and is_crew_card(c)]


def live_runs(runs, alive):
    """The runs worth killing: still marked running AND their pid still answers.

    A worker that died without writing a settle row leaves `status='running'` behind for ever, so the
    status column alone would send a kill at a pid the kernel has already reused.
    """
    out = []
    for r in runs:
        pid = r.get("worker_pid")
        if (r.get("status") or "") == "running" and pid and alive(pid):
            out.append(r)
    return out


def is_our_worker(cmdline, card_id):
    """True only for a process that is this card's own crew worker.

    The dispatcher spawns workers as `hermes -p <role> ... chat -q "work kanban task <id>"`, so the
    card id is in the command line. Everything else - the gateway, the board server, this process,
    a sibling session - fails the guard and is never signalled.
    """
    cmd = cmdline or ""
    return bool(cmd) and "work kanban task" in cmd and card_id in cmd


def came_back(before, after):
    """True when a card moved between two reads of its state.

    The pass uses it to compare the state it left the card in with the state a later promotion pass
    leaves it in (that is the stays-down check the proof runs): a stop holds only while the status
    stays down (parked or archived) and no run row is added.
    """
    return tuple(before) != tuple(after)


def family_of(links, cards):
    """The open cards linked to this one in either direction, named as `role: id (status)`.

    The decomposer links the card it decomposed to the children it created, so the work of a stopped
    card lives on in cards that are not the card - the reason the owner saw one restart by itself.
    """
    by_id = {c["id"]: c for c in cards}
    open_ids = []
    for l in links:
        for other in (l.get("parent_id"), l.get("child_id")):
            if other and other != l.get("self") and other not in open_ids:
                open_ids.append(other)
    out = []
    for oid in open_ids:
        c = by_id.get(oid)
        if c and (c.get("status") or "") not in CLOSED_STATUSES:
            out.append("%s: %s (%s)" % (c.get("assignee") or "?", oid, c.get("status")))
    return out


def report_lines(card, killed, closed, archived, after, family):
    """The pass's own wording: what it killed, what it closed, what it left open beside it."""
    lines = ["%s  %s -> %s" % (card["id"], card.get("status"), after[0])]
    if killed:
        lines.append("  killed: " + ", ".join("run %s pid %s" % (r["id"], r["worker_pid"]) for r in killed))
    else:
        lines.append("  killed: nothing running")
    if closed:
        lines.append("  session(s) closed: " + ", ".join(closed))
    if family:
        lines.append("  still open beside it: " + "; ".join(family))
        lines.append("  stopping those is a separate ask: crew_stop.py <id>")
    return lines


# ------------------------------------------------------------------ process/board work

def alive(pid):
    """True while the process really runs.

    A zombie counts as gone: a killed child nobody has reaped yet still answers signal 0, and a stop
    that reported "still alive" for a process it had just killed would be wrong (it did, on the first
    live run of this pass).
    """
    return crew_proc.alive(pid)


def cmdline_of(pid):
    return crew_proc.cmdline_of(pid)


def kill_run(run, card_id, dry):
    """Kill one run's worker process group. Returns (word, landed) for the report."""
    pid = int(run["worker_pid"])
    cmd = cmdline_of(pid)
    if not is_our_worker(cmd, card_id):      # keep the existing cmdline/card guard
        return "refused pid %s (not this card's worker%s)" % (pid, "" if cmd else ": command line unreadable"), False
    try:
        from hermes_cli.kanban_db_dispatch import _process_fingerprint
        recorded = run.get("worker_started_at")
        current = _process_fingerprint(pid)
        if not recorded or not current or recorded != current:
            return "refused pid %s (native start fingerprint missing/unreadable/reused)" % pid, False
        if os.name != "nt" and (os.getpgid(pid) != pid or os.getpgid(pid) == os.getpgrp()):
            return "refused pid %s (not an isolated worker process group)" % pid, False
    except Exception:  # identity-reader failure is refusal, never alternate kill authority
        return "refused pid %s (native identity unknown)" % pid, False
    if dry:
        return "would kill run %s pid %s" % (run["id"], pid), True
    crew_proc.terminate_tree(pid, TERM_WAIT_S)
    return "run %s pid %s %s" % (run["id"], pid, "dead" if not alive(pid) else "STILL ALIVE"), not alive(pid)


def sessions_of(card_id):
    """(session id, profile) for every session a worker recorded on this card, newest first."""
    out = []
    for row in q("select payload, run_id from task_events where task_id = ? and kind = 'session' "
                 "order by id desc", (card_id,)):
        try:
            data = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        sid, prof = data.get("session"), data.get("profile") or ""
        if sid and (sid, prof) not in out:
            out.append((sid, prof))
    return out


def hermes_root():
    """Where the profiles live. CREW_STOP_HERMES_ROOT points the session lookup at a scratch tree (the
    proof's), leaving HERMES_HOME real so the `hermes kanban` call still runs."""
    if os.environ.get("CREW_STOP_HERMES_ROOT"):
        return os.path.abspath(os.environ["CREW_STOP_HERMES_ROOT"])
    home = os.path.abspath(os.environ.get("HERMES_HOME") or crew_card.default_home())
    if os.path.basename(os.path.dirname(home)) == "profiles":
        return os.path.dirname(os.path.dirname(home))
    return home


def session_db(profile):
    root = hermes_root()
    if profile and profile != "default":
        return os.path.join(root, "profiles", profile, "state.db")
    return os.path.join(root, "state.db")


def close_sessions(card_id, dry):
    """Close every session row this card's workers left open, through Hermes' SessionDB.end_session.

    A killed worker never reaches its own exit path, so ended_at stays NULL and each surface that
    asks a session whether it is running answers yes for ever.
    """
    closed, skipped = [], []
    src = os.environ.get("HERMES_SRC") or crew_card.hermes_root()
    if os.path.isdir(src) and src not in sys.path:
        sys.path.append(src)
    try:
        from hermes_state import SessionDB
    except ImportError:
        return closed, ["%s (hermes_state not importable here)" % sid for sid, _ in sessions_of(card_id)]
    for sid, prof in sessions_of(card_id):
        path = session_db(prof)
        if not os.path.exists(path):
            skipped.append("%s (no %s)" % (sid, os.path.basename(path)))
            continue
        try:
            sdb = SessionDB(db_path=Path(path))
            try:
                sess = sdb.get_session(sid)
                if not sess or sess.get("ended_at") is not None:
                    continue
                if not dry:
                    sdb.end_session(sid, "crew_stop")
                closed.append(sid if not dry else "%s (would close)" % sid)
            finally:
                sdb.close()
        except Exception as exc:  # noqa: BLE001 - one unreadable store must not stop the stop
            skipped.append("%s (%s)" % (sid, exc))
    return closed, skipped


def snapshot(card_id):
    """(status, run count, running count) - what 'it came back' is measured against."""
    t = q("select status from tasks where id = ?", (card_id,))
    runs = q("select status from task_runs where task_id = ?", (card_id,))
    return ((t[0]["status"] if t else "?"), len(runs), sum(1 for r in runs if r["status"] == "running"))


def archive_card(card_id, dry):
    if dry:
        return 0, ""
    done = subprocess.run([crew_card.hermes_bin(), "kanban", "archive", card_id], capture_output=True, text=True,
                          timeout=120, env=dict(os.environ, HERMES_KANBAN_DB=KANBAN_DB), encoding="utf-8", errors="replace")
    return 1 if done.returncode == 0 else 0, (done.stdout + done.stderr).strip()


PARK_KINDS = ("needs_input", "capability", "transient")


def park_kind(card_id):
    """The block kind for the park: the first that differs from the card's last one, because a same-kind re-block
    is the kernel's loop breaker and routes the card to triage (BLOCK_RECURRENCE_LIMIT)."""
    rows = q("select block_kind from tasks where id = ?", (card_id,))
    last = rows[0]["block_kind"] if rows else None
    return next(k for k in PARK_KINDS if k != last)


def park_card(card_id, dry):
    """Park the card with the kernel's own verb for its state. Returns (status word, output)."""
    status = (q("select status from tasks where id = ?", (card_id,)) or [{"status": "?"}])[0]["status"]
    reason = crew_card.owner_stop_reason(card_id)
    if crew_card.parked_by_owner(KANBAN_DB, card_id):
        return "already parked", ""
    if status in ("running", "ready"):
        argv = ["block", card_id, "--kind", park_kind(card_id), reason]
    elif status in ("review", "triage"):
        argv = ["block", card_id, "--kind", "needs_input", reason]
    elif status in ("todo", "blocked"):
        argv = ["schedule", card_id, reason]
    elif status == "scheduled":
        return "foreign schedule (not an owner park)", ""
    else:
        return "cannot park a %s card" % status, ""
    if dry:
        return "would %s" % argv[0], ""
    done = subprocess.run([crew_card.hermes_bin(), "kanban"] + argv, capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, HERMES_KANBAN_DB=KANBAN_DB), encoding="utf-8", errors="replace")
    return argv[0] if done.returncode == 0 else "%s refused" % argv[0], (done.stdout + done.stderr).strip()


def stop_card(card, dry=False, archive=False):
    """Capture native identities, fence/read back first, then guarded termination and settlement."""
    card_id = card["id"]
    before = snapshot(card_id)
    sql = "select id, status, worker_pid, worker_started_at, claim_lock from task_runs where task_id = ?"
    initial = q(sql, (card_id,))
    captured = [r for r in initial if r["status"] == "running"]
    initial_ids = {r["id"] for r in initial}
    # A repeat stop must not forget a refused worker whose row the previous winning park closed.
    park = crew_card.parked_by_owner(KANBAN_DB, card_id)
    if park:
        refs = q("select run_id from task_events where id = ?", (park,))
        park_runs = {r["run_id"] for r in refs}
        captured += [r for r in initial if r["id"] in park_runs and
                     (r.get("worker_pid") or r.get("claim_lock"))]
    park_word, message = park_card(card_id, dry)
    held = snapshot(card_id) if not dry else before
    parked = bool(not dry and held[0] in ("blocked", "scheduled") and
                  crew_card.parked_by_owner(KANBAN_DB, card_id))
    # Include the run closed by the winning park, including one claimed after the initial read.
    after_park = q(sql, (card_id,)) if not dry else initial
    captured_ids = {r["id"] for r in captured}
    captured += [r for r in after_park if r["status"] == "running" or r["id"] in captured_ids or
                 (r["id"] not in initial_ids and (r.get("worker_pid") or r.get("claim_lock")))]
    relevant = { (r["id"], r.get("worker_pid"), r.get("worker_started_at"), r.get("claim_lock")): r
                 for r in captured }
    killed, kill_words, unsettled = [], [], []
    for r in relevant.values():
        pid = r.get("worker_pid")
        if not pid:
            unsettled.append(r["id"])
            kill_words.append("run %s identity unknown (no native worker pid)" % r["id"])
        elif not alive(pid):
            continue                  # dead/zombie is settled without a signal
        elif not parked and not dry:
            unsettled.append(r["id"])
            kill_words.append("run %s not signalled: native park not verified" % r["id"])
        else:
            word, landed = kill_run(r, card_id, dry)
            kill_words.append(word)
            if landed and not dry:
                killed.append(r)
            if not dry and (not landed or alive(pid)):
                unsettled.append(r["id"])
    # Native terminal status is NOT OS settlement. Recheck captured identities irrespective of status.
    if not dry:
        for r in relevant.values():
            if (not r.get("worker_pid") or alive(r["worker_pid"])) and r["id"] not in unsettled:
                unsettled.append(r["id"])
        for r in q(sql, (card_id,)):
            if r["status"] == "running" and r["id"] not in unsettled:
                unsettled.append(r["id"])
    worker_settled = not unsettled and not dry
    if worker_settled and (parked or (archive and held[0] in CLOSED_STATUSES)):
        closed, skipped = close_sessions(card_id, False)
    else:
        closed = []
        skipped = ["%s (worker settlement unconfirmed; session left open)" % sid
                   for sid, _prof in sessions_of(card_id)]
    if archive and worker_settled and (parked or held[0] in CLOSED_STATUSES):
        _archived, message = archive_card(card_id, False)
        park_word = "archive"
    elif archive and dry:
        park_word += "; would archive after settlement"
    after = snapshot(card_id) if not dry else before
    parked = bool(not dry and after[0] in ("blocked", "scheduled") and
                  crew_card.parked_by_owner(KANBAN_DB, card_id))
    worker_settled = worker_settled and after[2] == 0
    links = q("select parent_id, child_id, ? as self from task_links where parent_id = ? or child_id = ?",
              (card_id, card_id, card_id))
    family = family_of(links, q("select id, status, assignee from tasks"))
    ok = True if dry else worker_settled and (after[0] == "archived" if archive else parked)
    facts = {"card": card_id, "before": before, "after": after, "runs_killed": [r["id"] for r in killed],
             "sessions_closed": closed, "sessions_skipped": skipped, "family": family, "still_down": ok,
             "parked": parked, "worker_settled": worker_settled, "unsettled_runs": sorted(set(unsettled)),
             "how": park_word, "kill_words": kill_words}
    lines = report_lines(card, killed, closed, archive, after, family)
    if relevant and not killed:
        lines[1] = "  killed: none; see worker settlement below"
    if parked:
        lines.append("  parked (%s); worker_settled=%s - /crew-unstuck %s to continue" %
                     (park_word, worker_settled, card_id))
    lines += ["  " + w for w in kill_words]
    if skipped:
        lines.append("  session(s) not closed: " + ", ".join(skipped))
    if not ok:
        lines.append("  %s said: %s" % (park_word, (message or "(nothing)")[-300:]))
        lines.append("  NOT DOWN: status=%s unsettled workers=%s" % (after[0], sorted(set(unsettled))))
    return lines, ok, facts


def run(argv=None):
    """The whole stop pass as (exit code, the text it prints). main() prints it; the Hermes Desktop action route
    (dashboard/plugin_api.py) returns the same text to its button."""
    ap = argparse.ArgumentParser(description="Stop crew work: park the card, keep its history.")
    ap.add_argument("card", nargs="?", help="one card id; without it every open crew card is parked")
    ap.add_argument("--archive", action="store_true",
                    help="drop the card for good (archive it) instead of parking it; needs a card id")
    ap.add_argument("--dry-run", action="store_true", help="report what would be stopped")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true", help="only the failing cards and the count")
    args = ap.parse_args(argv)

    if args.archive and not args.card:
        ap.error("--archive drops a card for good: name the card (the all-cards form only parks)")
    if not os.path.exists(KANBAN_DB):
        return 2, "no board at %s" % KANBAN_DB
    cards = q("select id, status, assignee, title, body from tasks")
    if args.card:
        targets = stop_targets([dict(c) for c in cards], args.card)
        if not targets:
            return 3, "no such card: %s" % args.card
    else:
        targets = stop_targets([dict(c) for c in cards])
    if not targets:
        # a dry run says so even when there is nothing to do, so the caller can see which mode answered
        return 0, "stop%s: nothing to stop (0 card(s) open)" % (" (dry run)" if args.dry_run else "")

    out, results, fails = [], [], []
    mode = "dry run" if args.dry_run else "stop"
    if not args.quiet:
        out.append("crew stop - %s  %d card(s)%s" % (mode, len(targets), "  (--dry-run)" if args.dry_run else ""))
    for card in targets:
        lines, ok, facts = stop_card(card, args.dry_run, args.archive)
        results.append(facts)
        if not ok:
            fails.append(card["id"])
        if not args.quiet or not ok:
            out.append("\n".join(lines))
    killed = sum(len(f["runs_killed"]) for f in results)
    closed = sum(len(f["sessions_closed"]) for f in results)
    out.append("stop: %d card(s), %d run(s) killed, %d session(s) closed%s"
               % (len(results), killed, closed, "  (%d came back!)" % len(fails) if fails else ""))
    if args.json:
        out.append(json.dumps({"mode": mode, "cards": results, "came_back": fails}, indent=2))
    return (1 if fails else 0), "\n".join(out)


def main(argv=None):
    code, text = run(argv)
    print(text)
    return code


if __name__ == "__main__":
    crew_proc.utf8_stdio()
    sys.exit(main())
