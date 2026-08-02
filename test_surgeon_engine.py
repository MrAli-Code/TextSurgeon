#!/usr/bin/env python3
"""Unit tests for surgeon_engine.py (run: python3 -m unittest -v)."""

import unittest

import surgeon_engine as se
from surgeon_engine import (
    AnchorNotFoundError,
    AnchorNotUniqueError,
    AnchorOrderError,
    AnchorTooLongError,
    CannotUniquifyError,
    ContextMatchError,
    EditOp,
    GuardError,
    InvalidRequestError,
    OverlapError,
    SelectionEngine,
    TagSelectionError,
    apply_edit_ops,
    parse_markdown_edits,
    select_edit_ops,
)

DOC = """# Migration Guide

## Overview

The migration process begins when the operator freezes writes on the
primary cluster. Snapshots are taken in parallel, verified against the
manifest, and shipped to the standby region. Once the standby catches
up, traffic is drained connection by connection.

If verification fails at any point, the coordinator halts the pipeline
and completes the rollback safely.

## Aftermath

The migration process leaves an audit trail in the ledger. Every step
is timestamped so operators can reconstruct the timeline afterwards.
"""


def anchor_op(start, end, replace, **kwargs):
    return EditOp.from_payload_item(
        dict(strategy="anchor", start_anchor=start, end_anchor=end,
             replace=replace, **kwargs),
        1,
    )


class NormalizedMatchingTests(unittest.TestCase):
    def test_whitespace_elastic_match(self):
        """An anchor with plain spaces matches text wrapped across lines."""
        engine = SelectionEngine(DOC)
        op = anchor_op(
            "The migration process begins when the operator",
            "traffic is drained connection by connection.",
            "X",
        )
        sel = engine.select(op)
        self.assertEqual(sel.status, "SELECTION_CONFIRMED")
        self.assertTrue(sel.text.startswith("The migration process begins"))
        self.assertTrue(sel.text.endswith("connection by connection."))

    def test_word_alignment_prevents_substring_hits(self):
        """'the system' must not match inside 'breathe systems'."""
        view = se._NormalizedView("breathe systems are odd. the system works.")
        self.assertEqual(view.count("the system"), 1)
        self.assertEqual(view.count("cat"), 0 if "cat" not in "concatenate" else 0)
        view2 = se._NormalizedView("concatenate")
        self.assertEqual(view2.count("cat"), 0)

    def test_indented_and_rewrapped_anchor(self):
        doc = "def f():\n    return   the\n    final answer now\n"
        view = se._NormalizedView(doc)
        spans = view.find_spans("return the final answer now")
        self.assertEqual(len(spans), 1)
        start, end = spans[0]
        self.assertEqual(doc[start:start + 6], "return")
        self.assertTrue(doc[:end].endswith("now"))

    def test_markdown_emphasis_is_a_boundary(self):
        # **bold** and _italic_ markers must not block anchoring.
        doc = "intro\n\n**نوع پایان‌نامه**: نظری\n\noutro\n"
        sel = SelectionEngine(doc).select(anchor_op(
            "نوع پایان‌نامه", "نوع پایان‌نامه", "X"))
        self.assertEqual(sel.status, "SELECTION_CONFIRMED")
        # The asterisks stay outside the selection:
        self.assertEqual(doc[sel.start - 2:sel.start], "**")

    def test_italic_underscore_and_backtick_boundaries(self):
        doc = "See _Sorghum_ and `config.yaml` today.\n"
        v = se._NormalizedView(doc)
        self.assertEqual(v.count("config.yaml"), 1)   # backtick is a boundary
        # Underscore is a WORD char (code-safe), so bare "Sorghum" won't
        # match inside _Sorghum_ — but the italic quoted with markers does:
        self.assertEqual(v.count("Sorghum"), 0)
        self.assertEqual(v.count("_Sorghum_"), 1)

    def test_word_boundary_still_blocks_midword(self):
        v = se._NormalizedView("the systematic approach and my_system_var")
        self.assertEqual(v.count("the system"), 0)  # inside "systematic"
        self.assertEqual(v.count("system"), 0)      # inside identifier


