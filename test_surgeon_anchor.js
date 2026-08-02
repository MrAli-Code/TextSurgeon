#!/usr/bin/env node
/** Tests for surgeon_anchor.js — run: node test_surgeon_anchor.js */
"use strict";

const assert = require("assert");
const SA = require("./surgeon_anchor.js");

const DOC = `# Migration Guide

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
`;

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log("  ok  " + name);
  } catch (err) {
    console.error("FAIL  " + name);
    console.error(err && err.stack ? err.stack : err);
    process.exitCode = 1;
  }
}

test("whitespace-elastic selection across wrapped lines", () => {
  const sel = SA.selectByAnchors(DOC, {
    startAnchor: "The migration process begins when",
    endAnchor: "completes the rollback safely.",
  });
  assert.strictEqual(sel.status, "SELECTION_CONFIRMED");
  assert.ok(sel.text.startsWith("The migration process begins"));
  assert.ok(sel.text.endsWith("rollback safely."));
  assert.strictEqual(sel.startLine, 5);
  assert.strictEqual(sel.sha256.length, 64);
});

test("word alignment prevents substring hits", () => {
  const view = new SA.NormalizedView("breathe systems differ. the system works.");
  assert.strictEqual(view.count("the system"), 1);
  assert.strictEqual(new SA.NormalizedView("concatenate").count("cat"), 0);
});

test("basic replacement keeps structure", () => {
  const out = SA.applyAnchorEdit(DOC, {
    startAnchor: "The migration process begins when",
    endAnchor: "completes the rollback safely.",
    replace: "Replaced block.\n\n\n",
  });
  assert.ok(out.text.includes("## Overview\n\nReplaced block.\n\n## Aftermath"));
  assert.ok(out.removed.includes("Snapshots are taken"));
});

test("ambiguous anchor raises with unique suggestions", () => {
  assert.throws(
    () =>
      SA.selectByAnchors(DOC, {
        startAnchor: "The migration process",
        endAnchor: "completes the rollback safely.",
      }),
    (err) => {
      assert.strictEqual(err.code, "ANCHOR_NOT_UNIQUE");
      const sugg = err.details.start.suggestions.filter((s) => s.anchor);
      assert.ok(sugg.length >= 1);
      sugg.forEach((s) =>
        assert.ok(s.anchor.split(/\s+/).length <= SA.MAX_ANCHOR_WORDS));
      // Self-healing: a suggested anchor must resolve cleanly.
      const sel = SA.selectByAnchors(DOC, {
        startAnchor: sugg[0].anchor,
        endAnchor: sugg[0].anchor,
      });
      assert.strictEqual(sel.status, "SELECTION_CONFIRMED");
      return true;
    }
  );
});

test("anchor over 10 words rejected", () => {
  assert.throws(
    () =>
      SA.selectByAnchors(DOC, {
        startAnchor:
          "The migration process begins when the operator freezes writes on the primary",
        endAnchor: "completes the rollback safely.",
      }),
    (err) => err.code === "ANCHOR_TOO_LONG"
  );
});

test("not-found reports closest passage", () => {
  assert.throws(
    () =>
      SA.selectByAnchors(DOC, {
        startAnchor: "The migration process begins wen", // typo
        endAnchor: "completes the rollback safely.",
      }),
    (err) => {
      assert.strictEqual(err.code, "ANCHOR_NOT_FOUND");
      assert.ok(err.details.closestMatch.similarity >= 0.8);
      return true;
    }
  );
});

test("end before start rejected", () => {
  assert.throws(
    () =>
      SA.selectByAnchors(DOC, {
        startAnchor: "completes the rollback safely.",
        endAnchor: "The migration process begins when",
      }),
    (err) => err.code === "END_BEFORE_START"
  );
});

test("occurrence index + pair resolution", () => {
  const sel = SA.selectByAnchors(DOC, {
    startAnchor: "The migration process",
    endAnchor: "reconstruct the timeline afterwards.",
    occurrenceStart: 2,
  });
  assert.strictEqual(sel.resolution, "indexed");
  assert.ok(sel.text.includes("audit trail"));

  const doc2 = "alpha start A\nfiller\nend B omega\nalpha start A\nno end\n";
  const pairSel = SA.selectByAnchors(doc2, {
    startAnchor: "alpha start A",
    endAnchor: "end B omega",
    resolution: "pair",
  });
  assert.strictEqual(pairSel.resolution, "pair");
  assert.strictEqual(pairSel.startLine, 1);
});

