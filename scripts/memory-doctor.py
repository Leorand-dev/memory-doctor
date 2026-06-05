#!/usr/bin/env python3
"""
memory-doctor.py — minimal viable health check for the agent memory system.

Five checks, stdlib-only, safe by default:

  C1 STALE-ITEM       MEMORY.md / LRN / ERR entries aged > N days → flag "verify"
  C2 DUPLICATE-KEY    Same `**Key:** value` line repeated in one file, or
                      same concept defined in multiple places with conflict
  C3 DANGLING-REF     Ontology graph references a non-existent entity id
  C4 SECRET-LEAK      File contains a likely credential pattern (NEVER auto-fix)
  C5 BUDGET            MEMORY.md too long, or one section > threshold

Plus two cheap extras:
  C6 FILE-MISSING     Core bootstrap files absent
  C7 ONTOLOGY-STRUCT  Graph JSONL malformed, or schema missing required fields

Usage:
  memory-doctor.py [--workspace DIR] [--scan | --fix | --json] [--quiet]
                   [--stale-days 90] [--max-memory-lines 300]
                   [--max-section-lines 50]

Exit codes:
  0  clean (no findings)
  1  findings present (non-fix checks)
  2  secrets leaked (always non-zero, never auto-fixed)
  3  internal error

Default mode is --scan (read-only, no writes).
`--fix` only enables the safe subset (C3 dangling relation removal is opt-in
via --fix-dangling). Secrets are NEVER auto-removed — only reported.

Design notes (deliberate choices):
  * No external deps. Pure stdlib so it runs anywhere Python 3.8+ lives.
  * No writes unless explicitly opted in.
  * All findings have a stable `code` for downstream tooling / suppressions.
  * Stale threshold is configurable; default 90 days, conservatively long.
  * Secret regexes are intentionally narrow (high precision, low recall).
    Better to miss a weirdly-formatted secret than to false-positive on every
    hex string. (The hook already warns the agent, doctor is the safety net.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

CORE_FILES = ["MEMORY.md", "AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md"]
LEARNINGS_GLOB = ".learnings/*.md"
DAILY_GLOB = "memory/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"
ONTOLOGY_GRAPH = "memory/ontology/graph.jsonl"
ONTOLOGY_SCHEMA = "memory/ontology/schema.yaml"

# Narrow, high-precision secret patterns. Each is a (code, regex, description).
# These are NOT exhaustive — they are the patterns we KNOW to be sensitive.
SECRET_PATTERNS: list[tuple[str, str, str]] = [
    ("SECRET-GHP", r"\bghp_[A-Za-z0-9]{20,}\b", "GitHub personal access token"),
    ("SECRET-PAT", r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "GitHub fine-grained PAT"),
    ("SECRET-OAI", r"\bsk-[A-Za-z0-9]{20,}\b", "OpenAI API key"),
    ("SECRET-SLACK", r"\bxox[abop]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    ("SECRET-GOOGLE", r"\bAIza[0-9A-Za-z_\-]{20,}\b", "Google API key"),
    ("SECRET-PEM", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "PEM private key block"),
    ("SECRET-BEARER", r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]{40,}\b", "Long Bearer token"),
]

# Patterns for ontology entity references (used by C3).
ENTITY_ID_RE = re.compile(r"\b([a-z]{3,4}_[a-f0-9]{6,8})\b")


# --------------------------------------------------------------------------- #
# .memory-doctorignore — parser + matcher
# --------------------------------------------------------------------------- #
#
# Syntax (gitignore-style; # is comment, blank lines ignored):
#
#   code:CODE                    # suppress all findings with this code
#   path:RELATIVE_PATH           # suppress all findings at this path (glob)
#   code:CODE path:REL[:LINE[-LINE]]
#                                # suppress findings matching code AND (path,
#                                # optionally a line or line range)

_IGNORE_LINE_RE = re.compile(
    r"^\s*(?P<body>(?:code:[A-Za-z0-9_-]+|path:[^\s#]+)(?:\s+(?:code:[A-Za-z0-9_-]+|path:[^\s#]+))*)\s*$"
)
_IGNORE_TOKEN_RE = re.compile(r"(code|path):([^\s#]+)")


def _parse_ignore_file(path: Path) -> list[dict]:
    """Parse a .memory-doctorignore file into a list of rule dicts.

    Each rule has:
      - "codes": set[str] | None — None means "match any code"
      - "path_glob": str | None — None means "match any path"
      - "line_lo": int | None
      - "line_hi": int | None — if set, line_lo..line_hi inclusive
      - "raw": str — the original line, for `suppress_reason` reporting
    """
    rules: list[dict] = []
    if not path.exists():
        return rules
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return rules
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _IGNORE_LINE_RE.match(raw)
        if not m:
            continue
        body = m.group("body")
        codes: set[str] = set()
        path_glob: str | None = None
        line_lo: int | None = None
        line_hi: int | None = None
        for kind, val in _IGNORE_TOKEN_RE.findall(body):
            if kind == "code":
                codes.add(val)
            elif kind == "path":
                pv = val
                # path:NAME:N or path:NAME:N-M  (one colon)
                # Real paths rarely contain colons in this domain, so we
                # treat the suffix after the last ":" as a line spec when
                # it parses as N or N-M.
                line_spec: str | None = None
                if ":" in pv:
                    head, _, tail = pv.rpartition(":")
                    if "-" in tail and all(p.isdigit() for p in tail.split("-")):
                        line_spec = tail
                        pv = head
                    elif tail.isdigit():
                        line_spec = tail
                        pv = head
                path_glob = pv
                if line_spec is not None:
                    if "-" in line_spec:
                        lo_s, hi_s = line_spec.split("-", 1)
                        try:
                            line_lo = int(lo_s)
                            line_hi = int(hi_s)
                        except ValueError:
                            pass
                    else:
                        try:
                            line_lo = line_hi = int(line_spec)
                        except ValueError:
                            pass
        rules.append({
            "codes": codes or None,
            "path_glob": path_glob,
            "line_lo": line_lo,
            "line_hi": line_hi,
            "raw": raw,
        })
    return rules


def _matches_rule(finding, rule: dict) -> bool:
    if rule["codes"] is not None and finding.code not in rule["codes"]:
        return False
    if rule["path_glob"] is not None:
        import fnmatch
        if not fnmatch.fnmatchcase(finding.path, rule["path_glob"]):
            return False
    if rule["line_lo"] is not None:
        if finding.line is None:
            return False
        if finding.line < rule["line_lo"]:
            return False
        if rule["line_hi"] is not None and finding.line > rule["line_hi"]:
            return False
    return True


def _apply_ignore_rules(findings: list, rules: list) -> list:
    """Mutates each finding in place: sets `suppressed` and `suppress_reason`."""
    for f in findings:
        for rule in rules:
            if _matches_rule(f, rule):
                f.suppressed = True
                f.suppress_reason = f"matched ignore rule: {rule['raw'].strip()}"
                break
    return findings

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass
class Finding:
    code: str            # e.g. "STALE-ITEM", "SECRET-LEAK"
    severity: str        # info | low | medium | high | critical
    path: str            # repo-relative path
    line: int | None     # 1-indexed; None = whole-file
    message: str         # human-readable
    suggestion: str = "" # what to do
    fixable: bool = False  # can --fix touch this safely?
    suppressed: bool = False  # matched a .memory-doctorignore rule
    suppress_reason: str = ""  # human-readable which rule matched

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Report:
    workspace: str
    generated_at: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    def by_severity(self) -> Counter:
        # Count only unsuppressed findings. A suppressed medium does not
        # count toward the worst-severity exit-code calculation; the
        # suppression itself is reported via the separate `suppressed` count.
        return Counter(f.severity for f in self.findings if not f.suppressed)

    def by_code(self) -> Counter:
        return Counter(f.code for f in self.findings if not f.suppressed)

    def suppressed_count(self) -> int:
        return sum(1 for f in self.findings if f.suppressed)

    def worst(self) -> str:
        sev = self.by_severity()
        for level in ("critical", "high", "medium", "low", "info"):
            if sev.get(level, 0) > 0:
                return level
        return "none"

    def to_dict(self) -> dict:
        suppressed = sum(1 for f in self.findings if f.suppressed)
        return {
            "workspace": self.workspace,
            "generated_at": self.generated_at,
            "stats": self.stats,
            "summary": {
                "total": len(self.findings),
                "unsuppressed": len(self.findings) - suppressed,
                "suppressed": suppressed,
                "by_severity": dict(self.by_severity()),
                "by_code": dict(self.by_code()),
                "worst": self.worst(),
            },
            "findings": [f.to_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _iter_lines(text: str) -> Iterable[tuple[int, str]]:
    for i, line in enumerate(text.splitlines(), 1):
        yield i, line


# --------------------------------------------------------------------------- #
# Check C1 — STALE-ITEM
# --------------------------------------------------------------------------- #

# We detect two staleness signals:
#   1. ## [ID] ... section with "Last-Seen: YYYY-MM-DD" older than threshold
#   2. ## [ID] ... section with "Status: pending" AND "Logged" older than threshold
# Threshold is days; default 90. We do NOT auto-delete — only flag "verify".

ENTRY_HEADER_RE = re.compile(r"^##\s+\[(?P<id>[A-Z]+-\d{8}-\d{3})\]\s+(?P<kind>\w+)")
DATE_RE = r"(\d{4}-\d{2}-\d{2})"
LAST_SEEN_RE = re.compile(
    r"\*?\*?Last-Seen\*?\*?\s*:\s*" + DATE_RE
)
LOGGED_RE = re.compile(
    r"\*?\*?Logged\*?\*?\s*:\s*" + DATE_RE + r"T"
)


def _check_stale(workspace: Path, files: list[Path], stale_days: int) -> list[Finding]:
    out: list[Finding] = []
    cutoff = datetime.now(timezone.utc).date().toordinal() - stale_days
    for f in files:
        text = _read_text(f)
        cur_id: str | None = None
        cur_kind: str | None = None
        last_seen: str | None = None
        logged: str | None = None
        section_start = 0
        for ln, line in _iter_lines(text):
            m = ENTRY_HEADER_RE.match(line)
            if m:
                # flush previous
                if cur_id is not None:
                    out.extend(
                        _eval_stale(
                            workspace, f, cur_id, cur_kind, last_seen, logged, section_start, cutoff
                        )
                    )
                cur_id = m.group("id")
                cur_kind = m.group("kind")
                last_seen = None
                logged = None
                section_start = ln
            if cur_id is None:
                continue
            if (m := LAST_SEEN_RE.search(line)):
                last_seen = m.group(1)
            elif (m := LOGGED_RE.search(line)):
                logged = m.group(1)
        # tail flush
        if cur_id is not None:
            out.extend(
                _eval_stale(workspace, f, cur_id, cur_kind, last_seen, logged, section_start, cutoff)
            )
    return out


def _eval_stale(
    workspace: Path,
    f: Path,
    entry_id: str,
    kind: str | None,
    last_seen: str | None,
    logged: str | None,
    section_start: int,
    cutoff_ordinal: int,
) -> list[Finding]:
    """Compute a single STALE-ITEM finding (or empty list if fresh)."""
    today = datetime.now(timezone.utc).date()
    chosen: tuple[str, int] | None = None
    for label, val in (("Last-Seen", last_seen), ("Logged", logged)):
        if not val:
            continue
        try:
            d = datetime.strptime(val, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d.toordinal() < cutoff_ordinal:
            age = (today - d).days
            chosen = (label, age)
            break  # first hit wins
    if not chosen:
        return []
    label, age = chosen
    return [
        Finding(
            code="STALE-ITEM",
            severity="low",
            path=_rel(f, workspace),
            line=section_start,
            message=f"[{entry_id}] ({kind or '?'}) not seen for {age} days (signal: {label})",
            suggestion="Run `memory-doctor.py --path <file>` to inspect, then either update the entry or archive it.",
            fixable=False,
        )
    ]


# --------------------------------------------------------------------------- #
# Check C2 — DUPLICATE-KEY (within a single file)
# --------------------------------------------------------------------------- #

BOLD_KEY_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?\*\*([^*]+?):\*\*\s*(.*)$"
)


def _check_duplicates(workspace: Path, files: list[Path]) -> list[Finding]:
    out: list[Finding] = []
    for f in files:
        seen: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for ln, line in _iter_lines(_read_text(f)):
            m = BOLD_KEY_RE.match(line)
            if not m:
                continue
            key = m.group(1).strip()
            value = m.group(2).strip()
            if not value or value in ("-", "—", "TBD", "n/a"):
                continue
            seen[key].append((ln, value))
        for key, occurrences in seen.items():
            values = {v for _, v in occurrences}
            if len(occurrences) > 1 and len(values) > 1:
                # conflicting values → report
                lines = ", ".join(f"L{l}" for l, _ in occurrences)
                out.append(
                    Finding(
                        code="DUPLICATE-KEY",
                        severity="medium",
                        path=_rel(f, workspace),
                        line=occurrences[0][0],
                        message=f"Key '{key}' has conflicting values across {lines}: {sorted(values)}",
                        suggestion="Decide on a single value, or rename one of the keys.",
                        fixable=False,
                    )
                )
    return out


# --------------------------------------------------------------------------- #
# Check C3 — DANGLING-REF (ontology graph)
# --------------------------------------------------------------------------- #

def _check_ontology_dangling(workspace: Path) -> tuple[list[Finding], set[str], set[str]]:
    """Returns (findings, entity_ids, dangling_relation_target_ids)."""
    findings: list[Finding] = []
    graph = workspace / ONTOLOGY_GRAPH
    if not graph.exists():
        return findings, set(), set()
    entities: dict[str, dict] = {}
    dangling: list[tuple[int, dict, str]] = []  # (line, rel, missing_id)
    bad_json: list[tuple[int, str]] = []
    with graph.open(encoding="utf-8", errors="replace") as fh:
        for ln, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as e:
                bad_json.append((ln, str(e)))
                continue
            op = rec.get("op")
            ent = rec.get("entity")
            rel = rec.get("relation")
            if op == "create" and isinstance(ent, dict):
                eid = ent.get("id")
                if isinstance(eid, str):
                    entities[eid] = ent
            elif op == "relate" and isinstance(rel, dict):
                for k in ("from", "to"):
                    target = rel.get(k)
                    if isinstance(target, str) and target not in entities:
                        dangling.append((ln, rel, target))
    for ln, err in bad_json:
        findings.append(
            Finding(
                code="ONTOLOGY-STRUCT",
                severity="medium",
                path=ONTOLOGY_GRAPH,
                line=ln,
                message=f"Malformed JSON: {err}",
                suggestion="Inspect the line; remove or fix the record.",
                fixable=False,
            )
        )
    for ln, rel, missing in dangling:
        findings.append(
            Finding(
                code="DANGLING-REF",
                severity="medium",
                path=ONTOLOGY_GRAPH,
                line=ln,
                message=f"Relation references missing entity id: {missing!r}",
                suggestion="Either create the missing entity first, or remove this relation record.",
                fixable=True,  # we know exactly which line to drop
            )
        )
    return findings, set(entities.keys()), {m for _, _, m in dangling}


# --------------------------------------------------------------------------- #
# Check C4 — SECRET-LEAK
# --------------------------------------------------------------------------- #

def _redact_line(line: str, secret_rx: re.Pattern, code: str) -> str:
    """Replace the matched secret-shaped substring with a redaction marker.

    Keeps the line shape (leading context, trailing context) so the operator
    can still locate the leak in the file, but the secret value itself is
    replaced with `<REDACTED:CODE>`. The first 80 chars of the result are
    what the doctor will print.
    """
    redacted = secret_rx.sub(f"<REDACTED:{code}>", line)
    return redacted


def _check_secrets(
    workspace: Path,
    files: list[Path],
    redact: bool = True,
) -> list[Finding]:
    """Scan every text file for known secret-shaped patterns.

    When `redact` is True (default), the secret substring in the printed
    message is replaced with `<REDACTED:CODE>`. Set `redact=False` only
    when the operator needs the full value (e.g. rotating a token via a
    local script in a trusted environment).
    """
    out: list[Finding] = []
    compiled = [(code, re.compile(pat), desc) for code, pat, desc in SECRET_PATTERNS]
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for ln, line in _iter_lines(text):
            for code, rx, desc in compiled:
                if rx.search(line):
                    if redact:
                        shown = _redact_line(line.strip()[:80], rx, code)
                    else:
                        shown = line.strip()[:80]
                    out.append(
                        Finding(
                            code=code,
                            severity="critical",
                            path=_rel(f, workspace),
                            line=ln,
                            message=f"{desc} detected: {shown!r}",
                            suggestion="Rotate the credential immediately. Do NOT commit. Remove the line and replace with a reference (e.g. 'see ~/.config/...').",
                            fixable=False,  # NEVER auto-fix
                        )
                    )
    return out


# --------------------------------------------------------------------------- #
# Check C5 — BUDGET
# --------------------------------------------------------------------------- #

SECTION_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")


def _check_budget(workspace: Path, max_memory_lines: int, max_section_lines: int) -> list[Finding]:
    out: list[Finding] = []
    memory = workspace / "MEMORY.md"
    if not memory.exists():
        return out
    text = _read_text(memory)
    lines = text.splitlines()
    if len(lines) > max_memory_lines:
        out.append(
            Finding(
                code="BUDGET-MEMORY",
                severity="low",
                path="MEMORY.md",
                line=None,
                message=f"MEMORY.md has {len(lines)} lines (threshold {max_memory_lines})",
                suggestion="Promote stale daily logs to archive, or split into per-topic files referenced from MEMORY.md.",
                fixable=False,
            )
        )
    # Per-section line counts. If no ## headers exist, treat the whole file as one
    # implicit "Untitled" section so we still surface oversized bodies.
    cur_section: str | None = None
    cur_count = 0
    cur_start = 1
    sections: list[tuple[str, int, int]] = []  # (name, start, count)
    saw_header = False
    for ln, line in _iter_lines(text):
        m = SECTION_HEADER_RE.match(line)
        if m:
            saw_header = True
            if cur_section is not None:
                sections.append((cur_section, cur_start, cur_count))
            cur_section = m.group(1)
            cur_count = 0
            cur_start = ln
        else:
            cur_count += 1
    if cur_section is not None:
        sections.append((cur_section, cur_start, cur_count))
    elif not saw_header and len(lines) > 0:
        sections.append(("<no sections>", 1, len(lines)))
    for name, start, count in sections:
        if count > max_section_lines:
            out.append(
                Finding(
                    code="BUDGET-SECTION",
                    severity="low",
                    path="MEMORY.md",
                    line=start,
                    message=f"Section '{name}' has {count} lines (threshold {max_section_lines})",
                    suggestion="Split into a sub-file and link it from MEMORY.md.",
                    fixable=False,
                )
            )
    return out


# --------------------------------------------------------------------------- #
# Check C6 — FILE-MISSING (cheap)
# --------------------------------------------------------------------------- #

def _check_core_files(workspace: Path) -> list[Finding]:
    out: list[Finding] = []
    for name in CORE_FILES:
        if not (workspace / name).exists():
            out.append(
                Finding(
                    code="FILE-MISSING",
                    severity="info",
                    path=name,
                    line=None,
                    message=f"Core bootstrap file missing: {name}",
                    suggestion="Either create it from the template, or ignore if deliberately removed.",
                    fixable=False,
                )
            )
    return out


# --------------------------------------------------------------------------- #
# Check C7 — ONTOLOGY-STRUCT (schema)
# --------------------------------------------------------------------------- #

def _check_ontology_schema(workspace: Path) -> list[Finding]:
    schema = workspace / ONTOLOGY_SCHEMA
    if not schema.exists():
        return [
            Finding(
                code="ONTOLOGY-STRUCT",
                severity="info",
                path=ONTOLOGY_SCHEMA,
                line=None,
                message="Ontology schema not found (optional).",
                suggestion="If you use ontology, create schema.yaml; otherwise ignore.",
                fixable=False,
            )
        ]
    return []


# --------------------------------------------------------------------------- #
# Fixers (only ever run if user passes --fix AND the finding is marked fixable)
# --------------------------------------------------------------------------- #

def _fix_dangling_relations(workspace: Path, findings: list[Finding]) -> int:
    """Remove only the graph lines referenced by DANGLING-REF findings.

    Conservative: refuses to touch a line that wasn't flagged.
    Returns the count of lines removed.
    """
    graph = workspace / ONTOLOGY_GRAPH
    if not graph.exists():
        return 0
    targets: set[int] = {
        f.line for f in findings if f.code == "DANGLING-REF" and f.line is not None
    }
    if not targets:
        return 0
    keep: list[str] = []
    removed = 0
    with graph.open(encoding="utf-8", errors="replace") as fh:
        for ln, raw in enumerate(fh, 1):
            if ln in targets:
                removed += 1
                continue
            keep.append(raw if raw.endswith("\n") else raw + "\n")
    if removed:
        # Atomic-ish write: write to temp, replace.
        tmp = graph.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(keep), encoding="utf-8")
        tmp.replace(graph)
    return removed


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def _gather_files(workspace: Path, exclude: tuple[str, ...] = ()) -> tuple[list[Path], list[Path]]:
    """Return (memory_candidate_files, all_text_files).

    `exclude` is a list of directory names (relative to workspace) to skip
    during the *text* scan. Memory-style files (MEMORY.md, .learnings/*.md,
    memory/*.md) are NEVER excluded — the doctor's job is to audit those.
    """
    memory_files: list[Path] = []
    if (workspace / "MEMORY.md").exists():
        memory_files.append(workspace / "MEMORY.md")
    for pattern in (LEARNINGS_GLOB, DAILY_GLOB):
        for p in sorted(workspace.glob(pattern)):
            memory_files.append(p)
    # For secret-leak we scan everything that is plausibly text.
    # Compare against the workspace's RELATIVE parts only — the workspace
    # itself may live under a path like /home/.../.openclaw/workspace/ in
    # which case `.openclaw` would be a parent directory part and would
    # wrongly match the entire tree.
    # Note: we intentionally do NOT skip scripts/tests/ — test fixtures
    # can themselves leak real secrets. If a fixture uses a fake-but-matching
    # token, the test file should use a benign shape that won't match the
    # regex (e.g. an obvious placeholder like ghp_TEST_NOT_A_REAL_TOKEN).
    SKIP_DIRS = {".git", ".openclaw", "node_modules", "__pycache__"}
    BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
                       ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
                       ".mp3", ".mp4", ".mov", ".ogg", ".wav", ".flac"}
    text_files: list[Path] = []
    exclude_parts = {Path(x).parts for x in exclude}
    for q in workspace.rglob("*"):
        if not q.is_file():
            continue
        try:
            rel_parts = q.relative_to(workspace).parts
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        # Exclude via --exclude: any file whose relative path starts with
        # one of the excluded prefixes is skipped.
        if exclude_parts and rel_parts[:1] and any(
            rel_parts[: len(p)] == p for p in exclude_parts
        ):
            continue
        if q.suffix.lower() in BINARY_SUFFIXES:
            continue
        # Skip the doctor itself to avoid scanning its own SECRET_PATTERNS.
        if q.name == "memory-doctor.py":
            continue
        text_files.append(q)
    return memory_files, text_files


def run_doctor(
    workspace: Path,
    stale_days: int = 90,
    max_memory_lines: int = 300,
    max_section_lines: int = 50,
    exclude: Iterable[str] = (),
    redact: bool = True,
    ignore_file_name: str = ".memory-doctorignore",
) -> Report:
    workspace = workspace.resolve()
    report = Report(workspace=str(workspace), generated_at=_now_iso())

    memory_files, text_files = _gather_files(workspace, exclude=tuple(exclude))
    report.stats = {
        "memory_files_scanned": len(memory_files),
        "text_files_scanned": len(text_files),
    }

    for f in _check_core_files(workspace):
        report.add(f)
    for f in _check_budget(workspace, max_memory_lines, max_section_lines):
        report.add(f)
    for f in _check_stale(workspace, memory_files, stale_days):
        report.add(f)
    for f in _check_duplicates(workspace, memory_files):
        report.add(f)
    for f in _check_secrets(workspace, text_files, redact=redact):
        report.add(f)
    ontology_findings, _, _ = _check_ontology_dangling(workspace)
    for f in ontology_findings:
        report.add(f)
    for f in _check_ontology_schema(workspace):
        report.add(f)

    # Apply .memory-doctorignore rules: set `suppressed` on each finding that
    # matches. (Step 3 wires exit code 4 for clean-with-suppressions.)
    rules = _parse_ignore_file(workspace / ignore_file_name)
    report.stats["ignore_rules_loaded"] = len(rules)
    _apply_ignore_rules(report.findings, rules)

    # sort findings: severity desc, then path, then line; suppressed last
    report.findings.sort(
        key=lambda f: (
            f.suppressed,
            -SEVERITY_ORDER.get(f.severity, 0),
            f.path,
            f.line or 0,
        )
    )
    return report


# --------------------------------------------------------------------------- #
# Output formatting
# --------------------------------------------------------------------------- #

SEV_ICON = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "info": "⚪",
}


def render_text(report: Report, quiet: bool = False) -> str:
    if not report.findings:
        return f"✅ {report.workspace} — no findings.\n"
    if quiet:
        suppressed = report.suppressed_count()
        suffix = f", suppressed={suppressed}" if suppressed else ""
        return f"⚠️  {len(report.findings)} finding(s), worst={report.worst()}{suffix}\n"
    lines = [
        f"memory-doctor @ {report.workspace}",
        f"  generated: {report.generated_at}",
        f"  scanned:   {report.stats.get('memory_files_scanned', 0)} memory files, "
        f"{report.stats.get('text_files_scanned', 0)} text files",
        f"  findings:  {len(report.findings)} (worst={report.worst()})",
        "",
    ]
    cur_sev: str | None = None
    for f in report.findings:
        if f.severity != cur_sev:
            lines.append(f"── {SEV_ICON.get(f.severity, '?')} {f.severity.upper()} ──")
            cur_sev = f.severity
        loc = f.path
        if f.line is not None:
            loc = f"{f.path}:{f.line}"
        fixable = "  [fixable]" if f.fixable else ""
        suppressed = "  [suppressed]" if f.suppressed else ""
        lines.append(f"  {f.code}{fixable}{suppressed}  {loc}")
        lines.append(f"      {f.message}")
        if f.suppress_reason:
            lines.append(f"      → {f.suppress_reason}")
        elif f.suggestion:
            lines.append(f"      → {f.suggestion}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="memory-doctor",
        description="Health check for the agent memory system (stdlib only).",
    )
    ap.add_argument(
        "--workspace",
        type=Path,
        default=Path(os.environ.get("OPENCLAW_WORKSPACE", Path.cwd())),
        help="Workspace root to scan (default: $OPENCLAW_WORKSPACE or cwd)",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--scan", action="store_true", help="Read-only scan (default).")
    mode.add_argument(
        "--fix",
        action="store_true",
        help="Apply safe fixes (currently: dangling ontology relations only).",
    )
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ap.add_argument("--quiet", action="store_true", help="Only summary line.")
    ap.add_argument("--stale-days", type=int, default=90, help="Stale threshold (default 90).")
    ap.add_argument(
        "--max-memory-lines", type=int, default=300, help="MEMORY.md line budget (default 300)."
    )
    ap.add_argument(
        "--max-section-lines", type=int, default=50, help="Per-section line budget (default 50)."
    )
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="RELATIVE_DIR",
        help="Skip a directory (relative to workspace) when scanning. Repeatable.",
    )
    redact_group = ap.add_mutually_exclusive_group()
    redact_group.add_argument(
        "--redact",
        dest="redact",
        action="store_true",
        default=True,
        help="Redact matched secret substrings in output (default).",
    )
    redact_group.add_argument(
        "--no-redact",
        dest="redact",
        action="store_false",
        help="Show the full offending line (DANGEROUS — may re-leak the secret).",
    )
    ap.add_argument(
        "--ignore-file",
        default=".memory-doctorignore",
        metavar="PATH",
        help="Filename of the ignore file at the workspace root (default: .memory-doctorignore).",
    )
    args = ap.parse_args(argv)

    try:
        report = run_doctor(
            args.workspace,
            stale_days=args.stale_days,
            max_memory_lines=args.max_memory_lines,
            max_section_lines=args.max_section_lines,
            exclude=args.exclude,
            redact=args.redact,
            ignore_file_name=args.ignore_file,
        )
    except Exception as e:
        sys.stderr.write(f"memory-doctor internal error: {e}\n")
        return 3

    if args.fix:
        # Currently the only safe fix is dangling relations; secrets are NEVER auto-fixed.
        removed = _fix_dangling_relations(args.workspace, report.findings)
        report.stats["dangling_removed"] = removed

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_text(report, quiet=args.quiet))

    # Exit code policy
    #   0  clean (no unsuppressed findings, no suppressions used)
    #   1  unsuppressed findings present
    #   2  secret leaked (always fail)
    #   3  internal error
    #   4  clean, but N findings were suppressed (.memory-doctorignore matched)
    # A suppressed finding does not count toward the worst-severity bucket,
    # but if the scan would otherwise be clean AND suppressions were used,
    # we surface that with exit code 4 so a CI gate can flag drift.
    sev = report.by_severity()
    if sev.get("critical", 0) > 0:
        return 2
    unsuppressed_total = sum(sev.values())
    suppressed = report.suppressed_count()
    if unsuppressed_total > 0:
        return 1
    if suppressed > 0:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
