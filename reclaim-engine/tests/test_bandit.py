"""Phase 27 tests — the contextual bandit (architecture §7).

The bandit's job is to explore without becoming unreplayable and to hand the
offline evaluator a propensity it can actually divide by. So the tests that
matter are the propensity identities, the determinism of exploration, and the
refusal to write off an arm it has never tried.
"""
from decimal import Decimal

import pytest

from reclaim.bandit import (
    Action,
    ArmStats,
    BanditError,
    Choice,
    DEFAULT_CONFIG,
    EpsilonGreedyBandit,
    EpsilonGreedyConfig,
    FixedActionPolicy,
    GreedyPolicy,
    LoggingPolicyEcho,
)
from reclaim.recovery import Channel

D = Decimal

A1 = Action(Channel.UPI_RETRY, 10, "m1")
A2 = Action(Channel.WHATSAPP_NUDGE, 18, "m2")
A3 = Action(Channel.UPI_RETRY, 6, "m3")
ACTIONS = (A1, A2, A3)


def bandit(*, actions=ACTIONS, stats=None, epsilon="0.10") -> EpsilonGreedyBandit:
    return EpsilonGreedyBandit(actions, stats,
                               config=EpsilonGreedyConfig(epsilon=D(epsilon)))


# --------------------------------------------------------------------------
# Action / ArmStats
# --------------------------------------------------------------------------
def test_action_validates_its_parts():
    with pytest.raises(BanditError):
        Action("upi_retry", 10, "m1")
    with pytest.raises(BanditError):
        Action(Channel.UPI_RETRY, "10", "m1")
    with pytest.raises(BanditError):
        Action(Channel.UPI_RETRY, True, "m1")
    with pytest.raises(BanditError):
        Action(Channel.UPI_RETRY, 24, "m1")
    with pytest.raises(BanditError):
        Action(Channel.UPI_RETRY, -1, "m1")
    with pytest.raises(BanditError):
        Action(Channel.UPI_RETRY, 10, "")


def test_action_key_is_stable_and_sortable():
    assert A1.key == "upi_retry|10|m1"
    assert A3.key == "upi_retry|06|m3"          # zero-padded, so it sorts by hour
    assert sorted([A1.key, A3.key]) == [A3.key, A1.key]


def test_arm_stats_validate_their_counts():
    with pytest.raises(BanditError):
        ArmStats(-1, 0)
    with pytest.raises(BanditError):
        ArmStats(True, 0)
    with pytest.raises(BanditError):
        ArmStats(0, "1")


def test_an_untried_arm_reads_as_a_coin_flip_not_a_zero():
    """Pessimism about the untried looks like learning and is a self-fulfilling
    prophecy: the arm is never tried again, so it never improves."""
    assert ArmStats().mean == D("0.5000")
    assert ArmStats().n == 0


def test_the_posterior_mean_never_reaches_certainty():
    assert ArmStats(successes=10).mean == D("0.9167")     # 11/12, not 1
    assert ArmStats(failures=10).mean == D("0.0833")      # 1/12, not 0


def test_observations_accumulate_per_outcome():
    s = ArmStats().plus(True).plus(True).plus(False)
    assert (s.successes, s.failures, s.n) == (2, 1, 3)
    assert s.mean == D("0.6000")
    with pytest.raises(BanditError):
        ArmStats().plus(1)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def test_exploration_is_capped_because_it_costs_a_real_inbox():
    with pytest.raises(BanditError):
        EpsilonGreedyConfig(epsilon=0.1)                  # must be Decimal
    with pytest.raises(BanditError):
        EpsilonGreedyConfig(epsilon=D("0"))
    with pytest.raises(BanditError):
        EpsilonGreedyConfig(epsilon=D("0.6"))
    with pytest.raises(BanditError):
        EpsilonGreedyConfig(salt="")
    assert DEFAULT_CONFIG.epsilon == D("0.10")


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------
def test_a_bandit_needs_actions_and_they_must_be_unique():
    with pytest.raises(BanditError):
        EpsilonGreedyBandit([])
    with pytest.raises(BanditError):
        EpsilonGreedyBandit(["upi"])
    with pytest.raises(BanditError):
        EpsilonGreedyBandit([A1, Action(Channel.UPI_RETRY, 10, "m1")])


def test_actions_are_sorted_so_behaviour_is_order_independent():
    a = EpsilonGreedyBandit([A1, A2, A3])
    b = EpsilonGreedyBandit([A3, A2, A1])
    assert a.actions == b.actions
    assert a.k == b.k == 3
    assert a.greedy("ctx") == b.greedy("ctx")


def test_the_bandit_exposes_its_config_and_table():
    b = bandit()
    assert b.config.epsilon == D("0.10")
    assert b.stats_for("ctx", A1) == ArmStats()
    assert b.table("ctx") == {A3.key: D("0.5000"), A1.key: D("0.5000"),
                              A2.key: D("0.5000")}


# --------------------------------------------------------------------------
# Propensities — the identities the evaluator depends on
# --------------------------------------------------------------------------
@pytest.mark.parametrize("k,epsilon", [(1, "0.10"), (2, "0.10"), (3, "0.10"),
                                       (3, "0.07"), (3, "0.50"), (2, "0.01")])
