import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mas.config import ConfigWatcher, load_config


class _LazyCheckReload:
    _fn = None
    
    @classmethod
    def get(cls):
        if cls._fn is None:
            from mas.daemon import _check_reload_config
            cls._fn = _check_reload_config
        return cls._fn


class _LazyRunLoop:
    _fn = None
    
    @classmethod
    def get(cls):
        if cls._fn is None:
            from mas.daemon import _run_loop
            cls._fn = _run_loop
        return cls._fn


@pytest.fixture
def mas(tmp_path: Path) -> Path:
    user_cfg = tmp_path / "user_config"
    user_cfg.mkdir()
    (user_cfg / "config.yaml").write_text("providers: {}\nroles: {}\n")
    
    with patch("mas.config.USER_CONFIG_DIR", user_cfg):
        d = tmp_path / ".mas"
        d.mkdir(parents=True)
        (d / "logs").mkdir()
        (d / "prompts").mkdir()
        (d / "config.yaml").write_text(
            "providers:\n"
            "  mock:\n"
            "    cli: sh\n"
            "    max_concurrent: 1\n"
            "    extra_args: []\n"
        )
        (d / "roles.yaml").write_text(
            "roles:\n"
            "  proposer: {provider: mock}\n"
            "  orchestrator: {provider: mock}\n"
            "  implementer: {provider: mock}\n"
            "  tester: {provider: mock}\n"
            "  evaluator: {provider: mock}\n"
        )
        for role in ("proposer", "orchestrator", "implementer", "tester", "evaluator"):
            (d / "prompts" / f"{role}.md").write_text("goal=$goal")
        yield d


class TestConfigWatcher:
    """Tests for ConfigWatcher that tracks config.yaml mtime."""

    def test_watcher_returns_false_on_first_check(self, mas):
        watcher = ConfigWatcher(mas / "config.yaml")
        assert watcher.has_changed() is False

    def test_watcher_returns_true_after_file_modified(self, mas):
        watcher = ConfigWatcher(mas / "config.yaml")
        assert watcher.has_changed() is False
        watcher.mark_checked()
        assert watcher.has_changed() is False

        time.sleep(0.01)
        (mas / "config.yaml").write_text(
            "providers:\n"
            "  mock:\n"
            "    cli: sh\n"
            "    max_concurrent: 2\n"
            "    extra_args: []\n"
        )
        assert watcher.has_changed() is True

    def test_watcher_returns_false_after_mark_checked(self, mas):
        watcher = ConfigWatcher(mas / "config.yaml")
        time.sleep(0.01)
        (mas / "config.yaml").write_text(
            "providers:\n"
            "  mock:\n"
            "    cli: sh\n"
            "    max_concurrent: 2\n"
            "    extra_args: []\n"
        )
        assert watcher.has_changed() is True
        watcher.mark_checked()
        assert watcher.has_changed() is False


class TestDaemonConfigReload:
    """Tests for config hot-reload in daemon._run_loop."""

    def test_successful_reload_applies_new_config(self, mas):
        """When config.yaml is modified with valid content, daemon reloads it."""
        _check_reload_config = _LazyCheckReload.get()
        
        old_config = load_config(mas)
        assert old_config.providers["mock"].max_concurrent == 1

        (mas / "config.yaml").write_text(
            "providers:\n"
            "  mock:\n"
            "    cli: sh\n"
            "    max_concurrent: 2\n"
            "    extra_args: []\n"
        )

        new_config, changes = _check_reload_config(mas.parent, old_config)

        assert new_config.providers["mock"].max_concurrent == 2
        assert len(changes) > 0

    def test_reload_log_includes_changed_settings(self, mas):
        """Log output includes which settings changed (e.g. provider max_concurrent)."""
        _check_reload_config = _LazyCheckReload.get()
        
        old_config = load_config(mas)

        (mas / "config.yaml").write_text(
            "providers:\n"
            "  mock:\n"
            "    cli: sh\n"
            "    max_concurrent: 2\n"
            "    extra_args: []\n"
        )

        new_config, changes = _check_reload_config(mas.parent, old_config)

        field_change = next(
            (c for c in changes if c[0] == "providers.mock.max_concurrent"), None
        )
        assert field_change is not None
        assert field_change[1] == "1"
        assert field_change[2] == "2"

    def test_invalid_yaml_falls_back_to_previous_config(self, mas):
        """When config.yaml has invalid YAML, daemon continues with previous config."""
        _check_reload_config = _LazyCheckReload.get()
        
        old_config = load_config(mas)

        (mas / "config.yaml").write_text(
            "providers:\n"
            "  mock:\n"
            "    cli: sh\n"
            "    max_concurrent: 1\n"
            "invalid yaml: [broken\n"
        )

        new_config, changes = _check_reload_config(mas.parent, old_config)

        assert new_config.providers["mock"].max_concurrent == old_config.providers["mock"].max_concurrent

    def test_invalid_schema_falls_back_to_previous_config(self, mas):
        """When config.yaml has invalid schema, daemon continues with previous config."""
        _check_reload_config = _LazyCheckReload.get()
        
        old_config = load_config(mas)

        (mas / "config.yaml").write_text(
            "providers:\n"
            "  nonexistent_role:\n"
            "    cli: sh\n"
            "    max_concurrent: 1\n"
            "    extra_args: []\n"
        )

        new_config, changes = _check_reload_config(mas.parent, old_config)

        assert new_config.providers["mock"].max_concurrent == old_config.providers["mock"].max_concurrent


class TestDaemonLoopConfigReload:
    """Integration test: daemon loop applies new config during execution."""

    def test_run_loop_reloads_config_between_ticks(self, mas):
        """daemon picks up new config on next tick and logs the reload."""
        _run_loop = _LazyRunLoop.get()
        
        reload_count = [0]

        def mock_run_tick(start):
            pass

        stop_flag = {"stop": False}

        class FakeConfigWatcher:
            def __init__(self, path):
                self.config_path = path
                self._last_mtime = None

            def has_changed(self):
                return reload_count[0] < 1

            def mark_checked(self):
                reload_count[0] += 1

        with patch("mas.daemon.ConfigWatcher", FakeConfigWatcher):
            with patch("mas.daemon._check_reload_config") as mock_reload:
                mock_config = MagicMock()
                mock_config.providers["mock"].max_concurrent = 2
                mock_reload.return_value = (mock_config, [("providers.mock.max_concurrent", "1", "2")])

                with patch("mas.tick.run_tick", side_effect=mock_run_tick):
                    def fake_sleep(seconds):
                        if reload_count[0] >= 1:
                            stop_flag["stop"] = True

                    with patch("mas.daemon.time.sleep", side_effect=fake_sleep):
                        with patch("mas.daemon.log") as mock_log:
                            _run_loop(mas.parent, 1, stop_flag)

                            mock_reload.assert_called()
                            call_args = [c for c in mock_log.info.call_args_list]
                            reload_log = next(
                                (c for c in call_args if c[0][0] == "config_reloaded"), None
                            )
                            assert reload_log is not None, (
                                f"log.info('config_reloaded', ...) not found; calls: {[c[0][0] for c in call_args]}"
                            )
                            extra = reload_log[1].get("extra", {})
                            assert extra.get("event") == "config_reloaded"
                            assert isinstance(extra.get("changes"), list)
                            for change in extra.get("changes", []):
                                assert isinstance(change, dict)
                                assert "field" in change and "old" in change and "new" in change