class AnchorStrategyTests(unittest.TestCase):
    def test_basic_block_replacement(self):
        new_text, applied, skipped = apply_edit_ops(
            DOC,
            [anchor_op(
                "The migration process begins when",
                "completes the rollback safely.",
                "Replaced block.",
            )],
        )
        self.assertIn("## Overview\n\nReplaced block.\n\n## Aftermath", new_text)
        self.assertEqual(len(applied), 1)
        self.assertEqual(skipped, [])
        self.assertEqual(applied[0].selection.strategy, "anchor")

    def test_not_found_reports_closest_passage(self):
        with self.assertRaises(AnchorNotFoundError) as ctx:
            select_edit_ops(DOC, [anchor_op(
                "The migration process begins wen",  # typo
                "completes the rollback safely.",
                "X",
            )])
        details = ctx.exception.details
        self.assertEqual(details["role"], "start anchor")
        self.assertIn("closest_match", details)
        self.assertGreaterEqual(details["closest_match"]["similarity"], 0.8)

    def test_ambiguous_anchor_raises_with_unique_suggestions(self):
        """'The migration process' appears twice; the error must carry
        extended anchors that ARE unique — the self-healing loop."""
        with self.assertRaises(AnchorNotUniqueError) as ctx:
            select_edit_ops(DOC, [anchor_op(
                "The migration process",
                "completes the rollback safely.",
                "X",
            )])
        details = ctx.exception.details["start"]
        self.assertEqual(len(details["occurrences"]), 2)
        suggestions = [s["anchor"] for s in details["suggestions"] if s["anchor"]]
        self.assertTrue(suggestions, "expected at least one unique extension")
        for candidate in suggestions:
            self.assertLessEqual(len(candidate.split()), se.MAX_ANCHOR_WORDS)
            # Each suggestion must now resolve cleanly:
            sel = SelectionEngine(DOC).select(anchor_op(
                candidate, "completes the rollback safely."
                if "begins" in candidate else "reconstruct the timeline afterwards.",
                "X",
            ))
            self.assertEqual(sel.status, "SELECTION_CONFIRMED")

    def test_anchor_over_ten_words_rejected(self):
        with self.assertRaises(AnchorTooLongError):
            select_edit_ops(DOC, [anchor_op(
                "The migration process begins when the operator freezes "
                "writes on the primary",  # 13 words
                "completes the rollback safely.",
                "X",
            )])

    def test_end_before_start_rejected(self):
        with self.assertRaises(AnchorOrderError):
            select_edit_ops(DOC, [anchor_op(
                "completes the rollback safely.",
                "The migration process begins when",
                "X",
            )])

    def test_occurrence_index_resolution(self):
        sel = SelectionEngine(DOC).select(anchor_op(
            "The migration process",
            "reconstruct the timeline afterwards.",
            "X",
            occurrence_start=2,
        ))
        self.assertEqual(sel.resolution, "indexed")
        self.assertIn("audit trail", sel.text)

    def test_pair_resolution_single_pairing(self):
        doc = "alpha start A\nfiller\nend B omega\nalpha start A\nno end here\n"
        # start anchor x2, end anchor x1 -> exactly one tight pair (first one)
        sel = SelectionEngine(doc).select(EditOp.from_payload_item(
            dict(strategy="anchor", start_anchor="alpha start A",
                 end_anchor="end B omega", replace="X", resolution="pair"),
            1,
        ))
        self.assertEqual(sel.resolution, "pair")
        self.assertEqual(sel.start_line, 1)
        self.assertEqual(sel.end_line, 3)

    def test_pair_resolution_multiple_pairings_rejected(self):
        doc = ("alpha start\nbody one\nomega end\n"
               "alpha start\nbody two\nomega end\n")
        with self.assertRaises(AnchorNotUniqueError) as ctx:
            SelectionEngine(doc).select(EditOp.from_payload_item(
                dict(strategy="anchor", start_anchor="alpha start",
                     end_anchor="omega end", replace="X", resolution="pair"),
                1,
            ))
        self.assertEqual(len(ctx.exception.details["pairs"]), 2)

    def test_strict_mode_rejects_even_when_pair_would_work(self):
        doc = "alpha start A\nfiller\nend B omega\nalpha start A\n"
        with self.assertRaises(AnchorNotUniqueError):
            SelectionEngine(doc).select(EditOp.from_payload_item(
                dict(strategy="anchor", start_anchor="alpha start A",
                     end_anchor="end B omega", replace="X"),
                1,
            ))

    def test_start_equals_end_anchor_selects_exact_phrase(self):
        doc = "one two three four five six seven\n"
        sel = SelectionEngine(doc).select(anchor_op(
            "two three four", "two three four", "X",
        ))
        self.assertEqual(sel.text, "two three four")


class WhitespaceAndEolTests(unittest.TestCase):
    def test_crlf_document_preserved(self):
        doc = DOC.replace("\n", "\r\n")
        new_text, applied, _ = apply_edit_ops(doc, [anchor_op(
            "The migration process begins when",
            "completes the rollback safely.",
            "Line one.\nLine two.",
        )])
        self.assertIn("Line one.\r\nLine two.", new_text)
        self.assertNotIn("\n\n\r\n", new_text)
        # No bare LF anywhere:
        self.assertEqual(new_text.count("\n"), new_text.count("\r\n"))

    def test_trailing_newlines_in_replacement_do_not_double_blank_lines(self):
        new_text, _, _ = apply_edit_ops(DOC, [anchor_op(
            "The migration process begins when",
            "completes the rollback safely.",
            "Replaced block.\n\n\n",
        )])
        self.assertIn("Replaced block.\n\n## Aftermath", new_text)

    def test_paragraph_deletion_collapses_seam_to_blank_line(self):
        new_text, _, _ = apply_edit_ops(DOC, [anchor_op(
            "If verification fails at any point,",
            "completes the rollback safely.",
            "",
        )])
        self.assertIn("connection by connection.\n\n## Aftermath", new_text)

    def test_single_line_deletion_keeps_single_newline(self):
        doc = "aaa\nbbb ccc ddd\neee\n"
        new_text, _, _ = apply_edit_ops(doc, [anchor_op(
            "bbb ccc ddd", "bbb ccc ddd", "",
        )])
        self.assertEqual(new_text, "aaa\neee\n")

    def test_inline_deletion_heals_double_space(self):
        doc = "The quick brown fox jumps over the dog.\n"
        new_text, _, _ = apply_edit_ops(doc, [anchor_op(
            "brown fox", "brown fox", "",
        )])
        self.assertEqual(new_text, "The quick jumps over the dog.\n")

    def test_file_without_trailing_newline_stays_that_way(self):
        doc = "alpha beta gamma delta ends here"
        new_text, _, _ = apply_edit_ops(doc, [anchor_op(
            "alpha beta gamma", "delta ends here", "replaced tail",
        )])
        self.assertEqual(new_text, "replaced tail")


