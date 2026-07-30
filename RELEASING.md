# Releasing devkit

One bad tag here reddens every consumer, and one *missing* tag quietly breaks every
project generated after the feature it was supposed to carry. Both have happened. This
is the checklist that prevents them.

## Why a tag is not optional

Three things pin a devkit **tag**, never a branch:

| Pin | Where | Consequence of a stale tag |
| --- | --- | --- |
| `rev:` | a consumer's `.pre-commit-config.yaml` | pre-commit resolves hook ids **strictly** — against a tag that predates a hook, the consumer's next commit aborts with "hook not found" |
| `ref:` | a consumer's PR gate drift job | the drift check compares the vendored tree against the wrong revision |
| `FALLBACK_DEVKIT_REF` | `scripts/new-project.py` | every newly generated project pins it into both files above |

`new-project.py` resolves the ref as `latest_devkit_tag() or FALLBACK_DEVKIT_REF`. Both
paths normally return the same thing, so **a feature that is not tagged does not exist
as far as a generated project is concerned**, no matter that it is on `main`.

## The checklist

Order matters at steps 3–5; the rest is verification.

1. **Land the work on `main`** and confirm CI is green, `generated-project` job
   included. That job renders a project of every preset and runs its suites — it is
   the only check that catches vendored-tier coupling.

2. **Decide the version.** Bump the minor for new vendored files, published
   pre-commit hooks, or a manifest field; bump the patch for fixes to existing ones.

3. **Bump `FALLBACK_DEVKIT_REF`** in `scripts/new-project.py` to the version you are
   about to tag, and commit it.

   > `test_fallback_devkit_ref_tracks_the_newest_tag` will be **red between this step
   > and step 4**. That is the check working, not a problem to route around: it
   > compares the constant against `git describe --tags`, and the tag does not exist
   > yet. Do not "fix" it by reverting the bump.

4. **Tag and push, together:**

   ```bash
   git tag vX.Y.Z && git push && git push --tags
   ```

   Never push the tag before the commit that bumps the fallback — a tag that exists
   while the constant still names the previous one is precisely the state step 3's
   test was written to catch, and it would then pass for the wrong reason.

5. **Re-run the suite.** `test_fallback_devkit_ref_tracks_the_newest_tag` is green now
   that `git describe --tags` can see the tag.

## Verify the tag serves what it claims

A tag is only useful if it carries the channel a consumer will ask it for. Prove it
rather than assuming:

```bash
# The published pre-commit hooks must be IN the tagged tree.
git ls-tree -r --name-only vX.Y.Z | grep -E 'pre-commit-hooks|precommit/'

# End to end: a fresh project's commit gate must actually run.
python scripts/new-project.py probe_tag --preset bare --parent /tmp/gen \
  --no-remote --no-worktree --yes
cd /tmp/gen/probe_tag && pre-commit run --all-files
```

That last command is the acceptance test. It must not print "hook not found", and
`new-project.py` must not print the unpublished-channel warning
(`_warn_if_pre_commit_channel_is_unpublished`).

> The **executable bit** only fails here. The published hooks are `language: script`,
> so pre-commit execs them directly; a missing `chmod +x` fails on a consumer's
> machine, at commit time, after the tag is cut. A test guards it, but this run is the
> end-to-end confirmation.

## Tell adopters what changed

Adopters find out by running `sync-devkit.py --pull`, and **the commit message is the
only changelog they get.** When a change alters vendored behaviour, say so there.

## After the tag: the consumers

Each consuming repo needs, ideally in the same commit as its `--pull`:

- `.github/workflows/pr-gate.yml` — bump the drift job's `ref:`
- `.pre-commit-config.yaml` — bump the devkit `rev:` (or `pre-commit autoupdate`)

Bumping the `ref:` alone, without pulling, turns a green gate red by design: the drift
check compares the vendored tree against the checked-out ref.
