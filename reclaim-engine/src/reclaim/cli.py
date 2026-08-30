"""Command-line entry point:  python -m reclaim <batch.json> [--json]

Loads a JSON batch and runs RECLAIM's deterministic detection pipeline (exact +
probabilistic), printing the honest report. The AI resolver and payment
executor are external integrations and are *off* here — review-band candidates
and recoverable leaks are reported for a human/the next tier, never guessed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime

from .batch_io import BatchLoadError, load_batch_file
from .control import ControlError, GateState, gates_for_run
from .csv_io import load_batch_csv
from .ledger import LedgerError
from .money import Money, MoneyError
from .persistence import (
    AuditRepository,
    GateRepository,
    JsonlFileStore,
    LeakRepository,
    PersistenceError,
)
from .pipeline import persist_run, run_reclaim
from .reconciliation import ReconciliationError
from .signing import SigningError, signed_root_record, verify_signed_record
from .verification import VerificationResult, verify_stores


ROOT_FILE = "root.txt"
HEADS_FILE = "roots.log"   # append-only history of published (size, root) heads
GATES_FILE = "gates.jsonl"  # append-only HITL checkpoints (architecture 8)

_VERDICTS = {"approve": GateState.APPROVED, "reject": GateState.REJECTED,
             "cancel": GateState.CANCELLED}


def _print_human(rep) -> None:
    s = rep.summary()
    print("RECLAIM report")
    print(f"  total expected : {s['total_expected']}")
    print(f"  matched        : {s['matched']}   (match rate {s['match_rate']})")
    print(f"  recovered      : {s['recovered']}")
    print(f"  closed         : {s['closed']}   (closure rate {s['closure_rate']})")
    print(f"  residual       : {s['residual']}  ({s['residual_leaks']} leak/s)")
    print(f"  auto-matched   : {s['auto_matched']}   pending review : {s['pending_review']}")
    if rep.residual_leaks:
        print("  residual exception list:")
        for leak in rep.residual_leaks:
            print(f"    - {leak.id}: {leak.amount} [{leak.leak_type.value}] {leak.hypothesis}")
    if rep.pending_review:
        print("  pending review (needs resolver/human):")
        for c in rep.pending_review:
            print(f"    - {c.settlement.id} ~ {c.bank.id}  score {c.score}")
    print(f"  ledger balanced ({rep.currency}): {rep.ledger.is_globally_balanced(rep.currency)}")


def _run_stamp(settlements, banks) -> datetime | None:
    """Deterministic audit stamp derived from the batch (first transaction's ts)."""
    if settlements:
        return settlements[0].ts
    if banks:
        return banks[0].ts
    return None


def _print_verify(res: VerificationResult, source, anchor: str) -> None:
    print(f"replay verification: {'VERIFIED' if res.ok else 'FAILED'}")
    print(f"  source           : {source}")
    print(f"  anchor           : {anchor}")
    print(f"  postings         : {res.posting_count}")
    print(f"  ledger balanced  : {res.ledger_balanced} ({','.join(res.currencies) or '-'})")
    print(f"  audit events     : {res.audit_events}")
    print(f"  audit root       : {res.audit_root}")
    print(f"  inclusion proofs : {'ok' if res.proofs_ok else 'FAILED'}")
    if res.root_matches_expected is not None:
        print(f"  root matches     : {res.root_matches_expected}")
    if res.consistency_ok is not None:
        verdict = "ok" if res.consistency_ok else "FAILED"
        print(f"  append-only      : {verdict} (vs {res.heads_checked} published head/s)")


def _load_key(key_file):
    if key_file is None:
        return None
    p = pathlib.Path(key_file)
    if not p.exists():
        raise SigningError(f"key file not found: {key_file}")
    key = p.read_bytes()
    if len(key) == 0:
        raise SigningError(f"key file is empty: {key_file}")
    return key


def _read_heads(d: pathlib.Path):
    """Previously published (size, root) heads, oldest first.

    Each ``--store`` appends the tree head it published. Keeping the history —
    rather than only the latest root — is what makes a *consistency* check
    possible: the current log must be an append-only extension of every head
    that was ever published, not merely self-consistent today.
    """
    path = d / HEADS_FILE
    if not path.exists():
        return []
    heads = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            heads.append((int(parts[0]), parts[1]))
    return heads


def _resolve_anchor(d: pathlib.Path, expect_root, key):
    """Pick the trust anchor for a replay, or explain why there isn't one.

    An *unanchored* replay proves only self-consistency: delete or edit stored
    events and the log simply recomputes a valid Merkle root of whatever is
    left, with valid inclusion proofs. Detecting tampering requires comparing
    against something recorded outside the log's own contents -- a published
    root (``--expect-root`` / ``root.txt``) or an HMAC signature over it
    (``--key-file``). Returns ``(expect_root, anchor_label, error_or_None)``.
    """
    if expect_root is not None:
        return expect_root, "--expect-root", None
    root_file = d / ROOT_FILE
    if root_file.exists():
        published = root_file.read_text(encoding="utf-8").strip()
        if published == "":
            return None, None, f"{root_file} is empty (no published root to check against)"
        return published, str(root_file), None
    if key is not None:
        return None, "--key-file signature", None
    return None, None, (
        f"UNANCHORED replay - tampering cannot be detected.\n"
        f"  no {root_file}, no --expect-root, no --key-file.\n"
        f"  a modified log recomputes a valid root of itself; supply an anchor."
    )


def _do_replay(replay_dir: str, expect_root, key) -> int:
    d = pathlib.Path(replay_dir)
    ledger_file, audit_file = d / "ledger.jsonl", d / "audit.jsonl"
    if not ledger_file.exists() and not audit_file.exists():
        print(f"error: no stored run found at {d} (expected ledger.jsonl / audit.jsonl)",
              file=sys.stderr)
        return 2
    expect_root, anchor, err = _resolve_anchor(d, expect_root, key)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2
    try:
        res = verify_stores(JsonlFileStore(ledger_file), JsonlFileStore(audit_file),
                            expect_root=expect_root, prior_heads=_read_heads(d))
    except (PersistenceError, LedgerError) as exc:
        print("replay verification: FAILED", file=sys.stderr)
        print(f"  error: {exc}", file=sys.stderr)
        return 1
    _print_verify(res, d, anchor)

    sig_ok = True
    if key is not None:
        sig_file = d / "audit.sig"
        if not sig_file.exists():
            print("  signature       : MISSING (no audit.sig)")
            sig_ok = False
        else:
            record = json.loads(sig_file.read_text(encoding="utf-8"))
            sig_ok = verify_signed_record(record, key, res.audit_root)
            print(f"  signature        : {'VERIFIED' if sig_ok else 'FAILED'}")

    return 0 if (res.ok and sig_ok) else 1


def _gate_repo(d: pathlib.Path) -> GateRepository:
    return GateRepository(JsonlFileStore(d / GATES_FILE))


def _do_queue(queue_dir: str) -> int:
    """Render the human exception queue for a stored run."""
    d = pathlib.Path(queue_dir)
    if not (d / GATES_FILE).exists():
        print(f"error: no gate log found at {d / GATES_FILE}", file=sys.stderr)
        return 2
    plane = _gate_repo(d).load()
    waiting = plane.awaiting()
    print(f"human queue: {len(waiting)} awaiting / {plane.size} total  ({d / GATES_FILE})")
    for gate in waiting:
        amount = f" {gate.amount}" if gate.amount is not None else ""
        print(f"  [{gate.kind.value}] {gate.id}{amount}")
        print(f"      {gate.question}")
        for line in gate.evidence:
            print(f"      - {line}")
    for gate in plane.settled():
        tail = f" -- {gate.rationale}" if gate.rationale else ""
        print(f"  ({gate.state.value} by {gate.decided_by}) {gate.id}{tail}")
    for currency in sorted({g.amount.currency for g in waiting if g.amount is not None}):
        print(f"  parked behind unanswered questions: {plane.amount_awaiting(currency)}")
    return 0


def _do_decide(queue_dir: str, gate_id: str, verdict: str, actor, at_iso) -> int:
    """Answer one gate. The decision is appended, never written over."""
    if actor is None:
        print("error: --decide requires --actor (a decision with no name on it is not "
              "an audit trail)", file=sys.stderr)
        return 2
    if at_iso is None:
        print("error: --decide requires --at ISO-8601 (the engine never reads the clock; "
              "determinism is goal G4)", file=sys.stderr)
        return 2
    d = pathlib.Path(queue_dir)
    if not (d / GATES_FILE).exists():
        print(f"error: no gate log found at {d / GATES_FILE}", file=sys.stderr)
        return 2
    try:
        at = datetime.fromisoformat(at_iso)
    except ValueError as exc:
        print(f"error: invalid --at timestamp: {exc}", file=sys.stderr)
        return 2
    plane = _gate_repo(d).load()
    try:
        gate = plane.decide(gate_id, _VERDICTS[verdict], actor=actor, at=at)
    except ControlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _gate_repo(d).save_plane(plane)
    print(f"{gate.state.value}: {gate.id} by {gate.decided_by} at {gate.decided_at.isoformat()}")
    print(f"  {len(plane.awaiting())} gate/s still awaiting a human")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="reclaim",
                                     description="Run RECLAIM reconciliation over a JSON batch, "
                                                 "or verify a stored run with --replay.")
    parser.add_argument("batch", nargs="?", help="path to a JSON batch file")
    parser.add_argument("--csv", metavar="FILE", help="read the batch from a CSV file instead of JSON")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable JSON summary")
    parser.add_argument("--store", metavar="DIR",
                        help="persist the run's ledger + audit log (append-only) under DIR")
    parser.add_argument("--at", metavar="ISO",
                        help="ISO-8601 timestamp for audit events (default: first transaction's ts)")
    parser.add_argument("--replay", metavar="DIR",
                        help="verify a previously stored run under DIR (instead of processing a batch)")
    parser.add_argument("--expect-root", metavar="HEX",
                        help="with --replay: the audit Merkle root to check against (tamper detection)")
    parser.add_argument("--key-file", metavar="PATH",
                        help="HMAC key file: sign the audit root on --store, verify it on --replay")
    parser.add_argument("--value-threshold", metavar="AMT",
                        help="with --store: open a HITL sign-off gate for any "
                             "recoverable leak above AMT")
    parser.add_argument("--queue", metavar="DIR",
                        help="show the human exception queue for a stored run under DIR")
    parser.add_argument("--decide", metavar="GATE_ID",
                        help="with --queue: answer one gate (needs --verdict, --actor, --at)")
    parser.add_argument("--verdict", choices=sorted(_VERDICTS), default="approve",
                        help="with --decide: how to settle the gate (default: approve)")
    parser.add_argument("--actor", metavar="WHO",
                        help="with --decide: who is answering (recorded in the audit trail)")
    args = parser.parse_args(argv)

    try:
        key = _load_key(args.key_file)
    except SigningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    threshold = None
    if args.value_threshold is not None:
        try:
            # round to minor units so the gate question reads as money
            threshold = Money.of(args.value_threshold, "INR").round()
        except MoneyError as exc:
            print(f"error: invalid --value-threshold: {exc}", file=sys.stderr)
            return 2

    if args.replay:
        return _do_replay(args.replay, args.expect_root, key)
    if args.queue:
        if args.decide:
            return _do_decide(args.queue, args.decide, args.verdict,
                              args.actor, args.at)
        return _do_queue(args.queue)
    if args.decide:
        print("error: --decide needs --queue DIR to say which stored run to act on",
              file=sys.stderr)
        return 2
    if not args.batch and not args.csv:
        print("error: provide a batch file, --csv FILE, or --replay DIR to verify a stored run",
              file=sys.stderr)
        return 2

    try:
        settlements, banks = (load_batch_csv(args.csv) if args.csv
                              else load_batch_file(args.batch))
        rep = run_reclaim(settlements, banks)  # detection-only; no external deps
        at = datetime.fromisoformat(args.at) if args.at else _run_stamp(settlements, banks)
    except (BatchLoadError, ReconciliationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: invalid --at timestamp: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rep.summary(), indent=2))
    else:
        _print_human(rep)

    if args.store:
        store_dir = pathlib.Path(args.store)
        store_dir.mkdir(parents=True, exist_ok=True)
        leak_file = store_dir / "leaks.jsonl"
        audit = persist_run(rep, at or datetime(1970, 1, 1),
                            JsonlFileStore(store_dir / "ledger.jsonl"),
                            JsonlFileStore(store_dir / "audit.jsonl"),
                            JsonlFileStore(leak_file))
        open_count = len(LeakRepository(JsonlFileStore(leak_file)).load().open_queue())
        # Publish the head of the *store*, not of this run own log. A store
        # accumulates across runs, so the two diverge as soon as a second batch
        # lands in the same directory -- and it is the store that --replay reads.
        stored = AuditRepository(JsonlFileStore(store_dir / "audit.jsonl")).load()
        stored_root, stored_size = stored.root(), stored.size
        print(f"persisted: {len(rep.ledger.postings())} postings, {audit.size} audit events "
              f"-> {store_dir}  (store now {stored_size} events, root {stored_root[:16]}...)")
        print(f"leak ledger: {len(rep.exact.leaks)} leaks, {open_count} still open "
              f"-> {leak_file}")
        # Publish the root next to the run so a later --replay is anchored by
        # default. Keep it somewhere tamper-visible (VCS, a ticket) too.
        (store_dir / ROOT_FILE).write_text(stored_root + "\n", encoding="utf-8", newline="\n")
        heads_path = store_dir / HEADS_FILE
        head_line = f"{stored_size} {stored_root}\n"
        existing = heads_path.read_text(encoding="utf-8") if heads_path.exists() else ""
        if head_line not in existing:        # idempotent, like the stores themselves
            with heads_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(head_line)
        print(f"published: {store_dir / ROOT_FILE} (anchors --replay), "
              f"head appended to {heads_path.name}")
        if threshold is not None:
            plane = _gate_repo(store_dir).load()
            for gate in gates_for_run(rep, at or datetime(1970, 1, 1),
                                      value_threshold=threshold, audit=audit):
                plane.open_gate(gate)
            _gate_repo(store_dir).save_plane(plane)
            print(f"human queue: {len(plane.awaiting())} gate/s awaiting "
                  f"-> {store_dir / GATES_FILE}  (answer with --queue --decide)")
        if key is not None:
            record = signed_root_record(stored_root, key)
            (store_dir / "audit.sig").write_text(json.dumps(record), encoding="utf-8")
            print(f"signed: audit.sig written ({record['algo']}, sig {record['signature'][:16]}...)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
