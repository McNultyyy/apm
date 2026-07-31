"""Source-marker utilities, dependency scanning, and merge-operation helpers.

This module holds pure helper functions extracted from ``HookIntegrator``
to reduce the complexity of the merge path.  Nothing here depends on
the ``HookIntegrator`` class itself.
"""

import json
import logging
import re
from pathlib import Path

import yaml

from apm_cli.integration.hook_ownership import (
    dependency_hook_source_marker,
    dependency_hook_sources,
)
from apm_cli.utils.console import _rich_warning

from .hook_native_formats import _to_claude_hook_entries
from .hook_transforms import (
    _APM_HOOKS_SIDECAR,
    _reinject_apm_source_from_sidecar,
    _to_antigravity_hook_entries,
    _to_gemini_hook_entries,
)

_log = logging.getLogger("apm_cli.integration.hook_integrator")

# ---------------------------------------------------------------------------
# Package source-marker utilities
# ---------------------------------------------------------------------------


def _root_local_identity_root(package_info, project_root: Path | None) -> Path | None:
    """Return the project root used to identify root-local packages."""
    return getattr(package_info, "root_local_project_root", None) or project_root


def _is_root_local_package(package_info, project_root: Path | None) -> bool:
    """Return True when *package_info* represents the project's own .apm content."""
    identity_root = _root_local_identity_root(package_info, project_root)
    if identity_root is None:
        return False
    return Path(package_info.install_path).resolve() == Path(identity_root).resolve()


def _safe_source_name(value: str | None, fallback: str = "_local") -> str:
    """Return a stable source marker that is also safe for hook script paths."""
    if not isinstance(value, str) or not value:
        return fallback
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    # Collapse any run of 2+ dots to a single dot before stripping edges.
    # Embedded sequences like "foo..bar" would otherwise pass through the
    # earlier guard and reach downstream Path joins as a parent-dir hop.
    safe = re.sub(r"\.{2,}", ".", safe).strip(".-_")
    if not safe or safe in {".", ".."}:
        return fallback
    return safe


def _get_root_local_package_name(package_info, project_root: Path) -> str:
    """Get the stable source marker for root .apm content."""
    apm_yml = Path(project_root) / "apm.yml"
    if apm_yml.exists():
        try:
            from apm_cli.utils.yaml_io import load_yaml

            data = load_yaml(apm_yml)
            if isinstance(data, dict):
                manifest_name = _safe_source_name(data.get("name"))
                if manifest_name != "_local":
                    return manifest_name
        except (OSError, ValueError, yaml.YAMLError) as exc:
            _log.debug(
                "Hook integrator: apm.yml manifest unreadable for %s (%s: %s), "
                "falling back to install_path basename",
                project_root,
                exc.__class__.__name__,
                exc,
            )

    package = getattr(package_info, "package", None)
    package_name = _safe_source_name(getattr(package, "name", None))
    if package_name != "_local":
        return package_name
    return "_local"


def _get_package_name(package_info, project_root: Path | None = None) -> str:
    """Get a short package name for use in file/directory naming.

    Args:
        package_info: PackageInfo object
        project_root: When provided and the package is the project root,
            reads ``apm.yml`` ``name`` for a stable source marker instead
            of falling back to ``install_path.name`` (which drifts on
            directory renames and worktrees). See #1329.

    Returns:
        str: Package name used as hook source marker and script namespace
    """
    if _is_root_local_package(package_info, project_root):
        identity_root = _root_local_identity_root(package_info, project_root)
        return _get_root_local_package_name(package_info, Path(identity_root))
    return package_info.install_path.name


def _get_hook_source_marker(
    package_info,
    project_root: Path,
    package_name: str,
) -> str:
    """Get the marker stored in merged hook JSON for ownership cleanup."""
    if _is_root_local_package(package_info, project_root):
        if package_name == "_local":
            return "_local"
        return f"_local/{package_name}"
    try:
        dependency_ref = vars(package_info).get("dependency_ref")
    except TypeError:
        dependency_ref = None
    if dependency_ref is not None:
        marker = dependency_hook_source_marker(dependency_ref)
        if isinstance(marker, str) and marker and marker != "unknown":
            return marker
    return package_name


