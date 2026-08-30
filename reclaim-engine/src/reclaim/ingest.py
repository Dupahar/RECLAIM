"""Layer 1 — the medallion data platform, Bronze → Silver → Gold.

Architecture §4. Bronze: *"raw sources land exactly as received, immutable,
replayable."* Silver: *"cleaned, deduplicated, conformed into the canonical
transaction schema... every extracted field is validated against a deterministic
schema and low-confidence extractions are flagged, not guessed."* Gold:
*"reconciliation-ready views."*

Until now the engine had a validating *boundary* (``batch_io``, ``csv_io``) and no
*platform*. The difference matters in three places:

**Nothing was replayable from source.** A malformed row raised and the batch
stopped. There was no immutable record of what actually arrived, so "re-run
yesterday's file and get the same answer" was a property of the file sitting on
disk, not of the system.

**A rejected row disappeared.** Goal G2 is "nothing leaves the system
undocumented", and a row that failed validation left no trace at all — the one
category of data most likely to be hiding money. The invariant this module
enforces is arithmetic and checkable: **every Bronze record ends up in exactly
one of transactions, quarantine, or duplicates**, and ``accounted_for()`` proves
it.

**Narration parsing had nowhere to live.** Indian bank statements carry the
reference inside a free-text narration (``NEFT-UTR000123456789-ACME``,
``UPI/432198765432/PAYMENT``) rather than in a column. Extracting it is exactly
the "long tail" the architecture points at, and it is the one place in ingestion
where a wrong guess silently creates a false match. So extraction reports a
confidence, and anything below the threshold is **quarantined rather than
guessed** — the same stance the AI resolver takes on an uncertain match.

**Duplicate UTRs are caught at the boundary, not deep in the matcher.**
``reconcile_settlements_to_bank`` raises on a duplicate UTR, which is correct but
surfaces as an exception from the middle of the pipeline with a whole batch lost.
Gold detects the collision while it still knows which row and line number caused
it, and quarantines those rows so the rest of the batch still reconciles.

The LLM extractor the architecture mentions is left as a seam
(``NarrationExtractor``) with a deterministic implementation behind it, exactly
as ``ChatClient`` is for the resolver. No LLM is called here.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional, Protocol, runtime_checkable

from .domain import Fees, Source, Transaction, TransactionRefs
from .money import Money, MoneyError

_Q = Decimal("0.0001")


class IngestError(Exception):
    """Raised when a record cannot be conformed. Carries the reason for the quarantine."""


# Quarantine rule ids — machine-readable, so a report can be totalled by cause.
RULE_ADAPTER = "adapter_rejected"
RULE_LOW_CONFIDENCE = "extraction_below_threshold"
RULE_DUPLICATE_UTR = "duplicate_utr_in_batch"
RULE_MISSING_UTR = "no_reference_found"


# --------------------------------------------------------------------------
# Bronze — what actually arrived
# --------------------------------------------------------------------------
def _canonical(raw: dict) -> str:
    return json.dumps(raw, separators=(",", ":"), sort_keys=True, default=str)


@dataclass(frozen=True)
class BronzeRecord:
    """One row exactly as received, plus where it came from.

    ``content_hash`` makes Bronze content-addressed, which is what lets the same
    file land twice without producing two Silver rows — and lets a replay prove
    it read the same bytes rather than merely the same filename.
    """

    batch_id: str
    source: str
    line_no: int
    raw: dict
    content_hash: str

    def __post_init__(self) -> None:
        for name, val in (("batch_id", self.batch_id), ("source", self.source),
                          ("content_hash", self.content_hash)):
            if not (isinstance(val, str) and val):
                raise IngestError(f"{name} is required")
        if not isinstance(self.line_no, int) or isinstance(self.line_no, bool) or self.line_no < 1:
            raise IngestError("line_no must be a positive int")
        if not isinstance(self.raw, dict):
            raise IngestError("raw must be a dict")

    @property
    def lineage(self) -> str:
        """The string stamped into every Silver row's ``evidence``."""
        return f"bronze:{self.batch_id}:{self.source}:{self.line_no}"


