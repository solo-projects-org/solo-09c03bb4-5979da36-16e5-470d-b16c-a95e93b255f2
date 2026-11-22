"""Exact reconciliation for bound-periodical candidate groups.

The problem
-----------
A volume expects ``n`` issues (``n <= 24``).  Every physical copy has between
one and three *candidate groups* of issues.  A group models a combined / bound
volume ("issues 3+4 together").  A valid reconciliation assigns exactly one
candidate group to every copy such that no issue is claimed by two copies
("同一期最多归一册").

Definitions used by the API
---------------------------
For an issue ``b``, look at the set of *all* valid assignments:

* every assignment covers ``b``        -> ``held``   (有藏)
* no assignment covers ``b``            -> ``missing`` (缺刊)
* some cover it, some do not            -> ``review``  (留待翻册)

State-space reduction: only contended issues get mutual-exclusion states
-------------------------------------------------------------------------
An issue is **contended** (争用期次) when it appears in the candidate groups
of at least two different copies.  An issue mentioned by at most one copy can
never be the subject of a two-copy conflict, so it needs no DP state.

Only the ``c`` contended issues carry mutual-exclusion state.  Each candidate
group is projected onto a compact ``c``-bit *contended mask*; the reachability
DP then runs over at most ``2**c`` states instead of ``2**n``:

    next = OR over groups g of (reachable & free(g)) << g

The mutual-exclusion constraint only ever involves contended issues, so this
compact DP decides satisfiability exactly.

Per-issue classification (why a second, reduced DP is needed)
--------------------------------------------------------------
Whether some valid assignment *omits* an issue cannot be read from the
feasibility of individual groups: the choices of different copies are coupled
(a feasible omitting group of one copy may only co-occur with particular
choices of the others).  Instead, for each issue ``b`` we answer two exact
satisfiability questions with the same compact DP:

* ``can_cover(b)``   - is there a valid assignment in which some copy is
  assigned a group containing ``b``?  Tested by designating each copy that
  mentions ``b`` as the cover owner and restricting it to its covering groups.
* ``can_omit(b)``    - is there a valid assignment in which every copy picks a
  group *not* containing ``b``?  Tested by dropping every group that contains
  ``b`` and re-running the DP.

Then ``held`` = cover and not omit, ``missing`` = omit and not cover (the
issue is never mentioned by a usable group), ``review`` = both.  Every
reduced problem uses the same contended-mask projection and the same cap, so
its state space never exceeds ``2**max_contended`` either.

``c`` is bounded by ``max_contended`` (configurable, default
``DEFAULT_MAX_CONTENDED = 24``).  Exceeding it raises
:class:`ContentionLimitExceeded` *before* any DP allocation, so a small-memory
host never touches a ``2**c`` bitset it cannot afford.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_ISSUES = 24
MAX_CANDIDATES_PER_COPY = 3
DEFAULT_MAX_CONTENDED = 24

HELD = "held"
MISSING = "missing"
REVIEW = "review"


class ContentionLimitExceeded(Exception):
    """Raised when the number of contended issues exceeds the configured cap.

    The host is a small-memory machine: instead of allocating the
    ``2**contended`` bit set we refuse the computation with a resource error.
    """

    def __init__(self, contended_count: int, max_contended: int):
        self.contended_count = contended_count
        self.max_contended = max_contended
        super().__init__(
            f"contended issue count {contended_count} exceeds the configured "
            f"limit {max_contended}"
        )


@dataclass(slots=True)
class Evaluation:
    """Result of reconciling one volume."""

    satisfiable: bool
    statuses: list[str] = field(default_factory=list)
    assignment: list[int] = field(default_factory=list)
    used_mask: int = 0
    reason: str | None = None
    #: Indices (into the volume manifest) of issues contended by >= 2 copies.
    contended_issues: list[int] = field(default_factory=list)
    #: Number of issues that needed mutual-exclusion DP state.
    contended_count: int = 0
    #: Configured cap that was applied to this evaluation.
    max_contended: int = DEFAULT_MAX_CONTENDED

    @property
    def state_space_size(self) -> int:
        """Number of mutual-exclusion DP states (``2 ** contended_count``)."""
        return 1 << self.contended_count

    def count(self, status: str) -> int:
        return sum(1 for s in self.statuses if s == status)


def contended_issue_mask(candidates: list[list[int]]) -> int:
    """Bit mask of issues appearing in the candidates of at least two copies."""
    seen = 0
    contended = 0
    for groups in candidates:
        copy_mask = 0
        for mask in groups:
            copy_mask |= mask
        # An issue already seen in an earlier copy and seen again here is
        # contended; the local copy_mask dedupes repeated mentions per copy.
        contended |= seen & copy_mask
        seen |= copy_mask
    return contended


def _free_pattern(issue: int, n: int) -> int:
    """Return a mask whose bit ``s`` is set iff state ``s`` does not use ``issue``."""
    m = 1 << n
    nbytes = (m + 7) // 8
    if issue == 0:
        data = b"\x55" * nbytes
    elif issue == 1:
        data = b"\x33" * nbytes
    elif issue == 2:
        data = b"\x0f" * nbytes
    else:
        # Blocks of 2**issue states: the first half has the bit clear, the
        # second half has it set.
        half = 1 << (issue - 3)
        data = (b"\xff" * half + b"\x00" * half) * (nbytes // (2 * half))
    return int.from_bytes(data, "little")


def _project(mask: int, contended_full: int, compact_of_issue: dict[int, int]) -> int:
    """Project a full-volume issue mask onto the compact contended-bit mask."""
    compact = 0
    remaining = mask & contended_full
    while remaining:
        low = remaining & -remaining
        compact |= 1 << compact_of_issue[low.bit_length() - 1]
        remaining ^= low
    return compact


def _compact_satisfiable(
    translated: list[list[int]], c: int, free_patterns: list[int]
) -> bool:
    """Reachability DP over the compact contended masks.

    ``translated[j]`` is the list of compact candidate masks of copy ``j`` and
    ``free_patterns[bit]`` is the state bit set in which compact ``bit`` is
    free.  Every copy must choose exactly one group; groups stay disjoint on
    the contended bits, which is exactly the mutual-exclusion constraint.
    """
    universe = (1 << (1 << c)) - 1
    reachable = 1
    for compact_groups in translated:
        widened = 0
        for compact_mask in compact_groups:
            allowed = universe
            remaining = compact_mask
            while remaining:
                low = remaining & -remaining
                allowed &= free_patterns[low.bit_length() - 1]
                remaining ^= low
            widened |= (reachable & allowed) << compact_mask
        reachable = widened
        if not reachable:
            return False
    return True


def evaluate(
    n: int,
    candidates: list[list[int]],
    *,
    want_assignment: bool = True,
    max_contended: int = DEFAULT_MAX_CONTENDED,
) -> Evaluation:
    """Reconcile ``n`` issues against per-copy candidate issue masks.

    ``candidates[i]`` is the list of candidate groups of copy ``i``; every
    group is a bitmask over issue indices.  No copy may be omitted and no
    issue may be used twice.

    Only issues contended by at least two copies receive mutual-exclusion DP
    states.  Raises :class:`ContentionLimitExceeded` when their number exceeds
    ``max_contended``.
    """
    if n < 0 or n > MAX_ISSUES:
        raise ValueError(f"issue count must be between 0 and {MAX_ISSUES}")
    if max_contended < 0 or max_contended > MAX_ISSUES:
        raise ValueError(
            f"max_contended must be between 0 and {MAX_ISSUES}, got {max_contended}"
        )
    k = len(candidates)
    if n == 0:
        if k == 0:
            return Evaluation(True, max_contended=max_contended)
        return Evaluation(
            False,
            reason="no issues but copies were supplied",
            max_contended=max_contended,
        )
    if k == 0:
        return Evaluation(True, [MISSING] * n, [], 0, max_contended=max_contended)
    for groups in candidates:
        if not groups:
            raise ValueError("every copy needs at least one candidate group")
        if len(groups) > MAX_CANDIDATES_PER_COPY:
            raise ValueError("a copy may have at most three candidate groups")
        for mask in groups:
            if mask <= 0 or mask >= (1 << n):
                raise ValueError("candidate group contains an issue outside the manifest")

    contended_full = contended_issue_mask(candidates)
    contended_indices = [
        index for index in range(n) if (contended_full >> index) & 1
    ]
    c = len(contended_indices)
    if c > max_contended:
        # Refuse before allocating any 2**c bit set.
        raise ContentionLimitExceeded(c, max_contended)

    compact_of_issue = {
        full_index: compact_index
        for compact_index, full_index in enumerate(contended_indices)
    }
    free_patterns = [_free_pattern(bit, c) for bit in range(c)]

    # Compact candidate masks per copy, in the original group order.
    translated: list[list[int]] = [
        [_project(mask, contended_full, compact_of_issue) for mask in groups]
        for groups in candidates
    ]

    # More copies than issues can never be disjoint (groups are non-empty).
    if k > n:
        return Evaluation(
            False,
            reason=f"{k} copies cannot fit into {n} issues without sharing an issue",
            contended_issues=contended_indices,
            contended_count=c,
            max_contended=max_contended,
        )

    if not _compact_satisfiable(translated, c, free_patterns):
        return Evaluation(
            False,
            reason="no assignment of candidate groups is disjoint",
            contended_issues=contended_indices,
            contended_count=c,
            max_contended=max_contended,
        )

    # Per-issue exact classification with reduced compact DPs.  Both questions
    # are satisfiability tests over the same (capped) compact state space.
    statuses: list[str] = []
    for issue in range(n):
        bit = 1 << issue

        # can_omit: every copy must pick a group that does not contain issue.
        omit_translation: list[list[int]] | None = []
        for full_groups, compact_groups in zip(candidates, translated, strict=True):
            kept = [
                compact_mask
                for full_mask, compact_mask in zip(full_groups, compact_groups, strict=True)
                if not (full_mask & bit)
            ]
            if not kept:
                omit_translation = None
                break
            omit_translation.append(kept)
        can_omit = (
            omit_translation is not None
            and _compact_satisfiable(omit_translation, c, free_patterns)
        )

        # can_cover: some copy is assigned a covering group.  Try every copy
        # that mentions the issue as the designated cover owner, restricting
        # that copy to its covering groups while the others keep all theirs.
        can_cover = False
        for owner in range(k):
            owner_groups = [
                compact_mask
                for full_mask, compact_mask in zip(
                    candidates[owner], translated[owner], strict=True
                )
                if full_mask & bit
            ]
            if not owner_groups:
                continue
            restricted = [
                owner_groups if copy_index == owner else compact_groups
                for copy_index, compact_groups in enumerate(translated)
            ]
            if _compact_satisfiable(restricted, c, free_patterns):
                can_cover = True
                break

        if not can_cover:
            statuses.append(MISSING)
        elif not can_omit:
            statuses.append(HELD)
        else:
            statuses.append(REVIEW)

    assignment: list[int] = []
    used_mask = 0
    if want_assignment:
        assignment, used_mask = _reconstruct(
            candidates, translated, c, free_patterns
        )

    return Evaluation(
        True,
        statuses,
        assignment,
        used_mask,
        contended_issues=contended_indices,
        contended_count=c,
        max_contended=max_contended,
    )


def _reconstruct(
    candidates: list[list[int]],
    translated: list[list[int]],
    c: int,
    free_patterns: list[int],
) -> tuple[list[int], int]:
    """Rebuild one valid group choice per copy via layered DP backtracking."""
    universe = (1 << (1 << c)) - 1
    layers: list[int] = [1]
    reachable = 1
    for compact_groups in translated:
        widened = 0
        for compact_mask in compact_groups:
            allowed = universe
            remaining = compact_mask
            while remaining:
                low = remaining & -remaining
                allowed &= free_patterns[low.bit_length() - 1]
                remaining ^= low
            widened |= (reachable & allowed) << compact_mask
        reachable = widened
        layers.append(reachable)

    k = len(candidates)
    state = (reachable & -reachable).bit_length() - 1
    choices = [0] * k
    for index in range(k - 1, -1, -1):
        for option_index, compact_mask in enumerate(translated[index]):
            if state & compact_mask != compact_mask:
                continue
            previous = state ^ compact_mask
            if (layers[index] >> previous) & 1:
                choices[index] = option_index
                state = previous
                break
        else:  # pragma: no cover - defensive
            raise RuntimeError("failed to reconstruct a valid assignment")
    used_mask = 0
    for index, option_index in enumerate(choices):
        used_mask |= candidates[index][option_index]
    return choices, used_mask
