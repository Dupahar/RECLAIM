"""Phase 9 tests — the command-line entry point."""
import json
import pathlib

import pytest

from reclaim.cli import main

SAMPLE = pathlib.Path(__file__).resolve().parents[1] / "examples" / "sample_batch.json"


def test_cli_human_output(capsys):
    rc = main([str(SAMPLE)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RECLAIM report" in out
    # s1 exact (payout 4882) + s3 fuzzy auto (1500) = 6382 matched
    assert "6382.00 INR" in out
    # s4 is the residual leak
    assert "leak:missing:s4" in out


def test_cli_json_output(capsys):
    rc = main([str(SAMPLE), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["matched"] == "6382.00 INR"
    # detection-only (no recovery engine in the CLI): s2 short payment stays a
    # leak alongside s4 missing -> 2 residual leaks.
    assert data["residual_leaks"] == 2
    assert data["auto_matched"] == 1
    assert data["recovered"] == "0 INR"


def test_cli_clean_batch_no_residual(tmp_path, capsys):
    # all exact matches -> no residual, no pending review (covers those output branches)
    data = {
        "settlements": [{"id": "s1", "gross_amount": "100.00", "currency": "INR",
                         "ts": "2026-08-25T09:00:00", "refs": {"utr": "U1"}}],
        "bank_credits": [{"id": "b1", "gross_amount": "100.00", "currency": "INR",
                          "ts": "2026-08-25T09:00:00", "refs": {"utr": "U1"}}],
    }
    p = tmp_path / "clean.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    rc = main([str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 leak/s" in out
    assert "residual exception list" not in out
    assert "pending review (needs" not in out


def test_cli_missing_file_errors(capsys):
    rc = main(["does-not-exist.json"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "error:" in err


def test_cli_reconciliation_error(tmp_path, capsys):
    # a settlement without a UTR -> reconciliation cannot match deterministically
    bad = {"settlements": [{"id": "s1", "gross_amount": "100.00", "currency": "INR",
                            "ts": "2026-08-25T09:00:00"}],
           "bank_credits": []}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    rc = main([str(p)])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_cli_store_persists_and_reloads(tmp_path, capsys):
    store = tmp_path / "runstore"
    rc = main([str(SAMPLE), "--store", str(store)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "persisted:" in out
    # files exist and reload to a balanced ledger + a verifiable audit log
    from reclaim.persistence import JsonlFileStore, LedgerRepository, AuditRepository
    assert (store / "ledger.jsonl").exists() and (store / "audit.jsonl").exists()
    ledger = LedgerRepository(JsonlFileStore(store / "ledger.jsonl")).load()
    audit = AuditRepository(JsonlFileStore(store / "audit.jsonl")).load()
    assert ledger.is_globally_balanced("INR")
    assert audit.size >= 1
    root = audit.root()
    for i, e in enumerate(audit.events()):
        assert audit.verify_inclusion(e, i, audit.inclusion_proof(i), root)


def test_cli_store_with_explicit_at(tmp_path, capsys):
    store = tmp_path / "s2"
    rc = main([str(SAMPLE), "--store", str(store), "--at", "2026-08-26T00:00:00"])
    assert rc == 0
    assert "persisted:" in capsys.readouterr().out


def test_cli_bad_at_errors(capsys):
    rc = main([str(SAMPLE), "--at", "not-a-date"])
    assert rc == 2
    assert "invalid --at" in capsys.readouterr().err


def test_cli_store_banks_only(tmp_path, capsys):
    # no settlements -> audit stamp falls back to the first bank credit's ts
    data = {"settlements": [],
            "bank_credits": [{"id": "b1", "gross_amount": "10.00", "currency": "INR",
                              "ts": "2026-08-26T09:00:00", "refs": {"utr": "U1"}}]}
    p = tmp_path / "banks.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    rc = main([str(p), "--store", str(tmp_path / "st")])
    assert rc == 0
    assert "persisted:" in capsys.readouterr().out


def test_cli_store_empty_batch(tmp_path, capsys):
    # both empty -> _run_stamp returns None -> persist falls back to epoch stamp
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"settlements": [], "bank_credits": []}), encoding="utf-8")
    rc = main([str(p), "--store", str(tmp_path / "st2")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "persisted: 0 postings, 0 audit events" in out


def test_cli_replay_verifies_stored_run(tmp_path, capsys):
    store = tmp_path / "run"
    # first, persist a run
    assert main([str(SAMPLE), "--store", str(store)]) == 0
    capsys.readouterr()
    root = (store / "root.txt").read_text(encoding="utf-8").strip()

    # replay-verify it
    rc = main(["--replay", str(store)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "replay verification: VERIFIED" in out
    assert "ledger balanced  : True" in out
    assert out.count(root) >= 1          # the same root prefix appears


def test_cli_replay_expect_root_mismatch_fails(tmp_path, capsys):
    store = tmp_path / "run2"
    main([str(SAMPLE), "--store", str(store)])
    capsys.readouterr()
    rc = main(["--replay", str(store), "--expect-root", "00" * 32])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAILED" in out
    assert "root matches     : False" in out


def test_cli_replay_detects_tampered_ledger(tmp_path, capsys):
    store = tmp_path / "run3"
    main([str(SAMPLE), "--store", str(store)])
    capsys.readouterr()
    # tamper: corrupt the ledger file
    (store / "ledger.jsonl").write_text("not-json\n", encoding="utf-8")
    rc = main(["--replay", str(store)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAILED" in err


def test_cli_replay_missing_dir(tmp_path, capsys):
    rc = main(["--replay", str(tmp_path / "nope")])
    assert rc == 2
    assert "no stored run" in capsys.readouterr().err


def test_cli_requires_batch_or_replay(capsys):
    rc = main([])
    assert rc == 2
    assert "provide a batch file" in capsys.readouterr().err


def _keyfile(tmp_path):
    k = tmp_path / "key.bin"
    k.write_bytes(b"the-signing-secret")
    return k


def test_cli_sign_on_store_and_verify_on_replay(tmp_path, capsys):
    store = tmp_path / "srun"
    key = _keyfile(tmp_path)
    rc = main([str(SAMPLE), "--store", str(store), "--key-file", str(key)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "signed: audit.sig written" in out
    assert (store / "audit.sig").exists()

    rc = main(["--replay", str(store), "--key-file", str(key)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "signature        : VERIFIED" in out


def test_cli_replay_signature_fails_on_tamper(tmp_path, capsys):
    store = tmp_path / "srun2"
    key = _keyfile(tmp_path)
    main([str(SAMPLE), "--store", str(store), "--key-file", str(key)])
    capsys.readouterr()
    # tamper the audit log after signing -> recomputed root won't match the signed one
    with (store / "audit.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"kind":"match","at":"2026-08-26T09:00:00","detail":{"pair":"GHOST"}}\n')
    rc = main(["--replay", str(store), "--key-file", str(key)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "signature        : FAILED" in out


def test_cli_replay_signature_missing(tmp_path, capsys):
    store = tmp_path / "srun3"
    main([str(SAMPLE), "--store", str(store)])   # stored WITHOUT signing
    capsys.readouterr()
    rc = main(["--replay", str(store), "--key-file", str(_keyfile(tmp_path))])
    out = capsys.readouterr().out
    assert rc == 1
    assert "signature       : MISSING" in out


def test_cli_missing_key_file_errors(capsys):
    rc = main([str(SAMPLE), "--key-file", "no-such-key.bin"])
    assert rc == 2
    assert "key file not found" in capsys.readouterr().err


def test_cli_empty_key_file_errors(tmp_path, capsys):
    empty = tmp_path / "empty.key"
    empty.write_bytes(b"")
    rc = main([str(SAMPLE), "--key-file", str(empty)])
    assert rc == 2
    assert "key file is empty" in capsys.readouterr().err


SAMPLE_CSV = pathlib.Path(__file__).resolve().parents[1] / "examples" / "sample_batch.csv"


def test_cli_csv_input(capsys):
    rc = main(["--csv", str(SAMPLE_CSV), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    import json as _json
    data = _json.loads(out)
    # same batch as the JSON sample -> same detection-only numbers
    assert data["matched"] == "6382.00 INR"
    assert data["residual_leaks"] == 2


def test_cli_csv_bad_file_errors(capsys):
    rc = main(["--csv", "no-such.csv"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_cli_reports_pending_review(tmp_path, capsys):
    # garbled-UTR pair -> probabilistic review band, no resolver -> pending review
    data = {
        "settlements": [{"id": "s1", "gross_amount": "2000.00", "currency": "INR",
                         "ts": "2026-08-25T09:00:00", "refs": {"utr": "U1"}}],
        "bank_credits": [{"id": "b1", "gross_amount": "2000.00", "currency": "INR",
                          "ts": "2026-08-25T09:00:00", "refs": {"utr": "U1X"}}],
    }
    p = tmp_path / "b.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    rc = main([str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pending review" in out


# --------------------------------------------------------------------------
# Anchored replay — an unanchored replay cannot detect tampering, because a
# gutted log recomputes a valid Merkle root of whatever survives.
# --------------------------------------------------------------------------
def _gut_audit_log(store, keep=2):
    p = store / "audit.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    p.write_text("\n".join(lines[:keep]) + "\n", encoding="utf-8", newline="\n")


def test_store_publishes_root_file(tmp_path, capsys):
    store = tmp_path / "run"
    assert main([str(SAMPLE), "--store", str(store)]) == 0
    out = capsys.readouterr().out
    published = (store / "root.txt").read_text(encoding="utf-8").strip()
    assert len(published) == 64                      # full sha256 hex
    assert published[:16] in out                     # the printed prefix agrees
    assert "published:" in out
    # the head history records (size, root) so a later replay can prove append-only
    assert (store / "roots.log").read_text(encoding="utf-8").strip() == f"4 {published}"


def test_replay_auto_anchors_on_root_file(tmp_path, capsys):
    store = tmp_path / "run"
    main([str(SAMPLE), "--store", str(store)])
    capsys.readouterr()
    rc = main(["--replay", str(store)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "root.txt" in out                         # anchor line names the source
    assert "root matches     : True" in out


def test_replay_auto_anchor_catches_deleted_audit_events(tmp_path, capsys):
    """The regression this anchoring exists for: deleting audit events used to
    replay as VERIFIED because the log re-rooted itself."""
    store = tmp_path / "run"
    main([str(SAMPLE), "--store", str(store)])
    capsys.readouterr()
    _gut_audit_log(store)
    rc = main(["--replay", str(store)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "replay verification: FAILED" in out
    assert "root matches     : False" in out
    assert "audit events     : 2" in out             # proofs still pass on the gutted log...
    assert "inclusion proofs : ok" in out            # ...which is exactly why an anchor is required


def test_replay_refuses_when_unanchored(tmp_path, capsys):
    store = tmp_path / "run"
    main([str(SAMPLE), "--store", str(store)])
    capsys.readouterr()
    (store / "root.txt").unlink()                    # no published root, no key, no --expect-root
    rc = main(["--replay", str(store)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "UNANCHORED" in err


def test_replay_refuses_on_empty_root_file(tmp_path, capsys):
    store = tmp_path / "run"
    main([str(SAMPLE), "--store", str(store)])
    capsys.readouterr()
    (store / "root.txt").write_text("   \n", encoding="utf-8")
    rc = main(["--replay", str(store)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "is empty" in err


def test_explicit_expect_root_overrides_root_file(tmp_path, capsys):
    store = tmp_path / "run"
    main([str(SAMPLE), "--store", str(store)])
    capsys.readouterr()
    rc = main(["--replay", str(store), "--expect-root", "00" * 32])
    out = capsys.readouterr().out
    assert rc == 1
    assert "anchor           : --expect-root" in out


def test_key_file_alone_anchors_replay(tmp_path, capsys):
    """A signature over the root is an anchor too — no root.txt needed."""
    store, key = tmp_path / "run", tmp_path / "k.bin"
    key.write_bytes(b"0" * 32)
    main([str(SAMPLE), "--store", str(store), "--key-file", str(key)])
    capsys.readouterr()
    (store / "root.txt").unlink()
    rc = main(["--replay", str(store), "--key-file", str(key)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "anchor           : --key-file signature" in out
    assert "root matches" not in out                 # no published root to compare against
    assert "signature        : VERIFIED" in out


def test_key_file_alone_catches_deleted_audit_events(tmp_path, capsys):
    store, key = tmp_path / "run", tmp_path / "k.bin"
    key.write_bytes(b"0" * 32)
    main([str(SAMPLE), "--store", str(store), "--key-file", str(key)])
    capsys.readouterr()
    (store / "root.txt").unlink()
    _gut_audit_log(store)
    rc = main(["--replay", str(store), "--key-file", str(key)])
    assert rc == 1
    assert "signature        : FAILED" in capsys.readouterr().out


def test_cli_reports_closure_rate(capsys):
    main([str(SAMPLE), "--json"])
    data = json.loads(capsys.readouterr().out)
    # CLI is detection-only: nothing recovered, so closure == match.
    assert data["closed"] == data["matched"]
    assert data["closure_rate"] == data["match_rate"]


# --------------------------------------------------------------------------
# Append-only enforcement across runs (Phase 20). root.txt catches a log edited
# since publication; only the *head history* catches a log rewritten and then
# re-published.
# --------------------------------------------------------------------------
def _second_batch(tmp_path):
    """The sample batch with fresh ids, so it appends rather than dedupes."""
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    for s in data["settlements"]:
        s["id"] += "_r2"
        s["refs"]["utr"] = s["refs"].get("utr", "") + "_r2"
    for c in data["bank_credits"]:
        c["id"] += "_r2"
        c["refs"]["utr"] = c["refs"].get("utr", "") + "_r2"
    p = tmp_path / "batch2.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_head_published_describes_the_store_not_the_run(tmp_path, capsys):
    """A store accumulates; the head must track the store, not one batch."""
    store = tmp_path / "run"
    main([str(SAMPLE), "--store", str(store)])
    main([str(_second_batch(tmp_path)), "--store", str(store), "--at", "2026-08-26T09:00:00"])
    capsys.readouterr()
    heads = [l.split() for l in (store / "roots.log").read_text(encoding="utf-8").splitlines()]
    assert [int(h[0]) for h in heads] == [4, 8]          # store size, not 4 and 4
    assert (store / "root.txt").read_text(encoding="utf-8").strip() == heads[-1][1]


def test_replay_confirms_append_only_growth(tmp_path, capsys):
    store = tmp_path / "run"
    main([str(SAMPLE), "--store", str(store)])
    main([str(_second_batch(tmp_path)), "--store", str(store), "--at", "2026-08-26T09:00:00"])
    capsys.readouterr()
    rc = main(["--replay", str(store)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "append-only      : ok (vs 2 published head/s)" in out


def test_replay_catches_a_rewrite_hidden_behind_a_refreshed_root(tmp_path, capsys):
    """The attack root.txt alone cannot stop: rewrite history, republish the root."""
    store = tmp_path / "run"
    main([str(SAMPLE), "--store", str(store)])
    main([str(_second_batch(tmp_path)), "--store", str(store), "--at", "2026-08-26T09:00:00"])
    capsys.readouterr()

    audit_path = store / "audit.jsonl"
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["detail"]["amount"] = "9999.00 INR"
    lines[0] = json.dumps(rec, separators=(",", ":"), sort_keys=True)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    from reclaim.persistence import AuditRepository, JsonlFileStore
    forged = AuditRepository(JsonlFileStore(audit_path)).load().root()
    (store / "root.txt").write_text(forged + "\n", encoding="utf-8", newline="\n")

    rc = main(["--replay", str(store)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "root matches     : True" in out           # the forged anchor agrees...
    assert "inclusion proofs : ok" in out             # ...and the log self-verifies...
    assert "append-only      : FAILED" in out         # ...but history does not extend


def test_replay_ignores_malformed_head_lines(tmp_path, capsys):
    store = tmp_path / "run"
    main([str(SAMPLE), "--store", str(store)])
    capsys.readouterr()
    heads = store / "roots.log"
    heads.write_text("garbage\nnotanumber deadbeef\n" + heads.read_text(encoding="utf-8"),
                     encoding="utf-8", newline="\n")
    assert main(["--replay", str(store)]) == 0


def test_head_history_is_idempotent_on_repersist(tmp_path, capsys):
    store = tmp_path / "run"
    main([str(SAMPLE), "--store", str(store)])
    main([str(SAMPLE), "--store", str(store)])        # same run again
    capsys.readouterr()
    assert len((store / "roots.log").read_text(encoding="utf-8").strip().splitlines()) == 1


def test_replay_without_a_head_history_reports_no_append_only_check(tmp_path, capsys):
    """No published heads -> no consistency claim is made, rather than a false one."""
    store = tmp_path / "run"
    key = tmp_path / "k.bin"
    key.write_bytes(b"0" * 32)
    main([str(SAMPLE), "--store", str(store), "--key-file", str(key)])
    capsys.readouterr()
    (store / "roots.log").unlink()
    rc = main(["--replay", str(store), "--key-file", str(key)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "append-only" not in out          # silent, not a bogus "ok"
