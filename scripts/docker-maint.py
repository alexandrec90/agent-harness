#!/usr/bin/env python3
"""Daemon-global Docker Desktop maintenance, callable from any workspace.

Backs the three "Docker: ..." tasks in `alex-projects.code-workspace`. These act on
the whole Docker Desktop daemon / WSL2 VM, not on one project, which is why they are
defined once at workspace level rather than copy-pasted into every repo's
.vscode/tasks.json. They reach this script through `devkit_project.py`, which sets
cwd to the checkout picked in the task's project prompt.

Delegation: a repo that ships its own, better-informed version of a mode wins.
`prune` in particular must know the project's compose file and named volumes, so
carameli (scripts/docker-prune.py) and ibkr_trader (.vscode/docker_prune.py) each
keep theirs -- this script finds and runs it rather than duplicating the logic.
The generic fallbacks below only run when the workspace has no such script (a
scratch folder, a non-Docker repo, or a workspace that never needed one).

Usage:  python docker-maint.py {restart-engine|fix|prune} [--generic]
        (run with cwd set to the workspace folder; --generic skips delegation)

Windows-only by nature -- it drives Docker Desktop and compacts the WSL2 VHDX.
Never passes --volumes to any prune: named volumes hold real dev databases.
"""

import subprocess
import sys
import time
from pathlib import Path

MODES = ("restart-engine", "fix", "prune")

# Per-mode delegation targets, most-specific first. Relative to the workspace cwd.
DELEGATES = {
    "restart-engine": ("scripts/docker-restart-engine.py",),
    "fix": ("scripts/docker-fix.py",),
    "prune": ("scripts/docker-prune.py", ".vscode/docker_prune.py"),
}

DOCKER_PROCESSES = [
    "Docker Desktop",
    "com.docker.backend",
    "com.docker.service",
    "com.docker.proxy",
]
DOCKER_DESKTOP_EXE = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
POLL_TIMEOUT = 90
POLL_INTERVAL = 5


def banner(text: str) -> str:
    return f"\n{'=' * 60}\n  {text}\n{'=' * 60}\n"


def run(cmd, check: bool = False, timeout: int = 300) -> int:
    """Run `cmd`, streaming output. Returns the exit code (127 if not found)."""
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    try:
        code = subprocess.run(cmd, timeout=timeout).returncode
    except FileNotFoundError:
        print(f"  [skip] {cmd[0]} not on PATH")
        return 127
    except subprocess.TimeoutExpired:
        print(f"  [timeout] after {timeout}s")
        return 124
    if code and check:
        print(f"  [warn] exit {code}")
    return code


def docker_info_ok(timeout: int = 15) -> bool:
    try:
        return (
            subprocess.run(["docker", "info"], capture_output=True, timeout=timeout).returncode == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def poll_engine(timeout: int = POLL_TIMEOUT) -> bool:
    """Block until `docker info` succeeds. Guards against reporting success on a
    wedged 'Starting the Docker Engine'."""
    print(f"  Waiting up to {timeout}s for the engine ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if docker_info_ok():
            return True
        time.sleep(POLL_INTERVAL)
    return False


def stop_docker() -> None:
    for name in DOCKER_PROCESSES:
        run(["taskkill", "/F", "/IM", f"{name}.exe", "/T"], timeout=30)
    run(["wsl", "--shutdown"], timeout=60)


def start_docker() -> None:
    if not DOCKER_DESKTOP_EXE.is_file():
        print(f"  [skip] {DOCKER_DESKTOP_EXE} not found -- start Docker Desktop manually")
        return
    subprocess.Popen([str(DOCKER_DESKTOP_EXE)])


def find_delegate(mode: str) -> Path | None:
    for rel in DELEGATES[mode]:
        candidate = Path.cwd() / rel
        if candidate.is_file():
            return candidate
    return None


# --- generic fallbacks (used only when the workspace ships no script) ---------


def generic_restart_engine() -> int:
    print(banner("Docker Engine Restart (generic)"))
    stop_docker()
    start_docker()
    if poll_engine():
        print(banner("DOCKER ENGINE READY"))
        return 0
    print(banner("ENGINE STILL NOT RESPONDING"))
    print("  Try: check the Docker Desktop UI, rerun from an Admin terminal, or reboot.\n")
    return 1


def generic_fix() -> int:
    """More aggressive than restart: two stop/start rounds with a longer poll."""
    print(banner("Docker Desktop Fix (generic, aggressive)"))
    stop_docker()
    time.sleep(5)
    stop_docker()  # second pass catches processes respawned by the first
    start_docker()
    if poll_engine(timeout=POLL_TIMEOUT * 2):
        print(banner("DOCKER ENGINE READY"))
        return 0
    print(banner("DOCKER STILL WEDGED"))
    print("  Next: 'Troubleshoot -> Reset to factory defaults' in Docker Desktop, or reboot.\n")
    return 1


def generic_prune() -> int:
    """Reclaim image/build-cache space, then hand the freed space back to Windows.

    No --volumes and no `docker volume prune`, ever: named volumes are where dev
    databases live. Compose is left alone here -- a workspace that needs its stack
    torn down and brought back should ship its own docker-prune.py (see DELEGATES).
    """
    print(banner("Docker Prune + Compact VHDX (generic)"))
    if not docker_info_ok():
        print("  Docker is not responding; starting it first.")
        start_docker()
        if not poll_engine():
            print(banner("ENGINE UNAVAILABLE -- nothing pruned"))
            return 1

    run(["docker", "system", "prune", "-af"], timeout=600)
    run(["docker", "builder", "prune", "-af"], timeout=600)

    print("\n  Stopping Docker for exclusive VHDX access ...")
    stop_docker()

    vhdx = Path.home() / "AppData/Local/Docker/wsl/disk/docker_data.vhdx"
    if not vhdx.is_file():  # older layouts kept it under wsl/data
        vhdx = Path.home() / "AppData/Local/Docker/wsl/data/ext4.vhdx"
    if vhdx.is_file():
        print(f"  Compacting {vhdx}")
        code = run(
            ["powershell", "-NoProfile", "-Command", f"Optimize-VHD -Path '{vhdx}' -Mode Full"],
            timeout=900,
        )
        if code:
            print("  [warn] Optimize-VHD failed -- it needs an ELEVATED shell and Hyper-V tools.")
            print("         The prune above still freed space inside the VM.")
    else:
        print("  [skip] No Docker WSL VHDX found at the expected paths.")

    start_docker()
    if poll_engine():
        print(banner("PRUNE COMPLETE -- ENGINE READY"))
        return 0
    print(banner("PRUNE DONE, BUT ENGINE DID NOT COME BACK"))
    return 1


GENERIC = {"restart-engine": generic_restart_engine, "fix": generic_fix, "prune": generic_prune}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    generic_only = "--generic" in args
    positional = [a for a in args if not a.startswith("-")]

    if len(positional) != 1 or positional[0] not in MODES:
        print(f"usage: docker-maint.py {{{'|'.join(MODES)}}} [--generic]", file=sys.stderr)
        return 2
    mode = positional[0]

    if not generic_only:
        delegate = find_delegate(mode)
        if delegate:
            print(f"Delegating to this workspace's own script: {delegate}\n")
            return subprocess.run([sys.executable, str(delegate)]).returncode

    return GENERIC[mode]()


if __name__ == "__main__":
    sys.exit(main())
