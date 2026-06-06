# memory-doctor

> 🌐 [English](README.md) | [简体中文](README.zh-CN.md)

A health check for an AI agent's long-term memory system.

- **Stdlib only.** No install step, no dependencies, no network.
- **Single file.** `scripts/memory-doctor.py`, ~1300 lines.
- **Safe by default.** Read-only; `--fix` only acts on findings that opt in.
- **CI-friendly.** Exit codes distinguish "findings" (1) from "secret leaked" (2).

```bash
$ python3 scripts/memory-doctor.py --scan --quiet
⚠️  3 finding(s), worst=critical
```

By default, secret substrings in the output are masked as
`<REDACTED:CODE>` so a real token in the report can't be re-leaked
when you paste the output. Use `--no-redact` to reveal the full
value (only when the output stays in a trusted channel).

## What it catches

| Code | Severity | Example |
|---|---|---|
| `SECRET-LEAK` | 🔴 critical | A GitHub PAT pasted into a daily note |
| `DUPLICATE-KEY` | 🟡 medium | `**Timezone:**` defined in two sections with different values |
| `DANGLING-REF` | 🟡 medium | Ontology relation points at a deleted entity id |
| `BUDGET-MEMORY` | 🔵 low | `MEMORY.md` past 300 lines |
| `BUDGET-SECTION` | 🔵 low | A single section past 50 lines |
| `STALE-ITEM` | 🔵 low | LRN/ERR entry not seen for >90 days |
| `FILE-MISSING` | ⚪ info | Core bootstrap file absent |
| `ONTOLOGY-STRUCT` | 🟡/⚪ | Malformed graph.jsonl or missing schema |
| `EMPTY-HEADER` | 🔵 low | `## heading` with no body in `.learnings/*.md` |
| `BUDGET-MEMORY-SOFT` / `HARD` / `CRIT` | ⚪/🔵/🟡 | 3-tier graded warning at 200 / 300 / 500 lines |
| `ONTOLOGY-ISOLATED` | 🔵 low | Ontology node with no relations |
| `DAILY-MEMORY-NAME` | 🔵 low | `memory/*.md` filename does not match `YYYY-MM-DD.md` |

For the full semantics of each check, see [`docs/DESIGN.md`](docs/DESIGN.md).

Suppress known false positives with a gitignore-style
[`.memory-doctorignore`](docs/QUICKSTART.md#4b-silence-known-false-positives).

## Quick start

```bash
# 1. Get the file
git clone https://github.com/Leorand-dev/memory-doctor.git
cd memory-doctor

# 2. Run it
python3 scripts/memory-doctor.py --scan

# 3. (Optional) pre-commit hook
git config core.hooksPath scripts/hooks
```

Full instructions in [`docs/QUICKSTART.md`](docs/QUICKSTART.md).

## Tests

```bash
python3 -m unittest scripts.tests.test_memory_doctor
# Ran 53 tests in ~6s
# OK
```

## CI / GitHub Actions

The project ships an official GitHub Action in `action/`. Drop this
into any project with a Python workspace:

```yaml
- uses: Leorand-dev/memory-doctor@v1
  with:
    workspace: .
    fail-on: medium
    redact: "true"
    exclude: scripts/tests
```

See [`docs/ACTION.md`](docs/ACTION.md) for inputs, outputs, and
the exit-code-to-job-status map.

## Design principles

1. **Sensor, not actuator.** The doctor reports; the user decides.
   The only exception is `DANGLING-REF`, which is unambiguously a
   graph hygiene fix and is gated behind `--fix`.
2. **Secrets are sacred.** `SECRET-LEAK` is the highest severity
   finding and is **never auto-removed under any flag**. Exit code
   `2` is reserved for secrets so a CI gate can fail-loud without
   parsing JSON.
3. **No new dependencies.** The doctor runs on the Python 3.8+
   standard library and nothing else. Vendoring is one file copy.
4. **High precision over high recall.** A false positive trains
   users to ignore the doctor. The secret regexes are intentionally
   narrow.

## License

MIT. See [`LICENSE`](LICENSE).
