"""Uplift modelling — chase the persuadable, leave everyone else alone.

Architecture §7: *"Uplift model — target only **persuadable** customers, skipping
'sure things' and 'lost causes', to optimise **incremental** recovery per rupee
of effort."* This is the component that turns recovery from a campaign into a
decision.

The distinction it exists to make:

| segment | recovers if chased | recovers if left alone | chase? |
|---|---|---|---|
| sure thing   | yes | yes | **no** — the money was coming anyway |
| persuadable  | yes | no  | **yes** — this is the whole product |
| lost cause   | no  | no  | **no** — contact with no upside |
| sleeping dog | no  | yes | **never** — chasing actively makes it worse |

A response model — "who will recover?" — cannot tell those apart, because three
of the four rows contain recoveries. Only uplift, the *difference* between the
arms, can. Getting this wrong is not a missed optimisation: chasing sure things
inflates gross recovery while adding nothing, and chasing sleeping dogs destroys
value while looking like activity.

**A T-learner over discrete cells.** Contexts are bucketed into interpretable
cells (failure reason × amount band × recency × prior-failure band); each cell
records treated and control recovery rates from *observed* outcomes; uplift is
their difference. Two deliberate consequences: every prediction is explainable
down to the counts behind it, and the whole model is deterministic, so it
replays (G4). A gradient-boosted learner on continuous features would fit
better with enough data — and would be neither explainable to a merchant nor
trainable on the volume a design partner has in month one. That trade is
recorded in ADR-0024.

**Hierarchical fallback, because low volume is the normal case.** A cell with
too little evidence does not get a made-up number. It falls back to the pooled
estimate for its failure reason, then to the global estimate, and finally to
``INSUFFICIENT_EVIDENCE`` — and every estimate says which level answered it.
This is the architecture's own open question ("can lift be estimated reliably
for small merchants? may need hierarchical/pooled models") answered in code.

**The model never acts.** It returns an estimate and a segment. Whether to skip
a unit is a policy decision made by the caller, with an explicit rule for the
unknown case, because a model quietly reducing recovery coverage on thin
evidence is a worse failure than chasing a few sure things.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Iterable, Optional

from .measurement import Arm
from .money import Money
from .recovery import FailureReason

_Q = Decimal("0.0001")
_ZERO = Decimal("0")


class UpliftError(Exception):
    """Raised on an invalid uplift configuration or training set."""


class Segment(str, Enum):
    """The four-quadrant persuadability split, plus an honest fifth."""

    SURE_THING = "sure_thing"
    PERSUADABLE = "persuadable"
    LOST_CAUSE = "lost_cause"
    SLEEPING_DOG = "sleeping_dog"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


# The level of the hierarchy that answered a prediction.
BASIS_CELL = "cell"
BASIS_POOLED = "pooled by failure reason"
BASIS_GLOBAL = "global"
BASIS_NONE = "no evidence"


@dataclass(frozen=True)
class Context:
    """The features an uplift decision is allowed to see.

    Deliberately small and all discrete-able. Everything here is either already
    in the domain or cheaply derivable from a leak and its history; nothing
    requires a feature store that does not exist. ``prior_failures`` is the one
    field the engine cannot currently source on its own, which is stated in the
    ADR rather than hidden behind a default.
    """

    failure_reason: FailureReason
    amount: Money
    days_since_failure: int
    prior_failures: int

    def __post_init__(self) -> None:
        if not isinstance(self.failure_reason, FailureReason):
            raise UpliftError("failure_reason must be a FailureReason")
        if not isinstance(self.amount, Money):
            raise UpliftError("amount must be Money")
        for name, val in (("days_since_failure", self.days_since_failure),
                          ("prior_failures", self.prior_failures)):
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise UpliftError(f"{name} must be a non-negative int")


@dataclass(frozen=True)
class CellSpec:
    """How continuous features become interpretable buckets.

    Band edges are exclusive upper bounds; a value above the last edge lands in
    the final bucket. Edges are part of the model: changing them changes the
    cells, so they are stored with it and reported in explanations.
    """

    amount_edges: tuple[Money, ...] = ()
    day_edges: tuple[int, ...] = (2, 8, 31)
    prior_failure_edges: tuple[int, ...] = (1, 3)

    def __post_init__(self) -> None:
        if not isinstance(self.amount_edges, tuple):
            raise UpliftError("amount_edges must be a tuple of Money")
        if not all(isinstance(m, Money) for m in self.amount_edges):
            raise UpliftError("amount_edges must be a tuple of Money")
        currencies = {m.currency for m in self.amount_edges}
        if len(currencies) > 1:
            raise UpliftError(f"amount_edges span multiple currencies: {sorted(currencies)}")
        for name, edges in (("day_edges", self.day_edges),
                            ("prior_failure_edges", self.prior_failure_edges)):
            if not (isinstance(edges, tuple) and edges):
                raise UpliftError(f"{name} must be a non-empty tuple of ints")
            if not all(isinstance(e, int) and not isinstance(e, bool) for e in edges):
                raise UpliftError(f"{name} must be a non-empty tuple of ints")
            if list(edges) != sorted(edges):
                raise UpliftError(f"{name} must be ascending")
        if list(self.amount_edges) != sorted(self.amount_edges, key=lambda m: m.amount):
            raise UpliftError("amount_edges must be ascending")

    @property
    def currency(self) -> Optional[str]:
        return self.amount_edges[0].currency if self.amount_edges else None

    def _band(self, value, edges) -> int:
        for i, edge in enumerate(edges):
            if value < edge:
                return i
        return len(edges)

    def amount_band(self, amount: Money) -> int:
        if not self.amount_edges:
            return 0
        if amount.currency != self.currency:
            raise UpliftError(
                f"amount is {amount.currency} but the model's bands are {self.currency}")
        return self._band(amount, self.amount_edges)

    def cell(self, context: Context) -> tuple:
        """The lookup key. A tuple of small ints and one enum value — hashable,
        printable, and stable across runs."""
        return (context.failure_reason.value,
                self.amount_band(context.amount),
                self._band(context.days_since_failure, self.day_edges),
                self._band(context.prior_failures, self.prior_failure_edges))


DEFAULT_SPEC = CellSpec(amount_edges=(Money.of("500", "INR"), Money.of("5000", "INR")))


@dataclass(frozen=True)
class TrainingRow:
    """One observed outcome, with the context that preceded it.

    ``recovered`` must be an *observed* outcome — the T+1 loop's verdict, not the
    executor's claim. Training on claims would teach the model that whatever the
    executor reports success for is persuadable, which is not a fact about
    customers at all.
    """

    context: Context
    arm: Arm
    recovered: bool

    def __post_init__(self) -> None:
        if not isinstance(self.context, Context):
            raise UpliftError("context must be a Context")
        if not isinstance(self.arm, Arm):
            raise UpliftError("arm must be an Arm")
        if not isinstance(self.recovered, bool):
            raise UpliftError("recovered must be a bool")


@dataclass(frozen=True)
class CellStats:
    """Treated and control counts for one cell. The whole model, per cell."""

    treated_n: int = 0
    treated_recovered: int = 0
    control_n: int = 0
    control_recovered: int = 0

    def plus(self, arm: Arm, recovered: bool) -> "CellStats":
        if arm is Arm.TREATED:
            return CellStats(self.treated_n + 1, self.treated_recovered + int(recovered),
                             self.control_n, self.control_recovered)
        return CellStats(self.treated_n, self.treated_recovered,
                         self.control_n + 1, self.control_recovered + int(recovered))

    @property
    def treated_rate(self) -> Optional[Decimal]:
        if self.treated_n == 0:
            return None
        return (Decimal(self.treated_recovered) / Decimal(self.treated_n)).quantize(_Q)

    @property
    def control_rate(self) -> Optional[Decimal]:
        if self.control_n == 0:
            return None
        return (Decimal(self.control_recovered) / Decimal(self.control_n)).quantize(_Q)

    def has_support(self, min_support: int) -> bool:
        """Both arms, not just one. An uplift needs a difference to be a difference."""
        return self.treated_n >= min_support and self.control_n >= min_support


@dataclass(frozen=True)
class SegmentThresholds:
    """Where the four quadrants begin. Explicit, because they are a business
    choice about how much incremental value justifies a contact — not something
    to bury as literals in a branch."""

    persuadable_uplift: Decimal = Decimal("0.05")
    sleeping_dog_uplift: Decimal = Decimal("-0.05")
    sure_thing_control_rate: Decimal = Decimal("0.70")
    lost_cause_treated_rate: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        for name, val in (("persuadable_uplift", self.persuadable_uplift),
                          ("sleeping_dog_uplift", self.sleeping_dog_uplift),
                          ("sure_thing_control_rate", self.sure_thing_control_rate),
                          ("lost_cause_treated_rate", self.lost_cause_treated_rate)):
            if not isinstance(val, Decimal):
                raise UpliftError(f"{name} must be a Decimal")
        if self.sleeping_dog_uplift >= _ZERO:
            raise UpliftError("sleeping_dog_uplift must be negative")
        if self.persuadable_uplift <= _ZERO:
            raise UpliftError("persuadable_uplift must be positive")
        if not (_ZERO <= self.sure_thing_control_rate <= Decimal("1")):
            raise UpliftError("sure_thing_control_rate must be within [0,1]")
        if not (_ZERO <= self.lost_cause_treated_rate <= Decimal("1")):
            raise UpliftError("lost_cause_treated_rate must be within [0,1]")


DEFAULT_THRESHOLDS = SegmentThresholds()


@dataclass(frozen=True)
class UpliftEstimate:
    """One prediction, and everything needed to argue with it."""

    segment: Segment
    uplift: Optional[Decimal]
    treated_rate: Optional[Decimal]
    control_rate: Optional[Decimal]
    stats: CellStats
    basis: str
    cell: tuple

    @property
    def is_known(self) -> bool:
        return self.segment is not Segment.INSUFFICIENT_EVIDENCE

    @property
    def worth_chasing(self) -> bool:
        """Only the persuadable. An unknown is *not* silently worth chasing —
        the caller decides that, explicitly."""
        return self.segment is Segment.PERSUADABLE

    def explain(self) -> str:
        if not self.is_known:
            return (f"no reliable estimate for cell {self.cell}: "
                    f"treated n={self.stats.treated_n}, control n={self.stats.control_n}")
        return (f"{self.segment.value}: treated {self.treated_rate} vs control "
                f"{self.control_rate} = {self.uplift} uplift, from {self.basis} "
                f"(treated n={self.stats.treated_n}, control n={self.stats.control_n})")

    def summary(self) -> dict:
        def s(v):
            return None if v is None else str(v)
        return {"segment": self.segment.value, "uplift": s(self.uplift),
                "treated_rate": s(self.treated_rate), "control_rate": s(self.control_rate),
                "treated_n": self.stats.treated_n, "control_n": self.stats.control_n,
                "basis": self.basis, "cell": list(self.cell)}


def _classify(uplift: Decimal, treated: Decimal, control: Decimal,
              t: SegmentThresholds) -> Segment:
    """Order matters and is deliberate.

    Sleeping dogs are checked first: a segment that is actively harmed must never
    be reclassified as something merely unprofitable. Sure things come next,
    because a high do-nothing rate makes the contact pointless whatever the
    treated rate looks like. Lost causes follow, then persuadable — so a unit is
    only chased when nothing else explains it.
    """
    if uplift <= t.sleeping_dog_uplift:
        return Segment.SLEEPING_DOG
    if control >= t.sure_thing_control_rate:
        return Segment.SURE_THING
    if treated <= t.lost_cause_treated_rate:
        return Segment.LOST_CAUSE
    if uplift >= t.persuadable_uplift:
        return Segment.PERSUADABLE
    # A real but too-small difference. Not persuadable enough to justify contact,
    # and saying so is more useful than rounding it up.
    return Segment.LOST_CAUSE


class UpliftModel:
    """A fitted T-learner over discrete cells, with a hierarchical fallback."""

    def __init__(self, cells: dict, pooled: dict, glob: CellStats, *,
                 spec: CellSpec = DEFAULT_SPEC, min_support: int = 10,
                 thresholds: SegmentThresholds = DEFAULT_THRESHOLDS) -> None:
        self._cells = dict(cells)
        self._pooled = dict(pooled)
        self._global = glob
        self._spec = spec
        self._min_support = min_support
        self._thresholds = thresholds

    # ---- introspection ------------------------------------------------
    @property
    def spec(self) -> CellSpec:
        return self._spec

    @property
    def min_support(self) -> int:
        return self._min_support

    @property
    def cell_count(self) -> int:
        return len(self._cells)

    def stats_for(self, context: Context) -> CellStats:
        return self._cells.get(self._spec.cell(context), CellStats())

    # ---- prediction ---------------------------------------------------
    def predict(self, context: Context) -> UpliftEstimate:
        """Estimate uplift for one context, falling back rather than inventing.

        The fallback chain is cell -> failure reason -> global -> nothing. Each
        level is only used when it clears ``min_support`` in *both* arms, and the
        level that answered is reported, so a thin estimate can never be mistaken
        for a specific one.
        """
        if not isinstance(context, Context):
            raise UpliftError("predict() requires a Context")
        key = self._spec.cell(context)
        for stats, basis in ((self._cells.get(key, CellStats()), BASIS_CELL),
                             (self._pooled.get(context.failure_reason.value, CellStats()),
                              BASIS_POOLED),
                             (self._global, BASIS_GLOBAL)):
            if not stats.has_support(self._min_support):
                continue
            treated, control = stats.treated_rate, stats.control_rate
            uplift = (treated - control).quantize(_Q)
            return UpliftEstimate(
                segment=_classify(uplift, treated, control, self._thresholds),
                uplift=uplift, treated_rate=treated, control_rate=control,
                stats=stats, basis=basis, cell=key)
        return UpliftEstimate(segment=Segment.INSUFFICIENT_EVIDENCE, uplift=None,
                              treated_rate=None, control_rate=None,
                              stats=self._cells.get(key, CellStats()),
                              basis=BASIS_NONE, cell=key)

    def segment(self, context: Context) -> Segment:
        return self.predict(context).segment


def fit(rows: Iterable[TrainingRow], *, spec: CellSpec = DEFAULT_SPEC,
        min_support: int = 10,
        thresholds: SegmentThresholds = DEFAULT_THRESHOLDS) -> UpliftModel:
    """Fit the model from observed outcomes.

    Fitting is a counting exercise, which is the point: there is no optimiser to
    seed, nothing to converge, and the same rows in any order produce the same
    model.
    """
    if not isinstance(spec, CellSpec):
        raise UpliftError("spec must be a CellSpec")
    if not isinstance(min_support, int) or isinstance(min_support, bool) or min_support < 1:
        raise UpliftError("min_support must be a positive int")

    cells: dict[tuple, CellStats] = {}
    pooled: dict[str, CellStats] = {}
    glob = CellStats()
    seen = 0
    for row in rows:
        if not isinstance(row, TrainingRow):
            raise UpliftError("every training row must be a TrainingRow")
        seen += 1
        key = spec.cell(row.context)
        cells[key] = cells.get(key, CellStats()).plus(row.arm, row.recovered)
        reason = row.context.failure_reason.value
        pooled[reason] = pooled.get(reason, CellStats()).plus(row.arm, row.recovered)
        glob = glob.plus(row.arm, row.recovered)
    if seen == 0:
        raise UpliftError("cannot fit an uplift model with no observations")
    return UpliftModel(cells, pooled, glob, spec=spec, min_support=min_support,
                       thresholds=thresholds)


# --------------------------------------------------------------------------
# Turning an estimate into a decision — the caller's policy, made explicit
# --------------------------------------------------------------------------
class UnknownPolicy(str, Enum):
    """What to do when the model has no reliable estimate.

    ``CHASE`` preserves today's behaviour (chase every recoverable leak), which
    is the safe default for a *product*: a model silently shrinking recovery
    coverage on thin evidence loses a merchant money they were owed. ``SKIP``
    is available for a deployment that would rather under-contact.
    """

    CHASE = "chase"
    SKIP = "skip"


@dataclass(frozen=True)
class Selection:
    """A chase/skip decision with the reasoning attached."""

    chase: bool
    estimate: UpliftEstimate
    reason: str


@dataclass(frozen=True)
class SelectionSummary:
    """What a whole batch's targeting decided, and what it gave up.

    ``skipped_value`` is reported because skipping is not free: it is money the
    engine chose not to chase, and a targeting layer that hides that number is
    unauditable.
    """

    selections: dict = field(default_factory=dict)
    chased: int = 0
    skipped: int = 0
    skipped_value: Optional[Money] = None
    by_segment: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"chased": self.chased, "skipped": self.skipped,
                "skipped_value": str(self.skipped_value) if self.skipped_value else None,
                "by_segment": dict(self.by_segment)}


def decide(model: UpliftModel, context: Context, *,
           unknown: UnknownPolicy = UnknownPolicy.CHASE) -> Selection:
    """Chase or skip one unit, with the estimate and reason recorded."""
    if not isinstance(unknown, UnknownPolicy):
        raise UpliftError("unknown must be an UnknownPolicy")
    est = model.predict(context)
    if not est.is_known:
        chase = unknown is UnknownPolicy.CHASE
        verb = "chasing" if chase else "skipping"
        return Selection(chase, est, f"{verb} on the {unknown.value} policy: {est.explain()}")
    return Selection(est.worth_chasing, est, est.explain())


def select(model: UpliftModel, contexts: dict, *,
           unknown: UnknownPolicy = UnknownPolicy.CHASE) -> SelectionSummary:
    """Target a batch: ``{unit_id: Context}`` in, decisions and totals out."""
    selections: dict[str, Selection] = {}
    by_segment: dict[str, int] = {}
    chased = skipped = 0
    skipped_value: Optional[Money] = None
    for unit_id, context in contexts.items():
        sel = decide(model, context, unknown=unknown)
        selections[unit_id] = sel
        name = sel.estimate.segment.value
        by_segment[name] = by_segment.get(name, 0) + 1
        if sel.chase:
            chased += 1
        else:
            skipped += 1
            skipped_value = (context.amount if skipped_value is None
                             else skipped_value + context.amount)
    return SelectionSummary(selections=selections, chased=chased, skipped=skipped,
                            skipped_value=skipped_value, by_segment=by_segment)
