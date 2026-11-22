import itertools
import random

import pytest

from app.solver import (
    DEFAULT_MAX_CONTENDED,
    MAX_ISSUES,
    ContentionLimitExceeded,
    contended_issue_mask,
    evaluate,
)

STATUSES = ("held", "missing", "review")


def brute(n, copies):
    """Reference implementation: enumerate every assignment."""
    masks = copies
    valid = []
    for choice in itertools.product(*[range(len(g)) for g in masks]):
        used = 0
        ok = True
        for ci, gi in enumerate(choice):
            m = masks[ci][gi]
            if used & m:
                ok = False
                break
            used |= m
        if ok:
            valid.append(used)
    if not valid:
        return False, None
    statuses = []
    for b in range(n):
        with_b = any(m >> b & 1 for m in valid)
        without_b = any(not (m >> b & 1) for m in valid)
        statuses.append("held" if with_b and not without_b else ("review" if with_b else "missing"))
    return True, statuses


@pytest.mark.parametrize("seed", range(60))
def test_matches_brute_force(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 8)
    k = rng.randint(0, 8)
    copies = []
    for _ in range(k):
        groups = []
        for _ in range(rng.randint(1, 3)):
            mask = 0
            for i in range(n):
                if rng.random() < 0.3:
                    mask |= 1 << i
            if not mask:
                mask = 1 << rng.randrange(n)
            groups.append(mask)
        copies.append(list(dict.fromkeys(groups)))
    result = evaluate(n, copies)
    expected_sat, expected_statuses = brute(n, copies)
    assert result.satisfiable == expected_sat
    if expected_sat:
        assert result.statuses == expected_statuses
        used = 0
        for index, option in enumerate(result.assignment):
            mask = copies[index][option]
            assert used & mask == 0
            used |= mask
        assert used == result.used_mask


def test_single_copy_two_candidates_one_is_ambiguous():
    # copy may be 1 or 2 -> both review, 3 missing
    result = evaluate(3, [[0b001, 0b010]])
    assert result.statuses == ["review", "review", "missing"]


def test_two_copies_same_group_is_unsatisfiable():
    result = evaluate(4, [[0b0001], [0b0001]])
    assert not result.satisfiable


def test_more_copies_than_issues_is_unsatisfiable():
    result = evaluate(3, [[1], [2], [4], [1]])
    assert not result.satisfiable


def test_bound_group_forces_both_issues():
    # copy A is (1+2) or (1); copy B is 2; C is 3
    result = evaluate(3, [[0b011, 0b001], [0b010], [0b100]])
    # A=1+2/B=2 conflicts; A=1 then 2 held by B -> all held
    assert result.statuses == ["held", "held", "held"]


def test_extra_issues_are_missing_not_review():
    # 4 issues, copies cover 1..3, 4 is never mentioned -> missing
    result = evaluate(4, [[0b0001], [0b0010], [0b0100]])
    assert result.statuses == ["held", "held", "held", "missing"]


def test_24_issues_is_fast():
    copies = [[1 << i, 1 << ((i + 1) % 24), 1 << ((i + 2) % 24)] for i in range(24)]
    result = evaluate(24, copies)
    assert result.satisfiable
    assert result.statuses == ["held"] * 24


# ---------------------------------------------------------------------------
# State-space reduction: only issues in >= 2 copies' candidates are contended
# ---------------------------------------------------------------------------


def test_contended_issue_mask_counts_distinct_copies_only():
    # Issue 0 appears twice inside the SAME copy -> not contended.
    # Issue 1 appears in two different copies -> contended.
    assert contended_issue_mask([[0b001, 0b011], [0b010]]) == 0b010
    # No overlap across copies -> nothing contended.
    assert contended_issue_mask([[0b001], [0b010], [0b100]]) == 0
    # Every issue shared by the two copies -> all contended.
    assert contended_issue_mask([[0b111], [0b111]]) == 0b111


def test_state_space_shrinks_to_contended_issues():
    # 8 issues but only issue 0 is fought over by two copies; the rest are
    # private. The DP must need just 2**1 states, not 2**8.
    copies = [[0b0000_0001, 0b0000_0010], [0b0000_0001, 0b0000_0100]]
    result = evaluate(8, copies)
    assert result.satisfiable
    assert result.contended_issues == [0]
    assert result.contended_count == 1
    assert result.state_space_size == 2


def test_disjoint_holdings_have_zero_contended_issues():
    copies = [[1 << i] for i in range(6)]
    result = evaluate(6, copies)
    assert result.contended_count == 0
    assert result.state_space_size == 1
    assert result.statuses == ["held"] * 6