class TagStrategyTests(unittest.TestCase):
    DOC = (
        "top line\n"
        "// [START_EDIT]\n"
        "old body A\n"
        "old body B\n"
        "// [END_EDIT]\n"
        "bottom line\n"
    )

    def test_inner_mode_keeps_markers(self):
        op = EditOp.from_payload_item(
            dict(strategy="tags", replace="new body\n"), 1)
        new_text, applied, _ = apply_edit_ops(self.DOC, [op])
        self.assertEqual(
            new_text,
            "top line\n// [START_EDIT]\nnew body\n// [END_EDIT]\nbottom line\n",
        )
        self.assertEqual(applied[0].removed, "old body A\nold body B\n")

    def test_block_mode_removes_markers(self):
        op = EditOp.from_payload_item(
            dict(strategy="tags", mode="block", replace="new body"), 1)
        new_text, _, _ = apply_edit_ops(self.DOC, [op])
        self.assertEqual(new_text, "top line\nnew body\nbottom line\n")

    def test_missing_marker(self):
        with self.assertRaises(TagSelectionError):
            select_edit_ops("no markers here\n", [
                EditOp.from_payload_item(dict(strategy="tags", replace="x"), 1)
            ])

    def test_duplicate_marker(self):
        doc = self.DOC + "// [START_EDIT]\n// [END_EDIT]\n"
        with self.assertRaises(TagSelectionError) as ctx:
            select_edit_ops(doc, [
                EditOp.from_payload_item(dict(strategy="tags", replace="x"), 1)
            ])
        self.assertIn("exactly once", str(ctx.exception))

    def test_end_before_start(self):
        doc = "// [END_EDIT]\nbody\n// [START_EDIT]\n"
        with self.assertRaises(TagSelectionError):
            select_edit_ops(doc, [
                EditOp.from_payload_item(dict(strategy="tags", replace="x"), 1)
            ])

    def test_named_tags(self):
        doc = (
            "# [START_EDIT:intro]\nintro body\n# [END_EDIT:intro]\n"
            "# [START_EDIT:outro]\noutro body\n# [END_EDIT:outro]\n"
        )
        op = EditOp.from_payload_item(
            dict(strategy="tags", name="outro", replace="OUT!"), 1)
        new_text, _, _ = apply_edit_ops(doc, [op])
        self.assertIn("intro body", new_text)
        self.assertIn("OUT!\n# [END_EDIT:outro]", new_text)

    def test_insertion_between_adjacent_markers(self):
        doc = "a\n<!-- [START_EDIT] -->\n<!-- [END_EDIT] -->\nb\n"
        op = EditOp.from_payload_item(
            dict(strategy="tags", replace="inserted line"), 1)
        new_text, _, _ = apply_edit_ops(doc, [op])
        self.assertIn("[START_EDIT] -->\ninserted line\n<!-- [END_EDIT]", new_text)


