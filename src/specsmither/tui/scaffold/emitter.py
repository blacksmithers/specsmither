"""Write the Claude Code scaffold into a workspace — idempotent, engine-read-only.

``emit`` writes each artifact only when absent (re-running ``init`` is a no-op) and
*merges* ``settings.local.json`` (never clobbering existing user settings). The hook
scripts are written executable. Returns ``(path, note, status)`` rows for the
InitModal's plan table; :func:`plan_items` previews the same rows without writing.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from specsmither.tui.scaffold.templates import (
    AGENTS,
    HOOK_CONTEXT_SH,
    HOOK_STATUSLINE_SH,
    SCAFFOLD_VERSION,
    SKILLS,
)

__all__ = ["SCAFFOLD_VERSION", "emit", "plan_items"]

_SETTINGS_PATH = ".claude/settings.local.json"
_CONTEXT_HOOK = ".specsmither/hooks/context.sh"
_STATUSLINE_HOOK = ".specsmither/hooks/statusline.sh"

#: The MCP server registration + opt-in hooks merged into settings.local.json.
_MCP_SERVER = {"command": "specsmither-mcp"}
_STATUSLINE = {"type": "command", "command": _STATUSLINE_HOOK}
_HOOK_ENTRIES: dict[str, tuple[str, str]] = {
    # event -> (matcher, hook command)
    "SessionStart": ("*", _CONTEXT_HOOK),
    "UserPromptSubmit": ("*", _CONTEXT_HOOK),
    "PostToolUse": ("mcp__specsmither__.*", _STATUSLINE_HOOK),
}


class _Unparseable:
    """Sentinel: ``settings.local.json`` exists but is not valid JSON (do NOT clobber it)."""


_UNPARSEABLE = _Unparseable()


def _make_executable(path: Path) -> bool:
    """Ensure ``path`` has the user/group/other execute bits; return True if changed."""
    mode = path.stat().st_mode
    want = mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if want != mode:
        path.chmod(want)
        return True
    return False


def _content_files() -> list[tuple[str, str, str, bool]]:
    """The write-if-absent artifacts: ``(relpath, content, note, executable)``."""
    files: list[tuple[str, str, str, bool]] = []
    for name, content in SKILLS.items():
        files.append((f".claude/skills/{name}/SKILL.md", content, f"skill · {name}", False))
    for name, content in AGENTS.items():
        files.append((f".claude/agents/{name}.md", content, f"agent · {name}", False))
    files.append((_CONTEXT_HOOK, HOOK_CONTEXT_SH, "hook · context (read-only)", True))
    files.append((_STATUSLINE_HOOK, HOOK_STATUSLINE_SH, "hook · statusline (read-only)", True))
    return files


def plan_items(cwd: str | Path) -> list[tuple[str, str, str]]:
    """Preview what ``init`` would scaffold (status ``exists`` if already present)."""
    root = Path(cwd)
    rows: list[tuple[str, str, str]] = []
    for relpath, _content, note, _exe in _content_files():
        status = "exists" if (root / relpath).exists() else "pending"
        rows.append((relpath, note, status))
    rows.append((_SETTINGS_PATH, "MCP server + opt-in hooks + statusline", _settings_status(root)))
    return rows


def emit(cwd: str | Path) -> list[tuple[str, str, str]]:
    """Write the scaffold (idempotent). Returns ``(path, note, status)`` rows."""
    root = Path(cwd)
    rows: list[tuple[str, str, str]] = []
    for relpath, content, note, executable in _content_files():
        path = root / relpath
        if path.exists():
            # Re-running init repairs an exec bit lost to an interrupted scaffold or a
            # checkout/copy that didn't preserve it (else the hook silently won't run).
            repaired = executable and _make_executable(path)
            rows.append((relpath, note, "repaired" if repaired else "exists"))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if executable:
            _make_executable(path)
        rows.append((relpath, note, "created"))
    rows.append((_SETTINGS_PATH, "MCP server + opt-in hooks + statusline", _merge_settings(root)))
    return rows


def _load_settings(path: Path) -> dict[str, object] | _Unparseable:
    """Read ``settings.local.json``: ``{}`` if absent, the sentinel if it won't parse.

    Distinguishing absent from unparseable is what lets the merge REFUSE to overwrite
    a present-but-broken user file (a syntax error is recoverable; a clobber is not).
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return _UNPARSEABLE
    return data if isinstance(data, dict) else {}


def _settings_status(root: Path) -> str:
    """Preview the settings merge outcome without writing."""
    settings = _load_settings(root / _SETTINGS_PATH)
    if isinstance(settings, _Unparseable):
        return "skipped"
    return "exists" if not _settings_changes(settings) else "pending"


def _settings_changes(settings: dict[str, object]) -> bool:
    """Would merging the SpecSmither keys change ``settings``? (drives idempotency).

    Mirrors the ``setdefault`` semantics of :func:`_merge_settings` exactly — every
    check is presence-based — so a workspace that already has the SpecSmither keys
    (even alongside a user's OWN custom ``statusLine``) converges to "exists".
    """
    mcp = settings.get("mcpServers")
    if not isinstance(mcp, dict) or "specsmither" not in mcp:
        return True
    if "statusLine" not in settings:
        return True
    hooks = settings.get("hooks")
    hooks_map = hooks if isinstance(hooks, dict) else {}
    for event, (_matcher, command) in _HOOK_ENTRIES.items():
        if not _hook_present(hooks_map.get(event), command):
            return True
    return False


def _hook_present(entries: object, command: str) -> bool:
    """Is a hook with ``command`` already registered under this event?"""
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []) or []:
            if isinstance(hook, dict) and hook.get("command") == command:
                return True
    return False


def _merge_settings(root: Path) -> str:
    """Merge the SpecSmither MCP server + opt-in hooks into ``settings.local.json``.

    Conservative + idempotent: only adds the SpecSmither keys/entries that are not
    already present; an existing config with everything in place is left untouched.
    """
    path = root / _SETTINGS_PATH
    settings = _load_settings(path)
    if isinstance(settings, _Unparseable):
        # Present but broken JSON — leave the user's (recoverable) file untouched.
        return "skipped"
    if not _settings_changes(settings):
        return "exists"

    mcp = settings.get("mcpServers")
    if not isinstance(mcp, dict):
        mcp = {}
        settings["mcpServers"] = mcp
    mcp.setdefault("specsmither", dict(_MCP_SERVER))

    settings.setdefault("statusLine", dict(_STATUSLINE))

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    for event, (matcher, command) in _HOOK_ENTRIES.items():
        existing = hooks.get(event)
        entries = existing if isinstance(existing, list) else []
        if not _hook_present(entries, command):
            entries = [
                *entries,
                {"matcher": matcher, "hooks": [{"type": "command", "command": command}]},
            ]
        hooks[event] = entries

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return "created"
