# memory-doctor. Design Document

> 🌐 [English](DESIGN.md) | [简体中文](DESIGN.zh-CN.md)

> Health check for an agent's long-term memory system.
> Stdlib-only Python 3, no external dependencies, safe by default.

---

## 1. Motivation

A long-running AI agent accumulates state across many sessions: curated
long-term notes (`MEMORY.md`), per-day logs (`memory/YYYY-MM-DD.md`),
in-process learnings (`.learnings/*.md`), typed knowledge graphs
(`memory/ontology/graph.jsonl`). Over time this state rots in subtle ways:

- **Facts go stale.** A "Last-Seen" date from six months ago is no longer
  trustworthy evidence that the fact still holds.
- **Contradictions creep in.** The same key gets defined in two places
  with slightly different values.
- **Relations dangle.** An entity gets renamed or deleted but a relation
  still references its old id.
- **Secrets leak.** A debugging session accidentally pastes a real API
  key into a daily note. The agent's memory file is now a credential
  disclosure waiting to happen.
- **Memory files bloat.** The curated note grows past what any human
  can scan in 60 seconds, defeating the point of curation.

`memory-doctor` is a single-file, stdlib-only health check that surfaces
all five classes of problem in one pass, with machine-readable output
and CI-friendly exit codes. It is designed to be:

- **Strict enough to be useful.** Narrow, high-precision secret
  patterns; first-stale-wins; explicit `fixable` flag per finding.
- **Safe enough to run unattended.** Read-only by default; `--fix`
  only ever touches findings that opt in; secrets are never
  auto-removed, only reported.
- **Cheap enough to run often.** Sub-second on a typical workspace,
  zero dependencies, no network, no LLM calls.

## 2. Architecture

```
scripts/
├── memory-doctor.py          # 750 LOC, single file
└── tests/
    └── test_memory_doctor.py # 19 hermetic unit tests, ~2s total
```

The script is intentionally **one file**. There is no package layout,
no plugin system, no config-file parser. Configuration happens entirely
through CLI flags. This makes the tool trivial to vendor into any
project. Copy the file, run it, done.

### Data model

```python
@dataclass
class Finding:
    code: str            # e.g. "STALE-ITEM", "SECRET-LEAK"
    severity: str        # info | low | medium | high | critical
    path: str            # repo-relative
    line: int | None     # 1-indexed; None = whole-file
    message: str
    suggestion: str = ""
    fixable: bool = False
```

`severity` ordering is fixed: `critical > high > medium > low > info`.
`fixable` is set per-check; the `--fix` runner only acts on
`fixable=True` findings. `code` is stable across versions and intended
as the long-lived handle for suppression files and CI gates.

### Check pipeline

```
gather files
   │
   ├──> C6 FILE-MISSING         (info, never auto-fix)
   ├──> C5 BUDGET-MEMORY        (low, never auto-fix)
   ├──> C5 BUDGET-SECTION       (low, never auto-fix)
   ├──> C1 STALE-ITEM           (low, never auto-fix)
   ├──> C2 DUPLICATE-KEY        (medium, never auto-fix)
   ├──> C4 SECRET-LEAK          (critical, NEVER auto-fix)
   ├──> C3 DANGLING-REF         (medium, auto-fixable)
   ├──> C7 ONTOLOGY-STRUCT      (medium/info, never auto-fix)
   │
   ▼
sort by severity desc → render → exit
```

The checks are independent and can be run in any order. Sorting by
severity puts critical findings first so they cannot be missed in a
scrollback view.

## 3. Check semantics

### C1 — `STALE-ITEM`

Detects `## [TYPE-YYYYMMDD-NNN] (kind)` headers in any file matched by
`MEMORY.md`, `.learnings/*.md`, or `memory/YYYY-MM-DD.md`. Within each
entry, looks for `Last-Seen` (preferred) or `Logged` (fallback) date
fields. If the older of the two dates is more than `--stale-days` days
in the past, emits a `STALE-ITEM` finding.

**Why two signals?** `Last-Seen` is the authoritative "this still
holds" timestamp. `Logged` is the original creation date, useful as a
fallback when `Last-Seen` is missing. First hit wins.

**Deliberate non-action.** The doctor does *not* delete or move stale
items. Stale ≠ wrong. The user gets a "verify" prompt and decides.

**Default threshold: 90 days.** Long enough that day-to-day churn
doesn't trigger noise, short enough that a forgotten entry gets
attention within a quarter.

### C2 — `DUPLICATE-KEY`

Parses every line of the form:

```markdown
- **Key:** value
- Key: value         (no bold)
- * **Key:** value   (any list marker)
```

