"""Artifactory backward-compatibility stubs for the package downloader.

Split out of ``github_downloader`` to keep that module inside the module line
budget (#1078). These are pure delegations to ``DownloadDelegate``; they stay
methods (via a mixin) so existing callers and test monkeypatches that reach
``GitHubPackageDownloader._download_artifactory_archive`` keep resolving.
"""

from pathlib import Path


class ArtifactoryCompatMixin:
    """Delegating stubs that forward Artifactory calls to the strategies."""

    def _get_artifactory_headers(self) -> dict[str, str]:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.get_artifactory_headers()

    def _download_artifactory_archive(
        self,
        host: str,
        prefix: str,
        owner: str,
        repo: str,
        ref: str,
        target_path: Path,
        scheme: str = "https",
    ) -> None:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_artifactory_archive(
            host,
            prefix,
            owner,
            repo,
            ref,
            target_path,
            scheme=scheme,
        )

    def _download_file_from_artifactory(
        self,
        host: str,
        prefix: str,
        owner: str,
        repo: str,
        file_path: str,
        ref: str,
        scheme: str = "https",
    ) -> bytes:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_file_from_artifactory(
            host,
            prefix,
            owner,
            repo,
            file_path,
            ref,
            scheme=scheme,
        )
