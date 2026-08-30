# ADR-0023 — Escalation is a durable state, not an outcome

**Status:** Accepted · 2026-08-31 · implements architecture §8 (Layer 6, Agentic Control Plane)

## Context

Layer 6 was the thinnest layer in the engine — roughly 15% implemented — and the
reason was specific: `ESCALATE_HUMAN` and `HALTED` were *terminal labels on a
report*. The report was printed, the process exited, and the escalation
evaporated. The architecture promises that *"HITL gates are typed states, not
afterthoughts: any action above a value threshold, any low-confidence match, and
any novel exception routes to a human and **waits**"* and that *"a checkpointer
lets any decision pause, persist, and resume"*. Neither was true. There was a
human in the name of the design but no loop for them to be in.

## Decision

A `control` module holding the durable state machine, a `GateRepository` for its
storage, and `--queue` / `--decide` on the CLI so the loop can actually be
driven.

    open a typed gate  ->  AWAITING_HUMAN  ->  APPROVED / REJECTED / CANCELLED
         (checkpoint)         (durable)              (resumable)

**Gates are typed, and derived rather than raised.** `gates_for_run` reads a run
and mechanically produces every question it has to ask: one per AI escalation,
one per halted recovery, and — separately — `AFA_CEILING` for a halt that means
"has the customer authenticated?" versus `RECOVERY_HALT` for one that means
"should we keep going?". Those are different questions and must not share a
kind. Above a configured `value_threshold`, chasing a large leak needs sign-off
even when nothing went wrong. Deriving rather than hand-raising means a gate
cannot be forgotten.

**A gate is decided exactly once.** Re-deciding raises; replaying the *identical*
decision is a no-op. Two approvals of one high-value debit is the failure mode
that costs a merchant real money, so it is prevented by the state machine rather
than by a UI that hides the button.

**Approval authorises; it does not act.** The architecture's stance that *"an
agent can never move money by deciding to; it can only enqueue a bounded, gated,
idempotent saga"* is applied to humans identically. `resume` is what executes,
through the same injected `RecoveryEngine` with the same notice window, AFA
ceiling and idempotency keys. Two tests pin this: approving posts nothing, and
a human "yes" on an over-ceiling debit still halts. Approval changes who is
accountable, not what is safe.

**Append-only and versioned**, mirroring `LeakLedger` deliberately so there is
one way to reason about mutable state in this codebase rather than two. "Who
approved this, on what evidence, and when?" is always answerable, and rejecting
a gate writes the leak off *with a name attached* — a better outcome than a leak
sitting in a queue forever with nobody accountable.

**An unattended gate is not a clean run.** `ResumeOutcome.still_waiting` is a
first-class field and `is_complete` is false while anything is open. A resume
that silently ignored unanswered gates would let an incomplete run read as a
finished one.

**Rehydration cannot invent an authorisation.** `GateRepository.load` replays
versions through the same transition rules the live plane enforces, and a log
whose first record for an id is already `approved` is refused — a decision with
no open checkpoint behind it means the log lost the question, and reconstructing
it would fabricate consent.

**No anonymous, undated decisions.** `--decide` requires `--actor` and an
explicit `--at`; the engine never reads the clock (G4), and a decision with no
name on it is not an audit trail.

## What this deliberately does not do

This is **not** a workflow engine. There is no scheduler, no timer, no
retry-on-crash, no SLA on how long a gate may sit unanswered. Temporal and a
LangGraph checkpointer own those in the target architecture; what is built here
is the durable state and the transition rules they would persist, which is the
part that has to be correct whichever engine runs it.

`resume` does not re-enter the pipeline: an approved match gate is *reported* as
confirmed rather than folded back into a fresh `RunReport` with a recomputed
match rate. Closing that circle means re-running reconciliation with human
overrides as an input, which is a larger change to `run_reclaim` than this phase
took on.

Gates are also not yet linked into the Merkle audit log — `Gate.audit_ref`
exists and is persisted, but nothing populates it, so a decision is not yet
provable against the audit root the way a leak is.

## Tested by

`tests/test_control.py` (45), plus 9 in `tests/test_persistence.py` and 13 in
`tests/test_cli.py`. The load-bearing ones are attempts to break a safety claim:
`test_a_gate_is_decided_exactly_once`, `test_approving_a_gate_moves_no_money`,
`test_a_human_yes_does_not_bypass_the_afa_ceiling`,
`test_an_unanswered_gate_makes_the_resume_incomplete`,
`test_a_log_whose_gate_opens_already_approved_is_refused`, and
`test_the_checkpointer_survives_a_restart_on_disk` — which writes a gate in one
process, loads it in another, answers it, and reloads to find the decision and
its history intact.
