"""Runtime configuration for the reconciliation service.

The only tunable today is the cap on *contended* issues (issues appearing in
the candidates of at least two copies).  The solver allocates a bit set of
``2 ** contended`` machine states, so on a small-memory host the operator can
lower the cap via the ``MAX_CONTENDED_ISSUES`` environment variable.  The
default keeps the historical behaviour (the manifest maximum of 24).
"""

from __future__ import annotations

import os

from .solver import DEFAULT_MAX_CONTENDED, MAX_ISSUES

ENV_VAR = "MAX_CONTENDED_ISSUES"


class ConfigError(RuntimeError):
    """The environment provides an unusable configuration value."""


def max_contended_issues() -> int:
    """Return the configured cap on contended issues (default 24)."""
    raw = os.environ.get(ENV_VAR)
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_CONTENDED
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            f"{ENV_VAR} must be an integer between 0 and {MAX_ISSUES}, got {raw!r}"
        ) from exc
    if not 0 <= value <= MAX_ISSUES:
        raise ConfigError(
            f"{ENV_VAR} must be an integer between 0 and {MAX_ISSUES}, got {value}"
        )
    return value
