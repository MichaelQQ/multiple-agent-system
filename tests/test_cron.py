from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mas import cron


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


class TestGetCrontab:
    @patch("mas.cron.subprocess.run")
    def test_normal_read(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="* * * * * echo hello", stderr=""
        )
        result = cron._get_crontab()
        assert result == "* * * * * echo hello"
        mock_run.assert_called_once()

    @patch("mas.cron.subprocess.run")
    def test_empty_crontab_no_stderr(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no crontab for user")
        result = cron._get_crontab()
        assert result == ""
        mock_run.assert_called_once()

    @patch("mas.cron.subprocess.run")
    def test_no_crontab_stderr_case_insensitive(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="NO CRONTAB FOR USER")
        result = cron._get_crontab()
        assert result == ""
        mock_run.assert_called_once()

    @patch("mas.cron.subprocess.run")
    def test_unknown_error_returns_empty_string(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="some error message"
        )
        result = cron._get_crontab()
        assert result == ""
        mock_run.assert_called_once()

    @patch("mas.cron.subprocess.run")
    def test_permission_error_returns_empty_string(self, mock_run: MagicMock):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "crontab", output="", stderr="Permission denied"
        )
        result = cron._get_crontab()
        assert result == ""
        mock_run.assert_called_once()


class TestSetCrontab:
    @patch("mas.cron.subprocess.run")
    def test_passes_content_via_stdin(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=0)
        cron._set_crontab("* * * * * echo hello")
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["crontab", "-"]
        assert call_args[1]["input"] == "* * * * * echo hello"
        assert call_args[1]["text"] is True

    @patch("mas.cron.subprocess.run")
    def test_raises_on_failure(self, mock_run: MagicMock):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "crontab -", output="", stderr="error"
        )
        with pytest.raises(subprocess.CalledProcessError):
            cron._set_crontab("some content")


class TestResolveMas:
    @patch("mas.cron.Path")
    @patch("mas.cron.sys")
    def test_absolute_path_exists(self, mock_sys: MagicMock, mock_path_cls: MagicMock):
        mock_sys.argv = ["/usr/local/bin/mas"]
        mock_sys.executable = "/usr/bin/python3"
        mock_path_instance = MagicMock(spec=Path)
        mock_path_instance.is_absolute.return_value = True
        mock_path_instance.exists.return_value = True
        mock_path_instance.__str__ = MagicMock(return_value="/usr/local/bin/mas")
        mock_path_cls.return_value = mock_path_instance
        result = cron._resolve_mas()
        assert result == "/usr/local/bin/mas"

    @patch("mas.cron.shutil.which")
    @patch("mas.cron.sys")
    def test_relative_resolved_via_which(self, mock_sys: MagicMock, mock_which: MagicMock):
        mock_sys.argv = ["mas"]
        mock_sys.executable = "/usr/bin/python3"
        mock_which.return_value = "/usr/local/bin/mas"
        result = cron._resolve_mas()
        assert result == "/usr/local/bin/mas"

    @patch("mas.cron.shutil.which")
    @patch("mas.cron.sys")
    def test_fallback_to_sys_executable(self, mock_sys: MagicMock, mock_which: MagicMock):
        mock_sys.argv = []
        mock_sys.executable = "/usr/bin/python3"
        mock_which.return_value = None
        result = cron._resolve_mas()
        assert result == "/usr/bin/python3 -m mas.cli"


class TestBlock:
    @patch("mas.cron._resolve_mas")
    @patch("mas.cron._ident")
    def test_produces_correct_5_field_schedule(
        self, mock_ident: MagicMock, mock_resolve: MagicMock, tmp_project: Path
    ):
        mock_ident.return_value = "abc12345"
        mock_resolve.return_value = "/usr/bin/mas"
        block = cron._block(tmp_project, 5)
        assert "*/5 * * * *" in block

    @patch("mas.cron._resolve_mas")
    @patch("mas.cron._ident")
    def test_produces_correct_schedule_15_minutes(
        self, mock_ident: MagicMock, mock_resolve: MagicMock, tmp_project: Path
    ):
        mock_ident.return_value = "abc12345"
        mock_resolve.return_value = "/usr/bin/mas"
        block = cron._block(tmp_project, 15)
        assert "*/15 * * * *" in block

    @patch("mas.cron._resolve_mas")
    @patch("mas.cron._ident")
    def test_contains_cd_command(
        self, mock_ident: MagicMock, mock_resolve: MagicMock, tmp_project: Path
    ):
        mock_ident.return_value = "abc12345"
        mock_resolve.return_value = "/usr/bin/mas"
        block = cron._block(tmp_project, 5)
        resolved_path = str(tmp_project.resolve())
        assert f"cd {resolved_path}" in block

    @patch("mas.cron._resolve_mas")
    @patch("mas.cron._ident")
    def test_contains_begin_marker(
        self, mock_ident: MagicMock, mock_resolve: MagicMock, tmp_project: Path
    ):
        mock_ident.return_value = "abc12345"
        mock_resolve.return_value = "/usr/bin/mas"
        block = cron._block(tmp_project, 5)
        assert "# >>> mas-cron abc12345 >>>" in block

    @patch("mas.cron._resolve_mas")
    @patch("mas.cron._ident")
    def test_contains_end_marker(
        self, mock_ident: MagicMock, mock_resolve: MagicMock, tmp_project: Path
    ):
        mock_ident.return_value = "abc12345"
        mock_resolve.return_value = "/usr/bin/mas"
        block = cron._block(tmp_project, 5)
        assert "# <<< mas-cron abc12345 <<<" in block


