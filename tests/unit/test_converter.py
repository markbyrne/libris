"""Tests for libris.audio.converter helpers — disk space pre-check (Issue #30)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from libris.audio.converter import _check_disk_space, _choose_tmp_dir, _fmt_bytes
from libris.exceptions import ConversionError

# ---------------------------------------------------------------------------
# _fmt_bytes
# ---------------------------------------------------------------------------

class TestFmtBytes:
    def test_gb_range(self):
        assert _fmt_bytes(2 * 1024 ** 3) == "2.0 GB"
        assert _fmt_bytes(int(1.5 * 1024 ** 3)) == "1.5 GB"

    def test_mb_range(self):
        assert _fmt_bytes(512 * 1024 ** 2) == "512.0 MB"
        assert _fmt_bytes(1 * 1024 ** 2) == "1.0 MB"

    def test_boundary_exactly_1gb(self):
        result = _fmt_bytes(1024 ** 3)
        assert "GB" in result


# ---------------------------------------------------------------------------
# _check_disk_space
# ---------------------------------------------------------------------------

class TestCheckDiskSpace:
    def _make_parts(self, tmp_path: Path, sizes: list[int]) -> list[Path]:
        parts = []
        for i, size in enumerate(sizes):
            p = tmp_path / f"part{i:02d}.m4b"
            p.write_bytes(b"x" * size)
            parts.append(p)
        return parts

    def test_sufficient_space_does_not_raise(self, tmp_path):
        """When there is plenty of space, _check_disk_space returns without raising."""
        parts = self._make_parts(tmp_path, [1024, 1024])  # 2 KB total — trivial
        output = tmp_path / "out.m4b"

        huge = MagicMock()
        huge.free = 10 * 1024 ** 3  # 10 GB free everywhere
        with patch("libris.audio.converter.shutil.disk_usage", return_value=huge):
            _check_disk_space(parts, output)  # must not raise

    def test_insufficient_tmp_raises(self, tmp_path):
        """When the temp dir is too small, ConversionError is raised."""
        parts = self._make_parts(tmp_path, [100 * 1024 ** 2])  # 100 MB part
        output = tmp_path / "out.m4b"

        tight = MagicMock()
        tight.free = 50 * 1024 ** 2  # only 50 MB free — not enough for 2.1× = 210 MB
        with patch("libris.audio.converter.shutil.disk_usage", return_value=tight):
            with pytest.raises(ConversionError, match="Insufficient disk space"):
                _check_disk_space(parts, output)

    def test_insufficient_output_dir_raises(self, tmp_path):
        """When the output directory is too small, ConversionError is raised."""
        parts = self._make_parts(tmp_path, [100 * 1024 ** 2])  # 100 MB part
        output = tmp_path / "out.m4b"

        call_count = 0

        def _fake_usage(path):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            # First call = temp dir (plenty of space)
            # Second call = output dir (tight)
            result.free = 10 * 1024 ** 3 if call_count == 1 else 50 * 1024 ** 2
            return result

        with patch("libris.audio.converter.shutil.disk_usage", side_effect=_fake_usage):
            with pytest.raises(ConversionError, match="Insufficient disk space"):
                _check_disk_space(parts, output)

    def test_error_message_contains_human_readable_sizes(self, tmp_path):
        """The ConversionError message includes GB or MB size strings."""
        parts = self._make_parts(tmp_path, [2 * 1024 ** 3])  # 2 GB part
        output = tmp_path / "out.m4b"

        tiny = MagicMock()
        tiny.free = 1024 ** 2  # 1 MB — clearly insufficient
        with patch("libris.audio.converter.shutil.disk_usage", return_value=tiny):
            with pytest.raises(ConversionError) as exc_info:
                _check_disk_space(parts, output)
            msg = str(exc_info.value)
            assert "GB" in msg or "MB" in msg

    def test_error_message_names_temp_dir(self, tmp_path):
        """When the temp dir is the bottleneck, the message mentions 'temp dir'."""
        parts = self._make_parts(tmp_path, [100 * 1024 ** 2])
        output = tmp_path / "out.m4b"

        tight = MagicMock()
        tight.free = 1 * 1024 ** 2
        with patch("libris.audio.converter.shutil.disk_usage", return_value=tight):
            with pytest.raises(ConversionError) as exc_info:
                _check_disk_space(parts, output)
            assert "temp dir" in str(exc_info.value)

    def test_missing_part_files_excluded_from_size(self, tmp_path):
        """Parts that don't exist on disk are ignored in the size calculation."""
        existing = tmp_path / "part00.m4b"
        existing.write_bytes(b"x" * 1024)
        missing = tmp_path / "part01.m4b"  # not created
        output = tmp_path / "out.m4b"

        huge = MagicMock()
        huge.free = 10 * 1024 ** 3
        with patch("libris.audio.converter.shutil.disk_usage", return_value=huge):
            # Should not raise (missing file doesn't inflate the required size)
            _check_disk_space([existing, missing], output)


