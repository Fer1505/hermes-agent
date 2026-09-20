"""Actual kernel capability boundaries and actionable local/remote failures."""
import json
import time
from unittest.mock import Mock

import pytest

from tools import code_execution_tool as ce
from tools.code_kernel import shutdown_all_kernels


@pytest.fixture(autouse=True)
def kernels(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(ce, "_load_config", lambda: {"mode": "strict", "timeout": 10})
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


@pytest.mark.parametrize("enabled", [[], ["vision_analyze"], ["web_extract"]])
def test_explicit_capabilities_do_not_gain_terminal_import_or_rpc(enabled, monkeypatch):
    dispatch = Mock(return_value=json.dumps({"output": "must not run"}))
    monkeypatch.setattr("model_tools.handle_function_call", dispatch)
    first = json.loads(ce.execute_code(
        "from hermes_tools import terminal", task_id="recovery-test", enabled_tools=enabled))
    assert first["status"] == "error"
    assert "terminal" not in first.get("hint", "").split("Importable tools here:")[-1]
    # The generated module's low-level RPC also must enforce the same set.
    second = json.loads(ce.execute_code(
        "import hermes_tools\nprint(hermes_tools._call('terminal', {'command': 'ignored'}))",
        task_id="recovery-test", enabled_tools=enabled))
    assert second["status"] == "success"
    assert "not available" in second["output"]
    dispatch.assert_not_called()


def test_missing_parser_recovers_with_stdlib_in_same_kernel():
    # Explicit finder makes optional-package absence independent of host installs.
    failed = json.loads(ce.execute_code("""
import sys, importlib.abc
class AbsentParser(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'bs4':
            raise ModuleNotFoundError("No module named 'bs4'")
sys.meta_path.insert(0, AbsentParser())
saved_html = '<p>Verified fixture heading</p>'
import bs4
""", task_id="parser-recovery", enabled_tools=[]))
    assert failed["status"] == "error"
    assert "html.parser" in failed["hint"]
    assert "terminal(" not in failed["hint"]
    recovered = json.loads(ce.execute_code("""
from html.parser import HTMLParser
class Text(HTMLParser):
    def handle_data(self, data): print(data)
Text().feed(saved_html)
""", task_id="parser-recovery", enabled_tools=[]))
    assert recovered["status"] == "success"
    assert "Verified fixture heading" in recovered["output"]
    assert recovered["kernel"]["reused"] is True
    assert recovered["tool_calls_made"] == 0


def test_enabled_tool_still_dispatches_and_omitted_scope_remains_compatible(monkeypatch):
    dispatch = Mock(return_value=json.dumps({"content": "synthetic allowed read"}))
    monkeypatch.setattr("model_tools.handle_function_call", dispatch)
    result = json.loads(ce.execute_code(
        "from hermes_tools import read_file\nprint(read_file('/fixture'))",
        task_id="allowed-recovery", enabled_tools=["read_file"]))
    assert result["status"] == "success"
    assert "synthetic allowed read" in result["output"]
    assert result["tool_calls_made"] == 1
    assert dispatch.call_args.args[0] == "read_file"
    legacy = json.loads(ce.execute_code(
        "from hermes_tools import terminal\nprint('legacy import available')",
        task_id="legacy-recovery", enabled_tools=None))
    assert legacy["status"] == "success"
    assert legacy["tool_calls_made"] == 0
    assert dispatch.call_count == 1


@pytest.mark.parametrize("enabled", [[], ["web_extract"], ["terminal"]])
def test_missing_dependency_hint_only_offers_available_routes(enabled):
    hint = ce._sandbox_failure_hint("ModuleNotFoundError: No module named 'bs4'", enabled)
    assert "html.parser" in hint
    assert ("web_extract" in hint) == ("web_extract" in enabled)
    assert ("terminal(" in hint) == ("terminal" in enabled)


@pytest.mark.parametrize("status", ["success", "error", "timeout", "interrupted"])
def test_remote_kernel_hint_matches_local_error_only(status):
    trace = "ModuleNotFoundError: No module named 'bs4'"
    result = json.loads(ce._finish_remote_kernel_result(
        {"status": status, "stdout": "", "traceback": trace}, timeout=10,
        exec_start=time.monotonic(), enabled_tools=[]))
    if status == "error":
        assert result["hint"] == ce._sandbox_failure_hint(trace, [])
    else:
        assert "hint" not in result


@pytest.mark.parametrize("kernel_available", [True, False])
def test_remote_dispatch_honors_empty_capabilities_and_includes_hint(monkeypatch, kernel_available):
    import tools.code_kernel_remote as remote
    trace = "ModuleNotFoundError: No module named 'bs4'"
    received = []
    def kernel(*args, **kwargs):
        received.append(kwargs["sandbox_tools"])
        return {"status": "error", "traceback": trace} if kernel_available else None
    class Env:
        def get_temp_dir(self): return "/tmp"
        def execute(self, command, **kwargs):
            if "command -v python3" in command: return {"output": "OK"}
            if "python3 script.py" in command: return {"output": trace, "returncode": 1}
            return {"output": "", "returncode": 0}
    monkeypatch.setattr(ce, "_get_or_create_env", lambda _: (Env(), "ssh"))
    monkeypatch.setattr(remote, "execute_in_remote_kernel", kernel)
    monkeypatch.setattr(ce, "_ship_file_to_remote", lambda *args: None)
    monkeypatch.setattr(ce, "_rpc_poll_loop", lambda *args: None)
    result = json.loads(ce._execute_remote("import bs4", "remote-recovery", []))
    assert received == [frozenset()]
    assert result["status"] == "error"
    assert result["hint"] == ce._sandbox_failure_hint(trace, [])