(Format detection is intentionally liberal. See the regex in
`_check_duplicates`.) If the same key appears more than once in a single
file *and* the values disagree, emit a `DUPLICATE-KEY` finding listing
every line and every value. If the values agree, the duplicate is
considered intentional redundancy (e.g. mirrored across sections) and
silently ignored.

**Why per-file, not cross-file?** Cross-file duplicate detection is
much noisier and produces false positives on intentional splits. If you
need it, the tool is small enough to fork.

### C3 — `DANGLING-REF`

Parses `memory/ontology/graph.jsonl` line by line. Each line is one of:

```json
{"op": "create", "entity": {"id": "pers_xxxx", ...}}
{"op": "relate", "relation": {"from": "a", "to": "b", "type": "..."}}
```

A `relate` whose `from` or `to` references an id that has not been
created (or has been created *later* in the file) emits a
`DANGLING-REF` finding with the exact missing id.

**This is the only check that supports `--fix`.** The fix rewrites
the file with the offending line removed, atomically (write to temp
file then `Path.replace`). It refuses to touch a line that wasn't
flagged, so running `--fix` is idempotent.

**Known limitation:** the current implementation does not handle
`op: "delete"`: a deleted entity that was previously referenced will
not be detected as dangling. Tracked as a future extension.

### C4 — `SECRET-LEAK` 🔴

The most important check. Scans **every plausible text file** in the
workspace (skipping `.git`, `.openclaw`, `node_modules`, `__pycache__`,
and a list of binary suffixes) for seven high-precision patterns:

| Code | Pattern | Catches |
|---|---|---|
| `SECRET-GHP` | `\bghp_[A-Za-z0-9]{20,}\b` | GitHub classic PAT |
| `SECRET-PAT` | `\bgithub_pat_[A-Za-z0-9_]{20,}\b` | GitHub fine-grained PAT |
| `SECRET-OAI` | `\bsk-[A-Za-z0-9]{20,}\b` | OpenAI API key |
| `SECRET-SLACK` | `\bxox[abop]-[A-Za-z0-9-]{10,}\b` | Slack token |
| `SECRET-GOOGLE` | `\bAIza[0-9A-Za-z_\-]{20,}\b` | Google API key |
| `SECRET-PEM` | `-----BEGIN ...PRIVATE KEY-----` | PEM private key block |
| `SECRET-BEARER` | `Bearer <40+ chars>` | Long bearer token in a header |

**Deliberate non-action, even with `--fix`.** The doctor reports a
`critical` finding and exits with code `2`. It will never remove
the line, never rewrite the file. The reasoning:

1. Removing a secret without rotating it is worse than leaving it
   visible. An attacker with read access now has credentials that
   the operator thinks are deleted.
2. A line that *looks* like a secret might be a test fixture, a
   documentation example, or a placeholder. Auto-rewriting is a great
   way to corrupt the user's data.
3. The user (or their CI) is the right place to make the rotate-or-
   delete decision. The doctor is a sensor, not an actuator.

**Why narrow patterns?** Recall here would be a disaster; false
positives train users to ignore the doctor. A 20-character minimum
on GitHub-style tokens is conservative because real tokens are 36+
chars. Long base64-looking strings without a known prefix are
intentionally not flagged (too noisy).

**Exit code matrix:**

| Code | Meaning | When |
|---|---|---|
| 0 | clean | no unsuppressed findings, no suppressions used |
| 1 | findings | at least one unsuppressed finding |
| 2 | secret leaked | any unsuppressed `SECRET-*` finding |
| 3 | internal error | unexpected exception during scan |
| 4 | clean, but `N` findings were suppressed | `.memory-doctorignore` matched, and there are no unsuppressed findings |

A suppressed finding does not count toward the worst-severity bucket.
If the scan would otherwise be clean AND suppressions were used,
we surface that with exit code 4 so a CI gate can flag drift. A
secret that is suppressed does not trigger exit 2; it counts toward
the suppressed bucket and contributes to exit 4 instead.

**Output redaction (`--redact`, default ON).** A real secret in the
doctor's output is a re-leak. By default the matched secret-shaped
substring in the finding's `message` is replaced with
`<REDACTED:CODE>` so the operator can still locate the leak in the
file but the token value itself is hidden. The escape hatch
`--no-redact` shows the full value; use it only when the output is
going somewhere you control (a local rotate script, an encrypted
note). The default is ON for v1.1+; pre-v1.1 always showed the
full value.

### C5 — `BUDGET` (MEMORY and section)

`BUDGET-MEMORY`: the whole `MEMORY.md` has more than
`--max-memory-lines` lines (default 300). Emits a single low-severity
finding pointing at the file with no specific line.

