"""Stop hook: archives session summary to logs/agent/ for post-mortem analysis.

Parses the session transcript, extracts per-skill tool call statistics
and error context, and maintains a rolling aggregate (skills-profile.json)
across sessions for fixer skill optimization.

It also maintains an outcome ledger (error-ledger.json) that links each error a
fixer skill saw to whether a later run showed it resolved -- the
`error -> fix attempt -> did-it-stick` feedback the profile otherwise lacks.
"""

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime

READ_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "ToolSearch"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
COMMAND_TOOLS = {"Bash"}

ERROR_LOG_NAMES = {
    "test-failures.log",
    "e2e-failures.log",
    "lint-errors.log",
    "log-errors.log",
    "pre-commit-errors.log",
}

ERROR_LINE_RE = re.compile(
    r"^("
    r"FAILED .*"
    r"|E\s+(?:Assert|assert).*"
    r"|# fix:.*"
    r"|.*\bERROR\s*\|.*"
    r"|.*Exception\w*:.*"
    r"|>> Issue:.*"
    r"|.*\d+:\d+:\s*[A-Z]+\d+.*"
    r"|[A-Z]+\d{3,4}\s+.*"
    r"|.*CVE-\d{4}-\d+.*"
    r")",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_error_log(file_path):
    basename = os.path.basename(file_path.replace("\\", "/"))
    return basename in ERROR_LOG_NAMES


def normalize_path(file_path, project_root=""):
    p = file_path.replace("\\", "/")
    if project_root:
        root = project_root.replace("\\", "/").rstrip("/") + "/"
        if p.lower().startswith(root.lower()):
            p = p[len(root) :]
    return p


def extract_error_snippets(content, max_snippets=20):
    # Strip the "N\t" line-number prefix added by the Read tool before matching.
    content = re.sub(r"^\d+\t", "", content, flags=re.MULTILINE)
    snippets = []
    seen = set()
    for match in ERROR_LINE_RE.finditer(content):
        snippet = match.group(0).strip()[:200]
        if snippet and snippet not in seen:
            seen.add(snippet)
            snippets.append(snippet)
        if len(snippets) >= max_snippets:
            break
    return snippets


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def load_entries(transcript_path):
    entries = []
    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def extract_texts(entry):
    content = entry.get("message", {}).get("content", "")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
    return []


# Token usage fields carried by each assistant message in a Claude Code transcript.
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def extract_usage(entry):
    """Return the four token counts for an assistant message (0 when absent).

    Cache-read tokens are billed differently from fresh input, so they are kept
    as a separate field rather than folded into `input_tokens`; `total` is the
    full billed footprint across all four.
    """
    usage = entry.get("message", {}).get("usage") or {}
    counts = {}
    for field in USAGE_FIELDS:
        value = usage.get(field, 0)
        counts[field] = value if isinstance(value, int) else 0
    counts["total"] = sum(counts[f] for f in USAGE_FIELDS)
    return counts


def find_skill_segments(entries):
    """Identify (skill_name, start_idx, end_idx) segments."""
    segments = []
    current_skill = None
    current_start = None

    for i, entry in enumerate(entries):
        if entry.get("type") != "user":
            continue

        texts = extract_texts(entry)
        is_skill_invocation = False
        for t in texts:
            m = re.search(r"<command-name>/?(.+?)</command-name>", t)
            if m:
                if current_skill is not None:
                    segments.append((current_skill, current_start, i))
                current_skill = m.group(1)
                current_start = i
                is_skill_invocation = True
                break

        if (
            not is_skill_invocation
            and not entry.get("isMeta")
            and entry.get("userType") == "external"
            and current_skill is not None
        ):
            any_text = any(t.strip() and not t.startswith("<") for t in texts)
            if any_text:
                segments.append((current_skill, current_start, i))
                current_skill = None

    if current_skill is not None:
        segments.append((current_skill, current_start, len(entries)))

    return segments


def analyze_segment(entries, start, end, project_root=""):
    """Compute tool stats and error context for a single skill segment."""
    tools: dict[str, int] = {}
    max_streak = 0
    streak = 0
    max_bash_streak = 0
    bash_streak = 0

    tool_use_map = {}
    tool_result_map = {}
    files_read = []
    files_edited = []
    checked_known_fixes = False
    tokens = dict.fromkeys((*USAGE_FIELDS, "total"), 0)

    for entry in entries[start:end]:
        etype = entry.get("type", "")
        content = entry.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue

        if etype == "assistant":
            for field, value in extract_usage(entry).items():
                tokens[field] += value
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "?")
                tool_id = block.get("id", "")
                tool_input = block.get("input", {})

                tools[name] = tools.get(name, 0) + 1
                if name in READ_TOOLS:
                    streak += 1
                    max_streak = max(max_streak, streak)
                    bash_streak = 0
                elif name in WRITE_TOOLS:
                    streak = 0
                    bash_streak = 0
                elif name in COMMAND_TOOLS:
                    bash_streak += 1
                    max_bash_streak = max(max_bash_streak, bash_streak)
                    streak = 0

                tool_use_map[tool_id] = {"name": name, "input": tool_input}

                fp = tool_input.get("file_path", "")
                if name in ("Read", "Grep", "Glob") and fp:
                    norm = normalize_path(fp, project_root)
                    files_read.append(norm)
                    if fp.replace("\\", "/").endswith("known-fixes.md"):
                        checked_known_fixes = True
                elif name in ("Edit", "Write", "MultiEdit") and fp:
                    files_edited.append(normalize_path(fp, project_root))

        elif etype == "user":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id", "")
                rc = block.get("content", "")
                if isinstance(rc, list):
                    rc = "\n".join(b.get("text", "") for b in rc if isinstance(b, dict))
                tool_result_map[tid] = str(rc)

    # Extract error snippets from error log reads
    error_snippets = []
    for tid, use in tool_use_map.items():
        if use["name"] != "Read":
            continue
        fp = use["input"].get("file_path", "")
        if not is_error_log(fp):
            continue
        result = tool_result_map.get(tid, "")
        if result:
            error_snippets.extend(extract_error_snippets(result))

    total_reads = sum(tools.get(t, 0) for t in READ_TOOLS)
    total_writes = sum(tools.get(t, 0) for t in WRITE_TOOLS)
    total_bash = sum(tools.get(t, 0) for t in COMMAND_TOOLS)

    return {
        "skill": "",
        "tool_calls": tools,
        "total_tool_calls": sum(tools.values()),
        "total_reads": total_reads,
        "total_writes": total_writes,
        "total_bash": total_bash,
        "read_to_write_ratio": round(total_reads / max(total_writes, 1), 1),
        "max_consecutive_reads": max_streak,
        "max_consecutive_bash": max_bash_streak,
        "files_read": sorted(set(files_read)),
        "files_edited": sorted(set(files_edited)),
        "error_snippets": error_snippets[:20],
        "checked_known_fixes": checked_known_fixes,
        "input_tokens": tokens["input_tokens"],
        "output_tokens": tokens["output_tokens"],
        "cache_creation_tokens": tokens["cache_creation_input_tokens"],
        "cache_read_tokens": tokens["cache_read_input_tokens"],
        "total_tokens": tokens["total"],
    }