def _hook_entry_content_key(entry: dict) -> str:
    """Build a stable comparison key excluding APM ownership metadata."""
    comparable = {k: v for k, v in sorted(entry.items()) if k != "_apm_source"}
    return json.dumps(comparable, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Dependency source scanning
# ---------------------------------------------------------------------------


# Dependency-source discovery converged with main's integration/hook_ownership.py
# (#2362): main extracted the same family this branch had split here. Main's
# copy is canonical; keep a thin alias so existing importers keep working.
_dependency_hook_sources = dependency_hook_sources


def _should_remove_prior_merged_entry(
    entry,
    *,
    source_marker: str,
    legacy_source_markers: frozenset[str] = frozenset(),
    fresh_content_keys: set[str],
    heal_stale_root_source: bool,
    dependency_sources: set[str],
    remove_current_source: bool,
) -> bool:
    """Return True when an existing merged-hook entry should be replaced."""
    if not isinstance(entry, dict):
        return False
    source = entry.get("_apm_source")
    if remove_current_source and source == source_marker:
        return True
    if remove_current_source and source in legacy_source_markers:
        return True
    if not heal_stale_root_source or not source or source in dependency_sources:
        return False
    return _hook_entry_content_key(entry) in fresh_content_keys


# ---------------------------------------------------------------------------
# Merge operation helpers
# ---------------------------------------------------------------------------


def _load_merged_config_and_sidecar(
    json_path: Path,
    sidecar_path: Path,
    schema_strict: bool,
    container: str = "hooks",
) -> dict:
    """Load target config JSON and optionally re-inject sidecar _apm_source markers.

    Returns a json_config dict that always has the *container* key (the
    top-level event map).  *container* defaults to ``"hooks"``; Antigravity
    passes ``"apm"`` so its events nest under the reserved ``apm`` hook-name
    and sibling user hook-names in the native file are preserved.
    """
    json_config: dict = {}
    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                json_config = json.load(f)
        except (json.JSONDecodeError, OSError):
            json_config = {}

    if schema_strict and sidecar_path.exists():
        sidecar_data: dict = {}
        try:
            with open(sidecar_path, encoding="utf-8") as f:
                _raw = json.load(f)
            if isinstance(_raw, dict):
                sidecar_data = _raw
            else:
                _log.warning(
                    "Sidecar file %s contains non-dict JSON; treating as empty.",
                    sidecar_path,
                )
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Failed to read sidecar %s: %s; treating as empty.", sidecar_path, exc)

        if sidecar_data and "hooks" in json_config:
            _reinject_apm_source_from_sidecar(json_config["hooks"], sidecar_data)

    if container not in json_config:
        json_config[container] = {}

    return json_config


def _deduplicate_event_entries(entries: list) -> list:
    """Deduplicate hook entries by (source, content) key.

    Safety net for edge cases where multiple source files produce
    semantically identical entries.
    """
    seen_keys: set[str] = set()
    deduped: list = []
    for entry in entries:
        if not isinstance(entry, dict):
            deduped.append(entry)
            continue
        cmp = {k: v for k, v in sorted(entry.items()) if k != "_apm_source"}
        source = entry.get("_apm_source")
        dedup_key = json.dumps({"s": source, "c": cmp}, sort_keys=True)
        if dedup_key not in seen_keys:
            seen_keys.add(dedup_key)
            deduped.append(entry)
    return deduped


def _merge_hook_file_entries(
    json_config: dict,
    hooks: dict,
    target_key: str,
    event_map: dict,
    source_marker: str,
    cleared_events: set,
    *,
    legacy_source_markers: frozenset[str] = frozenset(),
    heal_stale_root_source: bool,
    dependency_sources: set,
    capture_entries: dict | None = None,
    container: str = "hooks",
) -> bool:
    """Merge hook entries from one hook file into ``json_config[container]``.

    Applies the target's nested/native transform (Gemini or Antigravity),
    stamps _apm_source, performs idempotent upsert (stripping prior
    same-package entries), and deduplicates.  *container* is the top-level
    event-map key ("hooks" for most targets, "apm" for Antigravity).

    Returns True when at least one event received new entries.
    """
    # Build reverse map: normalised name -> set of source aliases.
    # Used to clean up alias event keys left by mixed-case past installs.
    reverse_map: dict[str, set[str]] = {}
    for source_name, norm_name in event_map.items():
        reverse_map.setdefault(norm_name, set()).add(source_name)

    entries_appended = False
    for raw_event_name, entries in hooks.items():
        if not isinstance(entries, list) or not entries:
            continue
        event_name = event_map.get(raw_event_name, raw_event_name)
        if event_name not in json_config[container]:
            json_config[container][event_name] = []

        # Transform flat Copilot entries to the target's nested / native
        # hook shape.
        if target_key == "claude":
            entries = _to_claude_hook_entries(entries)
        elif target_key == "gemini":
            entries = _to_gemini_hook_entries(entries)
        elif target_key == "antigravity":
            entries = _to_antigravity_hook_entries(entries, event_name)

        # Mark each entry with APM source for sync/cleanup
        for entry in entries:
            if isinstance(entry, dict):
                entry["_apm_source"] = source_marker
        fresh_content_keys = {
            _hook_entry_content_key(entry) for entry in entries if isinstance(entry, dict)
        }

        # Idempotent upsert: drop prior entries owned by this package
        # before appending fresh ones.  Only strip once per event per
        # install run -- a package with multiple hook files targeting the
        # same event contributes each file's entries in turn, and stripping
        # on every iteration would erase earlier files' work.
        remove_current_source = event_name not in cleared_events
        if remove_current_source or heal_stale_root_source:
            _upsert_event_entries(
                json_config,
                event_name,
                source_marker,
                legacy_source_markers,
                fresh_content_keys,
                heal_stale_root_source,
                dependency_sources,
                reverse_map,
                remove_current_source,
                container=container,
            )
            cleared_events.add(event_name)

        json_config[container][event_name].extend(entries)
        json_config[container][event_name] = _deduplicate_event_entries(
            json_config[container][event_name]
        )
        entries_appended = True
        if capture_entries is not None:
            capture_entries.setdefault(event_name, []).extend(
                e for e in entries if isinstance(e, dict)
            )

    return entries_appended


def _upsert_event_entries(
    json_config: dict,
    event_name: str,
    source_marker: str,
    legacy_source_markers: frozenset[str],
    fresh_content_keys: set[str],
    heal_stale_root_source: bool,
    dependency_sources: set,
    reverse_map: dict,
    remove_current_source: bool,
    container: str = "hooks",
) -> None:
    """Remove stale same-package entries before fresh ones are appended.

    Mutates ``json_config[container]`` in-place.
    """
    prior_entries = json_config[container][event_name]
    kept_entries = [
        e
        for e in prior_entries
        if not _should_remove_prior_merged_entry(
            e,
            source_marker=source_marker,
            legacy_source_markers=legacy_source_markers,
            fresh_content_keys=fresh_content_keys,
            heal_stale_root_source=heal_stale_root_source,
            dependency_sources=dependency_sources,
            remove_current_source=remove_current_source,
        )
    ]
    if heal_stale_root_source:
        kept_ids = {id(e) for e in kept_entries}
        healed = sum(
            1
            for e in prior_entries
            if isinstance(e, dict)
            and e.get("_apm_source")
            and e.get("_apm_source") != source_marker
            and e.get("_apm_source") not in dependency_sources
            and id(e) not in kept_ids
        )
        if healed:
            _log.debug(
                "Hook integrator: healed %d stale same-content "
                "merged hook entries for source %s in event %s",
                healed,
                source_marker,
                event_name,
            )
    json_config[container][event_name] = kept_entries

    # Also clear from any alias events that map to this normalised name
    # (handles migration from corrupted installs with mixed-case event keys).
    for alias in reverse_map.get(event_name, set()):
        if alias != event_name and alias in json_config[container]:
            json_config[container][alias] = [
                e
                for e in json_config[container][alias]
                if not _should_remove_prior_merged_entry(
                    e,
                    source_marker=source_marker,
                    legacy_source_markers=legacy_source_markers,
                    fresh_content_keys=fresh_content_keys,
                    heal_stale_root_source=heal_stale_root_source,
                    dependency_sources=dependency_sources,
                    remove_current_source=remove_current_source,
                )
            ]
            # Remove the alias key entirely if now empty
            if not json_config[container][alias]:
                del json_config[container][alias]


def _warn_empty_hook_file(hook_file: Path, target_key: str) -> None:
    """Emit user-visible and structured-log warnings for an empty hook file.

    A hook file that parsed cleanly but contributed zero entries (all
    events empty / non-list) used to bump the counter and lie to the user.
    Now we skip it -- emit a warning so the author notices.
    """
    rel_hook = hook_file.name
    _rich_warning(f"Hook file {rel_hook} contributed no entries to {target_key} settings; skipped.")
    _log.warning(
        "Hook file %s contributed no entries to %s settings "
        "(all events empty or non-list); skipping.",
        hook_file,
        target_key,
    )


def _write_merged_config(
    json_path: Path,
    sidecar_path: Path,
    json_config: dict,
    schema_strict: bool,
) -> None:
    """Write the merged config (and optionally the sidecar) to disk.

    For schema-strict targets (e.g. Claude):
    - Builds a sidecar from entries that carry ``_apm_source``.
    - Strips ``_apm_source`` from the config before writing so the
      target's schema validator does not reject the file.
    - Writes the sidecar alongside the config, or removes it when empty.
    """
    if schema_strict:
        # Build sidecar from entries that have _apm_source
        sidecar_out: dict = {}
        for ev_name, entries_list in json_config.get("hooks", {}).items():
            if not isinstance(entries_list, list):
                continue
            owned = [e for e in entries_list if isinstance(e, dict) and "_apm_source" in e]
            if owned:
                sidecar_out[ev_name] = [dict(e) for e in owned]

        # Strip _apm_source from entries before writing to disk
        for entries_list in json_config.get("hooks", {}).values():
            if isinstance(entries_list, list):
                for entry in entries_list:
                    if isinstance(entry, dict):
                        entry.pop("_apm_source", None)

        # Write or remove sidecar
        if sidecar_out:
            try:
                with open(sidecar_path, "w", encoding="utf-8") as f:
                    json.dump(sidecar_out, f, indent=2)
                    f.write("\n")
            except OSError as exc:
                _log.warning("Failed to write sidecar %s: %s", sidecar_path, exc)
        elif sidecar_path.exists():
            sidecar_path.unlink()

    # Write the (now schema-clean) config
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_config, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Hook JSON parsing
# ---------------------------------------------------------------------------


def _parse_hook_json(hook_file: Path) -> dict | None:
    """Parse a hook JSON file and return the data dict.

    Accepts both the wrapped format (``{"hooks": {EventName: [...]}}``)
    and the "naked" Claude-settings hooks-slice format
    (``{EventName: [...], ...}`` with no outer ``"hooks":`` wrap).
    The naked shape is what Claude Code accepts inside its own
    ``settings.json`` and is a common authoring pattern -- silently
    dropping it produced the empty merge reported in microsoft/apm#1499.

    Returns the parsed dict (always wrapped), or None if invalid.
    """
    try:
        with open(hook_file, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        # Normalise naked-format files (no outer "hooks" key but
        # every top-level value is a list of matcher entries) into
        # the wrapped shape downstream code expects.  Only promote
        # when ALL values look like hook entry arrays -- a stray
        # scalar (e.g. "description") would mean this is malformed
        # rather than naked, so leave it alone.
        if "hooks" not in data and data and all(isinstance(v, list) for v in data.values()):
            _log.debug(
                "Promoted naked-format hook file %s (top-level event keys: %s) to wrapped shape",
                hook_file,
                sorted(data.keys()),
            )
            data = {"hooks": data}
        # Fail closed on malformed shapes where "hooks" is present but not
        # a dict (e.g. {"hooks": []}).  Downstream code calls .items() on
        # this value and would otherwise raise AttributeError mid-merge.
        if "hooks" in data and not isinstance(data["hooks"], dict):
            _log.warning(
                "Skipping malformed hook file %s: 'hooks' must be a dict, got %s",
                hook_file,
                type(data["hooks"]).__name__,
            )
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------


def _sync_claude_hooks_settings(json_path: Path, stats: dict[str, int]) -> None:
    """Remove APM-managed hook entries from a Claude settings.json file.

    Loads the sidecar to restore _apm_source markers, filters out all
    entries tagged with ``_apm_source``, writes the cleaned config back,
    and removes the sidecar when no hooks remain.
    """
    if not json_path.exists():
        return
    try:
        with open(json_path, encoding="utf-8") as f:
            settings = json.load(f)

        # Load sidecar to restore _apm_source markers
        sidecar_path = json_path.parent / _APM_HOOKS_SIDECAR
        sidecar_data: dict = {}
        if sidecar_path.exists():
            try:
                with open(sidecar_path, encoding="utf-8") as sf:
                    _raw = json.load(sf)
                if isinstance(_raw, dict):
                    sidecar_data = _raw
                else:
                    _log.warning(
                        "Sidecar file %s contains non-dict JSON; treating as empty.",
                        sidecar_path,
                    )
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning(
                    "Failed to read sidecar %s: %s; treating as empty.",
                    sidecar_path,
                    exc,
                )

        # Re-inject _apm_source from sidecar
        if sidecar_data and "hooks" in settings:
            _reinject_apm_source_from_sidecar(settings["hooks"], sidecar_data)

        if "hooks" in settings:
            modified = False
            for event_name in list(settings["hooks"].keys()):
                matchers = settings["hooks"][event_name]
                if isinstance(matchers, list):
                    filtered = [
                        m for m in matchers if not (isinstance(m, dict) and "_apm_source" in m)
                    ]
                    if len(filtered) != len(matchers):
                        modified = True
                    settings["hooks"][event_name] = filtered
                    if not filtered:
                        del settings["hooks"][event_name]

            if not settings["hooks"]:
                del settings["hooks"]

            if modified:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(settings, f, indent=2)
                    f.write("\n")
                stats["files_removed"] += 1

                # Clean up sidecar
                if sidecar_path.exists():
                    sidecar_path.unlink()

            # Remove stale sidecar when no hooks section remains
            if sidecar_path.exists() and "hooks" not in settings:
                sidecar_path.unlink()
    except (json.JSONDecodeError, OSError):
        stats["errors"] += 1


def _clean_apm_entries_from_json(
    json_path: Path,
    stats: dict[str, int],
    container: str = "hooks",
    sidecar_path: Path | None = None,
) -> None:
    """Remove APM-tagged entries from a native hooks JSON file.

    Reinjects *sidecar_path* ownership before filtering (when provided),
    then unlinks it.  Antigravity passes ``container="apm"``.
    """
    if not json_path.exists():
        return
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        if container not in data:
            if sidecar_path is not None and sidecar_path.exists():
                sidecar_path.unlink()
            return
        if sidecar_path is not None and sidecar_path.exists():
            try:
                with open(sidecar_path, encoding="utf-8") as sf:
                    sd = json.load(sf)
                if isinstance(sd, dict):
                    _reinject_apm_source_from_sidecar(data[container], sd)
            except (json.JSONDecodeError, OSError):
                pass
        modified = False
        for event_name in list(data[container].keys()):
            entries = data[container][event_name]
            if isinstance(entries, list):
                filtered = [e for e in entries if not (isinstance(e, dict) and "_apm_source" in e)]
                if len(filtered) != len(entries):
                    modified = True
                data[container][event_name] = filtered
                if not filtered:
                    del data[container][event_name]
        if not data[container]:
            del data[container]
        if modified:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            stats["files_removed"] += 1
        if sidecar_path is not None and sidecar_path.exists():
            sidecar_path.unlink()
    except (json.JSONDecodeError, OSError):
        stats["errors"] += 1