class ContextStrategyTests(unittest.TestCase):
    BOILER = (
        "resource block:\n"
        "  name: alpha\n"
        "  size: small\n"
        "  retries: 3\n"
        "resource block:\n"
        "  name: beta\n"
        "  size: small\n"
        "  retries: 3\n"
        "resource block:\n"
        "  name: gamma\n"
        "  size: small\n"
        "  retries: 3\n"
    )

    def test_unique_neighborhood_selects_correct_block(self):
        op = EditOp.from_payload_item(dict(
            strategy="context",
            before=["  name: beta"],
            after=["resource block:", "  name: gamma"],
            replace="  size: LARGE\n  retries: 9\n",
        ), 1)
        new_text, applied, _ = apply_edit_ops(self.BOILER, [op])
        self.assertIn("name: beta\n  size: LARGE\n  retries: 9\nresource block:",
                      new_text)
        # alpha and gamma untouched:
        self.assertEqual(new_text.count("size: small"), 2)
        self.assertEqual(applied[0].selection.strategy, "context")

    def test_fuzzy_context_with_typos_still_matches(self):
        op = EditOp.from_payload_item(dict(
            strategy="context",
            before=["  name: betaa"],          # typo
            after=["resource block:", "  name: gama"],  # typo
            replace="  size: LARGE\n",
            min_score=0.8,
        ), 1)
        new_text, applied, _ = apply_edit_ops(self.BOILER, [op])
        self.assertIn("name: beta\n  size: LARGE\nresource block:", new_text)
        self.assertLess(applied[0].selection.confidence, 1.0)

    def test_identical_neighborhoods_rejected_as_ambiguous(self):
        op = EditOp.from_payload_item(dict(
            strategy="context",
            before=["resource block:"],
            after=["  retries: 3"],
            replace="X\n",
        ), 1)
        with self.assertRaises(ContextMatchError) as ctx:
            select_edit_ops(self.BOILER, [op])
        self.assertIn("Ambiguous", str(ctx.exception))
        self.assertGreaterEqual(len(ctx.exception.details["candidates"]), 2)

    def test_target_hint_disambiguates(self):
        op = EditOp.from_payload_item(dict(
            strategy="context",
            before=["resource block:"],
            after=["  retries: 3"],
            target_hint="  name: gamma",
            replace="  name: gamma\n  size: LARGE\n",
        ), 1)
        new_text, _, _ = apply_edit_ops(self.BOILER, [op])
        self.assertIn("name: gamma\n  size: LARGE\n  retries: 3", new_text)
        self.assertEqual(new_text.count("size: small"), 2)

    def test_bof_pinned_when_before_empty(self):
        op = EditOp.from_payload_item(dict(
            strategy="context",
            before=[],
            after=["resource block:", "  name: beta"],
            replace="HEADER\n",
        ), 1)
        new_text, _, _ = apply_edit_ops(self.BOILER, [op])
        self.assertTrue(new_text.startswith("HEADER\nresource block:\n  name: beta"))

    def test_eof_pinned_when_after_empty(self):
        op = EditOp.from_payload_item(dict(
            strategy="context",
            before=["  name: gamma"],
            after=[],
            replace="TAIL\n",
        ), 1)
        new_text, _, _ = apply_edit_ops(self.BOILER, [op])
        self.assertTrue(new_text.endswith("  name: gamma\nTAIL\n"))

    def test_insertion_between_adjacent_contexts(self):
        doc = "Para one.\n\nPara two.\n"
        op = EditOp.from_payload_item(dict(
            strategy="context",
            before=["Para one.", ""],
            after=["Para two."],
            replace="Inserted para.\n\n",
        ), 1)
        new_text, applied, _ = apply_edit_ops(doc, [op])
        self.assertEqual(new_text, "Para one.\n\nInserted para.\n\nPara two.\n")
        self.assertEqual(applied[0].selection.line_count, 0)  # pure insertion

    def test_no_match_below_threshold(self):
        op = EditOp.from_payload_item(dict(
            strategy="context",
            before=["completely unrelated line"],
            after=["another unrelated line"],
            replace="X",
        ), 1)
        with self.assertRaises(ContextMatchError):
            select_edit_ops(self.BOILER, [op])


class GuardTests(unittest.TestCase):
    def _op(self, guards):
        return anchor_op(
            "The migration process begins when",
            "completes the rollback safely.",
            "X",
            guards=guards,
        )

    def test_sha_guard_passes_and_fails(self):
        sel = select_edit_ops(DOC, [self._op({})])[0]
        ok = self._op({"expected_sha256": sel.sha256[:16]})
        select_edit_ops(DOC, [ok])  # must not raise
        bad = self._op({"expected_sha256": "0" * 16})
        with self.assertRaises(GuardError):
            select_edit_ops(DOC, [bad])

    def test_max_lines_guard(self):
        with self.assertRaises(GuardError):
            select_edit_ops(DOC, [self._op({"max_lines": 3})])

    def test_line_range_guard(self):
        sel = select_edit_ops(DOC, [self._op({})])[0]
        ok = self._op({"expected_line_range": [sel.start_line, sel.end_line]})
        select_edit_ops(DOC, [ok])
        bad = self._op({"expected_line_range": [1, 2]})
        with self.assertRaises(GuardError):
            select_edit_ops(DOC, [bad])


class BatchTests(unittest.TestCase):
    def test_multiple_edits_bottom_up(self):
        ops = [
            anchor_op("# Migration Guide", "# Migration Guide", "# MG v2"),
            anchor_op("If verification fails at any point,",
                      "completes the rollback safely.",
                      "Failure halts everything."),
            EditOp.from_payload_item(
                dict(search="audit trail", replace="AUDIT TRAIL"), 3),
        ]
        new_text, applied, skipped = apply_edit_ops(DOC, ops)
        self.assertTrue(new_text.startswith("# MG v2\n"))
        self.assertIn("Failure halts everything.", new_text)
        self.assertIn("AUDIT TRAIL", new_text)
        self.assertEqual(len(applied), 3)
        self.assertEqual(skipped, [])

    def test_overlapping_edits_rejected(self):
        ops = [
            anchor_op("The migration process begins when",
                      "completes the rollback safely.", "X"),
            anchor_op("If verification fails at any point,",
                      "reconstruct the timeline afterwards.", "Y"),
        ]
        with self.assertRaises(OverlapError) as ctx:
            apply_edit_ops(DOC, ops)
        self.assertEqual(ctx.exception.details["edits"], [1, 2])

    def test_noop_edit_skipped(self):
        block = ("The migration process begins when the operator freezes "
                 "writes on the\nprimary cluster.")
        ops = [EditOp.from_payload_item(dict(search=block, replace=block), 1)]
        new_text, applied, skipped = apply_edit_ops(DOC, ops)
        self.assertEqual(new_text, DOC)
        self.assertEqual(applied, [])
        self.assertEqual(skipped, [1])

    def test_error_carries_edit_index(self):
        ops = [
            anchor_op("# Migration Guide", "# Migration Guide", "ok"),
            anchor_op("does not exist anywhere", "also missing", "X"),
        ]
        with self.assertRaises(AnchorNotFoundError) as ctx:
            apply_edit_ops(DOC, ops)
        self.assertEqual(ctx.exception.details["edit_index"], 2)