def analyze_session(entries, project_root=""):
    """Return session-level stats and per-skill breakdowns."""
    all_tools: dict[str, int] = {}
    max_streak = 0
    streak = 0
    max_bash_streak = 0
    bash_streak = 0
    user_prompts = []
    skills_invoked = []
    tokens = dict.fromkeys((*USAGE_FIELDS, "total"), 0)

    for entry in entries:
        etype = entry.get("type", "")

        if etype == "assistant":
            for field, value in extract_usage(entry).items():
                tokens[field] += value
            content = entry.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        all_tools[name] = all_tools.get(name, 0) + 1
                        if name in READ_TOOLS:
                            streak += 1
                            max_streak = max(max_streak, streak)
                            bash_streak = 0
                        elif name in WRITE_TOOLS:
                            streak = 0
                            bash_streak = 0
                        elif name in COMMAND_TOOLS:
                            bash_streak += 1
                            max_bash_streak = max(max_bash_streak, bash_streak)
                            streak = 0

        elif etype == "user":
            for t in extract_texts(entry):
                m = re.search(r"<command-name>/?(.+?)</command-name>", t)
                if m:
                    skills_invoked.append(m.group(1))
                elif not t.startswith("<") and t.strip() and entry.get("userType") == "external":
                    user_prompts.append(t[:200])

    total_reads = sum(all_tools.get(t, 0) for t in READ_TOOLS)
    total_writes = sum(all_tools.get(t, 0) for t in WRITE_TOOLS)
    total_bash = sum(all_tools.get(t, 0) for t in COMMAND_TOOLS)

    session_stats = {
        "skills_invoked": skills_invoked,
        "first_prompt": user_prompts[0] if user_prompts else "",
        "tool_calls": all_tools,
        "total_tool_calls": sum(all_tools.values()),
        "total_reads": total_reads,
        "total_writes": total_writes,
        "total_bash": total_bash,
        "read_to_write_ratio": round(total_reads / max(total_writes, 1), 1),
        "max_consecutive_reads": max_streak,
        "max_consecutive_bash": max_bash_streak,
        "input_tokens": tokens["input_tokens"],
        "output_tokens": tokens["output_tokens"],
        "cache_creation_tokens": tokens["cache_creation_input_tokens"],
        "cache_read_tokens": tokens["cache_read_input_tokens"],
        "total_tokens": tokens["total"],
    }

    segments = find_skill_segments(entries)
    skill_details = []
    for skill_name, seg_start, seg_end in segments:
        seg = analyze_segment(entries, seg_start, seg_end, project_root)
        seg["skill"] = skill_name
        skill_details.append(seg)

    return session_stats, skill_details


