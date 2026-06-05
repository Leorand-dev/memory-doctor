# memory-doctor

A health check for an AI agent's long-term memory system.

- **Stdlib only.** No install step, no dependencies, no network.
- **Single file.** `scripts/memory-doctor.py`, ~750 lines.
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
| `STALE-ITEM` | 🔵 low | LRN/ERR entry not seen for >90 days |
| `BUDGET-MEMORY` | 🔵 low | `MEMORY.md` past 300 lines |
| `BUDGET-SECTION` | 🔵 low | A single section past 50 lines |
| `FILE-MISSING` | ⚪ info | Core bootstrap file absent |
| `ONTOLOGY-STRUCT` | 🟡/⚪ | Malformed graph.jsonl or missing schema |

For the full semantics of each check, see [`docs/DESIGN.md`](docs/DESIGN.md).

## Quick start

```bash
# 1. Get the file
git clone https://github.com/leo-afk-sudo/memory-doctor.git
cd memory-doctor

# 2. Run it
python3 scripts/memory-doctor.py --scan

# 3. (Optional) pre-commit hook
git config core.hooksPath scripts/hooks
```

Full instructions in [`docs/QUICKSTART.md`](docs/QUICKSTART.md).

## Tests

```bash
python3 scripts/tests/test_memory_doctor.py
# Ran 19 tests in 2.0s
# OK
```

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
