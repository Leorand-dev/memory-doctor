# memory-doctor. Quickstart

A health check for an agent's long-term memory system. Stdlib only.
No install step.

## 1. Get the file

Either clone this repo, or copy `scripts/memory-doctor.py` (one file,
~750 lines) into your project.

```bash
# Option A: clone
git clone https://github.com/leo-afk-sudo/memory-doctor.git
cd memory-doctor

# Option B: just grab the file
curl -O https://raw.githubusercontent.com/leo-afk-sudo/memory-doctor/main/scripts/memory-doctor.py
chmod +x memory-doctor.py
```

## 2. Run it

```bash
# Default: read-only scan, human-readable output
python3 scripts/memory-doctor.py --scan

# Machine-readable, for piping into other tools
python3 scripts/memory-doctor.py --scan --json | jq '.summary'

# One line, for cron / heartbeat / pre-commit
python3 scripts/memory-doctor.py --scan --quiet
```

## 3. Wire it up

### Pre-commit hook (recommended)

```bash
mkdir -p scripts/hooks
cp scripts/hooks/post-commit.example scripts/hooks/post-commit
chmod +x scripts/hooks/post-commit
git config core.hooksPath scripts/hooks
```

The hook runs after every commit, prints a one-line summary on any
finding, dumps the full report on a critical (secret) finding, and
**never blocks the commit**. To skip for one commit:

```bash
SKIP_MEMORY_DOCTOR=1 git commit -m "..."
```

### CI gate

```yaml
# GitHub Actions example
- name: memory-doctor
  run: |
    python3 scripts/memory-doctor.py --scan --quiet --exclude scripts/tests
  # exit 0 = clean, 1 = findings, 2 = secret leaked (build fails)
```

### Weekly cron / heartbeat

```bash
# /etc/cron.d/memory-doctor
0 9 * * 1  cd /path/to/workspace && python3 scripts/memory-doctor.py --scan --quiet --exclude scripts/tests || echo "memory-doctor flagged issues" | mail -s "memory-doctor: $(hostname)" leo@example.com
```

## 4. Read the design

For the full semantics (what each check does, why some things are
intentionally not auto-fixed, how to extend the doctor), see
[`docs/DESIGN.md`](DESIGN.md).

## 4b. Silence known false positives

Add a `.memory-doctorignore` at the workspace root (gitignore-style):

```gitignore
# Suppress all SECRET-GHP findings anywhere
code:SECRET-GHP

# Suppress all findings under memory/archive/
path:memory/archive

# Suppress a specific finding (code + path + line)
code:STALE-ITEM path:.learnings/LEARNINGS.md:40-50

# Multiple conditions on one line: must match both code AND path
code:DUPLICATE-KEY path:MEMORY.md:9
```

Suppressed findings are still printed (with a `[suppressed]` tag and
the rule that matched) so the audit trail stays complete, but they
**do not** count toward the worst-severity exit code.

Use `--ignore-file path/to/other` to point at a different filename
(useful for testing rules in CI).

## 5. Run the tests

```bash
python3 scripts/tests/test_memory_doctor.py
# Ran 19 tests in 2.0s
# OK
```

The tests are hermetic (use `tempfile.mkdtemp()` for fixtures) and
have no external dependencies.

## What the doctor checks

| Code | Severity | Auto-fix? | Catches |
|---|---|---|---|
| `STALE-ITEM` | low | no | LRN/ERR entries not seen for >90 days |
| `DUPLICATE-KEY` | medium | no | Same `**Key:**` with conflicting values in one file |
| `DANGLING-REF` | medium | yes | Ontology graph points at a missing entity id |
| `SECRET-LEAK` | critical | **NEVER** | ghp_/sk-/xoxb-/PEM/Bearer patterns on disk |
| `BUDGET-MEMORY` | low | no | MEMORY.md exceeds line budget |
| `BUDGET-SECTION` | low | no | A section exceeds line budget |
| `FILE-MISSING` | info | no | Core bootstrap files absent |
| `ONTOLOGY-STRUCT` | medium/info | no | Malformed graph.jsonl or missing schema |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | clean (no findings, no suppressions) |
| 1 | unsuppressed findings |
| 2 | **secret leaked** (always fail) |
| 3 | internal error |
| 4 | clean, but `N` findings were suppressed via `.memory-doctorignore` |

Exit code 4 lets a CI gate tell the difference between "the workspace
is healthy" (0) and "the workspace is healthy **but** you have
suppression rules in play" (4). Use 4 to flag drift in how many
suppressions a workspace is accumulating.

## When something fires

1. **Read the message and suggestion.** They're specific.
2. **Open the file at the reported path:line.**
3. **Decide.** The doctor is a sensor, not an actuator. It never
   deletes data, never rotates credentials. You do.
4. **For secrets:** rotate the credential *first*, then remove the
   leaked line. Removing without rotating is worse than not removing.

## License

MIT. See `LICENSE`.
