"""Tests for libris.config — YAML loading and env var overrides."""

import textwrap
from pathlib import Path

import pytest

from libris.config import load_config
from libris.exceptions import ConfigError


@pytest.fixture
def minimal_yaml(tmp_path):
    """Write a minimal valid config YAML and return the path."""
    lib = tmp_path / "calibre-library"
    lib.mkdir()
    content = textwrap.dedent(f"""
        watcher:
          incoming_dir: {tmp_path}/incoming
        paths:
          staging_dir: {tmp_path}/staging
          review_dir: {tmp_path}/review
          failed_dir: {tmp_path}/failed
          state_db: {tmp_path}/state.db
        calibre:
          mode: local
          library_path: {lib}
        metadata:
          confidence_threshold: 0.75
        output:
          preferred_ebook_format: epub
          embed_cover_art: false
        ntfy:
          topic: testpipeline
        log_level: INFO
    """)
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return p, tmp_path


class TestLoadConfig:
    def test_minimal_valid_config(self, minimal_yaml):
        path, tmp = minimal_yaml
        config = load_config(path)
        assert config.watcher.incoming_dir == tmp / "incoming"
        assert config.calibre.mode == "local"
        assert config.metadata.confidence_threshold == 0.75
        assert config.ntfy.topic == "testpipeline"
        assert config.log_level == "INFO"

    def test_missing_config_file_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nonexistent.yaml")

    def test_missing_incoming_dir_raises(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        content = textwrap.dedent(f"""
            paths:
              staging_dir: {tmp_path}/staging
              review_dir: {tmp_path}/review
              failed_dir: {tmp_path}/failed
              state_db: {tmp_path}/state.db
            calibre:
              mode: local
              library_path: {lib}
        """)
        p = tmp_path / "bad.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="incoming_dir"):
            load_config(p)

    def test_invalid_calibre_mode_raises(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        content = textwrap.dedent(f"""
            watcher:
              incoming_dir: {tmp_path}/incoming
            paths:
              staging_dir: {tmp_path}/staging
              review_dir: {tmp_path}/review
              failed_dir: {tmp_path}/failed
              state_db: {tmp_path}/state.db
            calibre:
              mode: nfs
              library_path: {lib}
        """)
        p = tmp_path / "bad.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="local.*docker"):

            load_config(p)

    def test_invalid_threshold_raises(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        content = textwrap.dedent(f"""
            watcher:
              incoming_dir: {tmp_path}/incoming
            paths:
              staging_dir: {tmp_path}/staging
              review_dir: {tmp_path}/review
              failed_dir: {tmp_path}/failed
              state_db: {tmp_path}/state.db
            calibre:
              mode: local
              library_path: {lib}
            metadata:
              confidence_threshold: 1.5
        """)
        p = tmp_path / "bad.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="confidence_threshold"):
            load_config(p)


class TestEnvOverrides:
    def test_calibre_mode_override(self, minimal_yaml, monkeypatch):
        path, _ = minimal_yaml
        monkeypatch.setenv("LIBRIS_CALIBRE_MODE", "docker")
        monkeypatch.setenv("LIBRIS_CALIBRE_DOCKER_CONTAINER", "my-calibre")
        config = load_config(path)
        assert config.calibre.mode == "docker"
        assert config.calibre.docker_container == "my-calibre"

    def test_threshold_override(self, minimal_yaml, monkeypatch):
        path, _ = minimal_yaml
        monkeypatch.setenv("LIBRIS_METADATA_CONFIDENCE_THRESHOLD", "0.90")
        config = load_config(path)
        assert config.metadata.confidence_threshold == pytest.approx(0.90)

    def test_ntfy_topic_override(self, minimal_yaml, monkeypatch):
        path, _ = minimal_yaml
        monkeypatch.setenv("LIBRIS_NTFY_TOPIC", "overridden-topic")
        config = load_config(path)
        assert config.ntfy.topic == "overridden-topic"

    def test_log_level_override(self, minimal_yaml, monkeypatch):
        path, _ = minimal_yaml
        monkeypatch.setenv("LIBRIS_LOG_LEVEL", "DEBUG")
        config = load_config(path)
        assert config.log_level == "DEBUG"

    def test_mock_mode_override_truthy_values(self, minimal_yaml, monkeypatch):
        path, _ = minimal_yaml
        for truthy in ("1", "true", "yes", "on", "True"):
            monkeypatch.setenv("LIBRIS_METADATA_MOCK_MODE", truthy)
            config = load_config(path)
            assert config.metadata.mock_mode is True

    def test_mock_mode_override_falsy_values(self, minimal_yaml, monkeypatch):
        path, _ = minimal_yaml
        for falsy in ("0", "false", "no", "off"):
            monkeypatch.setenv("LIBRIS_METADATA_MOCK_MODE", falsy)
            config = load_config(path)
            assert config.metadata.mock_mode is False

    def test_library_db_path_env_override(self, minimal_yaml, monkeypatch, tmp_path):
        path, _ = minimal_yaml
        new_lib = tmp_path / "new-library"
        new_lib.mkdir()
        monkeypatch.setenv("LIBRIS_CALIBRE_LIBRARY_DB_PATH", str(new_lib))
        config = load_config(path)
        assert config.calibre.library_db_path == new_lib

    def test_book_file_path_env_override(self, minimal_yaml, monkeypatch, tmp_path):
        path, _ = minimal_yaml
        books = tmp_path / "books"
        books.mkdir()
        monkeypatch.setenv("LIBRIS_CALIBRE_BOOK_FILE_PATH", str(books))
        config = load_config(path)
        assert config.calibre.book_file_path == books
        assert config.calibre.effective_book_path == books


class TestSplitLibraryPaths:
    """Tests for Issue #18: library_db_path / book_file_path split-path config."""

    def _write_config(self, tmp_path, content: str) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(content)
        return p

    def test_legacy_library_path_key_still_works(self, tmp_path):
        """Existing configs using 'library_path' must continue to load without changes."""
        lib = tmp_path / "calibre"
        lib.mkdir()
        p = self._write_config(tmp_path, f"""
watcher:
  incoming_dir: {tmp_path}/incoming
paths:
  staging_dir: {tmp_path}/staging
  review_dir: {tmp_path}/review
  failed_dir: {tmp_path}/failed
  state_db: {tmp_path}/state.db
calibre:
  mode: local
  library_path: {lib}
""")
        config = load_config(p)
        assert config.calibre.library_db_path == lib
        assert config.calibre.library_path == lib       # backward-compat property
        assert config.calibre.book_file_path is None
        assert config.calibre.effective_book_path == lib  # falls back to library_db_path

    def test_new_library_db_path_key(self, tmp_path):
        """New configs using 'library_db_path' should load and expose correct fields."""
        lib = tmp_path / "calibre-db"
        lib.mkdir()
        p = self._write_config(tmp_path, f"""
watcher:
  incoming_dir: {tmp_path}/incoming
paths:
  staging_dir: {tmp_path}/staging
  review_dir: {tmp_path}/review
  failed_dir: {tmp_path}/failed
  state_db: {tmp_path}/state.db
calibre:
  mode: local
  library_db_path: {lib}
""")
        config = load_config(p)
        assert config.calibre.library_db_path == lib
        assert config.calibre.library_path == lib       # backward-compat property
        assert config.calibre.book_file_path is None
        assert config.calibre.effective_book_path == lib

    def test_split_paths_both_configured(self, tmp_path):
        """When both library_db_path and book_file_path are set, effective_book_path = book_file_path."""
        lib = tmp_path / "calibre-db"
        books = tmp_path / "books"
        lib.mkdir()
        books.mkdir()
        p = self._write_config(tmp_path, f"""
watcher:
  incoming_dir: {tmp_path}/incoming
paths:
  staging_dir: {tmp_path}/staging
  review_dir: {tmp_path}/review
  failed_dir: {tmp_path}/failed
  state_db: {tmp_path}/state.db
calibre:
  mode: local
  library_db_path: {lib}
  book_file_path: {books}
""")
        config = load_config(p)
        assert config.calibre.library_db_path == lib
        assert config.calibre.book_file_path == books
        assert config.calibre.effective_book_path == books   # uses book_file_path

    def test_missing_library_path_raises(self, tmp_path):
        """local mode with no library_db_path or library_path should raise ConfigError."""
        p = self._write_config(tmp_path, f"""
watcher:
  incoming_dir: {tmp_path}/incoming
paths:
  staging_dir: {tmp_path}/staging
  review_dir: {tmp_path}/review
  failed_dir: {tmp_path}/failed
  state_db: {tmp_path}/state.db
calibre:
  mode: local
""")
        with pytest.raises(ConfigError, match="library_db_path"):
            load_config(p)

    def test_library_db_path_overrides_library_path_when_both_present(self, tmp_path):
        """library_db_path takes precedence over library_path when both are in YAML."""
        lib_new = tmp_path / "new-lib"
        lib_old = tmp_path / "old-lib"
        lib_new.mkdir()
        lib_old.mkdir()
        p = self._write_config(tmp_path, f"""
watcher:
  incoming_dir: {tmp_path}/incoming
paths:
  staging_dir: {tmp_path}/staging
  review_dir: {tmp_path}/review
  failed_dir: {tmp_path}/failed
  state_db: {tmp_path}/state.db
calibre:
  mode: local
  library_db_path: {lib_new}
  library_path: {lib_old}
""")
        config = load_config(p)
        # library_db_path wins when both are present
        assert config.calibre.library_db_path == lib_new
