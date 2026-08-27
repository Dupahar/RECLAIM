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
from .csv_io import load_batch_csv
from .ledger import LedgerError
from .persistence import JsonlFileStore, PersistenceError
from .pipeline import persist_run, run_reclaim
from .reconciliation import ReconciliationError
from .signing import SigningError, signed_root_record, verify_signed_record
from .verification import VerificationResult, verify_stores


ROOT_FILE = "root.txt"


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
                            expect_root=expect_root)
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
    args = parser.parse_args(argv)

    try:
        key = _load_key(args.key_file)
    except SigningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.replay:
        return _do_replay(args.replay, args.expect_root, key)
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
        audit = persist_run(rep, at or datetime(1970, 1, 1),
                            JsonlFileStore(store_dir / "ledger.jsonl"),
                            JsonlFileStore(store_dir / "audit.jsonl"))
        print(f"persisted: {len(rep.ledger.postings())} postings, {audit.size} audit events "
              f"-> {store_dir}  (audit root {audit.root()[:16]}...)")
        # Publish the root next to the run so a later --replay is anchored by
        # default. Keep it somewhere tamper-visible (VCS, a ticket) too.
        (store_dir / ROOT_FILE).write_text(audit.root() + "\n", encoding="utf-8", newline="\n")
        print(f"published: {store_dir / ROOT_FILE} (anchors --replay)")
        if key is not None:
            record = signed_root_record(audit.root(), key)
            (store_dir / "audit.sig").write_text(json.dumps(record), encoding="utf-8")
            print(f"signed: audit.sig written ({record['algo']}, sig {record['signature'][:16]}...)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
