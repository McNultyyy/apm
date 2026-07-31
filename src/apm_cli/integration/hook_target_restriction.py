"""Merged-hook contraction when a package's target set narrows (#2362).

Split out of ``hook_integrator`` so the facade stays inside the module
line budget (#1078). ``HookIntegrator.reconcile_package_target_restriction``
delegates here; the integrator instance is passed in so the shared marker
helpers keep their single owner.
"""

import json
from pathlib import Path

from apm_cli.integration.hook_ownership import (
    extract_apm_source_sidecar as _extract_apm_source_sidecar,
)
from apm_cli.integration.hook_transforms import (
    _APM_HOOKS_SIDECAR,
    _MERGE_HOOK_TARGETS,
    _reinject_apm_source_from_sidecar,
)
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.paths import portable_relpath

from .hook_reconcile_types import HookTargetReconcileStats


def reconcile_package_target_restriction(
    integrator,
    package_info,
    project_root: Path,
    excluded_targets,
) -> HookTargetReconcileStats:
    """Remove this package's merged hooks from newly excluded targets."""
    stats: HookTargetReconcileStats = {
        "files_removed": 0,
        "errors": 0,
        "failed_targets": [],
        "failed_paths": [],
    }
    package_name = integrator._get_package_name(package_info, project_root)
    source_marker, legacy_source_markers = integrator._get_hook_source_markers(
        package_info,
        project_root,
        package_name,
    )
    source_markers = frozenset({source_marker, *legacy_source_markers})
    for target in excluded_targets:
        config = _MERGE_HOOK_TARGETS.get(target.name)
        if config is None:
            continue
        target_dir = project_root / target.root_dir
        json_path = target_dir / config.config_filename
        errors_before = stats["errors"]
        _clean_apm_source_from_json(
            json_path,
            source_markers,
            stats,
            container=config.event_container_key,
            sidecar_path=json_path.parent / _APM_HOOKS_SIDECAR,
        )
        if stats["errors"] != errors_before:
            stats["failed_targets"].append(target.name)
            stats["failed_paths"].append(portable_relpath(json_path, project_root))
    return stats


def _clean_apm_source_from_json(
    json_path: Path,
    source_markers: frozenset[str],
    stats: HookTargetReconcileStats,
    *,
    container: str,
    sidecar_path: Path,
) -> None:
    """Remove one package owner while preserving user and sibling entries."""
    if not json_path.exists() and not sidecar_path.exists():
        return
    try:
        data: dict = {}
        if json_path.exists():
            with open(json_path, encoding="utf-8") as handle:
                raw_data = json.load(handle)
            if not isinstance(raw_data, dict):
                raise TypeError("native hook config must contain an object")
            data = raw_data

        sidecar_data: dict = {}
        if sidecar_path.exists():
            with open(sidecar_path, encoding="utf-8") as handle:
                raw_sidecar = json.load(handle)
            if not isinstance(raw_sidecar, dict):
                raise TypeError("hook sidecar must contain an object")
            sidecar_data = raw_sidecar

        hooks = data.get(container)
        native_modified = False
        if isinstance(hooks, dict):
            if sidecar_data:
                _reinject_apm_source_from_sidecar(hooks, sidecar_data)
            for event_name in list(hooks):
                entries = hooks[event_name]
                if not isinstance(entries, list):
                    continue
                filtered = [
                    entry
                    for entry in entries
                    if not (isinstance(entry, dict) and entry.get("_apm_source") in source_markers)
                ]
                if len(filtered) == len(entries):
                    continue
                native_modified = True
                if filtered:
                    hooks[event_name] = filtered
                else:
                    del hooks[event_name]
            if not hooks:
                data.pop(container, None)

        sidecar_had_source = any(
            isinstance(entry, dict) and entry.get("_apm_source") in source_markers
            for entries in sidecar_data.values()
            if isinstance(entries, list)
            for entry in entries
        )
        if isinstance(hooks, dict):
            sidecar_out = _extract_apm_source_sidecar(hooks)
        else:
            sidecar_out = {
                event_name: [
                    entry
                    for entry in entries
                    if not (isinstance(entry, dict) and entry.get("_apm_source") in source_markers)
                ]
                for event_name, entries in sidecar_data.items()
                if isinstance(entries, list)
            }
            sidecar_out = {
                event_name: entries for event_name, entries in sidecar_out.items() if entries
            }

        if native_modified:
            atomic_write_text(json_path, json.dumps(data, indent=2) + "\n")
            stats["files_removed"] += 1
        if native_modified or sidecar_had_source:
            if sidecar_out:
                atomic_write_text(
                    sidecar_path,
                    json.dumps(sidecar_out, indent=2) + "\n",
                )
            elif sidecar_path.exists():
                sidecar_path.unlink()
            stats["files_removed"] += 1
    except (json.JSONDecodeError, OSError, TypeError):
        stats["errors"] += 1
