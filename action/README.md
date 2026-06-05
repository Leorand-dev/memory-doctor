# memory-doctor GitHub Action

Drop-in CI integration for the `memory-doctor` Python health check.

## Usage

```yaml
# .github/workflows/memory-doctor.yml
name: memory-doctor
on: [push, pull_request]
permissions:
  contents: read
jobs:
  memory-doctor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: leo-afk-sudo/memory-doctor@v1
        with:
          workspace: .
          fail-on: medium       # info | low | medium | high | critical
          redact: "true"        # strongly recommended
          exclude: scripts/tests
```

## Inputs

| Name | Required | Default | Description |
|---|---|---|---|
| `workspace` | no | `.` | Path to scan, relative to repo root. |
| `fail-on` | no | `medium` | Minimum severity that fails the job. |
| `redact` | no | `true` | Mask secret substrings in the output. |
| `exclude` | no | `""` | Comma-separated relative directories to skip. |
| `args` | no | `""` | Extra args forwarded verbatim (advanced). |

## Outputs

| Name | Description |
|---|---|
| `worst` | Highest severity found: `info`, `low`, `medium`, `high`, `critical`, or `none`. |
| `findings` | Number of unsuppressed findings. |
| `suppressed` | Number of findings suppressed via `.memory-doctorignore`. |
| `exit-code` | Raw exit code: `0` clean, `1` findings, `2` secret, `3` internal, `4` clean-with-suppressions. |

## Exit code → job status

| Doctor exit | Job status |
|---|---|
| 0 | pass (clean) |
| 1 | pass / fail depending on `fail-on` and the worst severity |
| 2 | **fail** (secret leaked — always) |
| 3 | **fail** (internal error — always) |
| 4 | pass (clean but suppressions in use) |

## License

MIT. See `LICENSE` in the repo root.
