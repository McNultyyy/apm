"""Azure DevOps URL builders/parsers extracted from :mod:`apm_cli.utils.github_host`.

Extracted to keep ``github_host.py`` under the 800-line threshold (issue
#1078 Stage 2) while preserving 100% behavioural equivalence.  This module
is private (``_`` prefix).  All public names are re-exported from
``github_host.py`` so ``apm_cli.utils.github_host.NAME`` continues to
resolve correctly.

Rule B note: unlike ``_github_host_artifactory``, the functions here DO
reference module-level names that tests monkeypatch on ``github_host``
(``is_azure_devops_hostname``, ``is_visualstudio_legacy_hostname``).  They
are therefore resolved late, through the facade module object, so a
``monkeypatch.setattr("apm_cli.utils.github_host.is_azure_devops_hostname", ...)``
is still observed here.
"""

from __future__ import annotations

import os
import urllib.parse

_ADO_SERVER_BASE_PATH_ERROR = (
    "Azure DevOps Server URLs mounted below '/tfs/' are not currently "
    "supported. Use a root-hosted collection URL such as "
    "'https://server/DefaultCollection/project/_git/repo'."
)


def _gh():
    """Return the ``github_host`` facade module (late, for Rule B)."""
    from . import github_host

    return github_host


def _normalize_configured_host(value: str) -> str:
    """Return a lowercase hostname from one configured host value."""
    candidate = value.strip()
    if not candidate:
        return ""
    parsed = urllib.parse.urlsplit(
        candidate if "://" in candidate else f"//{candidate}",
        scheme="https",
    )
    try:
        if parsed.port is not None:
            return ""
    except ValueError:
        return ""
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        return ""
    return (parsed.hostname or "").lower()


def _get_ado_single_host() -> str:
    """Return the normalized ADO_HOST env value."""
    return _normalize_configured_host(os.environ.get("ADO_HOST", ""))


def _get_ado_hosts_list() -> list[str]:
    """Return normalized APM_ADO_HOSTS entries."""
    raw = os.environ.get("APM_ADO_HOSTS", "")
    return [host for part in raw.split(",") if (host := _normalize_configured_host(part))]


def _is_valid_ado_server_fqdn(hostname: str) -> bool:
    """Return whether an ADO Server setting is a DNS name, not an IP literal."""
    return _gh().is_valid_fqdn(hostname) and not all(
        label.isdigit() for label in hostname.split(".")
    )


def reject_unsupported_ado_server_base_path(dependency_str: str) -> None:
    """Reject ADO Server URLs mounted below an unsupported path prefix."""
    source = dependency_str.split("#", 1)[0].strip()
    if source.lower().startswith(("ssh://", "git@")):
        return
    if source.lower().startswith(("https://", "http://")):
        parsed = urllib.parse.urlsplit(source)
        host = parsed.hostname or ""
        segments = [segment for segment in parsed.path.split("/") if segment]
    else:
        parts = [segment for segment in source.split("/") if segment]
        if len(parts) < 2:
            return
        try:
            host = urllib.parse.urlsplit(f"//{parts[0]}").hostname or ""
        except ValueError:
            return
        segments = parts[1:]
    if (
        not _gh().is_azure_devops_hostname(host)
        or host in {"dev.azure.com", "ssh.dev.azure.com"}
        or _gh().is_visualstudio_legacy_hostname(host)
        or "_git" not in segments
    ):
        return
    git_index = segments.index("_git")
    if segments[0].lower() == "tfs" and git_index >= 3:
        raise ValueError(_ADO_SERVER_BASE_PATH_ERROR)