# ---------------------------------------------------------------------------
# Rolling aggregate: skills-profile.json
# ---------------------------------------------------------------------------


def update_profile(agent_dir, skill_details, outcomes=None, recurring=None):
    outcomes = outcomes or {}
    recurring = recurring or {}
    profile_path = os.path.join(agent_dir, "skills-profile.json")
    try:
        with open(profile_path, encoding="utf-8") as f:
            profile = json.load(f)
    except (OSError, json.JSONDecodeError):
        profile = {}

    today = datetime.now().strftime("%Y-%m-%d")

    for seg in skill_details:
        skill = seg["skill"]
        p = profile.get(
            skill,
            {
                "invocations": 0,
                "total_reads_all": 0,
                "total_writes_all": 0,
                "total_bash_all": 0,
                "total_tokens_all": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_creation_tokens": 0,
                "total_cache_read_tokens": 0,
                "max_consecutive_reads_ever": 0,
                "max_consecutive_bash_ever": 0,
                "spiral_count": 0,
                "bash_spiral_count": 0,
                "known_fixes_checked": 0,
                "reads_history": [],
                "tokens_history": [],
                "error_patterns": {},
                "files_read_freq": {},
                "files_edited_freq": {},
            },
        )

        # Counters
        p["invocations"] = p.get("invocations", 0) + 1
        p["total_reads_all"] = p.get("total_reads_all", 0) + seg["total_reads"]
        p["total_writes_all"] = p.get("total_writes_all", 0) + seg["total_writes"]
        p["total_bash_all"] = p.get("total_bash_all", 0) + seg.get("total_bash", 0)
        p["total_tokens_all"] = p.get("total_tokens_all", 0) + seg.get("total_tokens", 0)
        p["total_input_tokens"] = p.get("total_input_tokens", 0) + seg.get("input_tokens", 0)
        p["total_output_tokens"] = p.get("total_output_tokens", 0) + seg.get("output_tokens", 0)
        p["total_cache_creation_tokens"] = p.get("total_cache_creation_tokens", 0) + seg.get(
            "cache_creation_tokens", 0
        )
        p["total_cache_read_tokens"] = p.get("total_cache_read_tokens", 0) + seg.get(
            "cache_read_tokens", 0
        )
        p["max_consecutive_reads_ever"] = max(
            p.get("max_consecutive_reads_ever", 0),
            seg["max_consecutive_reads"],
        )
        p["max_consecutive_bash_ever"] = max(
            p.get("max_consecutive_bash_ever", 0),
            seg.get("max_consecutive_bash", 0),
        )
        if seg["max_consecutive_reads"] >= 10:
            p["spiral_count"] = p.get("spiral_count", 0) + 1
        if seg.get("max_consecutive_bash", 0) >= 5:
            p["bash_spiral_count"] = p.get("bash_spiral_count", 0) + 1
        if seg.get("checked_known_fixes"):
            p["known_fixes_checked"] = p.get("known_fixes_checked", 0) + 1
        p["last_seen"] = today

        # Reads history (last 50 for percentile)
        history = p.get("reads_history", [])
        history.append(seg["total_reads"])
        p["reads_history"] = history[-50:]

        # Token history (last 50) — per-invocation total token footprint.
        tok_history = p.get("tokens_history", [])
        tok_history.append(seg.get("total_tokens", 0))
        p["tokens_history"] = tok_history[-50:]

        # Error patterns (top 50 by count)
        err = p.get("error_patterns", {})
        for snippet in seg.get("error_snippets", []):
            err[snippet] = err.get(snippet, 0) + 1
        if len(err) > 50:
            err = dict(sorted(err.items(), key=lambda x: -x[1])[:50])
        p["error_patterns"] = err

        # Files read frequency (top 30)
        fr = p.get("files_read_freq", {})
        for fp in seg.get("files_read", []):
            fr[fp] = fr.get(fp, 0) + 1
        if len(fr) > 30:
            fr = dict(sorted(fr.items(), key=lambda x: -x[1])[:30])
        p["files_read_freq"] = fr

        # Files edited frequency (top 30)
        fe = p.get("files_edited_freq", {})
        for fp in seg.get("files_edited", []):
            fe[fp] = fe.get(fp, 0) + 1
        if len(fe) > 30:
            fe = dict(sorted(fe.items(), key=lambda x: -x[1])[:30])
        p["files_edited_freq"] = fe

        # Derived fields
        inv = p["invocations"]
        p["avg_reads"] = round(p["total_reads_all"] / inv, 1)
        p["avg_writes"] = round(p["total_writes_all"] / inv, 1)
        p["avg_bash"] = round(p["total_bash_all"] / inv, 1)
        p["avg_tokens"] = round(p["total_tokens_all"] / inv)
        p["avg_ratio"] = round(
            p["total_reads_all"] / max(p["total_writes_all"], 1),
            1,
        )

        sorted_h = sorted(p["reads_history"])
        p90_idx = int(len(sorted_h) * 0.9)
        p["budget_recommendation"] = max(3, sorted_h[min(p90_idx, len(sorted_h) - 1)])

        # Outcome rollup from the error ledger (absent for non-fixer skills).
        if skill in outcomes:
            p["fix_outcomes"] = outcomes[skill]
        # Signatures whose fixes did not hold — surfaced so optimize-fixers can
        # escalate them (check 3g) instead of codifying another guess. Omit when
        # empty so non-fixer/clean skills carry no noise field.
        if recurring.get(skill):
            p["recurring_errors"] = recurring[skill]

        profile[skill] = p

    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)


