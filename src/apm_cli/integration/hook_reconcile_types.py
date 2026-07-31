"""Shared typed results for merged-hook reconciliation (#2362).

Kept in its own module so both ``hook_integrator`` (the facade) and
``hook_target_restriction`` (the contraction implementation) can name the
same type without importing each other (#1078).
"""

from typing import TypedDict


class HookTargetReconcileStats(TypedDict):
    """Counts and locations produced by package-target hook contraction."""

    files_removed: int
    errors: int
    failed_targets: list[str]
    failed_paths: list[str]