class SuggestAnchorTests(unittest.TestCase):
    def test_minimal_unique_pair(self):
        engine = SelectionEngine(DOC)
        # Lines 5-8: the Overview paragraph.
        result = engine.suggest_anchors_for_lines(5, 8)
        self.assertEqual(result["start_words"], 5)
        self.assertEqual(result["end_words"], 5)
        # Round-trip: the suggested anchors must select the same block.
        sel = engine.select(anchor_op(
            result["start_anchor"], result["end_anchor"], "X"))
        self.assertEqual(sel.start_line, 5)
        self.assertEqual(sel.end_line, 8)

    def test_repetitive_start_extends_past_minimum(self):
        doc = ("the same five words here tail-a unique-a\n"
               "the same five words here tail-b unique-b\n")
        engine = SelectionEngine(doc)
        result = engine.suggest_anchors_for_lines(2, 2)
        self.assertGreater(result["start_words"], 5)
        self.assertEqual(engine._view.count(result["start_anchor"]), 1)

    def test_impossible_uniquification(self):
        doc = "same words repeat\n" * 4
        engine = SelectionEngine(doc)
        with self.assertRaises(CannotUniquifyError):
            engine.suggest_anchors_for_lines(2, 2)

    def test_suggestions_never_exceed_word_cap(self):
        doc = ("alpha beta gamma delta epsilon zeta eta theta unique-one\n"
               "alpha beta gamma delta epsilon zeta eta theta unique-two\n")
        engine = SelectionEngine(doc)
        result = engine.suggest_anchors_for_lines(1, 1)
        self.assertLessEqual(len(result["start_anchor"].split()),
                             se.MAX_ANCHOR_WORDS)


class VerbatimStrategyTests(unittest.TestCase):
    def test_exact_replacement(self):
        new_text, _, _ = apply_edit_ops(DOC, [EditOp.from_payload_item(
            dict(search="audit trail", replace="paper trail"), 1)])
        self.assertIn("paper trail", new_text)

    def test_ambiguous_search_rejected(self):
        with self.assertRaises(AnchorNotUniqueError):
            apply_edit_ops(DOC, [EditOp.from_payload_item(
                dict(search="The migration process", replace="x"), 1)])

    def test_whitespace_mismatch_gets_targeted_hint(self):
        with self.assertRaises(AnchorNotFoundError) as ctx:
            apply_edit_ops(DOC, [EditOp.from_payload_item(
                dict(search="The migration process begins when the operator "
                            "freezes writes on the primary cluster.",
                     replace="x"), 1)])
        self.assertTrue(
            ctx.exception.details.get("whitespace_only_mismatch"))
        self.assertIn("anchor", ctx.exception.hint)

    def test_crlf_adaptation(self):
        doc = "line one\r\nline two\r\nline three\r\n"
        new_text, applied, _ = apply_edit_ops(doc, [EditOp.from_payload_item(
            dict(search="line one\nline two", replace="single line"), 1)])
        self.assertEqual(new_text, "single line\r\nline three\r\n")
        self.assertIn("adapted", applied[0].note)


class PayloadValidationTests(unittest.TestCase):
    def test_missing_replace(self):
        with self.assertRaises(InvalidRequestError):
            EditOp.from_payload_item(dict(strategy="anchor",
                                          start_anchor="a", end_anchor="b"), 1)

    def test_unknown_strategy(self):
        with self.assertRaises(InvalidRequestError):
            EditOp.from_payload_item(dict(strategy="telepathy", replace=""), 1)

    def test_strategy_inference(self):
        self.assertEqual(
            EditOp.from_payload_item(dict(search="x", replace="y"), 1).strategy,
            "verbatim")
        self.assertEqual(
            EditOp.from_payload_item(
                dict(start_anchor="a b c", end_anchor="d e f", replace=""), 1
            ).strategy,
            "anchor")

    def test_bad_occurrence_type(self):
        with self.assertRaises(InvalidRequestError):
            EditOp.from_payload_item(dict(
                strategy="anchor", start_anchor="a", end_anchor="b",
                replace="", occurrence_start="first"), 1)

    def test_unknown_guard_key(self):
        with self.assertRaises(InvalidRequestError):
            EditOp.from_payload_item(dict(
                search="a", replace="b", guards={"expected_md5": "x"}), 1)

    def test_context_requires_some_context(self):
        with self.assertRaises(InvalidRequestError):
            EditOp.from_payload_item(dict(strategy="context", replace="x"), 1)


