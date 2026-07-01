"""planner/02 #11–#13: the Claude Code scaffold init emits (idempotent, read-only)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from specsmither.tui.scaffold import emit, plan_items
from specsmither.tui.scaffold.emitter import _HOOK_ENTRIES

#: Verbs a context/statusline hook must NEVER call (it stays engine-read-only).
_MUTATING = (
    "start_planning_session",
    "action_planning_session",
    "complete_planning_session",
    "approve_handover",
    "reject_handover",
    "reopen_specification",
    "link_pull_request",
)


def test_emit_creates_skills_agents_hooks_and_settings(tmp_path: Path) -> None:
    rows = emit(tmp_path)
    statuses = {status for _, _, status in rows}
    assert statuses == {"created"}
    assert (tmp_path / ".claude" / "skills" / "ss-status" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "ss-search" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "agents" / "ssag-spec-creator.md").is_file()
    assert (tmp_path / ".claude" / "agents" / "ssag-orchestrator.md").is_file()
    assert (tmp_path / ".specsmither" / "hooks" / "context.sh").is_file()
    assert (tmp_path / ".claude" / "settings.local.json").is_file()


def test_emit_is_idempotent(tmp_path: Path) -> None:
    emit(tmp_path)
    second = emit(tmp_path)
    assert {status for _, _, status in second} == {"exists"}


def test_plan_items_previews_without_writing(tmp_path: Path) -> None:
    preview = plan_items(tmp_path)
    assert preview and {status for _, _, status in preview} == {"pending"}
    assert not (tmp_path / ".claude").exists()  # preview wrote nothing
    emit(tmp_path)
    assert {status for _, _, status in plan_items(tmp_path)} == {"exists"}


def test_skills_wrap_mcp_tools_with_frontmatter(tmp_path: Path) -> None:
    emit(tmp_path)
    status = (tmp_path / ".claude" / "skills" / "ss-status" / "SKILL.md").read_text("utf-8")
    assert "name: ss-status" in status
    assert "mcp__specsmither__" in status
    search = (tmp_path / ".claude" / "skills" / "ss-search" / "SKILL.md").read_text("utf-8")
    assert "mcp__specsmither__search" in search


def test_settings_registers_mcp_server_and_opt_in_hooks(tmp_path: Path) -> None:
    emit(tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text("utf-8"))
    assert settings["mcpServers"]["specsmither"]["command"] == "specsmither-mcp"
    assert settings["statusLine"]["command"].endswith("statusline.sh")
    for event in _HOOK_ENTRIES:
        assert event in settings["hooks"]


def test_hooks_never_mutate_engine_state(tmp_path: Path) -> None:
    emit(tmp_path)
    for name in ("context.sh", "statusline.sh"):
        body = (tmp_path / ".specsmither" / "hooks" / name).read_text("utf-8")
        for verb in _MUTATING:
            assert verb not in body, f"{name} references mutating verb {verb}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_context_hook_exits_zero_on_partial_config(tmp_path: Path) -> None:
    """A config with no specificationId must NOT abort the hook (set -e + grep no-match)."""
    emit(tmp_path)
    (tmp_path / ".specsmither" / "config.json").write_text('{"projectId": "abc-123"}', encoding="utf-8")
    result = subprocess.run(
        ["bash", ".specsmither/hooks/context.sh"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "project=abc-123" in result.stdout
    assert "spec=none" in result.stdout  # the fallback fires (not a dead default)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_context_hook_is_silent_when_unbound(tmp_path: Path) -> None:
    emit(tmp_path)  # no config.json written
    result = subprocess.run(
        ["bash", ".specsmither/hooks/context.sh"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_settings_merge_preserves_existing(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": {"FOO": "bar"}}), encoding="utf-8")
    emit(tmp_path)
    merged = json.loads(settings_path.read_text("utf-8"))
    assert merged["env"] == {"FOO": "bar"}  # user setting preserved
    assert merged["mcpServers"]["specsmither"]["command"] == "specsmither-mcp"


def test_idempotent_with_a_user_custom_statusline(tmp_path: Path) -> None:
    """A pre-existing custom statusLine is preserved AND the merge converges (no rewrite loop)."""
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    custom = {"statusLine": {"type": "command", "command": "my-own.sh"}}
    settings_path.write_text(json.dumps(custom), encoding="utf-8")
    emit(tmp_path)
    after_first = settings_path.read_text("utf-8")
    assert json.loads(after_first)["statusLine"]["command"] == "my-own.sh"  # not overwritten
    # second run must be a no-op: settings row reports "exists" and the file is unchanged
    rows = emit(tmp_path)
    settings_row = next(s for p, _, s in rows if p.endswith("settings.local.json"))
    assert settings_row == "exists"
    assert settings_path.read_text("utf-8") == after_first


def test_malformed_settings_is_never_clobbered(tmp_path: Path) -> None:
    """A present-but-broken settings.local.json is left untouched (skipped), not overwritten."""
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    broken = '{"env": {"FOO": "bar"},}'  # trailing comma — invalid JSON
    settings_path.write_text(broken, encoding="utf-8")
    rows = emit(tmp_path)
    settings_row = next(s for p, _, s in rows if p.endswith("settings.local.json"))
    assert settings_row == "skipped"
    assert settings_path.read_text("utf-8") == broken  # user's recoverable file intact


def test_exec_bit_is_repaired_on_rerun(tmp_path: Path) -> None:
    """A hook that lost its exec bit is re-chmod'd by a subsequent init (idempotent recovery)."""
    import stat

    emit(tmp_path)
    hook = tmp_path / ".specsmither" / "hooks" / "context.sh"
    hook.chmod(hook.stat().st_mode & ~stat.S_IXUSR & ~stat.S_IXGRP & ~stat.S_IXOTH)
    assert not (hook.stat().st_mode & stat.S_IXUSR)
    rows = emit(tmp_path)
    row = next(s for p, _, s in rows if p.endswith("context.sh"))
    assert row == "repaired"
    assert hook.stat().st_mode & stat.S_IXUSR  # exec bit restored