class BronzeLayer:
    """Append-only, content-addressed landing zone."""

    def __init__(self) -> None:
        self._records: list[BronzeRecord] = []
        self._hashes: set[str] = set()

    def land(self, batch_id: str, source: str,
             rows: Iterable[dict]) -> tuple[tuple[BronzeRecord, ...], tuple[BronzeRecord, ...]]:
        """Land rows. Returns ``(new, duplicates)``.

        A duplicate is a row whose content has been landed before — the same file
        re-delivered, or an overlapping export. It is *returned*, not discarded,
        so a caller can report how much of a delivery was already known.
        """
        if not (isinstance(batch_id, str) and batch_id):
            raise IngestError("batch_id is required")
        if not (isinstance(source, str) and source):
            raise IngestError("source is required")
        new: list[BronzeRecord] = []
        duplicates: list[BronzeRecord] = []
        for i, raw in enumerate(rows, start=1):
            if not isinstance(raw, dict):
                raise IngestError(f"{source} line {i}: every row must be a dict")
            digest = hashlib.sha256(_canonical(raw).encode("utf-8")).hexdigest()
            record = BronzeRecord(batch_id=batch_id, source=source, line_no=i,
                                  raw=dict(raw), content_hash=digest)
            if digest in self._hashes:
                duplicates.append(record)
                continue
            self._hashes.add(digest)
            self._records.append(record)
            new.append(record)
        return tuple(new), tuple(duplicates)

    def records(self) -> tuple[BronzeRecord, ...]:
        return tuple(self._records)

    @property
    def size(self) -> int:
        return len(self._records)


# --------------------------------------------------------------------------
# Narration extraction — the honest part of the long tail
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Extraction:
    """An extracted reference and how much to believe it."""

    utr: Optional[str]
    confidence: Decimal
    basis: str


@runtime_checkable
class NarrationExtractor(Protocol):
    """The seam an LLM extractor would implement.

    A deterministic implementation ships; nothing here calls a model. The
    architecture's case for LLM extraction is the tail this regex cannot reach,
    and the gate is the same either way: a low-confidence extraction is
    quarantined, never guessed.
    """

    def extract(self, narration: str) -> Extraction:  # pragma: no cover - protocol
        ...


# An explicitly labelled reference is the strongest signal available.
_LABELLED = re.compile(r"\bUTR[:\-/ ]?([A-Z0-9]{8,22})\b", re.IGNORECASE)
# A bare reference run inside a known scheme prefix is good but not certain: the
# same shape appears in account numbers and internal ids.
_SCHEME = re.compile(r"\b(NEFT|IMPS|UPI|RTGS)[:\-/ ]+([A-Z0-9]{8,22})\b", re.IGNORECASE)


class RegexNarrationExtractor:
    """Deterministic extraction with a calibrated, stated confidence.

    The two confidences are a claim about *this* rule set, not a probability from
    a model: a labelled ``UTR...`` is taken at 1.0 because the label removes the
    ambiguity, and a bare run after a scheme prefix at 0.75 because the same
    shape appears in account numbers. Anything else returns 0 and is quarantined,
    which is the only safe answer — a wrong reference here does not fail loudly,
    it silently creates a false match.
    """

    def extract(self, narration: str) -> Extraction:
        if not isinstance(narration, str) or not narration.strip():
            return Extraction(None, Decimal("0"), "narration is empty")
        labelled = _LABELLED.search(narration)
        if labelled:
            return Extraction(labelled.group(1).upper(), Decimal("1.0"),
                              "explicitly labelled UTR")
        scheme = _SCHEME.search(narration)
        if scheme:
            return Extraction(scheme.group(2).upper(), Decimal("0.75"),
                              f"reference after a {scheme.group(1).upper()} prefix")
        return Extraction(None, Decimal("0"), "no recognised reference pattern")