def build_ado_ssh_url(org: str, project: str, repo: str, host: str = "ssh.dev.azure.com") -> str:
    """Build Azure DevOps SSH clone URL for cloud or server.

    For Azure DevOps Services (cloud):
        git@ssh.dev.azure.com:v3/{org}/{project}/{repo}

    For Azure DevOps Server (on-premises):
        ssh://git@{host}/{org}/{project}/_git/{repo}

    Args:
        org: Azure DevOps organization name
        project: Azure DevOps project name
        repo: Repository name
        host: SSH host (default: ssh.dev.azure.com for cloud; set to your server for on-prem)

    Returns:
        str: SSH clone URL for Azure DevOps
    """
    quoted_project = urllib.parse.quote(project, safe="")
    if host == "ssh.dev.azure.com":
        # Cloud format
        return f"git@ssh.dev.azure.com:v3/{org}/{quoted_project}/{repo}"
    else:
        # Server format (user@host is optional, but commonly 'git@host')
        return f"ssh://git@{host}/{org}/{quoted_project}/_git/{repo}"


def build_ado_api_url(
    org: str,
    project: str,
    repo: str,
    path: str,
    ref: str = "main",
    host: str = "dev.azure.com",
    port: int | None = None,
) -> str:
    """Build Azure DevOps REST API URL for file contents.

    API format: https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/items

    Args:
        org: Azure DevOps organization name
        project: Azure DevOps project name
        repo: Repository name
        path: Path to file within the repository
        ref: Git reference (branch, tag, or commit). Defaults to "main"
        host: Azure DevOps host (default: dev.azure.com)
        port: Optional non-default HTTPS port for Azure DevOps Server

    Returns:
        str: API URL for retrieving file contents
    """
    api_host = "dev.azure.com" if host == "ssh.dev.azure.com" else host
    api_netloc = f"{api_host}:{port}" if port is not None else api_host
    encoded_path = urllib.parse.quote(path, safe="")
    quoted_org = urllib.parse.quote(org, safe="")
    quoted_project = urllib.parse.quote(project, safe="")
    quoted_repo = urllib.parse.quote(repo, safe="")
    quoted_ref = urllib.parse.quote(ref, safe="")
    org_path = "" if _gh().is_visualstudio_legacy_hostname(api_host) else f"{quoted_org}/"
    return (
        f"https://{api_netloc}/{org_path}{quoted_project}/_apis/git/repositories/{quoted_repo}/items"
        f"?path={encoded_path}&versionDescriptor.version={quoted_ref}&api-version=7.0"
    )


def parse_ado_repo_url(url: str | None) -> tuple[str, str, str] | None:
    """Decompose an Azure DevOps repo URL into ``(org, project, repo)``.

    Accepts the standard ``_git`` clone shape on both ADO hostnames:

    - ``https://dev.azure.com/{org}/{project}/_git/{repo}`` (org in path)
    - ``https://{org}.visualstudio.com/{project}/_git/{repo}`` (org in subdomain)

    A trailing ``.git`` and any segments after ``{repo}`` are ignored. Path
    segments are percent-decoded. Returns ``None`` when the URL is not an ADO
    host, lacks the ``_git`` marker, or cannot be decomposed.
    """
    if not url:
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    hostname = parsed.hostname or ""
    if not _gh().is_azure_devops_hostname(hostname):
        return None

    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    segments = [urllib.parse.unquote(s) for s in path.split("/") if s]
    if "_git" not in segments:
        return None
    git_idx = segments.index("_git")
    if (
        not _gh().is_visualstudio_legacy_hostname(hostname)
        and hostname not in {"dev.azure.com", "ssh.dev.azure.com"}
        and segments[0].lower() == "tfs"
        and git_idx >= 3
    ):
        raise ValueError(_ADO_SERVER_BASE_PATH_ERROR)
    if git_idx + 1 >= len(segments):
        return None
    repo = segments[git_idx + 1]
    before = segments[:git_idx]

    if _gh().is_visualstudio_legacy_hostname(hostname):
        # Legacy ``*.visualstudio.com``: org lives in the subdomain.
        org, project = hostname.split(".")[0], (before[-1] if before else "")
    else:
        # ``dev.azure.com``: org and project both precede ``_git``.
        org, project = (before[0], before[1]) if len(before) >= 2 else ("", "")

    if not (org and project and repo):
        return None
    return org, project, repo
