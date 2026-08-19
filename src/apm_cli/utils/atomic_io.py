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

# POSIX mode-bit handling only. On Windows (where Python 3.13 gained
# os.fchmod) mode bits reduce to the read-only attribute: copying a
# read-only destination's mode onto the temp would make the replace AND
# the failure-path unlink fail, stranding a read-only apm-atomic-* file
# inside the tree. Module-level so tests can exercise both branches.
_POSIX_MODES = os.name == "posix" and hasattr(os, "fchmod")


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
    """Atomically write ``data`` (UTF-8, LF-normalized) to ``path``.

    The temp file is created in ``path.parent`` so the eventual
    ``os.replace`` is a same-filesystem rename. Caller is responsible
    for ensuring the parent directory exists.

    POSIX mode handling (skipped entirely on Windows -- copying mode bits
    there would set FILE_ATTRIBUTE_READONLY on the temp for a read-only
    destination, making both the replace and the failure-path unlink fail
    and stranding a read-only ``apm-atomic-*`` file inside the tree):

    * ``new_file_mode`` given, ``path`` new: the destination is created
      with exactly that mode.
    * ``new_file_mode`` given, ``path`` exists: the destination gets
      ``existing_mode & new_file_mode`` -- it keeps its mode but is capped
      at the caller's ceiling, so security-sensitive callers (0o600-intent
      config writers) still heal a pre-existing loose 0o644 down to 0o600
      instead of preserving it forever.
    * ``new_file_mode`` omitted, ``path`` exists: the existing mode is
      preserved across the ``os.replace`` inode swap (we do not downgrade
      to mkstemp's 0600 -- multi-uid readers of apm_modules keep working).
    * ``new_file_mode`` omitted, ``path`` new: mkstemp's 0600 applies.

    On any failure -- including ``KeyboardInterrupt`` mid-write -- the
    temp file is removed and the original target file (if any) remains
    untouched. The write goes through the raw descriptor (``os.write``)
    rather than an ``os.fdopen`` wrapper so descriptor ownership is
    unambiguous: the fd is closed exactly once, with no window where an
    async exception could trigger a double close of a reused fd number.
    """
    existed = path.exists()
    fd, tmp_name = tempfile.mkstemp(prefix="apm-atomic-", dir=str(path.parent))
    try:
        try:
            if _POSIX_MODES:
                if new_file_mode is not None:
                    mode = new_file_mode
                    if existed:
                        with contextlib.suppress(OSError):
                            mode = stat.S_IMODE(os.stat(path).st_mode) & new_file_mode
                    with contextlib.suppress(OSError):
                        os.fchmod(fd, mode)
                elif existed:
                    with contextlib.suppress(OSError):
                        os.fchmod(fd, stat.S_IMODE(os.stat(path).st_mode))
            payload = memoryview(normalize_crlf_to_lf(data).encode("utf-8"))
            while payload:
                written = os.write(fd, payload)
                payload = payload[written:]
        finally:
            # Exactly-once close for every path through the inner block,
            # and before os.replace (Windows requires the handle released).
            os.close(fd)
        _replace_atomic_file(tmp_name, path)
    except BaseException:
        # BaseException, not Exception: a Ctrl+C (KeyboardInterrupt) mid-write
        # must still remove the temp file. When the destination directory is
        # an installed package tree (synthetic apm.yml, inline hooks.json --
        # apm#2619), a stranded apm-atomic-* file would be swept into
        # compute_package_hash and permanently poison the recorded hash.
        # Clear any read-only attribute first so the unlink cannot itself
        # fail and strand the temp (Windows cannot unlink read-only files).
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, 0o600)
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