# --------------------------------------------------------------------------
# Adapters — one per source format
# --------------------------------------------------------------------------
@runtime_checkable
class Adapter(Protocol):
    """Turns one Bronze record into a canonical ``Transaction``.

    Raises ``IngestError`` with a human reason rather than returning ``None``, so
    the quarantine record explains itself to whoever has to fix the file.
    """

    def to_transaction(self, record: BronzeRecord) -> Transaction:  # pragma: no cover - protocol
        ...


_SEPARATORS = re.compile(r"[\s\-/:._]+")
_UTR_LABEL = re.compile(r"^UTR", re.IGNORECASE)


def normalise_utr(value: str) -> str:
    """Conform a reference so both sides of a match agree on its spelling.

    This is what "cleaned, conformed" means in practice, and it is not cosmetic.
    A settlement export writes ``UTR000123456789``; the bank narration writes
    ``NEFT-UTR000123456789-ACME``, from which the label is a *label* and the
    reference is the run after it. Left unnormalised the two spellings differ,
    the exact matcher finds nothing, and the batch reports a 0% match rate with
    no error anywhere -- which is precisely the failure mode this layer exists to
    catch, and precisely the one a validating boundary alone does not.

    Uppercases, drops separators, and strips a leading ``UTR`` label. The
    original spelling is kept in the row's ``evidence``, so normalising loses
    nothing.
    """
    if not isinstance(value, str):
        raise IngestError("a reference must be a string")
    cleaned = _UTR_LABEL.sub("", _SEPARATORS.sub("", value.strip().upper()))
    if not cleaned.isalnum():
        raise IngestError(f"reference {value!r} is not alphanumeric after normalising")
    return cleaned


def _money(raw: dict, key: str, currency: str, where: str) -> Money:
    if key not in raw:
        raise IngestError(f"{where}: '{key}' is required")
    try:
        return Money.of(str(raw[key]), currency)
    except MoneyError as exc:
        raise IngestError(f"{where}: '{key}' is not an amount ({exc})") from exc


def _timestamp(raw: dict, key: str, where: str) -> datetime:
    if key not in raw:
        raise IngestError(f"{where}: '{key}' is required")
    try:
        return datetime.fromisoformat(str(raw[key]))
    except ValueError as exc:
        raise IngestError(f"{where}: '{key}' is not an ISO-8601 timestamp ({exc})") from exc


@dataclass(frozen=True)
class PgSettlementAdapter:
    """A payment-gateway settlement export, with the deductions still stacked.

    MDR, GST-on-MDR and TCS are unpacked here so nothing downstream ever sees a
    gross amount it might mistake for a payout — the architecture's *"unpack
    MDR / GST-on-MDR / TCS before anything downstream sees them"*.
    """

    currency: str = "INR"

    def to_transaction(self, record: BronzeRecord) -> Transaction:
        raw, where = record.raw, f"{record.source} line {record.line_no}"
        settlement_id = str(raw.get("settlement_id") or "").strip()
        if not settlement_id:
            raise IngestError(f"{where}: 'settlement_id' is required")
        raw_utr = str(raw.get("utr") or "").strip()
        if not raw_utr:
            raise IngestError(f"{where}: 'utr' is required for a settlement")
        # normalise_utr raises when a value reduces to nothing, so there is no
        # empty-result case to guard here.
        utr = normalise_utr(raw_utr)
        zero = Money.zero(self.currency)
        fees = Fees(
            mdr=_money(raw, "mdr", self.currency, where) if "mdr" in raw else zero,
            gst_on_mdr=(_money(raw, "gst_on_mdr", self.currency, where)
                        if "gst_on_mdr" in raw else zero),
            tcs=_money(raw, "tcs", self.currency, where) if "tcs" in raw else zero,
            other=_money(raw, "other_fees", self.currency, where) if "other_fees" in raw else zero,
        )
        gross = _money(raw, "gross_amount", self.currency, where)
        if fees.total() > gross:
            raise IngestError(f"{where}: fees {fees.total()} exceed gross {gross}")
        return Transaction(
            id=settlement_id, source=Source.SETTLEMENT, gross_amount=gross,
            ts=_timestamp(raw, "settled_at", where), fees=fees,
            refs=TransactionRefs(utr=utr, order_id=str(raw["order_id"]) if raw.get("order_id") else None),
            counterparty=str(raw["merchant_id"]) if raw.get("merchant_id") else None,
            evidence=(record.lineage, f"utr as delivered: {raw_utr}"),
        )


