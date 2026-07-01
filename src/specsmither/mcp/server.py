"""The L6 async MCP stdio server (work item #15) — the adapter over the sync engine.

A faithful port of ``packages/cli/src/server.ts`` (the tool definitions + the thin async
``dispatchToolCall`` wrapper), re-pointed at the SpecSmither
:class:`~specsmither.dispatch.facade.Dispatcher`. The engine is **synchronous**; this is
the single async layer in the system, so it owns exactly two responsibilities:

#. **advertise** the 21-tool surface (the 18
   :data:`~specsmither.dispatch.facade.TOOL_NAMES` + the 3
   :data:`~specsmither.dispatch.facade.HANDOVER_TOOL_NAMES`) with JSON-schema argument
   shapes the agent reads, and
#. **forward** each ``call_tool`` to :meth:`Dispatcher.dispatch` and TOON-encode the
   JSON-able content it returns.

The façade never raises — a domain error is already *content* (a ``standard_error``
envelope) — so the wrapper never needs an error branch of its own: it just encodes
whatever ``dispatch`` returns. The sync ``dispatch`` runs on a worker thread
(:func:`anyio.to_thread.run_sync`) so the event loop is never blocked by SQLite I/O.

The wire encoding follows ``output_format`` (:func:`encode_mcp`) — TOON by default, JSON
optional — mirroring the TS ``mcpOutputFormat`` resolution.
"""

from __future__ import annotations

import functools
import json
import os
import sys
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.dispatch.facade import HANDOVER_TOOL_NAMES, TOOL_NAMES, make_dispatcher
from specsmither.operations.workspace import resolve_db_path
from specsmither.toon import encode as toon_encode

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

__all__ = [
    "TOOLS",
    "build_server",
    "encode_mcp",
    "main",
    "serve_stdio",
]

#: The MCP server name advertised on the wire (the TS ``name: 'specforge'`` analogue).
SERVER_NAME = "specsmither"

#: Wire-format env override for :func:`main`. The DB path is resolved user-global
#: by ``specsmither.operations.workspace.resolve_db_path`` ($SPECSMITHER_DB →
#: $SPECSMITHER_HOME/specsmither.db → ~/.specsmither/specsmither.db).
_FORMAT_ENV = "SPECSMITHER_MCP_FORMAT"


# --------------------------------------------------------------------------------------
# Wire encoding — the mcpOutputFormat (TOON default, JSON optional).
# --------------------------------------------------------------------------------------


def encode_mcp(value: Any, fmt: str = "toon") -> str:
    """Encode an MCP result ``value`` for the wire per ``fmt`` (TOON default, JSON opt-in).

    ``fmt == 'json'`` emits ``json.dumps(value, ensure_ascii=False)``; anything else
    (the default ``'toon'``) routes through :func:`specsmither.toon.encode`. Mirrors the
    TS ``encodeMcp`` — a faithful, lossless rendering of the JSON-able content the façade
    returns.
    """
    if fmt == "json":
        return json.dumps(value, ensure_ascii=False)
    return toon_encode(value)


# --------------------------------------------------------------------------------------
# Tool definitions — the 21-tool surface (18 base + 3 handover), built from the façade's
# canonical name tuples so the advertised surface can never drift from what it routes.
# --------------------------------------------------------------------------------------

#: The three response-verbosity tiers, as a reusable schema fragment.
_RESPONSE_DETAIL: dict[str, Any] = {
    "type": "string",
    "enum": ["minimal", "standard", "full"],
    "description": "Response verbosity tier (default 'standard').",
}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    required: Sequence[str] = (),
) -> types.Tool:
    """Build a :class:`mcp.types.Tool` with a permissive JSON-schema object inputSchema."""
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return types.Tool(name=name, description=description, inputSchema=schema)


