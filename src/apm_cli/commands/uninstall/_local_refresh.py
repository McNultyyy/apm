"""Local-refresh helpers extracted from engine.py (800-line guardrail, issue #1078).

Contains helpers for staging and activating shared local install-slot
refreshes during uninstall.  These were in engine.py until the strangler-fig
refactor split that file to keep it under the 800-line limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ...core.command_logger import CommandLogger
from ...models.apm_package import DependencyReference
from ...models.dependency.selection import parse_dependency_entry as _parse_dependency_entry
from ...utils.path_security import PathTraversalError, ensure_path_within, safe_rmtree


def _is_marketplace_ref(package: str) -> bool:
    """Check if *package* is marketplace notation using the public API."""
    from ...marketplace.resolver import parse_marketplace_ref

    return parse_marketplace_ref(package) is not None


def _build_children_index(lockfile):
    """Build parent_url -> [child_deps] index in a single O(n) pass.

    Returns a dict mapping each ``resolved_by`` URL to the list of
    dependency objects that claim it as their parent.
    """
    children = {}
    for dep in lockfile.get_package_dependencies():
        parent = dep.resolved_by
        if parent:
            if parent not in children:
                children[parent] = []
            children[parent].append(dep)
    return children


def _dependency_public_label(dep_entry: object) -> str:
    """Return a path-safe dependency label for user-facing uninstall output."""
    try:
        dependency = _parse_dependency_entry(dep_entry)
    except (ValueError, TypeError, AttributeError, KeyError):
        return dep_entry if isinstance(dep_entry, str) else str(dep_entry)
    if dependency.is_local:
        return dependency.repo_url
    return dependency.get_identity()


def _is_private_local_identity(value: str) -> bool:
    """Return whether a diagnostic identity can expose a declared local path."""
    return value.startswith(("local:", "_local/")) or DependencyReference.is_local_path(value)


def _reintegration_error_detail(
    dependency: DependencyReference,
    error: Exception,
) -> str:
    """Return verbose failure detail without exposing a local declaration."""
    error_type = type(error).__name__
    if dependency.is_local:
        return error_type
    return f"{error_type}: {error}"


def _surviving_local_refs_at_install_path(
    package: object,
    surviving_dependencies: list[object],
    apm_modules_dir: Path,
) -> list[DependencyReference]:
    """Return surviving local declarations that share a package install slot."""
    try:
        removed_ref = _parse_dependency_entry(package)
        removed_path = removed_ref.get_install_path(apm_modules_dir)
    except (ValueError, TypeError, AttributeError, KeyError, PathTraversalError):
        return []

    survivors: list[DependencyReference] = []
    for entry in surviving_dependencies:
        try:
            survivor = _parse_dependency_entry(entry)
            if survivor.is_local and survivor.get_install_path(apm_modules_dir) == removed_path:
                survivors.append(survivor)
        except (ValueError, TypeError, AttributeError, KeyError, PathTraversalError):
            continue
    return survivors


class _LocalRefreshLogger:
    """Redact declared source paths from local re-materialization failures."""

    def __init__(self, logger: CommandLogger, package_label: str) -> None:
        self._logger = logger
        self._package_label = package_label
        self.failed = False

    def error(self, _message: str) -> None:
        """Report a safe, actionable local refresh failure."""
        self.failed = True
        self._logger.error(
            f"Failed to refresh {self._package_label} from its declared local path. "
            "Check the exact path in apm.yml, run 'apm install', and retry."
        )


class LocalSurvivorRefreshError(RuntimeError):
    """Raised when a shared local install slot cannot preserve its survivors."""


@dataclass(frozen=True)
class LocalSlotRefresh:
    """Validated staged content for one shared local materialization slot."""

    install_path: Path
    staged_path: Path
    survivor_keys: tuple[str, ...]


_LOCAL_REFRESH_BACKUP_SUFFIX = ".apm-uninstall-backup"


def _cleanup_staged_local_refreshes(
    refreshes: dict[Path, LocalSlotRefresh],
    apm_modules_dir: Path,
) -> None:
    """Best-effort cleanup for hidden local refresh staging directories."""
    for refresh in refreshes.values():
        if not refresh.staged_path.exists():
            continue
        try:
            safe_rmtree(refresh.staged_path, apm_modules_dir)
        except OSError:
            continue


def _recover_local_refresh_backups(
    apm_modules_dir: Path,
    logger: CommandLogger,
) -> None:
    """Recover or remove deterministic backups left by interrupted activation."""
    local_root = apm_modules_dir / "_local"
    if not local_root.is_dir():
        return
    for backup_path in local_root.glob(f".*{_LOCAL_REFRESH_BACKUP_SUFFIX}"):
        target_name = backup_path.name[1 : -len(_LOCAL_REFRESH_BACKUP_SUFFIX)]
        if not target_name:
            continue
        install_path = local_root / target_name
        ensure_path_within(backup_path, apm_modules_dir)
        ensure_path_within(install_path, apm_modules_dir)
        if install_path.exists():
            safe_rmtree(backup_path, apm_modules_dir)
            continue
        backup_path.rename(install_path)
        logger.progress(f"Recovered interrupted local materialization for _local/{target_name}")


def _stage_shared_local_survivors(
    packages_to_remove: list[object],
    surviving_dependencies: list[object],
    apm_modules_dir: Path,
    project_root: Path,
    logger: CommandLogger,
) -> dict[Path, LocalSlotRefresh]:
    """Validate and stage every surviving shared-slot local package."""
    from ...install.phases.local_content import _copy_local_package

    _recover_local_refresh_backups(apm_modules_dir, logger)
    refreshes: dict[Path, LocalSlotRefresh] = {}
    for package in packages_to_remove:
        try:
            install_path = _parse_dependency_entry(package).get_install_path(apm_modules_dir)
        except (ValueError, TypeError, AttributeError, KeyError, PathTraversalError):
            continue
        if install_path in refreshes:
            continue
        survivors = _surviving_local_refs_at_install_path(
            package,
            surviving_dependencies,
            apm_modules_dir,
        )
        if not survivors:
            continue
        if len(survivors) > 1:
            logger.error(
                f"Cannot preserve {_dependency_public_label(package)} because "
                f"{len(survivors)} declarations would still share one install slot. "
                "Use exact paths from apm.yml in one uninstall command so at most "
                "one declaration remains."
            )
            _cleanup_staged_local_refreshes(refreshes, apm_modules_dir)
            raise LocalSurvivorRefreshError(
                "Multiple local declarations would remain in one shared install slot. "
                "No manifest changes were made."
            )

        staged_path = apm_modules_dir / "_local" / f".apm-uninstall-refresh-{uuid4().hex}"
        refresh_logger = _LocalRefreshLogger(
            logger,
            _dependency_public_label(package),
        )
        try:
            for survivor in survivors:
                staged = _copy_local_package(
                    survivor,
                    staged_path,
                    project_root,
                    project_root=project_root,
                    logger=refresh_logger,
                )
                if staged is None:
                    raise LocalSurvivorRefreshError(
                        "A surviving local dependency could not be staged. "
                        "No manifest changes were made."
                    )
            from ...models.validation import validate_apm_package

            validation = validate_apm_package(staged_path)
            if not validation.is_valid or validation.package is None:
                refresh_logger.error("staged local package validation failed")
                raise LocalSurvivorRefreshError(
                    "A surviving local dependency could not be staged. "
                    "No manifest changes were made."
                )
        except Exception as exc:
            if staged_path.exists():
                safe_rmtree(staged_path, apm_modules_dir)
            _cleanup_staged_local_refreshes(refreshes, apm_modules_dir)
            if isinstance(exc, LocalSurvivorRefreshError):
                raise
            logger.verbose_detail(f"    Local refresh staging error type: {type(exc).__name__}")
            refresh_logger.error("local refresh staging failed")
            raise LocalSurvivorRefreshError(
                "A surviving local dependency could not be staged. No manifest changes were made."
            ) from None

        refreshes[install_path] = LocalSlotRefresh(
            install_path=install_path,
            staged_path=staged_path,
            survivor_keys=tuple(survivor.get_unique_key() for survivor in survivors),
        )
    return refreshes


def _activate_staged_local_refresh(
    refresh: LocalSlotRefresh,
    apm_modules_dir: Path,
) -> None:
    """Replace one materialization with staged survivor content or restore it."""
    install_path = refresh.install_path
    ensure_path_within(install_path, apm_modules_dir)
    ensure_path_within(refresh.staged_path, apm_modules_dir)
    backup_path = install_path.parent / (f".{install_path.name}{_LOCAL_REFRESH_BACKUP_SUFFIX}")
    ensure_path_within(backup_path, apm_modules_dir)
    if backup_path.exists():
        raise LocalSurvivorRefreshError(
            "A stale local refresh backup must be recovered before activation."
        )
    if install_path.exists():
        install_path.rename(backup_path)
    try:
        refresh.staged_path.rename(install_path)
    except OSError as exc:
        if backup_path.exists() and not install_path.exists():
            backup_path.rename(install_path)
        raise LocalSurvivorRefreshError(
            "A staged local dependency could not replace its shared install slot."
        ) from exc
    if backup_path.exists():
        safe_rmtree(backup_path, apm_modules_dir)
