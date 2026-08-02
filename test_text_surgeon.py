#!/usr/bin/env python3
"""Integration tests for text_surgeon.py (Protocol v2 pipeline).

Covers: response parsing (@@EDIT / JSON v2 / JSON v1), engine error mapping,
end-to-end apply against real files, CLI flows, and prompt generation.
"""

import json
import os
import shutil
import tempfile
import unittest

import text_surgeon as ts

DOC = """# Release Notes

## Summary

The scheduler was rewritten to use a priority queue instead of a linked
list, cutting dispatch latency by an order of magnitude under load.

## Known Issues

Retries are not yet jittered, so synchronized clients can still stampede
the backend after a regional failover event concludes.
"""


class ParseResponseTests(unittest.TestCase):
    def test_markdown_blocks_parsed(self):
        raw = (
            "<EXPLANATION>\nRewriting the summary.\n</EXPLANATION>\n\n"
            "@@EDIT anchor\n"
            "START-ANCHOR: The scheduler was rewritten to use\n"
            "END-ANCHOR: order of magnitude under load.\n"
            "<<<\n"
            "The scheduler now uses a calendar queue.\n"
            ">>>\n"
        )
        explanation, edits = ts.parse_ai_response(raw)
        self.assertEqual(explanation, "Rewriting the summary.")
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].strategy, "anchor")

    def test_v2_json_parsed(self):
        raw = json.dumps([{
            "strategy": "anchor",
            "start_anchor": "Retries are not yet jittered,",
            "end_anchor": "failover event concludes.",
            "replace": "Retries are jittered now.",
        }])
        _, edits = ts.parse_ai_response(raw)
        self.assertEqual(edits[0].strategy, "anchor")

    def test_v1_json_still_accepted(self):
        raw = ('<EXPLANATION>x</EXPLANATION>\n'
               '[{"search": "priority queue", "replace": "calendar queue"}]')
        _, edits = ts.parse_ai_response(raw)
        self.assertEqual(edits[0].strategy, "verbatim")

    def test_json_inside_code_fence(self):
        raw = ('```json\n[{"search": "a", "replace": "b"}]\n```')
        _, edits = ts.parse_ai_response(raw)
        self.assertEqual(len(edits), 1)

    def test_malformed_markdown_block_raises_payload_error(self):
        with self.assertRaises(ts.PayloadError):
            ts.parse_ai_response("@@EDIT anchor\nSTART-ANCHOR: a\n")

    def test_rtl_response_with_bidi_tainted_fences(self):
        # Full apply-path replay of the reported RTL failure.
        RLM = "‏"
        raw = (
            "<EXPLANATION>بازنویسی</EXPLANATION>\n\n"
            "@@EDIT anchor\n"
            "START-ANCHOR: The scheduler was rewritten to use\n"
            "END-ANCHOR: order of magnitude under load.\n"
            "<<<" + RLM + "\n"
            "متن تازه جایگزین شد.\n"
            ">>>" + RLM + "\n"
        )
        explanation, edits = ts.parse_ai_response(raw)
        self.assertEqual(explanation, "بازنویسی")
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].strategy, "anchor")

    def test_edit_token_detected_despite_leading_bidi_mark(self):
        raw = "‏@@EDIT verbatim\n" "SEARCH is not used; verbatim needs JSON\n"
        # Leading RLM must not hide the @@EDIT route; it will then fail on the
        # missing fence (proving it took the markdown branch, not JSON).
        with self.assertRaises(ts.PayloadError):
            ts.parse_ai_response(raw)

    def test_malformed_json_edit_raises_payload_error(self):
        with self.assertRaises(ts.PayloadError):
            ts.parse_ai_response('[{"strategy": "anchor", "replace": "x"}]')


