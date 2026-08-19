"""Atomic file-write primitive for APM.

Writes go to a temp file in the same directory as the target, then are
renamed via :func:`os.replace`. A crash mid-write cannot leave a half-
written destination, and on POSIX the rename is atomic with respect to
concurrent readers.

This is the single canonical implementation; both
``apm_cli.commands._helpers._atomic_write`` (kept as an alias for
backward compatibility with existing tests) and
``apm_cli.compilation.output_writer`` route through here.
"""

import contextlib
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

# Windows: os.replace needs DELETE access on the destination, so an AV
# scanner / indexer briefly holding the file open without
# FILE_SHARE_DELETE surfaces as a transient PermissionError. Retry a few
# times with short backoff (mirrors the AV-retry convention in
# apm_cli.utils.file_ops) before giving up.
_WIN_REPLACE_ATTEMPTS = 5
_WIN_REPLACE_INITIAL_DELAY = 0.05
# Module-level so tests can exercise the retry branch on any OS.
_IS_WINDOWS = sys.platform == "win32"


def _replace_atomic_file(source: str, destination: Path) -> None:
    """Replace one atomic temp file, with a narrow installed-binary test seam."""
    if os.environ.get("APM_TEST_FAIL_LOCK_REPLACE") == "1" and destination.name == "apm.lock.yaml":
        raise OSError("lockfile replace blocked by test")
    if not _IS_WINDOWS:
        os.replace(source, destination)
        return
    delay = _WIN_REPLACE_INITIAL_DELAY
    for attempt in range(_WIN_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == _WIN_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2


def normalize_crlf_to_lf(data: str) -> str:
    """Normalize CRLF to LF (leaves bare CR alone, like the drift normalizer).

    Mirrors :func:`apm_cli.utils.normalization._normalize_line_endings` at the
    string level. Combined with ``newline=""`` on the open() call, this keeps
    written bytes platform-independent so deployed-file hashes do not diverge
    between Windows (which would otherwise translate ``\\n`` -> ``\\r\\n``) and
    POSIX.
    """
    return data.replace("\r\n", "\n")


def write_text_lf(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` as UTF-8 with deterministic LF line endings.

    Non-atomic counterpart to :func:`atomic_write_text`. Caller must ensure
    ``path.parent`` exists. ``newline=""`` disables the platform newline
    translation that ``Path.write_text`` performs in text mode, so the on-disk
    bytes (and therefore their content hash) are identical on every OS.
    """
    path.write_text(normalize_crlf_to_lf(data), encoding="utf-8", newline="")


def atomic_write_text(path: Path, data: str, *, new_file_mode: int | None = None) -> None:
    """Atomically write ``data`` (UTF-8) to ``path``.

    The temp file is created in ``path.parent`` so the eventual
    ``os.replace`` is a same-filesystem rename. Caller is responsible
    for ensuring the parent directory exists.

    If ``new_file_mode`` is given and ``path`` does not yet exist,
    the temp file's POSIX mode bits are set to that value before
    the rename so the destination is created with the requested
    permissions. When ``path`` already exists, its current mode bits are
    copied onto the temp file first, so the ``os.replace`` inode swap
    does not silently downgrade the destination to mkstemp's 0600 (we
    do not downgrade nor upgrade perms). Both mode adjustments are
    silently skipped on platforms where ``os.fchmod`` is unavailable
    (e.g. Windows), where POSIX mode bits are not enforced anyway.

    On any failure -- including ``KeyboardInterrupt`` mid-write -- the
    temp file is removed and the original target file (if any) remains
    untouched.
    """
    existed = path.exists()
    fd, tmp_name = tempfile.mkstemp(prefix="apm-atomic-", dir=str(path.parent))
    fd_wrapped = False
    try:
        if hasattr(os, "fchmod"):
            if new_file_mode is not None and not existed:
                with contextlib.suppress(OSError):
                    os.fchmod(fd, new_file_mode)
            elif existed:
                with contextlib.suppress(OSError):
                    os.fchmod(fd, stat.S_IMODE(os.stat(path).st_mode))
        fh = os.fdopen(fd, "w", encoding="utf-8", newline="")
        fd_wrapped = True
        with fh:
            fh.write(normalize_crlf_to_lf(data))
        _replace_atomic_file(tmp_name, path)
    except BaseException:
        # BaseException, not Exception: a Ctrl+C (KeyboardInterrupt) mid-write
        # must still remove the temp file. When the destination directory is
        # an installed package tree (synthetic apm.yml, inline hooks.json --
        # apm#2619), a stranded apm-atomic-* file would be swept into
        # compute_package_hash and permanently poison the recorded hash.
        if not fd_wrapped:
            # fdopen never took ownership of the descriptor; close it so
            # Windows can release its lock and the tmp file can be unlinked.
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
