from unittest.mock import MagicMock

from shell_registry import ShellRegistry


def test_open_shell():
    reg = ShellRegistry()
    mock_shell = MagicMock()
    mock_shell.state = "idle"
    mock_shell.purpose = None
    mock_shell.uptime = 0
    mock_shell.last_command = None
    shell_id = reg.open("dev", mock_shell, purpose="test")
    assert shell_id.startswith("sh_")
    assert shell_id in [s["shell_id"] for s in reg.list_shells()]


def test_get_shell():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.open("dev", mock_shell)
    assert reg.get(shell_id) is mock_shell


def test_close_shell():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.open("dev", mock_shell)
    reg.close(shell_id)
    assert shell_id not in [s["shell_id"] for s in reg.list_shells()]
    mock_shell.close.assert_called_once()


def test_list_shells_by_target():
    reg = ShellRegistry()
    mock1 = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    mock2 = MagicMock(state="running", purpose="tests", uptime=0, last_command="pytest")
    reg.open("dev", mock1)
    reg.open("dev", mock2, purpose="tests")
    reg.open("db", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    dev_shells = reg.list_shells(target="dev")
    assert len(dev_shells) == 2


def test_list_shells_terminated_hint():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="terminated", purpose=None, uptime=0, last_command=None)
    reg.open("dev", mock_shell)
    shells = reg.list_shells()
    assert shells[0]["status"] == "terminated"
    assert "hint" in shells[0]


def test_get_or_create_default():
    reg = ShellRegistry()
    mock_shell = MagicMock(state="idle", purpose=None, uptime=0, last_command=None)
    shell_id = reg.get_or_create_default("dev", lambda: mock_shell)
    assert shell_id.startswith("sh_")
    assert reg.get_default_id("dev") == shell_id
    shell_id2 = reg.get_or_create_default("dev", lambda: MagicMock())
    assert shell_id == shell_id2


def test_set_default_shell():
    reg = ShellRegistry()
    shell1 = reg.open("dev", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    shell2 = reg.open("dev", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    target = reg.set_default(shell2)
    assert target == "dev"
    assert reg.get_target(shell2) == "dev"
    assert reg.get_default_id("dev") == shell2
    shells = reg.list_shells(target="dev")
    assert next(s for s in shells if s["shell_id"] == shell1)["is_default"] is False
    assert next(s for s in shells if s["shell_id"] == shell2)["is_default"] is True


def test_close_all_for_target():
    reg = ShellRegistry()
    reg.open("dev", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    reg.open("dev", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    reg.open("db", MagicMock(state="idle", purpose=None, uptime=0, last_command=None))
    reg.close_all_for_target("dev")
    assert len(reg.list_shells(target="dev")) == 0
    assert len(reg.list_shells()) == 1
