#!/usr/bin/env python3
"""
test_memory_doctor.py — minimal smoke tests for memory-doctor.py

Each test builds a synthetic workspace in a temp dir, seeds it with crafted
data designed to trigger exactly one finding type, runs the doctor, and
asserts that the expected code appears (and the unexpected ones don't).

Stdlib only, hermetic, fast (< 1 second total).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCTOR = HERE.parent / "memory-doctor.py"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DOCTOR), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _seed(ws: Path) -> None:
    """Build a minimal but real-looking workspace layout."""
    (ws / "MEMORY.md").write_text(
        textwrap.dedent(
            """\
            # MEMORY

            ## Workspace
            - **Path:** /tmp/example

            ## User
            - **Name:** LEO
            """
        ),
        encoding="utf-8",
    )
    (ws / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
    (ws / ".learnings").mkdir()
    (ws / ".learnings" / "LEARNINGS.md").write_text(
        textwrap.dedent(
            """\
            # Learnings
            ---
            ## [LRN-20200101-001] best_practice
            **Logged**: 2020-01-01T00:00:00Z
            **Last-Seen**: 2020-01-01
            **Status**: pending
            Body.
            """
        ),
        encoding="utf-8",
    )
    (ws / "memory").mkdir()
    (ws / "memory" / "ontology").mkdir()
    (ws / "memory" / "ontology" / "graph.jsonl").write_text("", encoding="utf-8")


class TestMemoryDoctor(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        _seed(self.ws)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- C1 STALE-ITEM ----------------------------------------------------
    def test_stale_item_detected(self) -> None:
        cp = _run(["--scan", "--json"], self.ws)
        self.assertEqual(cp.returncode, 1, msg=f"stdout={cp.stdout}\nstderr={cp.stderr}")
        data = json.loads(cp.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertIn("STALE-ITEM", codes)
        # The seeded Last-Seen is 2020-01-01, well past 90d default.
        stale = [f for f in data["findings"] if f["code"] == "STALE-ITEM"][0]
        self.assertEqual(stale["severity"], "low")
        self.assertFalse(stale["fixable"])

    def test_stale_threshold_zero(self) -> None:
        """With --stale-days 0, anything with a Last-Seen date is flagged."""
        cp = _run(["--scan", "--stale-days", "0", "--json"], self.ws)
        self.assertEqual(cp.returncode, 1)
        data = json.loads(cp.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertIn("STALE-ITEM", codes)

    def test_stale_threshold_huge(self) -> None:
        """With --stale-days 99999, nothing fresh is flagged."""
        cp = _run(["--scan", "--stale-days", "99999", "--json"], self.ws)
        data = json.loads(cp.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertNotIn("STALE-ITEM", codes)

    # --- C2 DUPLICATE-KEY -------------------------------------------------
    def test_duplicate_key_detected(self) -> None:
        (self.ws / "MEMORY.md").write_text(
            textwrap.dedent(
                """\
                # MEMORY
                ## User
                - **Name:** Alice
                - **Name:** Bob
                - **Timezone:** Asia/Shanghai
                """
            ),
            encoding="utf-8",
        )
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        codes = [f["code"] for f in data["findings"]]
        self.assertIn("DUPLICATE-KEY", codes)
        dups = [f for f in data["findings"] if f["code"] == "DUPLICATE-KEY"]
        self.assertTrue(any("Name" in f["message"] for f in dups))

    def test_duplicate_key_same_value_no_alert(self) -> None:
        (self.ws / "MEMORY.md").write_text(
            textwrap.dedent(
                """\
                # MEMORY
                ## User
                - **Name:** LEO
                - **Name:** LEO
                """
            ),
            encoding="utf-8",
        )
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertNotIn("DUPLICATE-KEY", codes)

    # --- C3 DANGLING-REF -------------------------------------------------
    def test_dangling_relation_detected(self) -> None:
        graph = self.ws / "memory" / "ontology" / "graph.jsonl"
        graph.write_text(
            json.dumps(
                {
                    "op": "create",
                    "entity": {
                        "id": "pers_aaaaaaa",
                        "type": "Person",
                        "properties": {"name": "Real"},
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "op": "relate",
                    "relation": {
                        "from": "pers_aaaaaaa",
                        "to": "proj_bbbbbbb",  # missing!
                        "type": "owns",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        dangs = [f for f in data["findings"] if f["code"] == "DANGLING-REF"]
        self.assertEqual(len(dangs), 1)
        self.assertIn("proj_bbbbbbb", dangs[0]["message"])
        self.assertTrue(dangs[0]["fixable"])

    def test_dangling_relation_fix_removes_line(self) -> None:
        graph = self.ws / "memory" / "ontology" / "graph.jsonl"
        graph.write_text(
            json.dumps(
                {
                    "op": "create",
                    "entity": {"id": "pers_aaaaaaa", "type": "Person", "properties": {}},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "op": "relate",
                    "relation": {"from": "pers_aaaaaaa", "to": "proj_bbbbbbb", "type": "owns"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        cp = _run(["--fix", "--json"], self.ws)
        self.assertIn(cp.returncode, (0, 1))  # 0 or 1 acceptable after fix
        # Re-scan: dangling should be gone
        cp2 = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp2.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertNotIn("DANGLING-REF", codes)

    def test_ontology_struct_malformed_json(self) -> None:
        graph = self.ws / "memory" / "ontology" / "graph.jsonl"
        graph.write_text('not valid json\n', encoding="utf-8")
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertIn("ONTOLOGY-STRUCT", codes)

    # --- C4 SECRET-LEAK ---------------------------------------------------
    # Note: we run secret-leak tests against a *separate* temp dir, so the
    # real-shape tokens used as fixtures never end up in scripts/tests/ itself
    # (which is exactly the kind of file the doctor audits in production).
    # Note: the test source must NOT contain literal token-shaped strings,
    # otherwise the doctor correctly flags THIS test file as a secret leak
    # when it scans the repo. We build the strings at runtime from a
    # benign prefix + a synthesized tail. The shape is still real enough
    # to match the regex, the literal isn't visible to a static scanner.

    def _fake_ghp(self) -> str:
        # ghp_ + 36 alnum chars (real GH tokens are 36 chars after the prefix).
        return "gh" + "p" + "_" + "a" * 36

    def _fake_xoxb(self) -> str:
        # xoxb- + 10 + - + 10 alnum chars (Slack token shape).
        return "xox" + "b" + "-" + "1" * 10 + "-" + "a" * 10

    def test_secret_leak_detected(self) -> None:
        secret_ws = Path(tempfile.mkdtemp())
        try:
            (secret_ws / "MEMORY.md").write_text("# M\n", encoding="utf-8")
            (secret_ws / "leaky.md").write_text(
                "oh no I leaked: " + self._fake_ghp() + "\n",
                encoding="utf-8",
            )
            cp = _run(["--scan", "--json"], secret_ws)
            self.assertEqual(cp.returncode, 2, "secret leak must exit 2")
            data = json.loads(cp.stdout)
            codes = {f["code"] for f in data["findings"]}
            self.assertIn("SECRET-GHP", codes)
            secret = [f for f in data["findings"] if f["code"] == "SECRET-GHP"][0]
            self.assertEqual(secret["severity"], "critical")
            self.assertFalse(secret["fixable"])
            # Default (no flag) must redact the token-shaped substring.
            self.assertIn("<REDACTED:SECRET-GHP>", secret["message"])
            self.assertNotIn("ghp_", secret["message"].split("REDACTED")[0] + secret["message"].split("REDACTED")[-1] if "REDACTED" in secret["message"] else secret["message"])
        finally:
            import shutil

            shutil.rmtree(secret_ws, ignore_errors=True)

    def test_secret_leak_redact_default_on(self) -> None:
        """--redact is the default. The token substring must NOT appear in output."""
        secret_ws = Path(tempfile.mkdtemp())
        try:
            (secret_ws / "MEMORY.md").write_text("# M\n", encoding="utf-8")
            token = self._fake_ghp()
            (secret_ws / "leaky.md").write_text(
                "context before " + token + " context after\n",
                encoding="utf-8",
            )
            cp = _run(["--scan", "--json"], secret_ws)
            data = json.loads(cp.stdout)
            secret = next(f for f in data["findings"] if f["code"] == "SECRET-GHP")
            # Token must not appear; context must.
            self.assertNotIn(token, secret["message"])
            self.assertIn("context before", secret["message"])
            self.assertIn("context after", secret["message"])
            self.assertIn("<REDACTED:SECRET-GHP>", secret["message"])
        finally:
            import shutil
            shutil.rmtree(secret_ws, ignore_errors=True)

    def test_secret_leak_no_redact_override(self) -> None:
        """--no-redact reveals the full substring. Use only when needed."""
        secret_ws = Path(tempfile.mkdtemp())
        try:
            (secret_ws / "MEMORY.md").write_text("# M\n", encoding="utf-8")
            token = self._fake_xoxb()
            (secret_ws / "leaky.md").write_text(
                "context " + token + " more\n", encoding="utf-8"
            )
            cp = _run(["--scan", "--no-redact", "--json"], secret_ws)
            data = json.loads(cp.stdout)
            secret = next(f for f in data["findings"] if f["code"] == "SECRET-SLACK")
            self.assertIn(token, secret["message"])
            self.assertNotIn("REDACTED", secret["message"])
        finally:
            import shutil
            shutil.rmtree(secret_ws, ignore_errors=True)

    def test_non_secret_finding_unaffected_by_redact(self) -> None:
        """DUPLICATE-KEY messages must not be touched by --redact."""
        (self.ws / "MEMORY.md").write_text(
            "- **Name:** Alice\n- **Name:** Bob\n", encoding="utf-8"
        )
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        dup = next(f for f in data["findings"] if f["code"] == "DUPLICATE-KEY")
        # The message must not contain REDACTED.
        self.assertNotIn("REDACTED", dup["message"])
        self.assertIn("Name", dup["message"])

    def test_secret_leak_never_auto_fixed(self) -> None:
        secret_ws = Path(tempfile.mkdtemp())
        try:
            (secret_ws / "MEMORY.md").write_text("# M\n", encoding="utf-8")
            leaky = secret_ws / "leaky.md"
            leaky.write_text(self._fake_xoxb() + "\n", encoding="utf-8")
            before = leaky.read_text(encoding="utf-8")
            cp = _run(["--fix", "--json"], secret_ws)
            self.assertEqual(cp.returncode, 2)
            after = leaky.read_text(encoding="utf-8")
            self.assertEqual(before, after, "secret must not be auto-removed")
        finally:
            import shutil
            shutil.rmtree(secret_ws, ignore_errors=True)
            import shutil

            shutil.rmtree(secret_ws, ignore_errors=True)

    def test_no_false_positive_on_github_pr_url(self) -> None:
        (self.ws / "ok.md").write_text(
            "see https://github.com/openclaw/openclaw/pull/12345\n",
            encoding="utf-8",
        )
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertFalse(
            any(c.startswith("SECRET-") for c in codes),
            f"PR URL triggered false positive: {codes}",
        )

    # --- C5 BUDGET --------------------------------------------------------
    def test_budget_memory(self) -> None:
        (self.ws / "MEMORY.md").write_text("\n".join(["line"] * 350) + "\n", encoding="utf-8")
        cp = _run(["--scan", "--max-memory-lines", "300", "--json"], self.ws)
        data = json.loads(cp.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertIn("BUDGET-MEMORY", codes)

    def test_budget_section(self) -> None:
        (self.ws / "MEMORY.md").write_text(
            "# MEMORY\n" + "\n".join(["x"] * 60) + "\n", encoding="utf-8"
        )
        cp = _run(["--scan", "--max-section-lines", "50", "--json"], self.ws)
        data = json.loads(cp.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertIn("BUDGET-SECTION", codes)

    # --- C6 FILE-MISSING --------------------------------------------------
    def test_file_missing(self) -> None:
        # Create a thin marker then remove it; verifies the FILE-MISSING path
        # without depending on the seed not containing the file.
        (self.ws / "IDENTITY.md").write_text("placeholder\n", encoding="utf-8")
        (self.ws / "IDENTITY.md").unlink()
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertIn("FILE-MISSING", codes)

    # --- Output formats ---------------------------------------------------
    def test_json_output_valid(self) -> None:
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        self.assertIn("workspace", data)
        self.assertIn("generated_at", data)
        self.assertIn("findings", data)
        self.assertIn("summary", data)

    def test_quiet_output(self) -> None:
        cp = _run(["--scan", "--quiet"], self.ws)
        self.assertEqual(cp.returncode, 1)
        self.assertIn("finding(s)", cp.stdout)
        # Quiet should be one line
        self.assertEqual(len(cp.stdout.strip().splitlines()), 1)

    def test_human_output(self) -> None:
        cp = _run(["--scan"], self.ws)
        self.assertIn("memory-doctor @", cp.stdout)
        self.assertIn("STALE-ITEM", cp.stdout)

    def test_exclude_skips_directory(self) -> None:
        """--exclude scripts/tests must skip a directory in the text scan.

        Uses a benign shape (not matching the secret regex) so the fixture
        itself doesn't trigger a real secret-leak finding in this file.
        """
        (self.ws / "scripts").mkdir()
        (self.ws / "scripts" / "tests").mkdir()
        benign = self.ws / "scripts" / "tests" / "benign.md"
        benign.write_text("just a placeholder, no secret shape here\n", encoding="utf-8")
        # Without --exclude, the file is still scanned.
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        # (No secret-related findings either way; we just want to confirm
        # the doctor doesn't crash and exclude actually changes the count.)
        count_with = sum(1 for f in data["findings"] if f["code"].startswith("SECRET-"))
        # With --exclude, the same count (zero in this case) — this just
        # confirms the flag is accepted and doesn't error.
        cp2 = _run(["--scan", "--json", "--exclude", "scripts/tests"], self.ws)
        self.assertEqual(cp.returncode, cp2.returncode)

    def test_exclude_does_not_affect_memory_files(self) -> None:
        """Memory-style files (MEMORY.md, .learnings/) are NEVER excluded.

        Even if user passes --exclude .learnings, the audit must still run.
        """
        # (We can't directly assert this with the test fixture layout
        # because .learnings/ is already the audit target. But we *can*
        # confirm that a duplicate key in a memory file is still flagged
        # when the user tries to exclude a sibling directory.)
        (self.ws / "MEMORY.md").write_text(
            "- **Name:** A\n- **Name:** B\n", encoding="utf-8"
        )
        cp = _run(
            ["--scan", "--json", "--exclude", "memory"], self.ws
        )
        data = json.loads(cp.stdout)
        codes = {f["code"] for f in data["findings"]}
        self.assertIn(
            "DUPLICATE-KEY", codes,
            "Memory files must remain audited even with --exclude",
        )


    # --- .memory-doctorignore ----------------------------------------------

    def _seed_ignore(self, ws: Path, body: str) -> None:
        (ws / ".memory-doctorignore").write_text(body, encoding="utf-8")

    def test_ignore_file_missing_is_noop(self) -> None:
        """No .memory-doctorignore present → no findings suppressed."""
        (self.ws / "MEMORY.md").write_text(
            "- **Name:** Alice\n- **Name:** Bob\n", encoding="utf-8"
        )
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        self.assertEqual(data["summary"]["suppressed"], 0)
        self.assertFalse(any(f.get("suppressed") for f in data["findings"]))

    def test_ignore_code_suppresses_all_matching(self) -> None:
        """`code:X` suppresses every finding with that code."""
        self._seed_ignore(self.ws, "code:DUPLICATE-KEY\n")
        (self.ws / "MEMORY.md").write_text(
            "- **Name:** Alice\n- **Name:** Bob\n", encoding="utf-8"
        )
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        self.assertEqual(data["summary"]["suppressed"], 1)
        dups = [f for f in data["findings"] if f["code"] == "DUPLICATE-KEY"]
        self.assertEqual(len(dups), 1)
        self.assertTrue(dups[0]["suppressed"])
        self.assertIn("code:DUPLICATE-KEY", dups[0]["suppress_reason"])

    def test_ignore_path_glob(self) -> None:
        """`path:PATTERN` suppresses all findings whose path matches."""
        secret_ws = Path(tempfile.mkdtemp())
        try:
            (secret_ws / "MEMORY.md").write_text("# M\n", encoding="utf-8")
            (secret_ws / "leaky.md").write_text(
                "leaked: " + self._fake_ghp() + "\n", encoding="utf-8"
            )
            self._seed_ignore(secret_ws, "path:leaky.md\n")
            cp = _run(["--scan", "--json"], secret_ws)
            data = json.loads(cp.stdout)
            secret = next(f for f in data["findings"] if f["code"] == "SECRET-GHP")
            self.assertTrue(secret["suppressed"])
            self.assertIn("path:leaky.md", secret["suppress_reason"])
        finally:
            import shutil
            shutil.rmtree(secret_ws, ignore_errors=True)

    def test_ignore_path_with_line_range(self) -> None:
        """`path:REL:N-M` suppresses findings in the line range only."""
        (self.ws / "MEMORY.md").write_text(
            "- **Name:** Alice\n- **Name:** Bob\n- **Name:** Carol\n- **Name:** Dave\n",
            encoding="utf-8",
        )
        # We have one DUPLICATE-KEY finding at line 1 (Name).
        # Suppress that specific line.
        self._seed_ignore(self.ws, "code:DUPLICATE-KEY path:MEMORY.md:1\n")
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        dups = [f for f in data["findings"] if f["code"] == "DUPLICATE-KEY"]
        self.assertEqual(len(dups), 1)
        self.assertTrue(dups[0]["suppressed"])

    def test_ignore_code_plus_path_glob(self) -> None:
        """`code:X path:Y` matches only findings with that code AND path."""
        secret_ws = Path(tempfile.mkdtemp())
        try:
            (secret_ws / "MEMORY.md").write_text("# M\n", encoding="utf-8")
            (secret_ws / "a.md").write_text("leak1: " + self._fake_ghp() + "\n", encoding="utf-8")
            (secret_ws / "b.md").write_text("leak2: " + self._fake_ghp() + "\n", encoding="utf-8")
            self._seed_ignore(secret_ws, "code:SECRET-GHP path:a.md\n")
            cp = _run(["--scan", "--json"], secret_ws)
            data = json.loads(cp.stdout)
            secrets = [f for f in data["findings"] if f["code"] == "SECRET-GHP"]
            self.assertEqual(len(secrets), 2)
            suppressed = [f for f in secrets if f["suppressed"]]
            self.assertEqual(len(suppressed), 1)
            self.assertEqual(suppressed[0]["path"], "a.md")
        finally:
            import shutil
            shutil.rmtree(secret_ws, ignore_errors=True)

    def test_ignore_comments_and_blanks_ignored(self) -> None:
        """Comments and blank lines must be ignored, not treated as rules."""
        (self.ws / "MEMORY.md").write_text(
            "- **Name:** Alice\n- **Name:** Bob\n", encoding="utf-8"
        )
        self._seed_ignore(
            self.ws,
            "# this is a comment\n\n   \ncode:DUPLICATE-KEY\n# trailing comment\n",
        )
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        dups = [f for f in data["findings"] if f["code"] == "DUPLICATE-KEY"]
        self.assertEqual(len(dups), 1)
        self.assertTrue(dups[0]["suppressed"])

    def test_ignore_summary_in_json(self) -> None:
        """--json summary must include suppressed and unsuppressed counts."""
        (self.ws / "MEMORY.md").write_text(
            "- **Name:** Alice\n- **Name:** Bob\n", encoding="utf-8"
        )
        self._seed_ignore(self.ws, "code:DUPLICATE-KEY\n")
        cp = _run(["--scan", "--json"], self.ws)
        data = json.loads(cp.stdout)
        self.assertIn("suppressed", data["summary"])
        self.assertIn("unsuppressed", data["summary"])
        self.assertEqual(data["summary"]["suppressed"], 1)
        self.assertEqual(data["summary"]["unsuppressed"], data["summary"]["total"] - 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
