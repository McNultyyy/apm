"""Azure DevOps policy-fetch helpers extracted from ``discovery.py`` (#1078).

The strangler-fig refactor keeps ``discovery.py`` under the source
file-length budget by relocating the ADO transport here.  To preserve the
existing test seams, every discovery-level symbol (``requests``, the cache
helpers, ``_fetch_ado_contents``) is resolved through the ``discovery``
module object at call time -- so patches targeting
``apm_cli.policy.discovery.<name>`` continue to intercept these functions
after relocation.  ``discovery`` re-exports both functions, so callers and
test patch targets keep using ``apm_cli.policy.discovery._fetch_from_ado_repo``
and ``..._fetch_ado_contents`` unchanged.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from ..cache.url_normalize import SCP_LIKE_RE
from ..utils.github_host import (
    build_ado_api_url,
    is_visualstudio_legacy_hostname,
    parse_ado_repo_url,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ._discovery_cache import _CacheEntry
    from .discovery import PolicyFetchResult


def _fetch_from_ado_repo(
    *,
    org: str,
    project: str,
    repo: str,
    host: str,
    project_root: Path,
    port: int | None = None,
    no_cache: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Fetch apm-policy.yml from an Azure DevOps repo.

    Mirrors ``_fetch_from_repo`` but uses ``_fetch_ado_contents`` (ADO
    Items API) instead of ``_fetch_github_contents`` (GitHub Contents API).
    """
    from apm_cli.policy import discovery as _d

    host_label = f"{host}:{port}" if port is not None else host
    repo_ref = f"{host_label}/{org}/{project}/{repo}"
    source_label = f"org:{repo_ref}"
    cache_entry: _CacheEntry | None = None

    if not no_cache:
        cache_entry = _d._read_cache_entry(repo_ref, project_root, expected_hash=expected_hash)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _d._is_policy_empty(cache_entry.policy) else "found"
            return _d.PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
                raw_bytes_hash=cache_entry.raw_bytes_hash or None,
                expected_hash=expected_hash,
                warnings=cache_entry.warnings,
            )

    fetch_kwargs: dict = {"host": host}
    if port is not None:
        fetch_kwargs["port"] = port
    content, error = _d._fetch_ado_contents(org, project, repo, "apm-policy.yml", **fetch_kwargs)

    if error:
        if "404" in error:
            return _d.PolicyFetchResult(source=source_label, outcome="absent")
        return _d._stale_fallback_or_error(
            cache_entry, error, source_label, "cache_miss_fetch_fail"
        )

    if content is None:
        return _d.PolicyFetchResult(source=source_label, outcome="absent")

    garbage_result = _d._detect_garbage(content, repo_ref, source_label, cache_entry)
    if garbage_result is not None:
        return garbage_result

    mismatch = _d._verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, warnings = _d.load_policy(content)
    except _d.PolicyValidationError as e:
        return _d.PolicyFetchResult(
            error=f"Invalid policy in {repo_ref}: {e}",
            source=source_label,
            outcome="malformed",
            warnings=e.warnings,
        )

    chain_refs = [repo_ref]
    actual_hash = _d._compute_hash_normalized(content, expected_hash)
    # Defer cache write for policies with extends: -- chain resolver writes merged cache.
    if not policy.extends:
        _d._write_cache(
            repo_ref,
            policy,
            project_root,
            chain_refs=chain_refs,
            raw_bytes_hash=actual_hash,
            warnings=warnings,
        )
    outcome = "empty" if _d._is_policy_empty(policy) else "found"
    return _d.PolicyFetchResult(
        policy=policy,
        source=source_label,
        outcome=outcome,
        raw_bytes_hash=actual_hash,
        expected_hash=expected_hash,
        warnings=warnings,
    )


