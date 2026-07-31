"""Merged-hook integration flow for JSON-config targets.

Split out of ``hook_integrator`` so that facade stays inside the module
line budget (#1078). ``HookIntegrator._integrate_merged_hooks`` delegates
here and passes itself, so every helper keeps its single canonical owner
on the integrator class.
"""

import logging
from pathlib import Path

from apm_cli.integration.hook_bundle import copy_deployed_hook_bundle
from apm_cli.integration.hook_file_routing import filter_hook_files_for_target
from apm_cli.integration.hook_transforms import (
    _APM_HOOKS_SIDECAR,
    _HOOK_EVENT_MAP,
    _emit_hook_event_diagnostics,
    _MergeHookConfig,
    _rewrite_hooks_data,
)

from . import hook_integrator as _hi
from .hook_integrator import HookIntegrationResult
from .hook_merge import (
    _load_merged_config_and_sidecar,
    _merge_hook_file_entries,
    _warn_empty_hook_file,
    _write_merged_config,
)

_log = logging.getLogger(__name__)
_filter_hook_files_for_target = filter_hook_files_for_target


def integrate_merged_hooks(
    integrator,
    config: "_MergeHookConfig",
    package_info,
    project_root: Path,
    *,
    force: bool = False,
    managed_files: set = None,  # noqa: RUF013
    diagnostics=None,
    target=None,
    user_scope: bool = False,
) -> HookIntegrationResult:
    _empty = HookIntegrationResult(
        files_integrated=0,
        files_updated=0,
        files_skipped=0,
        target_paths=[],
    )

    root_dir = target.root_dir if target else f".{config.target_key}"
    target_dir = project_root / root_dir

    # Opt-in check: some targets only deploy when their dir exists
    if config.require_dir and not target_dir.exists():
        return _empty

    # Absolutize hook commands only for user-scope deploys.
    _deploy_root_for_rewrite = integrator._deploy_root_for_hook_rewrite(project_root, user_scope)

    hook_files = integrator.find_hook_files(package_info.install_path)
    package_name = integrator._get_package_name(package_info, project_root)
    hook_files = _filter_hook_files_for_target(
        hook_files,
        config.target_key,
        package_name=package_name,
        warned_packages=integrator._deprecated_hook_routing_warnings,
        package_identity=package_info.get_canonical_dependency_string(),
    )
    if not hook_files:
        return _empty

    heal_stale_root_source = integrator._is_root_local_package(package_info, project_root)
    # RULE B: reach the seam through the facade module at call time so tests
    # patching ``hook_integrator.dependency_hook_sources`` still observe it.
    dependency_sources = (
        _hi.dependency_hook_sources(project_root) if heal_stale_root_source else set()
    )
    source_marker, legacy_source_markers = integrator._get_hook_source_markers(
        package_info,
        project_root,
        package_name,
        dependency_sources,
    )
    hooks_integrated = 0
    scripts_copied = 0
    scripts_adopted = 0
    target_paths: list[Path] = []
    display_payloads: list = []
    pending_display: list = []
    cleared_events: set = set()

    json_path = target_dir / config.config_filename
    sidecar_path = target_dir / _APM_HOOKS_SIDECAR
    json_config = _load_merged_config_and_sidecar(
        json_path, sidecar_path, config.schema_strict, container=config.event_container_key
    )

    injected_keys: list[str] = []
    for key, value in config.top_level_defaults.items():
        if key not in json_config:
            json_config[key] = value
            injected_keys.append(key)
    if injected_keys:
        _log.debug("Injected top_level_defaults into %s: %s", config.config_filename, injected_keys)

    for hook_file in hook_files:
        data = integrator._parse_hook_json(hook_file)
        if data is None:
            continue

        rewritten, scripts = _rewrite_hooks_data(
            data,
            package_info.install_path,
            package_name,
            config.target_key,
            hook_file_dir=hook_file.parent,
            root_dir=root_dir,
            deploy_root=_deploy_root_for_rewrite,
        )

        container = config.event_container_key
        hooks = rewritten.get("hooks", {})  # source files always use "hooks" key
        event_map = _HOOK_EVENT_MAP.get(config.target_key, {})
        _emit_hook_event_diagnostics(list(hooks.keys()), config.target_key, event_map)

        file_event_entries: dict = {}
        appended = _merge_hook_file_entries(
            json_config,
            hooks,
            config.target_key,
            event_map,
            source_marker,
            cleared_events,
            legacy_source_markers=legacy_source_markers,
            heal_stale_root_source=heal_stale_root_source,
            dependency_sources=dependency_sources,
            capture_entries=file_event_entries,
            container=container,
        )

        if appended:
            hooks_integrated += 1
            pending_display.append(
                (
                    config.config_filename,
                    config.config_filename,
                    hook_file,
                    file_event_entries,
                )
            )
        else:
            _warn_empty_hook_file(hook_file, config.target_key)

        copy_result = copy_deployed_hook_bundle(
            integrator,
            package_path=package_info.install_path,
            hook_file_dir=hook_file.parent,
            project_root=project_root,
            scripts=scripts,
            managed_files=managed_files,
            force=force,
            diagnostics=diagnostics,
            target_paths=target_paths,
            hook_descriptor_files=set(hook_files),
        )
        scripts_copied += copy_result.scripts_copied
        scripts_adopted += copy_result.files_adopted

    json_path.parent.mkdir(parents=True, exist_ok=True)
    _write_merged_config(json_path, sidecar_path, json_config, config.schema_strict)

    for _label, _path, _hook_file, _file_event_entries in pending_display:
        display_payloads.append(
            integrator._build_display_payload(
                _label,
                _path,
                _hook_file,
                {config.event_container_key: _file_event_entries},
            )
        )

    return HookIntegrationResult(
        files_integrated=hooks_integrated,
        files_updated=0,
        files_skipped=0,
        target_paths=target_paths,
        scripts_copied=scripts_copied,
        files_adopted=scripts_adopted,
        display_payloads=display_payloads,
    )