class MarkdownFormatTests(unittest.TestCase):
    def test_anchor_block_round_trip(self):
        response = (
            "@@EDIT anchor\n"
            "START-ANCHOR: The migration process begins when\n"
            "END-ANCHOR: completes the rollback safely.\n"
            "<<<\n"
            "Replaced via markdown format.\n"
            "\n"
            "Second paragraph of the replacement.\n"
            ">>>\n"
        )
        ops = parse_markdown_edits(response)
        self.assertEqual(len(ops), 1)
        new_text, _, _ = apply_edit_ops(DOC, ops)
        self.assertIn("Replaced via markdown format.\n\nSecond paragraph of the "
                      "replacement.\n\n## Aftermath", new_text)

    def test_context_block_with_repeated_keys(self):
        response = (
            "@@EDIT context\n"
            "BEFORE: Para one.\n"
            "BEFORE: \n"
            "AFTER: Para two.\n"
            "<<<\n"
            "Inserted.\n"
            ">>>\n"
        )
        ops = parse_markdown_edits(response)
        self.assertEqual(ops[0].before, ("Para one.", ""))
        self.assertEqual(ops[0].after, ("Para two.",))

    def test_empty_body_means_delete(self):
        response = (
            "@@EDIT anchor\n"
            "START-ANCHOR: If verification fails at any point,\n"
            "END-ANCHOR: completes the rollback safely.\n"
            "<<<\n"
            ">>>\n"
        )
        ops = parse_markdown_edits(response)
        self.assertEqual(ops[0].replace, "")
        new_text, _, _ = apply_edit_ops(DOC, ops)
        self.assertIn("connection by connection.\n\n## Aftermath", new_text)

    def test_missing_fence_rejected(self):
        with self.assertRaises(InvalidRequestError):
            parse_markdown_edits("@@EDIT anchor\nSTART-ANCHOR: a\n")

    def test_unclosed_body_rejected(self):
        with self.assertRaises(InvalidRequestError):
            parse_markdown_edits(
                "@@EDIT anchor\nSTART-ANCHOR: a\nEND-ANCHOR: b\n<<<\nbody\n")

    def test_multiple_blocks(self):
        response = (
            "@@EDIT verbatim\n"  # verbatim isn't markdown-supported for search
            "<<<\n>>>\n"
        )
        # verbatim without search must fail validation:
        with self.assertRaises(InvalidRequestError):
            parse_markdown_edits(response)

    def test_rtl_fences_with_trailing_bidi_marks(self):
        # The real bug: an RTL response attaches a RIGHT-TO-LEFT MARK to the
        # bare <<< / >>> fence lines, which .strip() does not remove.
        RLM = "‏"
        resp = (
            "@@EDIT anchor\n"
            "START-ANCHOR: نثر خوشه‌ای یا دم علمی\n"
            "END-ANCHOR: چیزهای جدید یاد بگیرند.\n"
            "<<<" + RLM + "\n"
            "گیاه سورگوم به‌عنوان پنجمین غله مهم جهان.\n"
            ">>>" + RLM + "\n"
        )
        ops = parse_markdown_edits(resp)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].strategy, "anchor")
        self.assertIn("پنجمین", ops[0].replace)

    def test_rtl_marks_on_edit_token_and_keys(self):
        LRM = "‎"; RLM = "‏"; RLI = "⁧"; PDI = "⁩"
        resp = (
            LRM + "@@EDIT anchor" + RLM + "\n"
            + RLI + "START-ANCHOR:" + PDI + " نثر خوشه‌ای یا دم علمی\n"
            "END-ANCHOR: چیزهای جدید یاد بگیرند.\n"
            "<<<\n" "متن جایگزین.\n" ">>>\n"
        )
        ops = parse_markdown_edits(resp)
        self.assertEqual(len(ops), 1)
        self.assertTrue(ops[0].start_anchor.startswith("نثر"))

    def test_missing_fence_still_rejected_with_rtl_hint(self):
        with self.assertRaises(InvalidRequestError) as ctx:
            parse_markdown_edits(
                "@@EDIT anchor\nSTART-ANCHOR: x\nیک خط بدون فنس و بدون کلید\n")
        self.assertIn("fence", str(ctx.exception).lower())
        self.assertIn("json", (ctx.exception.hint or "").lower())  # RTL guidance

    def test_two_anchor_blocks(self):
        response = (
            "@@EDIT anchor\n"
            "START-ANCHOR: # Migration Guide\n"
            "END-ANCHOR: # Migration Guide\n"
            "<<<\n# MG v2\n>>>\n"
            "\nSome prose the model added between blocks.\n\n"
            "@@EDIT anchor\n"
            "START-ANCHOR: leaves an audit trail\n"
            "END-ANCHOR: leaves an audit trail\n"
            "<<<\nkeeps an audit trail\n>>>\n"
        )
        ops = parse_markdown_edits(response)
        self.assertEqual(len(ops), 2)
        new_text, applied, _ = apply_edit_ops(DOC, ops)
        self.assertTrue(new_text.startswith("# MG v2\n"))
        self.assertIn("keeps an audit trail in the ledger", new_text)


