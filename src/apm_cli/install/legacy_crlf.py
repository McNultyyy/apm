"""Migration helpers for the apm#2619 / apm#2187 CRLF hash-domain bugs.

APM versions before the apm#2619 fix wrote the files APM itself authors
INSIDE installed package trees with platform-native line endings: the
synthesized ``apm.yml`` for marketplace plugins (written by
``synthesize_apm_yml_from_plugin`` and rewritten by
``stamp_plugin_version``), the inline-hooks ``.apm/hooks/hooks.json``,
and -- before 0.26.0 / apm#2187 -- the virtual-file package ``apm.yml``.
On Windows those writes produced CRLF bytes, and because
``compute_package_hash`` hashes raw bytes, the recorded lockfile
``content_hash`` landed in a Windows-only "CRLF domain".

The fix makes every such write LF-deterministic, which leaves two legacy
artifacts behind:

1. **Lockfiles recorded by a pre-fix Windows APM** carry the CRLF-domain
   hash. A post-fix fresh download produces the LF-domain hash, and the
   supply-chain check would hard-fail with a misleading
   "supply-chain attack" message. :func:`legacy_crlf_hash` computes the
   CRLF-domain equivalent of the freshly downloaded tree so the caller
   can recognize this exact benign difference and accept + re-record the
   platform-independent hash instead.

2. **Warm ``apm_modules`` trees materialized by a pre-fix Windows APM**
   still hold CRLF bytes in the APM-authored files. The cached install
   path re-records the hash of the on-disk tree, so without intervention
   the stale CRLF hash would be copied into every regenerated lockfile
   forever. :func:`converge_apm_authored_files` rewrites exactly those
   files to LF so the tree (and hence the recorded hash) converges to
   what a fresh post-fix download produces.

Only files APM itself authored are ever considered. Upstream content is
never touched: git-materialized bytes are identical on every OS at a
pinned commit, and normalizing them would CREATE divergence against a
fresh download (which byte-copies them). For the same reason
``.apm/hooks/hooks.json`` is only treated as APM-authored when the
plugin manifest declares ``hooks`` as an INLINE object -- when ``hooks``
names a config file, hooks.json is a byte-copy of upstream content.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.content_hash import compute_package_hash_with_overrides

if TYPE_CHECKING:
    from pathlib import Path

_MARKETPLACE_PLUGIN = "marketplace_plugin"


def _package_type_value(package_type: Any) -> str | None:
    """Normalize a PackageType enum / plain string / None to its string value."""
    if package_type is None:
        return None
    return str(getattr(package_type, "value", package_type))


def _is_virtual_file_dep(dep_ref: Any) -> bool:
    """True when *dep_ref* is a virtual single-file dependency (apm#2187 class)."""
    if dep_ref is None or not getattr(dep_ref, "is_virtual", False):
        return False
    is_virtual_file = getattr(dep_ref, "is_virtual_file", None)
    if not callable(is_virtual_file):
        return False
    try:
        return bool(is_virtual_file())
    except Exception:
        return False


def _has_inline_hooks(install_path: Path) -> bool:
    """True when the tree's plugin manifest declares ``hooks`` as an inline dict.

    Fail-closed: any lookup or parse problem returns False, so hooks.json
    is left alone unless we can positively establish APM authored it.
    """
    from apm_cli.utils.helpers import find_plugin_json

    try:
        plugin_json_path = find_plugin_json(install_path)
    except Exception:
        return False
    if plugin_json_path is None:
        return False

    from apm_cli.deps.plugin_parser import parse_plugin_manifest

    try:
        manifest = parse_plugin_manifest(plugin_json_path)
    except Exception:
        return False
    return isinstance(manifest.get("hooks"), dict)


def apm_authored_files(
    install_path: Path,
    package_type: Any,
    dep_ref: Any,
) -> list[str]:
    """POSIX relative paths of files APM itself authored inside *install_path*.

    Empty for package types where APM writes nothing into the tree (plain
    APM packages, skill bundles, hook packages): their ``apm.yml`` is
    upstream content and must never be rewritten or hash-substituted.

    When *package_type* is ``None`` (several install paths carry a
    ``PackageInfo`` without it -- e.g. pre-download results replayed by
    ``FreshDependencySource``), the type is detected from the tree itself.
    Detection keys on the SAME on-disk evidence (``plugin.json`` /
    ``.claude-plugin/``) that made validation synthesize the manifest in
    the first place, so it cannot claim authorship of an upstream
    ``apm.yml``: a plain APM package has no plugin evidence and detects as
    ``APM_PACKAGE``, which owns nothing here.
    """
    type_value = _package_type_value(package_type)
    if type_value is None and not _is_virtual_file_dep(dep_ref):
        try:
            from apm_cli.models.validation import detect_package_type

            detected, _plugin_json = detect_package_type(install_path)
            type_value = _package_type_value(detected)
        except Exception:
            type_value = None
    files: list[str] = []
    if type_value == _MARKETPLACE_PLUGIN:
        files.append("apm.yml")
        if _has_inline_hooks(install_path):
            files.append(".apm/hooks/hooks.json")
    elif _is_virtual_file_dep(dep_ref):
        # Virtual single-file packages: apm.yml is synthesized by
        # download_virtual_file_package (LF since 0.26.0 / PR #2223).
        files.append("apm.yml")
    return files


def _crlf_expand(data: bytes) -> bytes:
    """Return *data* as a pre-fix Windows text-mode write would have produced it.

    Normalizes to LF first so the expansion is idempotent on mixed input,
    then expands every LF to CRLF -- exactly what Python's platform-native
    newline translation did to the LF text the pre-fix writers emitted.
    """
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def legacy_crlf_hash(
    install_path: Path,
    package_type: Any,
    dep_ref: Any,
) -> str | None:
    """Hash *install_path* as if APM-authored files carried legacy CRLF bytes.

    Returns None when no APM-authored file exists or none would change
    under CRLF expansion -- callers treat None as "no legacy domain to
    compare against" and keep their normal mismatch handling.
    """
    overrides: dict[str, bytes] = {}
    for rel in apm_authored_files(install_path, package_type, dep_ref):
        candidate = install_path / rel
        if not candidate.is_file() or candidate.is_symlink():
            continue
        raw = candidate.read_bytes()
        if b"\x00" in raw:
            # Binary content was never produced by the text-mode writers.
            continue
        expanded = _crlf_expand(raw)
        if expanded != raw:
            overrides[rel] = expanded
    if not overrides:
        return None
    return compute_package_hash_with_overrides(install_path, overrides)


def converge_apm_authored_files(
    install_path: Path,
    package_type: Any,
    dep_ref: Any,
) -> list[str]:
    """CRLF->LF rewrite legacy APM-authored files in a warm tree.

    Returns the POSIX relative paths that were rewritten (empty when the
    tree is already in the LF domain). Non-UTF-8 or NUL-containing files
    are left untouched -- the pre-fix writers only ever produced UTF-8
    (or ASCII) text, so anything else is not ours to rewrite.
    """
    changed: list[str] = []
    for rel in apm_authored_files(install_path, package_type, dep_ref):
        candidate = install_path / rel
        if not candidate.is_file() or candidate.is_symlink():
            continue
        raw = candidate.read_bytes()
        if b"\r\n" not in raw or b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        # atomic_write_text normalizes CRLF -> LF and writes newline="".
        atomic_write_text(candidate, text)
        changed.append(rel)
    return changed