# Every tool definition, keyed by name. Assembled into TOOLS below in the façade's order;
# a missing/extra name is caught at import time by the comprehension's KeyError.
_DEFS: dict[str, types.Tool] = {
    tool.name: tool
    for tool in (
        # -- Lifecycle: planning verbs (3) -----------------------------------------------
        _tool(
            "start_planning_session",
            "Begin a planning session for a specification (draft -> planning).",
            {"specId": {"type": "string", "description": "Specification id to plan."}},
            required=("specId",),
        ),
        _tool(
            "action_planning_session",
            "Apply a planning operation (create/update epics, tickets, dependencies, ...) "
            "to an active planning session.",
            {
                "sessionId": {"type": "string", "description": "Active planning session id."},
                "operation": {"type": "string", "description": "The planning operation name."},
                "payload": {"type": "object", "description": "Operation arguments."},
                "actor": {"type": "string", "description": "Optional acting identity."},
                "responseDetail": _RESPONSE_DETAIL,
            },
            required=("sessionId", "operation"),
        ),
        _tool(
            "complete_planning_session",
            "Complete an active planning session and request handover review.",
            {"sessionId": {"type": "string", "description": "Active planning session id."}},
            required=("sessionId",),
        ),
        # -- Work-session verbs: frozen-zone 0.1.0 stubs (4) -----------------------------
        _tool(
            "start_work_session",
            "Begin a work session on a ticket (not implemented in 0.1.0; planned 0.2.0).",
            {"ticketId": {"type": "string", "description": "Ticket id to work on."}},
        ),
        _tool(
            "action_work_session",
            "Apply a work-session operation (not implemented in 0.1.0; planned 0.2.0).",
            {
                "sessionId": {"type": "string"},
                "operation": {"type": "string"},
                "payload": {"type": "object"},
            },
        ),
        _tool(
            "complete_work_session",
            "Complete a work session (not implemented in 0.1.0; planned 0.2.0).",
            {"sessionId": {"type": "string"}},
        ),
        _tool(
            "reset_work_session",
            "Reset a work session (not implemented in 0.1.0; planned 0.2.0).",
            {"sessionId": {"type": "string"}},
        ),
        # -- Queries (8) -----------------------------------------------------------------
        _tool(
            "get",
            "Fetch a single entity by id.",
            {
                "type": {
                    "type": "string",
                    "enum": ["specification", "epic", "ticket", "project"],
                    "description": "Entity kind to fetch.",
                },
                "id": {"type": "string", "description": "Entity id."},
            },
            required=("type", "id"),
        ),
        _tool(
            "list",
            "List entities of a kind, scoped + paginated.",
            {
                "type": {
                    "type": "string",
                    "enum": ["specifications", "epics", "tickets", "projects"],
                    "description": "Entity kind to list.",
                },
                "projectId": {"type": "string"},
                "specificationId": {"type": "string"},
                "epicId": {"type": "string"},
                "status": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
                "cursor": {"type": "string"},
                "summary": {"type": "boolean"},
            },
            required=("type",),
        ),
        _tool(
            "search",
            "Full-text + tag search over tickets.",
            {
                "query": {"type": "string", "description": "Search text."},
                "tags": {"type": "array", "items": {"type": "string"}},
                "matchAllTags": {"type": "boolean"},
                "relatedTo": {"type": "string"},
                "status": {"type": "string"},
                "complexity": {"type": "string"},
                "projectId": {"type": "string"},
                "specificationId": {"type": "string"},
                "epicId": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
        ),
        _tool(
            "get_next_actionable_tickets",
            "List the tickets whose dependencies are all satisfied (ready to start).",
            {
                "specificationId": {"type": "string"},
                "limit": {"type": "integer"},
            },
            required=("specificationId",),
        ),
        _tool(
            "get_blocked_tickets",
            "List the tickets blocked by unsatisfied dependencies.",
            {"specificationId": {"type": "string"}},
            required=("specificationId",),
        ),
        _tool(
            "get_report",
            "Run an analytics report (dashboard, implementation, time, blockers, "
            "readiness, sessions).",
            {
                "report": {
                    "type": "string",
                    "enum": [
                        "dashboard",
                        "implementation",
                        "time",
                        "blockers",
                        "readiness",
                        "sessions",
                    ],
                    "description": "Report kind.",
                },
                "specificationId": {"type": "string"},
                "projectId": {"type": "string"},
                "epicId": {"type": "string"},
            },
            required=("report",),
        ),
        _tool(
            "get_critical_path",
            "Compute the critical path through a specification's ticket DAG.",
            {"specificationId": {"type": "string"}},
            required=("specificationId",),
        ),
        _tool(
            "get_dependency_tree",
            "Build the dependency tree for a specification's tickets.",
            {"specificationId": {"type": "string"}},
            required=("specificationId",),
        ),
        # -- Mutations (2) ---------------------------------------------------------------
        _tool(
            "reopen_specification",
            "Reopen a completed specification back into planning.",
            {"specificationId": {"type": "string"}},
            required=("specificationId",),
        ),
        _tool(
            "link_pull_request",
            "Attach a pull request to a specification.",
            {
                "specificationId": {"type": "string"},
                "prNumber": {"type": "integer", "description": "Pull request number."},
                "url": {"type": "string"},
                "title": {"type": "string"},
                "state": {"type": "string"},
            },
            required=("specificationId", "prNumber"),
        ),
        # -- Utility (1) -----------------------------------------------------------------
        _tool(
            "feedback",
            "Send freeform feedback (acknowledged locally).",
            {"message": {"type": "string", "description": "Feedback text."}},
        ),
        # -- Handover verbs (3) ----------------------------------------------------------
        _tool(
            "approve_handover",
            "Approve a pending handover and commit the planning write plan.",
            {"sessionId": {"type": "string"}},
            required=("sessionId",),
        ),
        _tool(
            "reject_handover",
            "Reject a pending handover (returns the session to planning).",
            {"sessionId": {"type": "string"}},
            required=("sessionId",),
        ),
        _tool(
            "reject_handover_with_feedback",
            "Reject a pending handover with reviewer feedback.",
            {
                "sessionId": {"type": "string"},
                "feedback": {"type": "string", "description": "Reviewer feedback."},
            },
            required=("sessionId", "feedback"),
        ),
    )
}

#: The advertised tool surface, in the façade's canonical order (18 base + 3 handover).
#: Building it by lookup against the façade's name tuples guarantees the advertised set
#: equals the routed set — a name present in one but not the other fails fast at import.
TOOLS: list[types.Tool] = [_DEFS[name] for name in (*TOOL_NAMES, *HANDOVER_TOOL_NAMES)]


# --------------------------------------------------------------------------------------
# Server construction — wire the list/call handlers over a Dispatcher.
# --------------------------------------------------------------------------------------


def build_server(
    session_factory: sessionmaker[Session],
    *,
    output_format: str = "toon",
    clock: Callable[[], datetime] | datetime | None = None,
) -> Server:
    """Build the ``specsmither`` MCP :class:`~mcp.server.Server` over ``session_factory``.

    ``list_tools`` returns the static :data:`TOOLS` surface; ``call_tool`` forwards to the
    sync :meth:`Dispatcher.dispatch` (on a worker thread, so the loop is not blocked) and
    TOON/JSON-encodes the JSON-able content it returns per ``output_format``. The façade
    never raises, so the handler has no error branch — a domain error is already content.
    """
    dispatcher = make_dispatcher(session_factory, clock=clock)
    server: Server = Server(SERVER_NAME)

    # The MCP SDK's registration decorators carry no return annotation, so under
    # --strict they read as untyped; the handlers themselves are fully typed.
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        args = arguments or {}
        detail = args.get("responseDetail", "standard")
        content = await anyio.to_thread.run_sync(
            functools.partial(dispatcher.dispatch, name, args, response_detail=detail)
        )
        return [types.TextContent(type="text", text=encode_mcp(content, output_format))]

    return server


# --------------------------------------------------------------------------------------
# stdio entrypoint.
# --------------------------------------------------------------------------------------


async def serve_stdio(db_path: str | Path, *, output_format: str = "toon") -> None:
    """Run the MCP server over stdio against the SQLite database at ``db_path``.

    Bootstraps the database (:func:`~specsmither.db.migrations.init_db`), builds a session
    factory + server, and serves until the stdio streams close.
    """
    engine = init_db(db_path)
    session_factory = make_session_factory(engine)
    server = build_server(session_factory, output_format=output_format)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Console entrypoint: resolve a DB path + output format, then serve over stdio.

    The DB path comes from ``argv[1]`` (explicit override), else the user-global
    location from :func:`~specsmither.operations.workspace.resolve_db_path`
    (``$SPECSMITHER_DB`` → ``$SPECSMITHER_HOME/specsmither.db`` →
    ``~/.specsmither/specsmither.db``); the wire format from ``$SPECSMITHER_MCP_FORMAT``
    (default TOON).
    """
    db_path: str | Path = sys.argv[1] if len(sys.argv) > 1 else resolve_db_path()
    output_format = os.environ.get(_FORMAT_ENV, "toon")
    anyio.run(functools.partial(serve_stdio, db_path, output_format=output_format))