class TestIdent:
    def test_deterministic_hash(self, tmp_project: Path):
        result1 = cron._ident(tmp_project)
        result2 = cron._ident(tmp_project)
        assert result1 == result2

    def test_different_paths_different_hashes(self, tmp_path: Path):
        p1 = tmp_path / "project1"
        p1.mkdir()
        p2 = tmp_path / "project2"
        p2.mkdir()
        result1 = cron._ident(p1)
        result2 = cron._ident(p2)
        assert result1 != result2

    def test_hash_is_8_characters(self, tmp_project: Path):
        result = cron._ident(tmp_project)
        assert len(result) == 8


class TestInstall:
    @patch("mas.cron._get_crontab")
    @patch("mas.cron._set_crontab")
    @patch("mas.cron._ident")
    def test_fresh_install_appends_block(
        self,
        mock_ident: MagicMock,
        mock_set: MagicMock,
        mock_get: MagicMock,
        tmp_project: Path,
    ):
        mock_ident.return_value = "abc12345"
        mock_get.return_value = ""
        cron.install(tmp_project, 5)
        mock_set.assert_called_once()
        call_content = mock_set.call_args[0][0]
        assert "# >>> mas-cron abc12345 >>>" in call_content

    @patch("mas.cron._get_crontab")
    @patch("mas.cron._set_crontab")
    @patch("mas.cron._ident")
    @patch("mas.cron.uninstall")
    def test_reinstall_calls_uninstall_first(
        self,
        mock_uninstall: MagicMock,
        mock_ident: MagicMock,
        mock_set: MagicMock,
        mock_get: MagicMock,
        tmp_project: Path,
    ):
        mock_ident.return_value = "abc12345"
        mock_get.return_value = "# >>> mas-cron abc12345 >>>\n*/5 * * * * echo test\n# <<< mas-cron abc12345 <<<"
        cron.install(tmp_project, 5)
        mock_uninstall.assert_called_once_with(tmp_project)

    @patch("mas.cron._get_crontab")
    @patch("mas.cron._set_crontab")
    @patch("mas.cron._ident")
    def test_default_interval_is_5_minutes(
        self,
        mock_ident: MagicMock,
        mock_set: MagicMock,
        mock_get: MagicMock,
        tmp_project: Path,
    ):
        mock_ident.return_value = "abc12345"
        mock_get.return_value = ""
        cron.install(tmp_project)
        mock_set.assert_called_once()
        call_content = mock_set.call_args[0][0]
        assert "*/5 * * * *" in call_content


class TestUninstall:
    @patch("mas.cron._get_crontab")
    @patch("mas.cron._set_crontab")
    @patch("mas.cron._ident")
    def test_removes_only_marked_block(
        self,
        mock_ident: MagicMock,
        mock_set: MagicMock,
        mock_get: MagicMock,
        tmp_project: Path,
    ):
        mock_ident.return_value = "abc12345"
        mock_get.return_value = (
            "# other cron entry\n"
            "# >>> mas-cron abc12345 >>>\n"
            "*/5 * * * * echo test\n"
            "# <<< mas-cron abc12345 <<<\n"
            "# another entry"
        )
        cron.uninstall(tmp_project)
        mock_set.assert_called_once()
        call_content = mock_set.call_args[0][0]
        assert "mas-cron" not in call_content
        assert "# other cron entry" in call_content
        assert "# another entry" in call_content

    @patch("mas.cron._get_crontab")
    @patch("mas.cron._set_crontab")
    @patch("mas.cron._ident")
    def test_handles_missing_block(
        self,
        mock_ident: MagicMock,
        mock_set: MagicMock,
        mock_get: MagicMock,
        tmp_project: Path,
    ):
        mock_ident.return_value = "abc12345"
        mock_get.return_value = "# some other entry"
        cron.uninstall(tmp_project)
        mock_set.assert_called_once()
        call_content = mock_set.call_args[0][0]
        assert "# some other entry" in call_content

    @patch("mas.cron._get_crontab")
    @patch("mas.cron._set_crontab")
    @patch("mas.cron._ident")
    def test_preserves_surrounding_entries(
        self,
        mock_ident: MagicMock,
        mock_set: MagicMock,
        mock_get: MagicMock,
        tmp_project: Path,
    ):
        mock_ident.return_value = "abc12345"
        mock_get.return_value = (
            "# entry 1\n"
            "# >>> mas-cron abc12345 >>>\n"
            "*/5 * * * * echo test\n"
            "# <<< mas-cron abc12345 <<<\n"
            "# entry 2"
        )
        cron.uninstall(tmp_project)
        mock_set.assert_called_once()
        call_content = mock_set.call_args[0][0]
        assert "# entry 1" in call_content
        assert "# entry 2" in call_content