def _fetch_ado_contents(
    org: str,
    project: str,
    repo: str,
    file_path: str,
    *,
    host: str = "dev.azure.com",
    port: int | None = None,
) -> tuple[str | None, str | None]:
    """Fetch file contents from Azure DevOps Items API.

    Returns ``(content_string, error_string)``. One will be ``None``.
    Uses ``try_with_fallback`` for PAT-to-bearer protocol support.
    """
    from apm_cli.policy import discovery as _d

    api_url = build_ado_api_url(org, project, repo, file_path, host=host, port=port)
    host_label = f"{host}:{port}" if port is not None else host
    repo_ref = f"{host_label}/{org}/{project}/{repo}"

    # ADO auth is centralized in AuthResolver: ADO_APM_PAT uses Basic auth,
    # and az CLI AAD tokens use Bearer. try_with_fallback handles PAT-to-bearer.
    from ..core.auth import AuthResolver

    auth_resolver = AuthResolver()

    def _headers(token, git_env):
        if not token:
            return {}
        count = int(git_env.get("GIT_CONFIG_COUNT", "0") or "0")
        for index in range(max(0, count)):
            value = git_env.get(f"GIT_CONFIG_VALUE_{index}", "")
            if value.lower().startswith("authorization: bearer "):
                return {"Authorization": value[len("authorization: ") :].strip()}
        cred = base64.b64encode(f":{token}".encode()).decode()
        return {"Authorization": f"Basic {cred}"}

    def _request(token, git_env):
        response = _d.requests.get(
            api_url,
            headers=_headers(token, git_env),
            timeout=10,
            allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise RuntimeError(f"{response.status_code}: unauthorized")
        return response

    try:
        fallback_kwargs = {"org": org, "path": f"{org}/{project}/{repo}", "unauth_first": False}
        if port is not None:
            fallback_kwargs["port"] = port
        resp = auth_resolver.try_with_fallback(host, _request, **fallback_kwargs)
        if resp.status_code == 404:
            return None, "404: Policy file not found"
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location", "<no Location header>")
            return None, (
                f"Refusing HTTP redirect ({resp.status_code}) from {api_url} to {location}"
            )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code} fetching policy from {repo_ref}"
        # ADO Items API returns raw file content by default
        return resp.text, None
    except _d.requests.exceptions.Timeout:
        return None, f"Timeout fetching policy from {repo_ref}"
    except _d.requests.exceptions.ConnectionError:
        return None, f"Connection error fetching policy from {repo_ref}"
    except RuntimeError as exc:
        error_kwargs = {"org": org}
        if port is not None:
            error_kwargs["port"] = port
        remediation = auth_resolver.build_error_context(host, "fetch org policy", **error_kwargs)
        return None, f"{exc}: Access denied to {repo_ref}{remediation}"
    except Exception as e:
        return None, f"Error fetching policy from {repo_ref}: {e}"


# ---------------------------------------------------------------------------
# Host-classification and token helpers (re-exported via discovery.py)
# ---------------------------------------------------------------------------


def _is_github_host(host: str) -> bool:
    """Return True if *host* is a known GitHub-family hostname."""
    import os

    if host == "github.com":
        return True
    if host.endswith(".ghe.com"):
        return True
    gh_host = os.environ.get("GITHUB_HOST", "")
    if gh_host and host == gh_host:  # noqa: SIM103
        return True
    return False


def _get_token_for_host(host: str) -> str | None:
    """Get authentication token for a given host.

    Environment-variable tokens (GITHUB_TOKEN, GITHUB_APM_PAT, GH_TOKEN)
    are only returned when *host* is a recognized GitHub-family hostname.
    For other hosts the token manager + git credential helpers are used.
    """
    import logging
    import os

    logger = logging.getLogger(__name__)
    try:
        from ..core.token_manager import GitHubTokenManager

        manager = GitHubTokenManager()
        return manager.get_token_with_credential_fallback("modules", host)
    except Exception as exc:
        logger.debug("Token manager failed for %s: %s", host, exc)
        if _is_github_host(host):
            return (
                os.environ.get("GITHUB_TOKEN")
                or os.environ.get("GITHUB_APM_PAT")
                or os.environ.get("GH_TOKEN")
            )
        return None


# ---------------------------------------------------------------------------
# File-based policy loader (re-exported via discovery.py)
# ---------------------------------------------------------------------------


