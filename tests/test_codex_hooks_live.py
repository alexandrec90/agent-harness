"""Opt-in black-box smoke test for generated hooks in the real Codex CLI."""

import json
import shutil
import subprocess
import textwrap

import pytest
from support import REPO_ROOT, load_script

hook = load_script("scripts/sync-codex-hooks.py")

pytestmark = [
    pytest.mark.codex_live,
    pytest.mark.paid,
]


def test_project_hooks_are_discovered_and_block_a_real_tool_call(tmp_path):
    """Exercise repo discovery, generation, adapter execution, and denial together."""
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("codex CLI is not installed")

    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    scripts_dir = tmp_path / "scripts/hooks"
    scripts_dir.mkdir(parents=True)
    shutil.copyfile(
        REPO_ROOT / "scripts/hooks/codex-hook-adapter.py",
        scripts_dir / "codex-hook-adapter.py",
    )
    (tmp_path / "hook-recorder.py").write_text(
        textwrap.dedent(
            """\
            import json
            import sys
            from pathlib import Path

            payload = json.load(sys.stdin)
            event = payload.get("hook_event_name") or payload.get("hookEventName")
            with (Path(__file__).parent / "hook-events.jsonl").open(
                "a", encoding="utf-8", newline=""
            ) as stream:
                stream.write(
                    json.dumps(
                        {
                            "event": event,
                            "tool_name": payload.get("tool_name"),
                            "tool_input": payload.get("tool_input"),
                        }
                    )
                    + "\\n"
                )

            if event == "PreToolUse":
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": (
                                    "Expected smoke-test denial. Do not retry with another tool."
                                ),
                            }
                        }
                    )
                )
            else:
                print("{}")
            """
        ),
        encoding="utf-8",
        newline="",
    )
    handler = {
        "hooks": [
            {
                "type": "command",
                "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}/hook-recorder.py"',
            }
        ]
    }
    claude_settings = {
        "hooks": {
            **{event: [handler] for event in ("SessionStart", "UserPromptSubmit", "Stop")},
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}/hook-recorder.py"',
                        }
                    ],
                }
            ],
        }
    }
    generated = hook.to_codex_hooks(claude_settings)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(
        json.dumps(generated, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )

    result = subprocess.run(
        [
            codex,
            "exec",
            "--dangerously-bypass-hook-trust",
            "--enable",
            "hooks",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "workspace-write",
            "--color",
            "never",
            (
                "Use the shell tool exactly once to run a command that creates a file named "
                "blocked-sentinel.txt in the current directory. Do not use apply_patch or any "
                "other tool. The repository hook is expected to deny the shell call. After it "
                "is denied, do not retry; reply with exactly CODEX_HOOK_BLOCKED_OK."
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        check=False,
    )
    diagnostic = f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"

    assert result.returncode == 0, diagnostic
    assert "CODEX_HOOK_BLOCKED_OK" in result.stdout, diagnostic
    assert not (tmp_path / "blocked-sentinel.txt").exists(), diagnostic
    events_path = tmp_path / "hook-events.jsonl"
    assert events_path.is_file(), diagnostic
    records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    events = {record["event"] for record in records}
    assert {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"} <= events, diagnostic
    assert any(
        record["event"] == "PreToolUse" and record["tool_name"] == "Bash" for record in records
    ), diagnostic
