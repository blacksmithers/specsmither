"""planner/02 #14: the ``specsmither-mcp`` (+ ``specsmither``) console scripts boot."""

from __future__ import annotations

import inspect
from functools import partial
from importlib.metadata import entry_points
from pathlib import Path

import pytest

import specsmither.mcp.server as server
from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.mcp.server import build_server, main, serve_stdio


def test_console_scripts_registered() -> None:
    scripts = {e.name: e.value for e in entry_points(group="console_scripts")}
    assert scripts.get("specsmither-mcp") == "specsmither.mcp.server:main"
    assert scripts.get("specsmither") == "specsmither.tui.app:run"


def test_serve_stdio_is_async() -> None:
    assert inspect.iscoroutinefunction(serve_stdio)
    assert callable(main)


def test_main_resolves_user_global_db_and_toon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main()`` (no argv) resolves the user-global DB and serves TOON by default."""
    db = tmp_path / "specsmither.db"
    monkeypatch.setenv("SPECSMITHER_DB", str(db))
    monkeypatch.delenv("SPECSMITHER_MCP_FORMAT", raising=False)
    monkeypatch.setattr(server.sys, "argv", ["specsmither-mcp"])

    captured: dict[str, object] = {}

    def fake_run(func: object) -> None:
        # main() passes functools.partial(serve_stdio, db_path, output_format=...)
        assert isinstance(func, partial)
        captured["db_path"] = func.args[0]
        captured["format"] = func.keywords.get("output_format")

    monkeypatch.setattr(server.anyio, "run", fake_run)
    main()

    assert str(captured["db_path"]) == str(db)
    assert captured["format"] == "toon"


def test_main_honours_json_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECSMITHER_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("SPECSMITHER_MCP_FORMAT", "json")
    monkeypatch.setattr(server.sys, "argv", ["specsmither-mcp"])
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        server.anyio, "run", lambda func: captured.update(format=func.keywords.get("output_format"))
    )
    main()
    assert captured["format"] == "json"


def test_build_server_boots_over_a_db(tmp_path: Path) -> None:
    factory = make_session_factory(init_db(tmp_path / "boot.db"))
    built = build_server(factory)
    assert built is not None  # the agent surface boots over the same engine the TUI uses