def _load_from_file(path: Path, *, expected_hash: str | None = None) -> PolicyFetchResult:
    """Load policy from a local file.

    Rule B: Uses ``_verify_hash_pin``, ``_is_policy_empty``,
    ``_compute_hash_normalized`` via ``apm_cli.policy.discovery`` so test
    patches on ``discovery.*`` still intercept.
    """
    from apm_cli.policy import discovery as _d
    from apm_cli.policy.parser import PolicyValidationError, load_policy

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return _d.PolicyFetchResult(
            error=f"Failed to read {path}: {e}",
            outcome="cache_miss_fetch_fail",
        )

    source_label = f"file:{path}"
    mismatch = _d._verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, warnings = load_policy(content)
        outcome = "empty" if _d._is_policy_empty(policy) else "found"
        actual_hash = (
            _d._compute_hash_normalized(content, expected_hash)
            if expected_hash is not None
            else None
        )
        return _d.PolicyFetchResult(
            policy=policy,
            source=source_label,
            outcome=outcome,
            raw_bytes_hash=actual_hash,
            expected_hash=expected_hash,
            warnings=warnings,
        )
    except PolicyValidationError as e:
        return _d.PolicyFetchResult(
            error=f"Invalid policy file {path}: {e}",
            outcome="malformed",
            warnings=e.warnings,
        )


def _parse_remote_url(url: str) -> tuple[str, str] | None:
    """Parse a git remote URL into (org, host).

    Accepts SCP-style SSH URLs with any username (not just ``git@``), so
    EMU/GHE deployments that use a non-``git`` SSH user parse correctly.
    Also handles Azure DevOps SSH URLs (``v3/`` path prefix).

    Returns None if URL can't be parsed.
    """
    if not url:
        return None

    scp_match = SCP_LIKE_RE.match(url)
    if scp_match:
        host = scp_match.group("host")
        path_part = scp_match.group("path")
        try:
            parts = path_part.rstrip("/").removesuffix(".git").split("/")
            parts = [p for p in parts if p]
            if not parts:
                return None
            if host == "ssh.dev.azure.com" and parts[0] == "v3" and len(parts) >= 2:
                return (parts[1], host)
            return (parts[0], host)
        except (ValueError, IndexError):
            return None

    if "://" in url:
        return _parse_scheme_remote_url(url)

    return None


def _parse_scheme_remote_url(url: str) -> tuple[str, str] | None:
    """Parse a scheme-style remote URL (``https://host/org/...``).

    Azure DevOps legacy ``*.visualstudio.com`` hosts encode the org in the
    hostname rather than the path, so they are handled before the generic
    path-based extraction.

    Uses ``_d.urlparse`` so test patches on
    ``apm_cli.policy.discovery.urlparse`` are intercepted.
    """
    from apm_cli.policy import discovery as _d

    from ..utils.github_host import is_azure_devops_hostname

    try:
        parsed = _d.urlparse(url)
        host = parsed.hostname or ""
        path_parts = parsed.path.strip("/").removesuffix(".git").rstrip("/").split("/")
        if is_visualstudio_legacy_hostname(host):
            return (host[: -len(".visualstudio.com")], host)
        if is_azure_devops_hostname(host):
            ado_coordinates = parse_ado_repo_url(url)
            if ado_coordinates is None:
                return None
            return ado_coordinates[0], host
        if host and path_parts and path_parts[0]:
            return (path_parts[0], host)
    except ValueError as exc:
        if "mounted below '/tfs/'" in str(exc):
            raise
        return None
    except Exception:
        return None

    return None


def _extract_port_from_git_remote(project_root) -> int | None:
    """Return the explicit HTTPS port from the git remote ``origin``, if any.

    Used by the on-premises ADO discovery path to pass non-default ports
    through to the Items API URL builder and the auth resolver.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=project_root,
            timeout=5,
        )
        if result.returncode != 0 or "://" not in result.stdout:
            return None
        from apm_cli.policy import discovery as _d

        return _d.urlparse(result.stdout.strip()).port
    except (ValueError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
