"""Unit tests for scripts/sync-devkit.py (harness vendoring + drift check)."""

import subprocess
from pathlib import Path

from conftest import load_module

sh = load_module("scripts/sync-devkit.py")


def test_resolve_src_prefers_arg_then_env():
    assert sh.resolve_src("/a/b", {sh.SRC_ENV: "/c"}) == Path("/a/b").expanduser().resolve()
    assert sh.resolve_src(None, {sh.SRC_ENV: "/c"}) == Path("/c").expanduser().resolve()
    assert sh.resolve_src(None, {}) is None


def _seed(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# --- the pin: the third thing an upgrade has to move ------------------------
# Files, DEVKIT_VERSION and the .pre-commit-config.yaml `rev:` describe one
# upstream revision. When only two of them move, the commit-time gate compares
# against the revision the pin names and reports every file added upstream since
# as drift -- a diagnosis that points at the files and never at the pin.

CONFIG = f"""\
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: check-yaml
  # A tag, never a branch -- one bad upstream commit must not redden this repo.
  - repo: {sh.DEVKIT_REPO}
    rev: v0.5.2 # keep in step with DEVKIT_VERSION
    hooks:
      - id: devkit-drift
"""


def test_the_pin_moves_to_the_pulled_tag():
    updated, previous = sh.bump_pin(CONFIG, "v0.5.3")
    assert previous == "v0.5.2"
    assert "rev: v0.5.3" in updated


def test_bumping_the_pin_leaves_other_repos_alone():
    """Only devkit's pin moves. Retargeting a third-party hook to a devkit tag
    would break the hook and be invisible until the next commit."""
    updated, _ = sh.bump_pin(CONFIG, "v0.5.3")
    assert "rev: v5.0.0" in updated
    assert updated.count("v0.5.3") == 1


def test_the_rationale_comment_survives_the_bump():
    # The comment is why the pin is a tag at all; a rewrite that drops it deletes
    # the reasoning and invites someone to point it at a branch.
    updated, _ = sh.bump_pin(CONFIG, "v0.5.3")
    assert "rev: v0.5.3 # keep in step with DEVKIT_VERSION" in updated
    assert "must not redden this repo" in updated


def test_a_project_without_a_devkit_pin_is_not_an_error():
    text = "repos:\n  - repo: https://example.com/other\n    rev: v1\n"
    updated, previous = sh.bump_pin(text, "v0.5.3")
    assert previous is None
    assert updated == text


GATE = f"""\
jobs:
  harness:
    steps:
      - uses: actions/checkout@v7
      - name: Check out the shared harness repo
        uses: actions/checkout@v7
        with:
          repository: {sh.DEVKIT_SLUG}
          ref: v0.5.2 # bump with the --pull it corresponds to
          path: .devkit-src
      - name: Check out a vendor fixture
        uses: actions/checkout@v7
        with:
          repository: someone/else
          ref: v1.2.3
"""


def test_the_gate_ref_moves_to_the_pulled_tag():
    updated, previous = sh.bump_gate_ref(GATE, "v0.5.3")
    assert previous == "v0.5.2"
    assert "ref: v0.5.3 # bump with the --pull it corresponds to" in updated


def test_only_devkits_checkout_step_is_retargeted():
    """A workflow checks out several repos. Retargeting the wrong one points a
    third-party checkout at a devkit tag, and CI fails somewhere unrelated."""
    updated, _ = sh.bump_gate_ref(GATE, "v0.5.3")
    assert "ref: v1.2.3" in updated
    assert updated.count("v0.5.3") == 1


def test_a_workflow_without_a_devkit_checkout_is_not_an_error():
    updated, previous = sh.bump_gate_ref("jobs:\n  x:\n    steps: []\n", "v0.5.3")
    assert previous is None
    assert "v0.5.3" not in updated


def test_pull_moves_both_consumer_pins(tmp_path, monkeypatch):
    """RELEASING.md: the `rev:` and the gate's `ref:` are two separate pins and both
    have to land in the same change as the files. Moving one is a half-upgrade."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    _seed(repo, sh.PR_GATE_FILE, GATE)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert sh.read_pin((repo / sh.PRECOMMIT_FILE).read_text()) == "v0.5.3"
    assert "ref: v0.5.3" in (repo / sh.PR_GATE_FILE).read_text()


def test_a_project_with_no_pr_gate_still_pulls(tmp_path, monkeypatch):
    # Not every consumer has a PR gate; its absence is not a failure.
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert not (repo / sh.PR_GATE_FILE).exists()


def test_the_pin_is_read_back_without_its_comment():
    assert sh.read_pin(CONFIG) == "v0.5.2"


def test_a_stale_pin_is_detected_from_the_project_alone(tmp_path):
    """No network, no source checkout: both files are committed in the project."""
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _seed(tmp_path, sh.VERSION_FILE, "v0.5.3\n")
    assert sh.stale_pin(tmp_path) == ("v0.5.2", "v0.5.3")


def test_a_matching_pin_and_stamp_is_not_stale(tmp_path):
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _seed(tmp_path, sh.VERSION_FILE, "v0.5.2\n")
    assert sh.stale_pin(tmp_path) is None


def test_a_sha_stamp_reads_as_stale(tmp_path):
    """A SHA can never equal a tag, so an untagged or dirty pull reports stale --
    which is correct: it left the gate measuring against the wrong revision."""
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _seed(tmp_path, sh.VERSION_FILE, "71a9b47\n")
    assert sh.stale_pin(tmp_path) == ("v0.5.2", "71a9b47")


def test_a_project_missing_either_file_is_not_stale(tmp_path):
    # Pre-adoption, or a project that does not use the published hooks.
    assert sh.stale_pin(tmp_path) is None
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    assert sh.stale_pin(tmp_path) is None


def test_classify_partitions_ok_drift_missing(tmp_path):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    manifest = ("scripts/a.py", "scripts/b.py", "scripts/c.py")
    _seed(src, "scripts/a.py", "same")
    _seed(repo, "scripts/a.py", "same")  # ok
    _seed(src, "scripts/b.py", "upstream")
    _seed(repo, "scripts/b.py", "local-edit")  # drift
    _seed(repo, "scripts/c.py", "only-here")  # missing in src

    drifted, missing, ok = sh.classify(src, repo, manifest)
    assert ok == ["scripts/a.py"]
    assert drifted == ["scripts/b.py"]
    assert missing == ["scripts/c.py"]


# --- the two guards on --pull ------------------------------------------------


def _git(root: Path, *args: str):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _repo(root: Path, tag: str = "", files: dict[str, str] | None = None) -> Path:
    """A one-commit git repo, optionally tagged. Real git: `describe --exact-match`
    and `status --porcelain` are the behaviours under test, and a fake would only
    assert that the fake works.

    `files` are committed, not left in the tree -- an uncommitted file would make
    the source dirty and trip the other guard, which is the bug this helper had on
    its first outing.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("one")
    for rel, text in (files or {}).items():
        _seed(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    if tag:
        _git(root, "tag", tag)
    return root


def test_a_tagged_clean_source_is_pullable(tmp_path):
    src = _repo(tmp_path / "src", tag="v9.9.9")
    assert sh.source_tag(src) == "v9.9.9"
    assert not sh.source_dirty(src)


def test_an_untagged_source_has_no_tag(tmp_path):
    assert sh.source_tag(_repo(tmp_path / "src")) is None


def test_uncommitted_changes_make_a_source_dirty(tmp_path):
    src = _repo(tmp_path / "src", tag="v9.9.9")
    (src / "f.txt").write_text("changed")
    assert sh.source_dirty(src)


def test_a_non_repo_is_neither_tagged_nor_dirty(tmp_path):
    # Not a git checkout at all: report nothing rather than crashing the gate.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sh.source_tag(plain) is None
    assert not sh.source_dirty(plain)


def test_pull_refuses_a_dirty_source_without_copying_anything(tmp_path, monkeypatch):
    """The refusal has to come before the copy: a half-upgraded project with a
    matching stamp is the state that cannot be diagnosed afterwards."""
    src = _repo(tmp_path / "src", tag="v9.9.9", files={"scripts/hooks/x.py": "upstream"})
    (src / "f.txt").write_text("now uncommitted")
    repo = tmp_path / "proj"
    _seed(repo, "scripts/hooks/x.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 2
    assert (repo / "scripts/hooks/x.py").read_text() == "old"
    assert not (repo / sh.VERSION_FILE).exists()


def test_pull_refuses_an_untagged_source(tmp_path, monkeypatch):
    src = _repo(tmp_path / "src", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 2
    assert not (repo / "scripts/hooks/x.py").exists()


def test_pull_stamps_the_tag_and_bumps_the_pin_together(tmp_path, monkeypatch):
    """The whole point: three things move as one, or the upgrade is half-done."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert (repo / "scripts/hooks/x.py").read_text() == "upstream"
    assert (repo / sh.VERSION_FILE).read_text().strip() == "v0.5.3"
    assert "rev: v0.5.3" in (repo / sh.PRECOMMIT_FILE).read_text()
    assert sh.stale_pin(repo) is None


def test_an_allowed_dirty_pull_is_stamped_provisional(tmp_path, monkeypatch):
    """Marked so it can never be mistaken for a release -- and so `stale_pin`
    keeps reporting it until a real upgrade replaces it."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    (src / "f.txt").write_text("uncommitted")
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-dirty"]) == 0
    assert (repo / sh.VERSION_FILE).read_text().strip() == "v0.5.3-dirty"
    assert sh.stale_pin(repo) is not None


def test_an_untagged_pull_leaves_the_pin_alone(tmp_path, monkeypatch):
    # There is no tag to move it to; pretending otherwise would pin a nonexistent rev.
    src = _repo(tmp_path / "src", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert sh.read_pin((repo / sh.PRECOMMIT_FILE).read_text()) == "v0.5.2"


def test_check_noop_when_src_unset(capsys, monkeypatch):
    monkeypatch.delenv(sh.SRC_ENV, raising=False)
    assert sh.main(["--check"]) == 0
    assert "skipping" in capsys.readouterr().out


def test_check_passes_when_in_sync(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed(repo, "scripts/x.py", "v1")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--check", "--src", str(src)]) == 0


def test_check_fails_on_drift(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, "scripts/x.py", "local")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--check", "--src", str(src)]) == 1


def test_pull_copies_shared_into_project(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    # --allow-untagged throughout the mechanics tests below: `src` is a plain
    # directory, not a checkout, so the release guards have nothing to read. What
    # is under test here is the copy/retire/receipt behaviour, not the guards.
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / "scripts/x.py").read_text() == "upstream"


def test_pull_removes_only_reviewed_retired_files(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/SKILL.md", "obsolete")
    _seed(repo, ".claude/skills/old/state.json", "project-owned")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/SKILL.md",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / ".claude/skills/old/SKILL.md").exists()
    assert (repo / ".claude/skills/old/state.json").read_text() == "project-owned"


def test_pull_receipt_removes_a_no_longer_managed_unchanged_file(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/old.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/old.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0

    _seed(src, "scripts/new.py", "new")
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/new.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / "scripts/old.py").exists()
    assert (repo / "scripts/new.py").read_text() == "new"


def test_pull_receipt_preserves_a_locally_edited_retired_file(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/old.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/old.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    _seed(repo, "scripts/old.py", "local edit")

    monkeypatch.setattr(sh, "MANIFEST", ())
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / "scripts/old.py").read_text() == "local edit"


def test_check_fails_while_a_retired_file_is_present(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "same")
    _seed(repo, "scripts/x.py", "same")
    _seed(repo, ".claude/skills/old/SKILL.md", "obsolete")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/SKILL.md",))

    assert sh.main(["--check", "--src", str(src)]) == 1


def test_push_copies_project_into_shared(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(repo, "scripts/x.py", "authored-here")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--push", "--src", str(src)]) == 0
    assert (src / "scripts/x.py").read_text() == "authored-here"


def test_list_prints_manifest(capsys):
    assert sh.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "scripts/hooks/harness_config.py" in out


def test_manifest_files_exist_in_repo():
    # The vendored manifest must reference real files in this repo.
    for rel in sh.MANIFEST:
        assert (sh.REPO_ROOT / rel).exists(), f"manifest lists missing file: {rel}"


def test_version_file_not_in_manifest():
    # DEVKIT_VERSION is a per-project artifact, never synced/drift-checked.
    assert sh.VERSION_FILE not in sh.MANIFEST
    assert sh.RECEIPT_FILE not in sh.MANIFEST


# ---- version stamping ------------------------------------------------------


def test_read_version_roundtrip(tmp_path):
    assert sh.read_version(tmp_path) is None
    (tmp_path / sh.VERSION_FILE).write_text("abc1234\n")
    assert sh.read_version(tmp_path) == "abc1234"


def test_git_head_parses_sha(monkeypatch):
    import subprocess as _sp

    monkeypatch.setattr(
        sh.subprocess, "run", lambda *a, **k: _sp.CompletedProcess([], 0, "deadbee\n", "")
    )
    assert sh.git_head(Path(".")) == "deadbee"


def test_git_head_none_on_failure(monkeypatch):
    import subprocess as _sp

    monkeypatch.setattr(
        sh.subprocess, "run", lambda *a, **k: _sp.CompletedProcess([], 128, "", "not a git repo")
    )
    assert sh.git_head(Path(".")) is None


def test_pull_stamps_harness_version(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    # Untagged, so the stamp falls back to the SHA -- and `stale_pin` will keep
    # reporting it until a tagged pull replaces it.
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / sh.VERSION_FILE).read_bytes() == b"abc1234\n"
