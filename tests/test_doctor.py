import argparse
from unittest.mock import MagicMock, patch

from pyqforge.cli import cmd_doctor, main
from pyqforge.engines import build_engine, detect_available_engines
from pyqforge.engines.claude_cli import ClaudeCLIEngine
from pyqforge.engines.codex_cli import CodexCLIEngine


def _args(engine="auto"):
    return argparse.Namespace(engine=engine)


def test_doctor_passes_when_both_authenticated(capsys):
    with patch("pyqforge.cli.build_engine") as mock_build:
        claude_mock = MagicMock()
        claude_mock.available.return_value = True
        claude_mock.doctor.return_value = (True, "ok (logged in)")

        codex_mock = MagicMock()
        codex_mock.available.return_value = True
        codex_mock.doctor.return_value = (True, "Logged in using ChatGPT")

        mock_build.side_effect = lambda name, **kw: claude_mock if name == "claude" else codex_mock

        rc = cmd_doctor(_args("auto"))
        assert rc == 0

        out = capsys.readouterr().out
        assert "[OK] claude: ok (logged in)" in out
        assert "[OK] codex: Logged in using ChatGPT" in out
        assert "Either can be used" in out
        assert "Doctor check passed" in out


def test_doctor_passes_when_only_codex_authenticated_claude_missing(capsys):
    with patch("pyqforge.cli.build_engine") as mock_build:
        claude_mock = MagicMock()
        claude_mock.available.return_value = False
        claude_mock.doctor.return_value = (False, "`claude` not found on PATH")

        codex_mock = MagicMock()
        codex_mock.available.return_value = True
        codex_mock.doctor.return_value = (True, "Logged in using ChatGPT")

        mock_build.side_effect = lambda name, **kw: claude_mock if name == "claude" else codex_mock

        rc = cmd_doctor(_args("auto"))
        assert rc == 0

        out = capsys.readouterr().out
        assert "[MISSING] claude: `claude` not found on PATH" in out
        assert "[OK] codex: Logged in using ChatGPT" in out
        assert "Doctor check passed: codex is available and authenticated." in out


def test_doctor_passes_when_only_claude_authenticated_codex_missing(capsys):
    with patch("pyqforge.cli.build_engine") as mock_build:
        claude_mock = MagicMock()
        claude_mock.available.return_value = True
        claude_mock.doctor.return_value = (True, "ok (logged in)")

        codex_mock = MagicMock()
        codex_mock.available.return_value = False
        codex_mock.doctor.return_value = (False, "`codex` not found on PATH")

        mock_build.side_effect = lambda name, **kw: claude_mock if name == "claude" else codex_mock

        rc = cmd_doctor(_args("auto"))
        assert rc == 0

        out = capsys.readouterr().out
        assert "[OK] claude: ok (logged in)" in out
        assert "[MISSING] codex: `codex` not found on PATH" in out
        assert "Doctor check passed: claude is available and authenticated." in out


def test_doctor_passes_when_both_installed_but_one_not_logged_in(capsys):
    with patch("pyqforge.cli.build_engine") as mock_build:
        claude_mock = MagicMock()
        claude_mock.available.return_value = True
        claude_mock.doctor.return_value = (False, "not logged in; run `claude` and /login")

        codex_mock = MagicMock()
        codex_mock.available.return_value = True
        codex_mock.doctor.return_value = (True, "Logged in using ChatGPT")

        mock_build.side_effect = lambda name, **kw: claude_mock if name == "claude" else codex_mock

        rc = cmd_doctor(_args("auto"))
        assert rc == 0

        out = capsys.readouterr().out
        assert "[FAIL] claude: not logged in" in out
        assert "[OK] codex: Logged in using ChatGPT" in out
        assert "Doctor check passed: codex is available and authenticated." in out


def test_doctor_fails_when_all_missing(capsys):
    with patch("pyqforge.cli.build_engine") as mock_build:
        claude_mock = MagicMock()
        claude_mock.available.return_value = False
        claude_mock.doctor.return_value = (False, "`claude` not found on PATH")

        codex_mock = MagicMock()
        codex_mock.available.return_value = False
        codex_mock.doctor.return_value = (False, "`codex` not found on PATH")

        mock_build.side_effect = lambda name, **kw: claude_mock if name == "claude" else codex_mock

        rc = cmd_doctor(_args("auto"))
        assert rc == 1

        out = capsys.readouterr().out
        assert "[MISSING] claude:" in out
        assert "[MISSING] codex:" in out
        assert "Doctor check failed: no provider is available and authenticated." in out


def test_doctor_fails_when_all_unauthenticated(capsys):
    with patch("pyqforge.cli.build_engine") as mock_build:
        claude_mock = MagicMock()
        claude_mock.available.return_value = True
        claude_mock.doctor.return_value = (False, "not logged in")

        codex_mock = MagicMock()
        codex_mock.available.return_value = True
        codex_mock.doctor.return_value = (False, "not logged in")

        mock_build.side_effect = lambda name, **kw: claude_mock if name == "claude" else codex_mock

        rc = cmd_doctor(_args("auto"))
        assert rc == 1

        out = capsys.readouterr().out
        assert "[FAIL] claude: not logged in" in out
        assert "[FAIL] codex: not logged in" in out
        assert "Doctor check failed: no provider is available and authenticated." in out


def test_doctor_cli_subparser():
    with patch("pyqforge.cli.cmd_doctor", return_value=0) as mock_doc:
        rc = main(["doctor", "--engine", "codex"])
        assert rc == 0
        assert mock_doc.call_args[0][0].engine == "codex"


def test_detect_available_engines():
    with patch.object(ClaudeCLIEngine, "available", return_value=True), \
         patch.object(CodexCLIEngine, "available", return_value=False):
        assert detect_available_engines() == ["claude"]


def test_claude_doctor_not_available():
    eng = ClaudeCLIEngine()
    with patch.object(eng, "available", return_value=False):
        ok, msg = eng.doctor()
        assert not ok
        assert "not found on PATH" in msg


def test_claude_doctor_auth_status_logged_in():
    eng = ClaudeCLIEngine()
    mock_proc = MagicMock(returncode=0, stdout='{"loggedIn": true, "email": "test@example.com", "planType": "Pro"}', stderr="")
    with patch.object(eng, "available", return_value=True), \
         patch("shutil.which", return_value="/fake/claude"), \
         patch("subprocess.run", return_value=mock_proc):
        ok, msg = eng.doctor()
        assert ok
        assert "test@example.com" in msg
        assert "Pro" in msg


def test_claude_doctor_auth_status_not_logged_in():
    eng = ClaudeCLIEngine()
    mock_proc = MagicMock(returncode=1, stdout='{"loggedIn": false}', stderr="")
    with patch.object(eng, "available", return_value=True), \
         patch("shutil.which", return_value="/fake/claude"), \
         patch("subprocess.run", return_value=mock_proc):
        ok, msg = eng.doctor()
        assert not ok
        assert "not logged in" in msg


def test_codex_doctor_not_available():
    eng = CodexCLIEngine()
    with patch.object(eng, "available", return_value=False):
        ok, msg = eng.doctor()
        assert not ok
        assert "not found on PATH" in msg


def test_codex_doctor_logged_in():
    eng = CodexCLIEngine()
    mock_proc = MagicMock(returncode=0, stdout="Logged in using ChatGPT\n", stderr="")
    with patch.object(eng, "available", return_value=True), \
         patch("shutil.which", return_value="/fake/codex"), \
         patch("subprocess.run", return_value=mock_proc):
        ok, msg = eng.doctor()
        assert ok
        assert "Logged in using ChatGPT" in msg