@dataclass(frozen=True)
class BankNarrationAdapter:
    """A bank statement whose reference lives in free text, not a column.

    Only credits are ingested: a debit on the merchant's account is not a
    settlement payout, and treating one as a credit would invent money.
    """

    currency: str = "INR"
    extractor: object = None
    min_confidence: Decimal = Decimal("0.70")

    def __post_init__(self) -> None:
        if not isinstance(self.min_confidence, Decimal):
            raise IngestError("min_confidence must be a Decimal")

    def _extractor(self):
        return self.extractor if self.extractor is not None else RegexNarrationExtractor()

    def to_transaction(self, record: BronzeRecord) -> Transaction:
        raw, where = record.raw, f"{record.source} line {record.line_no}"
        txn_id = str(raw.get("txn_id") or "").strip()
        if not txn_id:
            raise IngestError(f"{where}: 'txn_id' is required")
        direction = str(raw.get("direction") or "credit").strip().lower()
        if direction != "credit":
            raise IngestError(f"{where}: direction is {direction!r}; only credits are "
                              "settlement payouts, and treating a debit as one "
                              "would invent money")
        narration = str(raw.get("narration") or "")
        found = self._extractor().extract(narration)
        if found.utr is None:
            raise IngestError(f"{where}: {found.basis} in narration {narration!r}")
        normalised = normalise_utr(found.utr)
        if found.confidence < self.min_confidence:
            raise IngestError(
                f"{where}: reference {found.utr!r} extracted at confidence "
                f"{found.confidence} ({found.basis}), below {self.min_confidence}; "
                "quarantined rather than guessed -- a wrong reference here does not "
                "fail loudly, it silently creates a false match")
        return Transaction(
            id=txn_id, source=Source.BANK,
            gross_amount=_money(raw, "amount", self.currency, where),
            ts=_timestamp(raw, "value_date", where),
            refs=TransactionRefs(utr=normalised),
            counterparty=str(raw["counterparty"]) if raw.get("counterparty") else None,
            narration_raw=narration or None,
            match_confidence=found.confidence,
            evidence=(record.lineage, f"utr extracted: {found.basis}",
                      f"utr as written: {found.utr}"),
        )


# --------------------------------------------------------------------------
# Silver — conformed, with nothing dropped
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Quarantined:
    """A row that could not be conformed, and why."""

    record: BronzeRecord
    rule: str
    reason: str

    def summary(self) -> dict:
        return {"source": self.record.source, "line": self.record.line_no,
                "rule": self.rule, "reason": self.reason}


@dataclass(frozen=True)
class SilverBatch:
    transactions: tuple[Transaction, ...]
    quarantined: tuple[Quarantined, ...]
    duplicates: tuple[BronzeRecord, ...]
    landed: int

    def accounted_for(self) -> bool:
        """Every landed row is in exactly one bucket. G2, as arithmetic.

        This is the property the whole layer exists to provide: a row cannot be
        silently dropped, because the counts would not add up and a test checks
        that they do.
        """
        return self.landed == len(self.transactions) + len(self.quarantined)

    def quarantine_by_rule(self) -> dict:
        counts: dict[str, int] = {}
        for item in self.quarantined:
            counts[item.rule] = counts.get(item.rule, 0) + 1
        return dict(sorted(counts.items()))

    def summary(self) -> dict:
        return {"landed": self.landed, "conformed": len(self.transactions),
                "quarantined": len(self.quarantined),
                "duplicates_skipped": len(self.duplicates),
                "accounted_for": self.accounted_for(),
                "quarantine_by_rule": self.quarantine_by_rule()}