class PersianRealWorldTests(unittest.TestCase):
    """Regression tests for the invisible-char and confusable folding that
    real Farsi documents demand (soft hyphens, ZWNJ, Arabic vs Persian
    kaf/yeh, mixed digits). Raw bytes must never be rewritten outside the
    replaced span."""

    # 'ژن' + SOFT HYPHEN + 'های' — as produced by many Persian editors.
    SOFT = "­"
    DOC = (
        "عنوان:\n\n"
        "ژن" + SOFT + "های کلیدی در تحمل تنش خشکی سورگوم\n\n"
        "كلمات كليدي: ژن مركزي\n"  # Arabic kaf (0643) + Arabic yeh (064A)
    )

    def test_soft_hyphen_doc_matches_zwnj_anchor(self):
        # Anchor typed with ZWNJ (0x200C); doc uses soft hyphen (0x00AD).
        op = anchor_op("ژن‌های کلیدی در", "تنش خشکی سورگوم", "REPLACED")
        new_text, applied, _ = apply_edit_ops(self.DOC, [op])
        self.assertIn("REPLACED", new_text)
        # The soft hyphen elsewhere is untouched (byte fidelity):
        self.assertIn("عنوان:", new_text)

    def test_soft_hyphen_doc_matches_plain_anchor(self):
        # Anchor typed with NO joiner at all.
        op = anchor_op("ژنهای کلیدی در", "تنش خشکی سورگوم", "X")
        sel = SelectionEngine(self.DOC).select(op)
        self.assertEqual(sel.status, "SELECTION_CONFIRMED")

    def test_persian_anchor_matches_arabic_form_doc(self):
        # Doc: Arabic kaf/yeh. Anchor: Persian keheh (06A9) + farsi yeh (06CC).
        op = anchor_op("کلمات کلیدی: ژن", "کلمات کلیدی: ژن", "واژگان کلیدی: ژن مرکزی")
        new_text, applied, _ = apply_edit_ops(self.DOC, [op])
        self.assertIn("واژگان", new_text)
        # Raw removed text kept the document's ORIGINAL Arabic forms:
        self.assertIn("ك", applied[0].removed)  # arabic kaf (0643) preserved

    def test_confusable_fold_is_one_to_one_offsets_intact(self):
        # A fold must not shift the raw span: verify exact char span.
        doc = "aaa كليدي bbb"  # arabic-form
        eng = SelectionEngine(doc)
        sel = eng.select(anchor_op("كليدي", "كليدي", "X"))
        self.assertEqual(doc[sel.start:sel.end], "كليدي")

    def test_digits_fold_persian_vs_ascii(self):
        doc = "کد ملی ۲۹۴۰۰۵۶۱۶۱ است\n"  # Persian digits
        op = anchor_op("کد ملی 2940056161", "2940056161 است", "کد حذف شد")
        new_text, _, _ = apply_edit_ops(doc, [op])
        self.assertIn("کد حذف شد", new_text)

    def test_no_trailing_newline_preserved_on_farsi_doc(self):
        doc = "خط اول\n\nخط دوم بدون نیولاین آخر"
        op = anchor_op("خط دوم بدون", "نیولاین آخر", "خط دوم جدید")
        new_text, _, _ = apply_edit_ops(doc, [op])
        self.assertFalse(new_text.endswith("\n"))
        self.assertEqual(new_text, "خط اول\n\nخط دوم جدید")

    def test_verbatim_invisible_mismatch_suggests_anchor(self):
        with self.assertRaises(AnchorNotFoundError) as ctx:
            select_edit_ops(self.DOC, [EditOp.from_payload_item(
                {"search": "ژن‌های کلیدی", "replace": "x"}, 1)])
        self.assertTrue(ctx.exception.details.get("whitespace_only_mismatch"))


class ResponseChaosTests(unittest.TestCase):
    """Real-world response mutations the parser must survive."""

    BLOCK = (
        "@@EDIT anchor\n"
        "START-ANCHOR: # Migration Guide\n"
        "END-ANCHOR: # Migration Guide\n"
        "<<<\n# MG chaos\n>>>\n"
    )

    def test_code_fence_wrapped_block(self):
        resp = "```\n" + self.BLOCK + "```\n"
        ops = parse_markdown_edits(resp)
        self.assertEqual(len(ops), 1)
        new_text, _, _ = apply_edit_ops(DOC, ops)
        self.assertTrue(new_text.startswith("# MG chaos\n"))

    def test_code_fence_between_header_lines(self):
        resp = (
            "@@EDIT anchor\n"
            "```\n"  # stray fence a model left inside the block
            "START-ANCHOR: # Migration Guide\n"
            "END-ANCHOR: # Migration Guide\n"
            "<<<\nX\n>>>\n"
            "```\n"
        )
        self.assertEqual(len(parse_markdown_edits(resp)), 1)

    def test_sloppy_quadruple_fences(self):
        resp = self.BLOCK.replace("<<<", "<<<<").replace(">>>", ">>>>")
        ops = parse_markdown_edits(resp)
        self.assertEqual(ops[0].replace, "# MG chaos")

    def test_crlf_response_text(self):
        ops = parse_markdown_edits(self.BLOCK.replace("\n", "\r\n"))
        self.assertEqual(len(ops), 1)

    def test_prose_mentioning_edit_token_midline_is_ignored(self):
        resp = "I will use the @@EDIT anchor format below.\n\n" + self.BLOCK
        ops = parse_markdown_edits(resp)
        self.assertEqual(len(ops), 1)  # prose line is not a block opener

    def test_fences_with_spaces_and_bidi_marks(self):
        RLM = "‏"
        resp = self.BLOCK.replace("<<<\n", "  <<< " + RLM + " \n").replace(
            ">>>\n", " " + RLM + ">>>  \n")
        self.assertEqual(len(parse_markdown_edits(resp)), 1)