def test_propensities_sum_to_exactly_one(k, epsilon):
    """Not 0.9999. An importance-weighted estimate against a distribution that
    is not one is biased, and the bias grows with the weights."""
    b = bandit(actions=ACTIONS[:k], epsilon=epsilon)
    assert sum(b.propensity("ctx", a) for a in b.actions) == D("1")


def test_the_greedy_arm_gets_one_minus_epsilon_plus_its_share():
    b = bandit(epsilon="0.30")
    greedy = b.greedy("ctx")
    assert b.propensity("ctx", greedy) == D("0.8000")     # 1 - 2 * (0.3/3)
    for other in (a for a in b.actions if a != greedy):
        assert b.propensity("ctx", other) == D("0.1000")  # 0.3/3


def test_no_action_ever_has_zero_propensity():
    """A zero would make that row un-evaluable offline, which is the failure the
    evaluator refuses to work around."""
    b = bandit(epsilon="0.02")
    assert all(b.propensity("ctx", a) > 0 for a in b.actions)


def test_propensity_of_an_unknown_action_is_an_error():
    with pytest.raises(BanditError):
        bandit().propensity("ctx", Action(Channel.UPI_RETRY, 23, "unseen"))


# --------------------------------------------------------------------------
# Choosing
# --------------------------------------------------------------------------
def test_choose_validates_its_keys():
    b = bandit()
    with pytest.raises(BanditError):
        b.choose("", "u1")
    with pytest.raises(BanditError):
        b.choose("ctx", "")


def test_choices_are_deterministic():
    b = bandit()
    assert b.choose("ctx", "u1") == b.choose("ctx", "u1")
    assert bandit().choose("ctx", "u1") == b.choose("ctx", "u1")


def test_exploration_happens_at_roughly_epsilon():
    b = bandit(epsilon="0.30")
    picks = [b.choose("ctx", f"u{i}") for i in range(2000)]
    non_greedy = sum(1 for c in picks if not c.is_greedy)
    # epsilon * (k-1)/k = 0.30 * 2/3 = 0.20
    assert 0.17 < non_greedy / 2000 < 0.23


def test_an_exploration_draw_that_lands_on_the_greedy_arm_is_not_exploration():
    """The propensity already accounts for both routes to the greedy action, so
    the flag must describe the outcome, not the coin."""
    b = bandit(epsilon="0.50")
    greedy = b.greedy("ctx")
    found = next(c for i in range(500)
                 if (c := b.choose("ctx", f"u{i}")).action == greedy)
    assert found.explored is False and found.is_greedy is True
    assert found.propensity == b.propensity("ctx", greedy)


def test_every_choice_carries_the_propensity_it_was_made_with():
    b = bandit()
    for i in range(50):
        c = b.choose("ctx", f"u{i}")
        assert c.propensity == b.propensity("ctx", c.action)
        assert c.context_key == "ctx"
    assert isinstance(c, Choice)
    assert set(c.summary()) == {"context", "action", "propensity", "explored", "greedy"}


def test_different_contexts_learn_independently():
    b = bandit().observe_all([("ctx-a", A2, True)] * 10 + [("ctx-a", A1, False)] * 10)
    assert b.greedy("ctx-a") == A2
    assert b.greedy("ctx-b") == b.actions[0]        # untouched: still the cold tie


# --------------------------------------------------------------------------
# Learning
# --------------------------------------------------------------------------
def test_learning_returns_a_new_policy_rather_than_mutating():
    """A policy that changed under a caller holding a reference to it would make
    already-logged propensities wrong, which is the one thing this must not do."""
    before = bandit()
    after = before.updated("ctx", A2, True)
    assert before.stats_for("ctx", A2) == ArmStats()
    assert after.stats_for("ctx", A2) == ArmStats(successes=1)
    assert before is not after


def test_learning_moves_the_greedy_arm():
    b = bandit()
    cold = b.greedy("ctx")
    warm = b.observe_all([("ctx", A2, True)] * 20 + [("ctx", A1, False)] * 20)
    assert cold == A3                              # lowest key among cold ties
    assert warm.greedy("ctx") == A2
    assert warm.propensity("ctx", A2) > warm.propensity("ctx", A1)


def test_updating_an_unknown_action_is_an_error():
    with pytest.raises(BanditError):
        bandit().updated("ctx", Action(Channel.UPI_RETRY, 23, "unseen"), True)


def test_the_config_survives_an_update():
    b = bandit(epsilon="0.25").updated("ctx", A1, True)
    assert b.config.epsilon == D("0.25")


# --------------------------------------------------------------------------
# Target policies for offline evaluation
# --------------------------------------------------------------------------
def test_greedy_policy_is_deterministic_over_the_action_set():
    b = bandit().observe_all([("ctx", A2, True)] * 10)
    p = GreedyPolicy(b)
    assert p.probability("ctx", A2.key) == D("1")
    assert p.probability("ctx", A1.key) == D("0")


def test_fixed_action_policy_is_the_baseline_to_beat():
    p = FixedActionPolicy(A1.key)
    assert p.probability("anything", A1.key) == D("1")
    assert p.probability("anything", A2.key) == D("0")


def test_the_logging_echo_reproduces_the_bandits_own_propensities():
    """Evaluating this policy must return the log's own mean reward — the sanity
    check every IPS implementation should pass before anyone trusts it."""
    b = bandit()
    echo = LoggingPolicyEcho(b)
    for a in b.actions:
        assert echo.probability("ctx", a.key) == b.propensity("ctx", a)
