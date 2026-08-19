"""Unit tests for apm_cli.utils.atomic_io.atomic_write_text.

Covers:
- Basic write: file created with correct content
- Overwrites existing file atomically
- new_file_mode applied via fchmod when file is new
- new_file_mode NOT applied when file already exists
- new_file_mode=None never calls fchmod
- Write failure: tmp file is cleaned up and exception propagates
- Write failure + unlink failure: original exception still propagates
- Unicode content round-trips correctly
- Temp file uses correct prefix and parent directory
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.utils.atomic_io import atomic_write_text, write_text_lf

# Entire module: this is the canonical owner of platform-independent
# atomic/LF-safe text writes (microsoft/apm#2233 CRLF-drift class).
# Selected by the PR-time Windows Compatibility Gate via
# `pytest -m windows_compat`; also runs on every other OS.
pytestmark = pytest.mark.windows_compat


class TestAtomicWriteText:
    """Tests for atomic_write_text()."""

    # ------------------------------------------------------------------
    # Happy-path writes
    # ------------------------------------------------------------------

    def test_creates_file_with_correct_content(self, tmp_path: Path) -> None:
        """A new file is created and contains the expected content."""
        target = tmp_path / "output.txt"
        atomic_write_text(target, "hello world\n")
        assert target.read_text(encoding="utf-8") == "hello world\n"

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        """An already-existing file is replaced atomically."""
        target = tmp_path / "existing.txt"
        target.write_text("old content", encoding="utf-8")
        atomic_write_text(target, "new content")
        assert target.read_text(encoding="utf-8") == "new content"

    def test_unicode_content_round_trips(self, tmp_path: Path) -> None:
        """Non-ASCII / emoji content is preserved through UTF-8 encoding."""
        target = tmp_path / "unicode.txt"
        content = "café ☕ 日本語\n"
        atomic_write_text(target, content)
        assert target.read_text(encoding="utf-8") == content

    # ------------------------------------------------------------------
    # Temp file properties
    # ------------------------------------------------------------------

    def test_tmp_file_is_in_path_parent(self, tmp_path: Path) -> None:
        """mkstemp is called with dir=path.parent."""
        target = tmp_path / "out.txt"
        created_tmp_names: list[str] = []

        original_mkstemp = tempfile.mkstemp

        def capturing_mkstemp(
            prefix: str = "",
            suffix: str = "",
            dir: str | None = None,
        ) -> tuple[int, str]:
            fd, name = original_mkstemp(prefix=prefix, suffix=suffix, dir=dir)
            created_tmp_names.append(name)
            return fd, name

        with patch("apm_cli.utils.atomic_io.tempfile.mkstemp", side_effect=capturing_mkstemp):
            atomic_write_text(target, "data")

        assert len(created_tmp_names) == 1
        assert Path(created_tmp_names[0]).parent == tmp_path

    def test_tmp_file_uses_apm_atomic_prefix(self, tmp_path: Path) -> None:
        """mkstemp is called with prefix='apm-atomic-'."""
        target = tmp_path / "out.txt"
        captured_kwargs: list[dict] = []

        # Save the original before patching to avoid recursion
        import tempfile as _tempfile

        _orig_mkstemp = _tempfile.mkstemp

        def mock_mkstemp(**kwargs):  # type: ignore[override]
            captured_kwargs.append(dict(kwargs))
            return _orig_mkstemp(**kwargs)

        with patch("apm_cli.utils.atomic_io.tempfile.mkstemp", side_effect=mock_mkstemp):
            atomic_write_text(target, "data")

        assert captured_kwargs[0]["prefix"] == "apm-atomic-"

    # ------------------------------------------------------------------
    # fchmod / new_file_mode
    # ------------------------------------------------------------------

    def test_fchmod_called_for_new_file_with_mode(self, tmp_path: Path) -> None:
        """fchmod is called when new_file_mode is set and file is new."""
        target = tmp_path / "new_file.txt"
        assert not target.exists()

        with patch("apm_cli.utils.atomic_io.os.fchmod", create=True) as mock_fchmod:
            with patch("apm_cli.utils.atomic_io.hasattr", return_value=True):
                atomic_write_text(target, "data", new_file_mode=0o600)

        mock_fchmod.assert_called_once()
        _fd, mode = mock_fchmod.call_args[0]
        assert mode == 0o600

    def test_existing_file_keeps_its_mode_not_new_file_mode(self, tmp_path: Path) -> None:
        """An existing target keeps ITS mode: fchmod copies the destination's
        current mode onto the temp (never ``new_file_mode``), so the
        ``os.replace`` inode swap neither downgrades to mkstemp's 0600 nor
        applies the new-file-only mode hint (apm#2619 review)."""
        import os as _os
        import stat as _stat

        target = tmp_path / "existing.txt"
        target.write_text("existing", encoding="utf-8")
        existing_mode = _stat.S_IMODE(_os.stat(target).st_mode)

        with patch("apm_cli.utils.atomic_io.os.fchmod", create=True) as mock_fchmod:
            atomic_write_text(target, "data", new_file_mode=0o600)

        mock_fchmod.assert_called_once()
        assert mock_fchmod.call_args.args[1] == existing_mode

    def test_fchmod_not_called_when_mode_is_none(self, tmp_path: Path) -> None:
        """fchmod is NOT called when new_file_mode is None."""
        target = tmp_path / "out.txt"

        with patch("apm_cli.utils.atomic_io.os.fchmod", create=True) as mock_fchmod:
            atomic_write_text(target, "data", new_file_mode=None)

        mock_fchmod.assert_not_called()

    # ------------------------------------------------------------------
    # Error paths
    # ------------------------------------------------------------------

    def test_tmp_file_cleaned_up_on_write_failure(self, tmp_path: Path) -> None:
        """When fdopen/write raises, the tmp file is deleted."""
        target = tmp_path / "fail.txt"
        tmp_names: list[str] = []

        original_mkstemp = tempfile.mkstemp

        def capturing_mkstemp(**kwargs):
            fd, name = original_mkstemp(**kwargs)
            tmp_names.append(name)
            return fd, name

        with patch("apm_cli.utils.atomic_io.tempfile.mkstemp", side_effect=capturing_mkstemp):
            with patch(
                "apm_cli.utils.atomic_io.os.fdopen",
                side_effect=OSError("disk full"),
            ):
                with pytest.raises(OSError, match="disk full"):
                    atomic_write_text(target, "data")

        # Tmp file must not remain after failure
        if tmp_names:
            assert not Path(tmp_names[0]).exists()

    def test_exception_propagates_even_when_unlink_fails(self, tmp_path: Path) -> None:
        """If cleanup unlink fails, the original write exception still propagates."""
        target = tmp_path / "fail_unlink.txt"

        with patch("apm_cli.utils.atomic_io.os.fdopen", side_effect=RuntimeError("boom")):
            with patch("apm_cli.utils.atomic_io.os.unlink", side_effect=OSError("locked")):
                with pytest.raises(RuntimeError, match="boom"):
                    atomic_write_text(target, "data")

    def test_target_not_written_when_exception_occurs(self, tmp_path: Path) -> None:
        """If write fails, the target path must not be created."""
        target = tmp_path / "ghost.txt"

        with patch("apm_cli.utils.atomic_io.os.fdopen", side_effect=OSError("nope")):
            with pytest.raises(OSError):
                atomic_write_text(target, "data")

        assert not target.exists()

    def test_lock_replace_fault_is_narrow_and_preserves_original(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The installed-binary seam fails only lock replacement on every OS."""
        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text("original\n", encoding="ascii")
        other_path = tmp_path / "other.yaml"
        monkeypatch.setenv("APM_TEST_FAIL_LOCK_REPLACE", "1")

        with pytest.raises(OSError, match="lockfile replace blocked by test"):
            atomic_write_text(lock_path, "replacement\n")
        atomic_write_text(other_path, "allowed\n")

        assert lock_path.read_text(encoding="ascii") == "original\n"
        assert other_path.read_text(encoding="ascii") == "allowed\n"
        assert list(tmp_path.glob("apm-atomic-*")) == []

    # ------------------------------------------------------------------
    # Deterministic LF line endings (cross-platform hash stability)
    # ------------------------------------------------------------------

    def test_crlf_normalized_to_lf(self, tmp_path: Path) -> None:
        """CRLF input is written as LF so on-disk bytes are OS-independent."""
        target = tmp_path / "crlf.txt"
        atomic_write_text(target, "a\r\nb\r\nc")
        assert target.read_bytes() == b"a\nb\nc"

    def test_lf_input_unchanged(self, tmp_path: Path) -> None:
        """LF input round-trips without gaining a carriage return."""
        target = tmp_path / "lf.txt"
        atomic_write_text(target, "a\nb\n")
        assert target.read_bytes() == b"a\nb\n"

    def test_bare_cr_input_unchanged(self, tmp_path: Path) -> None:
        """Bare CR round-trips; only CRLF pairs are normalized."""
        target = tmp_path / "bare-cr.txt"
        atomic_write_text(target, "a\rb\n")
        assert target.read_bytes() == b"a\rb\n"


class TestWriteTextLf:
    """Tests for write_text_lf() (non-atomic, LF-normalizing writer)."""

    def test_creates_file_with_lf(self, tmp_path: Path) -> None:
        """Content is written and CRLF is normalized to LF."""
        target = tmp_path / "out.md"
        write_text_lf(target, "a\r\nb\r\n")
        assert target.read_bytes() == b"a\nb\n"

    def test_lf_input_unchanged(self, tmp_path: Path) -> None:
        """LF-only input is left byte-for-byte intact."""
        target = tmp_path / "out.md"
        write_text_lf(target, "# H\n\ntext\n")
        assert target.read_bytes() == b"# H\n\ntext\n"

    def test_bare_cr_input_unchanged(self, tmp_path: Path) -> None:
        """Bare CR is not normalized so drift-normalizer semantics match."""
        target = tmp_path / "bare-cr.md"
        write_text_lf(target, "a\rb\n")
        assert target.read_bytes() == b"a\rb\n"

    def test_unicode_round_trips(self, tmp_path: Path) -> None:
        """Non-ASCII content survives the UTF-8 + LF write."""
        target = tmp_path / "u.md"
        write_text_lf(target, "café ☕ 日本語\r\n")
        assert target.read_text(encoding="utf-8") == "café ☕ 日本語\n"


class TestAtomicWriteHardening:
    """apm#2619 review hardening: mode preservation, interrupt cleanup, AV retry."""

    @pytest.mark.skipif(
        os.name == "nt" or not hasattr(os, "fchmod"),
        reason="POSIX mode-bit semantics (Windows chmod only toggles read-only)",
    )
    def test_existing_file_mode_is_preserved(self, tmp_path: Path) -> None:
        """os.replace swaps inodes, so the temp must inherit the target's mode.

        Without this, every rewrite of an existing file silently downgrades
        it to mkstemp's 0600 -- breaking multi-uid readers of apm_modules.
        """
        target = tmp_path / "apm.yml"
        target.write_bytes(b"name: foo\n")
        os.chmod(target, 0o644)

        atomic_write_text(target, "name: bar\n")

        assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
        assert target.read_bytes() == b"name: bar\n"

    def test_keyboard_interrupt_mid_write_removes_temp_file(self, tmp_path: Path) -> None:
        """Ctrl+C mid-write must not strand an apm-atomic-* temp file.

        When the destination directory is an installed package tree
        (synthetic apm.yml, inline hooks.json -- apm#2619), a stranded temp
        would be swept into compute_package_hash and permanently poison the
        recorded hash. BaseException (not just Exception) must trigger the
        cleanup path.
        """
        target = tmp_path / "apm.yml"
        target.write_bytes(b"name: foo\n")

        with (
            patch(
                "apm_cli.utils.atomic_io._replace_atomic_file",
                side_effect=KeyboardInterrupt,
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            atomic_write_text(target, "name: bar\n")

        assert target.read_bytes() == b"name: foo\n"  # original intact
        strays = list(tmp_path.glob("apm-atomic-*"))
        assert strays == [], strays

    def test_windows_replace_retries_transient_permission_error(self, tmp_path: Path) -> None:
        """A transient PermissionError (AV/indexer holding the target) is retried."""
        target = tmp_path / "apm.yml"
        real_replace = os.replace
        failures = {"remaining": 2}

        def flaky_replace(src: object, dst: object) -> None:
            if failures["remaining"] > 0:
                failures["remaining"] -= 1
                raise PermissionError("sharing violation")
            real_replace(src, dst)

        with (
            patch("apm_cli.utils.atomic_io._IS_WINDOWS", True),
            patch("apm_cli.utils.atomic_io.os.replace", side_effect=flaky_replace),
            patch("apm_cli.utils.atomic_io.time.sleep") as fake_sleep,
        ):
            atomic_write_text(target, "name: bar\n")

        assert target.read_bytes() == b"name: bar\n"
        assert failures["remaining"] == 0
        assert fake_sleep.call_count == 2

    def test_windows_replace_gives_up_after_bounded_retries(self, tmp_path: Path) -> None:
        """A persistent PermissionError still surfaces (bounded retry, no hang)."""
        target = tmp_path / "apm.yml"

        with (
            patch("apm_cli.utils.atomic_io._IS_WINDOWS", True),
            patch(
                "apm_cli.utils.atomic_io.os.replace",
                side_effect=PermissionError("sharing violation"),
            ),
            patch("apm_cli.utils.atomic_io.time.sleep"),
            pytest.raises(PermissionError),
        ):
            atomic_write_text(target, "name: bar\n")

        strays = list(tmp_path.glob("apm-atomic-*"))
        assert strays == [], strays