def to_silver(records: Iterable[BronzeRecord], adapter: Adapter, *,
              duplicates: Iterable[BronzeRecord] = ()) -> SilverBatch:
    """Conform Bronze to canonical transactions, quarantining what will not go."""
    landed = list(records)
    conformed: list[Transaction] = []
    held: list[Quarantined] = []
    for record in landed:
        try:
            conformed.append(adapter.to_transaction(record))
        except IngestError as exc:
            rule = (RULE_LOW_CONFIDENCE if "below" in str(exc)
                    else RULE_MISSING_UTR if "narration" in str(exc)
                    else RULE_ADAPTER)
            held.append(Quarantined(record=record, rule=rule, reason=str(exc)))
    return SilverBatch(transactions=tuple(conformed), quarantined=tuple(held),
                       duplicates=tuple(duplicates), landed=len(landed))


# --------------------------------------------------------------------------
# Gold — reconciliation-ready, with the matcher's preconditions checked here
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class GoldBatch:
    settlements: tuple[Transaction, ...]
    bank_credits: tuple[Transaction, ...]
    quarantined: tuple[Quarantined, ...]

    def to_reconcile(self) -> tuple[list, list]:
        """The pair ``run_reclaim`` takes."""
        return list(self.settlements), list(self.bank_credits)

    def summary(self) -> dict:
        return {"settlements": len(self.settlements),
                "bank_credits": len(self.bank_credits),
                "quarantined": len(self.quarantined)}


def _dedupe_by_utr(txns, index: dict) -> tuple[list, list]:
    """Split rows into those with a unique UTR and those colliding.

    Both sides of a collision are quarantined rather than keeping the first.
    Keeping one would be a silent choice about which row is real, and a duplicate
    UTR usually means the file was double-delivered or the export is wrong — both
    of which a human should see rather than have resolved by arrival order.
    """
    by_utr: dict[str, list] = {}
    for txn in txns:
        by_utr.setdefault(txn.refs.utr, []).append(txn)
    kept, colliding = [], []
    for utr, group in by_utr.items():
        if len(group) == 1:
            kept.append(group[0])
        else:
            colliding.extend(group)
    return kept, colliding


def to_gold(*batches: SilverBatch, bronze_by_txn: Optional[dict] = None) -> GoldBatch:
    """Combine Silver batches into a reconciliation-ready view.

    Duplicate UTRs are detected here, where the line number that caused them is
    still known, instead of surfacing as an exception from inside the matcher
    with the whole batch lost.
    """
    settlements, banks = [], []
    held: list[Quarantined] = list()
    for batch in batches:
        held.extend(batch.quarantined)
        for txn in batch.transactions:
            (settlements if txn.source is Source.SETTLEMENT else banks).append(txn)

    lineage = bronze_by_txn or {}

    def _quarantine(txn: Transaction, side: str) -> Quarantined:
        record = lineage.get(txn.id)
        if record is None:
            record = BronzeRecord(batch_id="unknown", source="unknown", line_no=1,
                                  raw={"id": txn.id}, content_hash="unknown")
        return Quarantined(
            record=record, rule=RULE_DUPLICATE_UTR,
            reason=(f"UTR {txn.refs.utr!r} appears more than once among {side}s; "
                    "both rows quarantined rather than resolving by arrival order"))

    kept_settlements, clashing_settlements = _dedupe_by_utr(settlements, lineage)
    kept_banks, clashing_banks = _dedupe_by_utr(banks, lineage)
    held.extend(_quarantine(t, "settlement") for t in clashing_settlements)
    held.extend(_quarantine(t, "bank credit") for t in clashing_banks)

    return GoldBatch(
        settlements=tuple(sorted(kept_settlements, key=lambda t: t.id)),
        bank_credits=tuple(sorted(kept_banks, key=lambda t: t.id)),
        quarantined=tuple(held))