`BUDGET-SECTION`: any `## section` has more than
`--max-section-lines` lines (default 50). Emits a low-severity finding
pointing at the section header. If the file has no `##` headers, the
whole file is treated as one implicit section.

**Why split MEMORY and per-section?** A 1000-line MEMORY.md with 30
sections of 33 lines each is not a budget problem. It's just a
well-organized long document. A 200-line file with one 180-line
section is bad: that section needs splitting. The two checks catch
different pathologies.

### C6 — `FILE-MISSING`

Emits an `info` finding for every missing file in
`[MEMORY.md, AGENTS.md, SOUL.md, IDENTITY.md, USER.md, TOOLS.md]`. Info
severity means: it does not contribute to the exit code. The reasoning
is that a "missing" file may simply mean the user is running the
doctor in a sub-tree, or that the file is deliberately absent (e.g.
in a vendored copy of just the doctor).

### C7 — `ONTOLOGY-STRUCT`

Two flavours:

- **Malformed JSON line** in `graph.jsonl` → `medium` finding with
  the line number.
- **Missing `schema.yaml`** → `info` finding. Schema is optional, so
  this is advisory only.

## 4. Output formats

### Human-readable (default)

```text
memory-doctor @ /home/vmser/.openclaw/workspace
  generated: 2026-06-05T13:50:33Z
  scanned:   5 memory files, 48 text files
  findings:  3 (worst=critical)

── 🔴 CRITICAL ──
  SECRET-GHP  scripts/tests/test_memory_doctor.py:221
      GitHub personal access token detected: '...'
      → Rotate the credential immediately...

── 🟡 MEDIUM ──
  DUPLICATE-KEY  MEMORY.md:9
      Key 'Timezone' has conflicting values...
```

Findings are grouped by severity, worst first. Each finding shows the
code, the location, the message, and an actionable suggestion. The
`[fixable]` tag marks findings that `--fix` can repair.

### JSON (`--json`)

```json
{
  "workspace": "/home/vmser/.openclaw/workspace",
  "generated_at": "2026-06-05T13:50:33Z",
  "stats": { "memory_files_scanned": 5, "text_files_scanned": 48 },
  "summary": {
    "total": 3,
    "by_severity": {"critical": 1, "medium": 1, "info": 1},
    "by_code": {"SECRET-GHP": 1, "DUPLICATE-KEY": 1, "FILE-MISSING": 1},
    "worst": "critical"
  },
  "findings": [
    {"code": "SECRET-GHP", "severity": "critical", "path": "...", "line": 221, "message": "...", "suggestion": "...", "fixable": false}
  ]
}
```

`summary.worst` is the highest severity present (or `"none"`). This is
the recommended single field for CI status checks.

### Quiet (`--quiet`)

```text
⚠️  3 finding(s), worst=critical
```

One line. Designed for cron, heartbeat, status bar, and pre-commit
hooks that just need a pass/fail signal.

## 5. Exit code policy

| Code | Meaning | When |
|---|---|---|
| 0 | clean | no findings |
| 1 | findings | at least one finding, none critical |
| 2 | secret leaked | any `SECRET-*` finding |
| 3 | internal error | unexpected exception during scan |

Exit code `2` is **reserved** for secrets. This is a deliberate
choice: it lets a CI gate (or a cron job, or a pre-push hook) trip
a different alarm on a credential leak than on a stale-item note.
The two are very different operational responses and the exit code
distinguishes them without parsing JSON.

## 6. Extension points

The current design has three places where the doctor can be extended
without forking:

1. **New check functions.** Each check has the signature
   `_check_*(workspace, *args) -> list[Finding]`. Add a new function,
   wire it into `run_doctor`, and add a finding code to the table
   above. The new code becomes the stable handle for suppression.

2. **`--exclude` for sub-trees.** Pass a relative path (repeatable)
   to skip a directory in the text-file scan. Memory-style files
   (`MEMORY.md`, `.learnings/*.md`, `memory/*.md`) are *never* skipped
   The doctor's job is to audit those.
3. **Project config (`.memory-doctor.json`).** Optional JSON at the
   workspace root. Schema:

   ```json
   {
     "stale_days": 60,
     "max_memory_lines": 200,
     "max_section_lines": 30,
     "exclude": ["scripts/tests"],
     "redact": true,
     "ignore_file": ".memory-doctorignore"
   }
   ```

   CLI flags always win over the file. A file with an unknown key,
   wrong type, or invalid JSON triggers exit code 3 (internal error)
   with a message pointing at the offending file. A missing file is
   silent.