test("CRLF document preserved", () => {
  const crlf = DOC.replace(/\n/g, "\r\n");
  const out = SA.applyAnchorEdit(crlf, {
    startAnchor: "The migration process begins when",
    endAnchor: "completes the rollback safely.",
    replace: "Line one.\nLine two.",
  });
  assert.ok(out.text.includes("Line one.\r\nLine two."));
  assert.strictEqual(
    (out.text.match(/\n/g) || []).length,
    (out.text.match(/\r\n/g) || []).length
  );
});

test("paragraph deletion collapses seam", () => {
  const out = SA.applyAnchorEdit(DOC, {
    startAnchor: "If verification fails at any point,",
    endAnchor: "completes the rollback safely.",
    replace: "",
  });
  assert.ok(out.text.includes("connection by connection.\n\n## Aftermath"));
});

test("inline deletion heals doubled spaces", () => {
  const out = SA.applyAnchorEdit("The quick brown fox jumps over the dog.\n", {
    startAnchor: "brown fox",
    endAnchor: "brown fox",
    replace: "",
  });
  assert.strictEqual(out.text, "The quick jumps over the dog.\n");
});

test("suggestAnchors returns minimal unique pair (round-trips)", () => {
  const start = DOC.indexOf("The migration process begins");
  const end = DOC.indexOf("connection by connection.") +
    "connection by connection.".length;
  const sugg = SA.suggestAnchors(DOC, start, end);
  assert.strictEqual(sugg.startWords, 5);
  const sel = SA.selectByAnchors(DOC, {
    startAnchor: sugg.startAnchor,
    endAnchor: sugg.endAnchor,
  });
  assert.strictEqual(sel.start, start);
  assert.strictEqual(sel.end, end);
});

test("suggestAnchors refuses impossible blocks", () => {
  const doc = "same words repeat\n".repeat(4);
  assert.throws(
    () => SA.suggestAnchors(doc, 18, 36),
    (err) => err.code === "CANNOT_UNIQUIFY"
  );
});

test("pure-JS sha256 matches Node crypto", () => {
  const crypto = require("crypto");
  ["", "abc", "The migration process begins when", "متن فارسی ✂ emoji 🚀"].forEach(
    (sample) => {
      const expected = crypto.createHash("sha256").update(sample, "utf8").digest("hex");
      // Access the pure implementation via a fresh context without require:
      assert.strictEqual(SA.sha256Hex(sample), expected);
    }
  );
});

test("Persian: soft-hyphen doc matches ZWNJ / plain anchor", () => {
  const soft = "­";
  const doc = "عنوان:\n\nژن" + soft + "های کلیدی در تحمل تنش خشکی\n";
  // Anchor typed with ZWNJ:
  const sel = SA.selectByAnchors(doc, {
    startAnchor: "ژن‌های کلیدی در",
    endAnchor: "تحمل تنش خشکی",
  });
  assert.strictEqual(sel.status, "SELECTION_CONFIRMED");
  // Anchor typed with no joiner:
  const sel2 = SA.selectByAnchors(doc, {
    startAnchor: "ژنهای کلیدی در",
    endAnchor: "تحمل تنش خشکی",
  });
  assert.strictEqual(sel2.status, "SELECTION_CONFIRMED");
});

test("Persian: Persian-form anchor matches Arabic-form doc, bytes intact", () => {
  const doc = "aaa كليدي bbb"; // arabic kaf 0643 + arabic yeh 064A
  const out = SA.applyAnchorEdit(doc, {
    startAnchor: "کلیدی", // persian keheh 06A9 + farsi yeh 06CC
    endAnchor: "کلیدی",
    replace: "X",
  });
  assert.strictEqual(out.text, "aaa X bbb");
  assert.strictEqual(out.removed, "كليدي"); // raw arabic form preserved
});

test("Persian: digits fold (persian vs ascii)", () => {
  const doc = "کد ملی ۲۹۴۰۰۵۶۱۶۱ است\n";
  const out = SA.applyAnchorEdit(doc, {
    startAnchor: "کد ملی 2940056161",
    endAnchor: "2940056161 است",
    replace: "حذف شد",
  });
  assert.ok(out.text.includes("حذف شد"));
});

test("unicode / RTL text", () => {
  const doc = "مقدمه‌ای کوتاه درباره سیستم\nمتن اصلی که باید عوض شود اینجاست\nپایان سند\n";
  const out = SA.applyAnchorEdit(doc, {
    startAnchor: "متن اصلی که باید",
    endAnchor: "عوض شود اینجاست",
    replace: "متن تازه و بهتر",
  });
  assert.ok(out.text.includes("متن تازه و بهتر"));
});

console.log(passed + " tests passed");