class RobustnessScenarioTests(unittest.TestCase):
    """Broad 'don't make mistakes' battery: every mutation must apply
    correctly OR refuse safely — never edit the wrong text."""

    def test_smart_quotes_fold_both_directions(self):
        doc = 'He said "hello world" to the team.\n'
        op = anchor_op('said “hello world” to', "world” to the team.", "X")
        self.assertEqual(SelectionEngine(doc).select(op).status,
                         "SELECTION_CONFIRMED")
        doc2 = 'The “clever” idea won.\n'
        op2 = anchor_op('The "clever" idea', '"clever" idea won.', "Y")
        self.assertEqual(SelectionEngine(doc2).select(op2).status,
                         "SELECTION_CONFIRMED")

    def test_curly_apostrophe_matches_straight(self):
        doc = "It doesn't matter now.\n"                       # straight '
        op = anchor_op("It doesn’t matter", "doesn’t matter now.", "Z")  # curly
        self.assertEqual(SelectionEngine(doc).select(op).status,
                         "SELECTION_CONFIRMED")

    def test_persian_guillemets_not_folded(self):
        view = se._NormalizedView("متن «کلیدی» مهم است")
        self.assertIn("«", view.norm)
        self.assertNotIn('"', view.norm)

    def test_nbsp_and_exotic_spaces_treated_as_space(self):
        doc = "alpha beta gamma delta done\n"   # NBSP/NNBSP/thin
        op = anchor_op("alpha beta gamma", "gamma delta done", "X")
        self.assertEqual(SelectionEngine(doc).select(op).status,
                         "SELECTION_CONFIRMED")

    def test_tab_vs_space_indentation_elastic(self):
        doc = "def f():\n\treturn 1 + 2\n"
        new_text, _, _ = apply_edit_ops(
            doc, [anchor_op("return 1 + 2", "return 1 + 2", "return 3")])
        self.assertIn("return 3", new_text)

    def test_batch_aborts_atomically_on_second_bad_edit(self):
        doc = DOC + "\nThe migration process begins when spring arrives.\n"
        ops = [
            anchor_op("# Migration Guide", "# Migration Guide", "# MG v2"),
            anchor_op("The migration process begins when",
                      "in parallel, verified against the", "boom"),
        ]
        with self.assertRaises(se.SelectionError):
            apply_edit_ops(doc, ops)
        # Proof of atomicity: first edit's target is still original.
        self.assertIn("# Migration Guide", doc)

    def test_midword_anchor_is_refused_not_misapplied(self):
        doc = "The processing pipeline processes data.\n"
        with self.assertRaises(AnchorNotFoundError):
            SelectionEngine(doc).select(anchor_op("process", "process", "X"))

    def test_large_document_stays_fast(self):
        big = "".join(
            "Filler paragraph %d of the corpus here.\n\n" % i
            for i in range(4000)
        ) + "Final unique paragraph zzz-END here.\n"
        import time
        t0 = time.time()
        new_text, _, _ = apply_edit_ops(big, [anchor_op(
            "Final unique paragraph zzz-END", "zzz-END here.", "DONE.")])
        self.assertLess(time.time() - t0, 3.0)
        self.assertIn("DONE.", new_text)


class TrickyAnchorTests(unittest.TestCase):
    """Anchors quoted the way real users/AIs quote them."""

    DOC = (
        "intro line\n\n"
        "**با توجه به ویژگی مقاوم بودن گیاه سورگوم نسبت به تنش خشکی** ادامه دارد\n\n"
        "The plant _Sorghum bicolor_ (L.) Moench survives drought.\n\n"
        "tail line\n"
    )

    def test_rtl_and_accents(self):
        doc = ("مقدمه‌ای کوتاه درباره سیستم\n"
               "متن اصلی که باید عوض شود اینجاست\n"
               "پایان سند و نتیجه‌گیری نهایی\n")
        op = anchor_op("متن اصلی که باید", "عوض شود اینجاست",
                       "متن تازه و بهتر")
        new_text, _, _ = apply_edit_ops(doc, [op])
        self.assertIn("متن تازه و بهتر", new_text)
        self.assertIn("مقدمه‌ای", new_text)

    def test_selection_sha_is_stable(self):
        sel1 = select_edit_ops(DOC, [anchor_op(
            "The migration process begins when",
            "completes the rollback safely.", "X")])[0]
        sel2 = select_edit_ops(DOC, [anchor_op(
            "The migration process begins when",
            "completes the rollback safely.", "Y")])[0]
        self.assertEqual(sel1.sha256, sel2.sha256)


if __name__ == "__main__":
    unittest.main(verbosity=2)