3. **Suppression file (`.memory-doctorignore`).** Gitignore-style
   syntax. Each non-comment, non-blank line is a rule:

   ```
   code:CODE                       # suppress all findings with this code
   path:RELATIVE_PATH              # suppress all findings at this path (glob)
   code:CODE path:REL[:LINE[-LINE]]
                                   # suppress findings matching code AND
                                   # path (optionally a line or line range)
   ```

   The doctor loads the file at the workspace root, parses each rule
   into a `(codes, path_glob, line_lo, line_hi)` tuple, and applies
   them in order. The first matching rule wins. Suppressed findings
   remain in the report with `suppressed: true` and a `suppress_reason`
   string, but do not count toward the worst-severity exit code
   (Step 3 wires exit code 4 for "clean but suppressed"). The
   `--ignore-file` flag overrides the default filename.

   See `QUICKSTART.md` for worked examples.

## 7. Stdlib only

The doctor is meant to be vendored into any Python project with zero
friction. Adding a dependency (even a small one) creates a setup
tax, a security surface, and a version-pin headache. The checks we
need (regex, file walking, JSON parsing, dataclass) are all in the
stdlib since Python 3.2.

If you find yourself wanting a third-party HTML renderer, a fuzzy
matcher, or a TOML parser, that's a signal the check is doing
something the doctor probably shouldn't be doing. Push the complexity
upstream into a transformation step that runs *before* the doctor.

## 8. Testing strategy

19 unit tests, all hermetic, total runtime ~2 seconds:

| Bucket | Count | Approach |
|---|---|---|
| `STALE-ITEM` | 3 | seed entries with various `Last-Seen` ages, assert threshold behavior |
| `DUPLICATE-KEY` | 2 | seed conflicting vs agreeing duplicates, assert only conflicts fire |
| `DANGLING-REF` | 3 | seed graph with missing target, run `--fix`, assert removal |
| `SECRET-LEAK` | 3 | seed real-shape tokens in `tempfile.mkdtemp()` (never the audited tree) |
| `BUDGET` | 2 | seed oversized files, assert both MEMORY and per-section fire |
| `FILE-MISSING` | 1 | create then delete a core file |
| Output formats | 3 | assert `--json` validity, `--quiet` line count, human output keywords |
| `--exclude` | 2 | assert that test-fixture paths are skipped, that memory files are not |

The secret-leak tests use `tempfile.mkdtemp()` rather than seeding
tokens into the audited test file, so the **fixture itself never
triggers a false positive** when the doctor is run against the test
directory in production.

To run:

```bash
python3 scripts/tests/test_memory_doctor.py
```

## 9. Operational notes

### Pre-commit integration

`scripts/hooks/post-commit` shows the recommended local pattern:
register a hook via `git config core.hooksPath scripts/hooks`, then
the hook runs `memory-doctor --scan --quiet --exclude scripts/tests`
after every commit. The hook never blocks the commit. It prints
findings if any exist and exits 0 either way. To skip for a specific
commit: `SKIP_MEMORY_DOCTOR=1 git commit ...`.

### CI gate

The recommended CI integration is one line:

```bash
python3 scripts/memory-doctor.py --scan --quiet --exclude scripts/tests
```

followed by a check on the exit code: `2` fails the build
unconditionally, `1` warns, `0` passes. For richer integration,
consume `--json` and fail on `summary.worst in {"critical", "high"}`.

### Periodic run

A weekly cron or heartbeat is the right cadence for the staleness
check. Daily is overkill (entries don't go stale overnight). Monthly
is too slow (you'll have a year of un-triaged findings by the time
you notice). The 7-day window is a sweet spot for a typical agent.

## 10. Limitations and known gaps

- **No cross-file duplicate detection.** Intentional. See C2 above.
- **No `op: "delete"` handling in the graph.** Tracked.
- **No secret redaction in output.** When a secret is found, the
  doctor prints the first 80 characters of the offending line. This
  is a privacy trade-off. A redacted output would be safer to paste
  into chat, but harder to act on. The recommendation is to treat
  any doctor run that produced a `SECRET-*` finding as sensitive
  and not paste the output into shared channels.
- **No streaming scan.** For workspaces with thousands of files,
  the text-file scan walks the whole tree on every run. This is
  fine for typical agent workspaces (≤ a few hundred files) but
  would need streaming for monorepo-scale directories. If you hit
  that, file an issue. The design accommodates it via a generator
  in `_gather_files`.
- **English-only suggestions.** The human-readable output is in
  English. The `suggestion` field is a string in the JSON output and
  is the right place to localize if needed.

## 11. License

MIT. See `LICENSE` in the repo root.