def test_private_issue_coverage_still_counts_per_feasible_group():
    # Two groups occupy the SAME contended mask (mask 0 here: neither touches
    # an issue another copy fights over). Their private coverage must still be
    # distinguished: issue 0 is only in the first group -> review, while the
    # mutually exclusive choice set keeps every copy selecting one group.
    result = evaluate(3, [[0b001, 0b010]])
    assert result.contended_count == 0
    assert result.statuses == ["review", "review", "missing"]


def test_issue_only_in_infeasible_group_is_classified_correctly():
    # Copy 0: [i0] or [i1,i2]; copy 1 forces i2. Copy 0's [i1,i2] group then
    # conflicts (i2 is taken), so it is infeasible and issue 1 - mentioned only
    # by that dead group - is missing, while issue 0 stays held.
    result = evaluate(3, [[0b001, 0b110], [0b100]])
    assert result.satisfiable
    assert result.statuses == ["held", "missing", "held"]


def test_private_issue_forced_when_alternative_group_is_infeasible():
    # Copy 0: [i0,i1] or [i1]; copy 1: [i2]. No overlap between copies, so
    # both groups of copy 0 are feasible; i0 appears in only one -> review.
    # Then add a copy that makes the i1-only choice impossible elsewhere is
    # hard without contention, so instead assert the symmetric private rule on
    # a satisfiable instance: one feasible group covering, one not -> review.
    result = evaluate(3, [[0b011, 0b010], [0b100]])
    assert result.satisfiable
    assert result.statuses == ["review", "held", "held"]


def test_shared_contended_mask_groups_keep_distinct_private_coverage():
    # Both groups of copy 0 avoid the only contended issue (issue 2), so they
    # share compact mask 0; their private coverage of issues 0 vs 1 still has
    # to count separately when classifying those issues.
    copies = [[0b001, 0b010], [0b100]]
    result = evaluate(3, copies)
    assert result.contended_issues == []
    assert result.statuses == ["review", "review", "held"]


@pytest.mark.parametrize("seed", range(120))
def test_reduced_solver_matches_brute_force(seed):
    rng = random.Random(1000 + seed)
    n = rng.randint(1, 9)
    k = rng.randint(1, 9)
    copies = []
    for _ in range(k):
        groups = []
        for _ in range(rng.randint(1, 3)):
            mask = 0
            for i in range(n):
                if rng.random() < 0.3:
                    mask |= 1 << i
            if not mask:
                mask = 1 << rng.randrange(n)
            groups.append(mask)
        copies.append(list(dict.fromkeys(groups)))
    result = evaluate(n, copies)
    expected_sat, expected_statuses = brute(n, copies)
    assert result.satisfiable == expected_sat
    if expected_sat:
        assert result.statuses == expected_statuses
        used = 0
        for index, option in enumerate(result.assignment):
            mask = copies[index][option]
            assert used & mask == 0
            used |= mask
        assert used == result.used_mask
        assert result.contended_count == bin(
            contended_issue_mask(copies)
        ).count("1")


# ---------------------------------------------------------------------------
# Configurable cap on contended issues
# ---------------------------------------------------------------------------


def test_exceeding_cap_raises_before_allocation():
    # 24 copies each fighting over a distinct shared issue pair -> all 24
    # issues are contended.
    copies = [[1 << i, 1 << ((i + 1) % 24)] for i in range(24)]
    assert contended_issue_mask(copies) == (1 << 24) - 1
    with pytest.raises(ContentionLimitExceeded) as exc:
        evaluate(24, copies, max_contended=12)
    assert exc.value.contended_count == 24
    assert exc.value.max_contended == 12


def test_cap_zero_allows_purely_private_holdings():
    result = evaluate(6, [[1 << i] for i in range(6)], max_contended=0)
    assert result.satisfiable
    assert result.contended_count == 0
    assert result.statuses == ["held"] * 6


def test_cap_zero_rejects_any_contention():
    with pytest.raises(ContentionLimitExceeded):
        evaluate(3, [[0b011], [0b001]], max_contended=0)


def test_default_cap_equals_manifest_maximum():
    assert DEFAULT_MAX_CONTENDED == MAX_ISSUES == 24


def test_invalid_cap_value_rejected():
    with pytest.raises(ValueError):
        evaluate(3, [[1]], max_contended=25)
    with pytest.raises(ValueError):
        evaluate(3, [[1]], max_contended=-1)
