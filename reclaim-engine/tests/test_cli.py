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
    # capture the audit root that was printed
    persisted_out = capsys.readouterr().out
    root = persisted_out.split("audit root ")[1].split("...")[0]

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