class SpliceMappingTests(unittest.TestCase):
    def _doc(self, text=DOC):
        d = ts.Document.__new__(ts.Document)
        d.path, d.text, d.had_bom = "/tmp/x.md", text, False
        return d

    def test_applied_records_carry_selection_metadata(self):
        _, edits = ts.parse_ai_response(
            "@@EDIT anchor\n"
            "START-ANCHOR: The scheduler was rewritten to use\n"
            "END-ANCHOR: order of magnitude under load.\n"
            "<<<\nNew paragraph.\n>>>\n"
        )
        new_text, applied, skipped = self._doc().splice(edits)
        self.assertIn("New paragraph.", new_text)
        record = applied[0]
        self.assertEqual(record.strategy, "anchor")
        self.assertEqual(record.line, 5)
        self.assertEqual(record.end_line, 6)
        self.assertEqual(len(record.sha256), 64)
        self.assertIn("priority queue", record.edit.search)  # resolved removal

    def test_engine_errors_become_surgical_errors_with_suggestions(self):
        doc_text = DOC + "\nThe scheduler was rewritten to use magic beans.\n"
        _, edits = ts.parse_ai_response(
            "@@EDIT anchor\n"
            "START-ANCHOR: The scheduler was rewritten to use\n"
            "END-ANCHOR: order of magnitude under load.\n"
            "<<<\nX\n>>>\n"
        )
        with self.assertRaises(ts.SurgicalError) as ctx:
            self._doc(doc_text).splice(edits)
        message = str(ctx.exception)
        self.assertIn("Edit #1", message)
        self.assertIn("ANCHOR_NOT_UNIQUE", message)
        self.assertIn("Unique start-anchor extensions", ctx.exception.hint)
        self.assertIn('"', ctx.exception.hint)  # concrete quoted suggestion

    def test_overlap_maps_to_surgical_error(self):
        raw = json.dumps([
            {"strategy": "anchor",
             "start_anchor": "The scheduler was rewritten to use",
             "end_anchor": "order of magnitude under load.",
             "replace": "A"},
            {"strategy": "verbatim", "search": "priority queue", "replace": "B"},
        ])
        _, edits = ts.parse_ai_response(raw)
        with self.assertRaises(ts.SurgicalError) as ctx:
            self._doc().splice(edits)
        self.assertIn("EDITS_OVERLAP", str(ctx.exception))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="surgeon-test-")
        self.path = os.path.join(self.dir, "notes.md")
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(DOC)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_full_apply_cycle_with_backup_and_memory(self):
        response = (
            "<EXPLANATION>Fix the known issue paragraph.</EXPLANATION>\n"
            "@@EDIT anchor\n"
            "START-ANCHOR: Retries are not yet jittered,\n"
            "END-ANCHOR: failover event concludes.\n"
            "<<<\n"
            "Retries now use decorrelated jitter, eliminating the stampede\n"
            "path entirely.\n"
            ">>>\n"
        )
        response_path = os.path.join(self.dir, "response.txt")
        with open(response_path, "w", encoding="utf-8") as fh:
            fh.write(response)

        code = ts.main([self.path, "--apply", response_path,
                        "--intent", "fix retries"])
        self.assertEqual(code, ts.EXIT_OK)

        with open(self.path, "r", encoding="utf-8", newline="") as fh:
            result = fh.read()
        self.assertIn("decorrelated jitter", result)
        self.assertNotIn("stampede\nthe backend", result)
        # Structure intact:
        self.assertIn("## Known Issues\n\nRetries now use", result)
        self.assertTrue(result.endswith("entirely.\n"))
        # Backup and memory log:
        self.assertTrue(os.path.isfile(self.path + ".bak"))
        memory_path = os.path.join(self.dir, ".surgeon_memory.json")
        with open(memory_path, "r", encoding="utf-8") as fh:
            log = json.load(fh)
        self.assertEqual(log[-1]["intent"], "fix retries")
        self.assertIn("synchronized clients", log[-1]["diff"][0]["removed"])

    def test_dry_run_leaves_file_untouched(self):
        response = (
            '[{"strategy": "verbatim", "search": "priority queue", '
            '"replace": "calendar queue"}]'
        )
        code = ts.main([self.path, "--apply", response, "--dry-run"])
        self.assertEqual(code, ts.EXIT_OK)
        with open(self.path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), DOC)
        self.assertFalse(os.path.isfile(self.path + ".bak"))

    def test_ambiguous_selection_aborts_without_modification(self):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("\nThe scheduler was rewritten to use hopes and dreams.\n")
        with open(self.path, "r", encoding="utf-8") as fh:
            before = fh.read()
        response = (
            "@@EDIT anchor\n"
            "START-ANCHOR: The scheduler was rewritten to use\n"
            "END-ANCHOR: order of magnitude under load.\n"
            "<<<\nX\n>>>\n"
        )
        code = ts.main([self.path, "--apply", response])
        self.assertEqual(code, ts.EXIT_SURGICAL)
        with open(self.path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)

    def test_suggest_anchors_cli(self):
        code = ts.main([self.path, "--suggest-anchors", "5:6"])
        self.assertEqual(code, ts.EXIT_OK)

    def test_crlf_file_end_to_end(self):
        crlf_path = os.path.join(self.dir, "win.md")
        with open(crlf_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(DOC.replace("\n", "\r\n"))
        response = (
            "@@EDIT anchor\n"
            "START-ANCHOR: Retries are not yet jittered,\n"
            "END-ANCHOR: failover event concludes.\n"
            "<<<\nLine A.\nLine B.\n>>>\n"
        )
        code = ts.main([crlf_path, "--apply", response, "--no-backup"])
        self.assertEqual(code, ts.EXIT_OK)
        with open(crlf_path, "rb") as fh:
            data = fh.read()
        self.assertIn(b"Line A.\r\nLine B.", data)
        self.assertNotIn(b"\n\n", data.replace(b"\r\n", b""))  # no bare LFs


class PromptTests(unittest.TestCase):
    def _doc(self):
        d = ts.Document.__new__(ts.Document)
        d.path, d.text, d.had_bom = "/tmp/notes.md", DOC, False
        return d

    def test_generate_prompt_teaches_v2(self):
        prompt = ts.build_surgeon_prompt(self._doc(), "improve summary")
        self.assertIn("SURGEON PROTOCOL v2.0", prompt)
        self.assertIn("@@EDIT anchor", prompt)
        self.assertIn("START-ANCHOR", prompt)
        self.assertIn("NEVER more than 10 words", prompt)
        self.assertIn("<DOCUMENT>", prompt)
        self.assertIn(DOC, prompt)

    def test_verification_prompt_elides_huge_blocks(self):
        removed = "\n".join(f"removed line {i}" for i in range(200))
        applied = [ts.AppliedEdit(
            edit=ts.Edit(search=removed, replace="short"),
            line=10, end_line=209, strategy="anchor", sha256="ab" * 32,
        )]
        prompt = ts.build_verification_prompt("intent", applied, "notes.md")
        self.assertIn("lines omitted for brevity", prompt)
        self.assertIn("removed line 0", prompt)
        self.assertIn("removed line 199", prompt)
        self.assertNotIn("removed line 100", prompt)
        self.assertIn("lines 10-209 via anchor", prompt)


class FollowupPromptTests(unittest.TestCase):
    """The compact same-conversation prompt (no document embed)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="surgeon-followup-")
        self.path = os.path.join(self.dir, "doc.md")
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(DOC)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _doc(self):
        return ts.Document.load(self.path)

    def test_followup_omits_document_and_is_far_smaller(self):
        doc = self._doc()
        full = ts.build_surgeon_prompt(doc, "x")
        follow = ts.build_followup_prompt(doc, "tighten the summary")
        self.assertNotIn("<DOCUMENT>", follow)
        self.assertNotIn(DOC, follow)
        self.assertIn("FOLLOW-UP EDIT", follow)
        self.assertIn("tighten the summary", follow)
        self.assertLess(len(follow), len(full) // 3)

    def test_followup_reminds_of_the_format_rules(self):
        follow = ts.build_followup_prompt(self._doc(), "do it")
        self.assertIn("@@EDIT anchor", follow)
        self.assertIn("EXACTLY ONCE", follow)

    def test_followup_summarizes_changes_since_baseline(self):
        # Full prompt sets the baseline...
        ts.main([self.path, "--generate", "first change"])
        # ...an edit is applied...
        ts.main([self.path, "--apply",
                 '[{"strategy":"verbatim","search":"priority queue",'
                 '"replace":"calendar queue"}]', "--intent", "swap queue"])
        # ...so the follow-up must carry that change, not the document.
        doc = self._doc()
        session = ts.SessionStore(doc.directory)
        baseline = session.get_baseline(doc.name)
        self.assertIsNotNone(baseline)
        follow = ts.build_followup_prompt(doc, "next", baseline=baseline)
        self.assertIn("CHANGE LOG", follow)
        self.assertIn("calendar queue", follow)   # the applied result
        self.assertNotIn("<DOCUMENT>", follow)

    def test_followup_without_baseline_warns_model(self):
        follow = ts.build_followup_prompt(self._doc(), "go", baseline=None)
        # No baseline and no ops -> states the copy is exact but also has no
        # embed; with ops-but-no-baseline it cautions. Here: no ops.
        self.assertNotIn("<DOCUMENT>", follow)
        self.assertIn("FOLLOW-UP", follow)

    def test_generate_full_sets_baseline_followup_does_not(self):
        doc = self._doc()
        session = ts.SessionStore(doc.directory)
        self.assertIsNone(session.get_baseline(doc.name))
        ts.main([self.path, "--generate", "full one"])
        self.assertIsNotNone(session.get_baseline(doc.name))
        # A follow-up generate must not move/replace the baseline digest.
        first = session.get_baseline(doc.name)["sha256"]
        ts.main([self.path, "--generate", "follow one", "--followup"])
        self.assertEqual(session.get_baseline(doc.name)["sha256"], first)

    def test_cli_followup_runs_and_saves_tokens(self):
        ts.main([self.path, "--generate", "seed full"])  # baseline
        code = ts.main([self.path, "--generate", "compact one", "--followup"])
        self.assertEqual(code, ts.EXIT_OK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