# ---------------------------------------------------------------------------
# Outcome ledger: error -> fix attempt -> did-it-stick
# ---------------------------------------------------------------------------


def current_head(cwd):
    """Best-effort short commit SHA at `cwd`; '' when git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _ledger_key(skill, signature):
    return f"{skill}::{signature}"


def reconcile_outcomes(ledger, skill_details, head_sha, today):
    """Fold this session's fixer segments into the outcome ledger and return it.

    A fixer skill reads an error log (capturing the signatures it saw) and may edit
    files (a fix attempt). Across sessions we infer outcomes by watching signatures
    appear and disappear:

      - first time a signature is seen        -> status "open"
      - seen again after being marked fixed   -> status "recurring" (fix didn't hold)
      - gone from a fresh read after >=1 fix  -> status "fixed"

    Only `fix*` skills carry error logs, so non-fixer segments are ignored. Keying by
    (skill, signature) keeps each fixer's errors separate. Pure: mutates and returns
    the passed-in dict, no I/O.
    """
    observed: dict[str, set[str]] = {}  # skill -> signatures seen this session
    attempted: dict[str, bool] = {}  # skill -> the skill edited files this session
    for seg in skill_details:
        skill = seg.get("skill", "")
        if not skill.startswith("fix"):
            continue
        observed.setdefault(skill, set()).update(seg.get("error_snippets", []))
        if seg.get("files_edited"):
            attempted[skill] = True

    for skill, signatures in observed.items():
        attempted_now = attempted.get(skill, False)

        # (a) Upsert every signature seen this session.
        for sig in signatures:
            key = _ledger_key(skill, sig)
            entry = ledger.get(key)
            if entry is None:
                entry = {
                    "skill": skill,
                    "signature": sig,
                    "first_seen": today,
                    "last_seen": today,
                    "attempts": 0,
                    "status": "open",
                    "last_commit": None,
                }
                ledger[key] = entry
            if entry["status"] == "fixed":
                # It came back -> the previous fix did not hold.
                entry["status"] = "recurring"
            entry["last_seen"] = today
            if attempted_now:
                entry["attempts"] += 1
                entry["last_commit"] = head_sha or entry.get("last_commit")

        # (b) Disappearance: an open/recurring signature absent from this fresh read,
        #     with at least one prior attempt, counts as resolved.
        for entry in ledger.values():
            if entry["skill"] != skill:
                continue
            if entry["signature"] in signatures:
                continue
            if entry["attempts"] >= 1 and entry["status"] in ("open", "recurring"):
                entry["status"] = "fixed"
                entry["fixed_on"] = today

    return ledger


def outcome_counts(ledger):
    """Per-skill {fixed, recurring, open, success_rate} rollup for the profile.

    success_rate = fixed / (fixed + recurring + open), rounded to 2 dp; 0.0 when a
    skill has no resolved-or-open entries yet.
    """
    counts: dict[str, dict[str, float]] = {}
    for entry in ledger.values():
        skill = entry.get("skill", "")
        if not skill:
            continue
        c = counts.setdefault(skill, {"fixed": 0, "recurring": 0, "open": 0})
        status = entry.get("status", "open")
        if status in c:
            c[status] += 1
    for c in counts.values():
        total = c["fixed"] + c["recurring"] + c["open"]
        c["success_rate"] = round(c["fixed"] / total, 2) if total else 0.0
    return counts


def recurring_signatures(ledger, cap=10):
    """Per-skill list of signatures whose status == 'recurring', newest first.

    Surfaced into the profile so optimize-fixers can escalate fixes that did not
    hold without a 4th file read (it only reads SKILL.md + known-fixes.md + the
    profile). Capped at `cap` per skill, ordered by `last_seen` descending.
    """
    by_skill: dict[str, list] = {}
    for entry in ledger.values():
        if entry.get("status") != "recurring":
            continue
        skill = entry.get("skill", "")
        if not skill:
            continue
        by_skill.setdefault(skill, []).append(entry)

    result: dict[str, list[str]] = {}
    for skill, entries in by_skill.items():
        entries.sort(key=lambda e: e.get("last_seen", ""), reverse=True)
        result[skill] = [e.get("signature", "") for e in entries[:cap]]
    return result


def update_outcome_ledger(agent_dir, skill_details, head_sha):
    """Load, reconcile, and persist the outcome ledger.

    Returns `(per-skill counts, per-skill recurring signatures)` so the caller can
    fold both into the profile in a single write.
    """
    ledger_path = os.path.join(agent_dir, "error-ledger.json")
    try:
        with open(ledger_path, encoding="utf-8") as f:
            ledger = json.load(f)
    except (OSError, json.JSONDecodeError):
        ledger = {}

    today = datetime.now().strftime("%Y-%m-%d")
    reconcile_outcomes(ledger, skill_details, head_sha, today)

    with open(ledger_path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2)

    return outcome_counts(ledger), recurring_signatures(ledger)


# ---------------------------------------------------------------------------
# Error logging
# ---------------------------------------------------------------------------


def _error_log_path(cwd):
    d = os.path.join(cwd, "logs", "agent")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "hook-errors.log")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def read_stdin_payload():
    """Parse the hook payload from stdin as UTF-8, tolerant of odd bytes.

    Mirrors stop.py's reader: decode the raw bytes as UTF-8 with surrogateescape
    rather than the process locale (cp1252 on Windows), which would mis-decode the
    UTF-8 the parent writes to this child's stdin.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    raw = buffer.read() if buffer is not None else sys.stdin.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="surrogateescape")
    return json.loads(raw)


def main():
    cwd = ""
    try:
        hook_input = read_stdin_payload()
        session_id = hook_input.get("session_id", "unknown")
        transcript_path = hook_input.get("transcript_path", "")
        cwd = hook_input.get("cwd", "")

        if not transcript_path or not os.path.exists(transcript_path):
            sys.exit(0)

        entries = load_entries(transcript_path)
        session_stats, skill_details = analyze_session(entries, cwd)

        summary = {
            "session_id": session_id,
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "transcript_path": transcript_path.replace("\\", "/"),
            **session_stats,
            "skill_segments": skill_details,
        }

        if not skill_details:
            sys.exit(0)

        agent_dir = os.path.join(cwd, "logs", "agent")
        os.makedirs(agent_dir, exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d")
        short_id = session_id[:8]
        summary_path = os.path.join(agent_dir, f"{date_str}_{short_id}.json")

        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        # Reconcile the error-outcome ledger first so the profile can carry the
        # per-skill fix success counts in the same write.
        outcomes, recurring = update_outcome_ledger(agent_dir, skill_details, current_head(cwd))
        update_profile(agent_dir, skill_details, outcomes=outcomes, recurring=recurring)

    except Exception:
        import traceback

        try:
            log = _error_log_path(cwd) if cwd else None
            if log:
                with open(log, "a", encoding="utf-8") as f:
                    f.write(f"--- {datetime.now().isoformat()} ---\n")
                    traceback.print_exc(file=f)
                    f.write("\n")
        except OSError:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