# ---------------------------------------------------------------------------
# _choose_tmp_dir
# ---------------------------------------------------------------------------

class TestChooseTmpDir:
    """Tests for the smart temp-dir selection with /tmp fallback."""

    def _make_parts(self, tmp_path: Path, sizes: list[int]) -> list[Path]:
        parts = []
        for i, size in enumerate(sizes):
            p = tmp_path / f"part{i:02d}.m4b"
            p.write_bytes(b"x" * size)
            parts.append(p)
        return parts

    def test_uses_system_tmp_when_sufficient(self, tmp_path):
        """Returns system temp dir when it has enough free space."""
        import tempfile
        parts = self._make_parts(tmp_path, [100 * 1024 ** 2])  # 100 MB
        output = tmp_path / "out.m4b"

        huge = MagicMock(free=10 * 1024 ** 3)  # 10 GB everywhere
        with patch("libris.audio.converter.shutil.disk_usage", return_value=huge):
            result = _choose_tmp_dir(parts, output)
        assert result == Path(tempfile.gettempdir())

    def test_falls_back_to_output_dir_when_tmp_tight(self, tmp_path):
        """Falls back to output_path.parent when /tmp is tight but output dir has room."""
        parts = self._make_parts(tmp_path, [100 * 1024 ** 2])  # 100 MB
        output = tmp_path / "subdir" / "out.m4b"
        output.parent.mkdir(parents=True, exist_ok=True)

        call_count = 0

        def _fake_usage(path):
            nonlocal call_count
            call_count += 1
            m = MagicMock()
            # First call = sys_tmp (tight), second call = out_dir (plenty)
            m.free = 50 * 1024 ** 2 if call_count == 1 else 10 * 1024 ** 3
            return m

        with patch("libris.audio.converter.shutil.disk_usage", side_effect=_fake_usage):
            result = _choose_tmp_dir(parts, output)
        assert result == output.parent

    def test_raises_when_both_dirs_insufficient(self, tmp_path):
        """ConversionError with TMPDIR hint when neither location has enough space."""
        parts = self._make_parts(tmp_path, [100 * 1024 ** 2])  # 100 MB
        output = tmp_path / "out.m4b"

        tiny = MagicMock(free=50 * 1024 ** 2)  # 50 MB everywhere — not enough
        with patch("libris.audio.converter.shutil.disk_usage", return_value=tiny):
            with pytest.raises(ConversionError, match="TMPDIR"):
                _choose_tmp_dir(parts, output)

    def test_error_message_includes_tmpdir_hint(self, tmp_path):
        """The ConversionError message includes a TMPDIR usage example."""
        parts = self._make_parts(tmp_path, [500 * 1024 ** 2])  # 500 MB
        output = tmp_path / "out.m4b"

        tiny = MagicMock(free=10 * 1024 ** 2)  # 10 MB — clearly not enough
        with patch("libris.audio.converter.shutil.disk_usage", return_value=tiny):
            with pytest.raises(ConversionError) as exc_info:
                _choose_tmp_dir(parts, output)
        assert "TMPDIR" in str(exc_info.value)
        assert "temp dir" in str(exc_info.value)