class TestStatus:
    @patch("mas.cron._get_crontab")
    @patch("mas.cron._ident")
    def test_returns_block_when_present(
        self,
        mock_ident: MagicMock,
        mock_get: MagicMock,
        tmp_project: Path,
    ):
        mock_ident.return_value = "abc12345"
        mock_get.return_value = (
            "# >>> mas-cron abc12345 >>>\n"
            "*/5 * * * * echo test\n"
            "# <<< mas-cron abc12345 <<<"
        )
        result = cron.status(tmp_project)
        assert "# >>> mas-cron abc12345 >>>" in result
        assert "*/5 * * * *" in result
        assert "# <<< mas-cron abc12345 <<<" in result

    @patch("mas.cron._get_crontab")
    @patch("mas.cron._ident")
    def test_returns_no_cron_entry_message_when_absent(
        self,
        mock_ident: MagicMock,
        mock_get: MagicMock,
        tmp_project: Path,
    ):
        mock_ident.return_value = "abc12345"
        mock_get.return_value = "# some other cron"
        result = cron.status(tmp_project)
        resolved = str(tmp_project.resolve())
        assert f"no cron entry for {resolved}" in result


class TestMultipleProjects:
    @patch("mas.cron._get_crontab")
    @patch("mas.cron._set_crontab")
    @patch("mas.cron._ident")
    def test_multiple_projects_no_conflict(
        self,
        mock_ident: MagicMock,
        mock_set: MagicMock,
        mock_get: MagicMock,
        tmp_path: Path,
    ):
        p1 = tmp_path / "project1"
        p1.mkdir()
        p2 = tmp_path / "project2"
        p2.mkdir()

        def ident_side_effect(path: Path):
            if str(path).endswith("project1"):
                return "id1"
            return "id2"

        mock_ident.side_effect = ident_side_effect
        mock_get.return_value = "# existing entry"

        cron.install(p1, 5)
        call1_content = mock_set.call_args[0][0]
        assert "mas-cron id1" in call1_content

        mock_get.return_value = call1_content
        mock_ident.side_effect = ident_side_effect
        mock_set.reset_mock()

        cron.install(p2, 10)
        call2_content = mock_set.call_args[0][0]
        assert "mas-cron id2" in call2_content
        assert "mas-cron id1" in call2_content


class TestEdgeCases:
    @patch("mas.cron.subprocess.run")
    def test_crontab_permission_error_returns_empty_string(self, mock_run: MagicMock):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "crontab -l", output="", stderr="Permission denied"
        )
        result = cron._get_crontab()
        assert result == ""

    @patch("mas.cron.subprocess.run")
    def test_crontab_permission_error_on_write(self, mock_run: MagicMock):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "crontab -", output="", stderr="Permission denied"
        )
        mock_run.return_value = MagicMock()
        try:
            cron._set_crontab("some content")
        except subprocess.CalledProcessError:
            pass

    @patch("mas.cron._get_crontab")
    @patch("mas.cron._set_crontab")
    @patch("mas.cron._ident")
    def test_install_handles_existing_crontab_with_trailing_newline(
        self,
        mock_ident: MagicMock,
        mock_set: MagicMock,
        mock_get: MagicMock,
        tmp_project: Path,
    ):
        mock_ident.return_value = "abc12345"
        mock_get.return_value = "existing line\n"
        cron.install(tmp_project, 5)
        mock_set.assert_called_once()

    @patch("mas.cron._get_crontab")
    @patch("mas.cron._ident")
    def test_status_returns_only_project_block_not_others(
        self,
        mock_ident: MagicMock,
        mock_get: MagicMock,
        tmp_path: Path,
    ):
        p = tmp_path / "proj"
        p.mkdir()
        mock_ident.return_value = "proj123"
        mock_get.return_value = (
            "# other cron\n"
            "# >>> mas-cron other456 >>>\n"
            "* * * * * echo other\n"
            "# <<< mas-cron other456 <<<\n"
            "# >>> mas-cron proj123 >>>\n"
            "*/5 * * * * echo proj\n"
            "# <<< mas-cron proj123 <<<"
        )
        result = cron.status(p)
        assert "proj123" in result
        assert "other456" not in result