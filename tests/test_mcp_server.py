"""Tests for the L6 async MCP stdio server (work item #15).

Drives :func:`~specsmither.mcp.server.build_server` in-process — NO subprocess / real
stdio transport. The async ``list_tools`` / ``call_tool`` handlers the server registers
are reached through ``server.request_handlers`` (the production registration path) and
driven with :func:`asyncio.run`; the TOON-encoded :class:`~mcp.types.TextContent` they
return is decoded back and asserted. A tmp-path :func:`~specsmither.db.migrations.init_db`
database seeded with a project + specification exercises the query path end to end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import mcp.types as types
from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import Project, Specification
from specsmither.dispatch.facade import HANDOVER_TOOL_NAMES, TOOL_NAMES
from specsmither.mcp.server import TOOLS, build_server, encode_mcp
from specsmither.toon import encode as toon_encode

PROJECT_ID = "01PROJECT0000000000000000A"
SPEC_ID = "01SPEC000000000000000000A"


# --------------------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------------------


def _open(tmp_path: Path) -> sessionmaker[Session]:
    factory = make_session_factory(init_db(tmp_path / "mcp.db"))
    with factory.begin() as session:
        session.add(Project(id=PROJECT_ID, name="P"))
        session.add(
            Specification(
                id=SPEC_ID,
                project_id=PROJECT_ID,
                title="S",
                status="draft",
                specification_type_id=None,
            )
        )
    return factory


def _list_tools(server: object) -> list[types.Tool]:
    """Invoke the registered ``list_tools`` handler and unwrap its tool list."""
    handler = server.request_handlers[types.ListToolsRequest]  # type: ignore[attr-defined]
    result = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    tools: list[types.Tool] = result.root.tools
    return tools


def _call_tool(server: object, name: str, arguments: dict[str, object]) -> list[types.TextContent]:
    """Invoke the registered ``call_tool`` handler and unwrap its content blocks."""
    handler = server.request_handlers[types.CallToolRequest]  # type: ignore[attr-defined]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    content: list[types.TextContent] = asyncio.run(handler(request)).root.content
    return content


# --------------------------------------------------------------------------------------
# encode_mcp — the mcpOutputFormat (TOON default, JSON optional)
# --------------------------------------------------------------------------------------


def test_encode_mcp_defaults_to_toon() -> None:
    value = {"kind": "lifecycle", "agent_response": {"outcome": "success"}}
    assert encode_mcp(value) == toon_encode(value)
    assert encode_mcp(value, "toon") == toon_encode(value)


def test_encode_mcp_json_roundtrips() -> None:
    import json

    value = {"a": 1, "b": ["x", "y"], "café": True}
    assert encode_mcp(value, "json") == json.dumps(value, ensure_ascii=False)
    assert json.loads(encode_mcp(value, "json")) == value


# --------------------------------------------------------------------------------------
# list_tools — the advertised 21-tool surface
# --------------------------------------------------------------------------------------


def test_tools_surface_is_18_plus_3_handover() -> None:
    assert len(TOOLS) == 21
    assert [t.name for t in TOOLS] == list(TOOL_NAMES) + list(HANDOVER_TOOL_NAMES)


def test_list_tools_handler_returns_full_surface(tmp_path: Path) -> None:
    server = build_server(_open(tmp_path))
    tools = _list_tools(server)

    assert len(tools) == 21
    assert [t.name for t in tools] == list(TOOL_NAMES) + list(HANDOVER_TOOL_NAMES)
    for tool in tools:
        assert tool.inputSchema
        assert tool.inputSchema["type"] == "object"


# --------------------------------------------------------------------------------------
# call_tool — unknown tool -> standard_error content; query -> a payload
# --------------------------------------------------------------------------------------


def test_call_tool_unknown_returns_standard_error(tmp_path: Path) -> None:
    server = build_server(_open(tmp_path))

    content = _call_tool(server, "does_not_exist", {})

    assert len(content) == 1
    block = content[0]
    assert isinstance(block, types.TextContent)
    assert block.type == "text"
    # The TOON-encoded standard_error envelope is on the wire as content, not an exception.
    assert "kind: standard_error" in block.text
    assert "UNKNOWN_TOOL" in block.text


def test_call_tool_get_specification_returns_payload(tmp_path: Path) -> None:
    server = build_server(_open(tmp_path))

    content = _call_tool(server, "get", {"type": "specification", "id": SPEC_ID})

    assert len(content) == 1
    block = content[0]
    assert isinstance(block, types.TextContent)
    assert SPEC_ID in block.text
    # A bare success payload — never the error discriminator.
    assert "standard_error" not in block.text


def test_call_tool_json_format(tmp_path: Path) -> None:
    import json

    server = build_server(_open(tmp_path), output_format="json")

    content = _call_tool(server, "get", {"type": "specification", "id": SPEC_ID})

    payload = json.loads(content[0].text)
    assert payload["id"] == SPEC_ID
