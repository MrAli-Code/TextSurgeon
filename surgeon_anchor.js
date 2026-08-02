/**
 * surgeon_anchor.js — Statistical Anchor Marking (Surgeon Protocol v2)
 * ====================================================================
 * JavaScript port of Method 1 from surgeon_engine.py: replace a large block
 * of text by referencing only its first ~5-10 words (start anchor) and its
 * last ~5-10 words (end anchor). Anchors are matched whitespace-elastically
 * (re-wrapped lines still hit) but word-aligned (never inside a larger
 * token), and each anchor must be statistically unique — exactly one
 * occurrence — before any splice is allowed.
 *
 * Zero dependencies; runs in Node and the browser. Mirrors the Python
 * engine's semantics and error codes:
 *
 *   ANCHOR_NOT_FOUND    anchor has zero occurrences (closest match reported)
 *   ANCHOR_NOT_UNIQUE   anchor is ambiguous (unique extensions suggested)
 *   ANCHOR_TOO_LONG     anchor exceeds MAX_ANCHOR_WORDS (10)
 *   END_BEFORE_START    end anchor resolves before the start anchor
 *   CANNOT_UNIQUIFY     suggestAnchors found no unique <=10-word marker
 *   INVALID_REQUEST     malformed operation
 *
 * API:
 *   selectByAnchors(text, op)  -> Selection  (status: "SELECTION_CONFIRMED")
 *   applyAnchorEdit(text, op)  -> { text, selection, removed, added }
 *   suggestAnchors(text, start, end [, opts]) -> { startAnchor, endAnchor, … }
 *
 *   op = { startAnchor, endAnchor, replace,
 *          occurrenceStart?, occurrenceEnd?,   // 1-based explicit indices
 *          resolution? }                       // "strict" (default) | "pair"
 */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.SurgeonAnchor = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MAX_ANCHOR_WORDS = 10;
  var SUGGESTED_MIN_ANCHOR_WORDS = 5;
  var EXTEND_SCAN_CHARS = 6000;

  // ------------------------------------------------------------------ errors

  function SelectionError(code, message, hint, details) {
    var err = new Error(message);
    err.name = "SelectionError";
    err.code = code;
    err.hint = hint || null;
    err.details = details || {};
    err.toDict = function () {
      return {
        status: "SELECTION_REJECTED",
        code: err.code,
        error: err.message,
        hint: err.hint,
        details: err.details,
      };
    };
    return err;
  }

  // ------------------------------------ whitespace/invisible-elastic view

  function isSpace(ch) {
    return /\s/.test(ch);
  }

  // Typographically invisible / zero-width characters made transparent for
  // matching (soft hyphen, ZWNJ/ZWJ, zero-width spaces, bidi marks/controls,
  // BOM). Mirrors _INVISIBLE ∪ _BIDI_CONTROLS in surgeon_engine.py.
  var INVISIBLE = {
    "­": 1, "​": 1, "‌": 1, "‍": 1,
    "‎": 1, "‏": 1, "⁠": 1, "﻿": 1,
    "؜": 1, "‪": 1, "‫": 1, "‬": 1, "‭": 1, "‮": 1,
    "⁦": 1, "⁧": 1, "⁨": 1, "⁩": 1,
  };

  // Persian/Arabic confusables folded 1:1 for matching (kaf, yeh, alef
  // maksura, teh marbuta, hamza-alef forms, both digit sets). Offsets stay
  // exact; raw bytes are never rewritten. Mirrors _CONFUSABLE in the engine.
  var CONFUSABLE = {
    "ك": "ک", // ARABIC KAF -> KEHEH
    "ى": "ی", // ALEF MAKSURA -> FARSI YEH
    "ي": "ی", // ARABIC YEH -> FARSI YEH
    "ة": "ه", // TEH MARBUTA -> HEH
    "أ": "ا", // ALEF+HAMZA ABOVE -> ALEF
    "إ": "ا", // ALEF+HAMZA BELOW -> ALEF
    "آ": "ا", // ALEF+MADDA -> ALEF
    // Smart quotes/apostrophes -> ASCII (guillemets « » left intentional).
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "ʼ": "'",
  };
  (function () {
    var bases = [0x0660, 0x06f0];
    for (var b = 0; b < bases.length; b++) {
      for (var d = 0; d < 10; d++) {
        CONFUSABLE[String.fromCharCode(bases[b] + d)] = String(d);
      }
    }
  })();

  function fold(ch) {
    return CONFUSABLE[ch] || ch;
  }

  var WORD_CHAR = /[\p{L}\p{N}_]/u;
  function isWordChar(ch) {
    return WORD_CHAR.test(ch);
  }

  /** Normalises a document with invisible removal + confusable folding,
   *  keeping an offset map from each normalised char to its raw offset. */
  function NormalizedView(raw) {
    this.raw = raw;
    var chars = [];
    var map = [];
    var runStart = -1;
    for (var i = 0; i < raw.length; i++) {
      var ch = raw[i];
      if (INVISIBLE[ch]) continue;
      if (isSpace(ch)) {
        if (runStart < 0) runStart = i;
        continue;
      }
      if (runStart >= 0 && chars.length) {
        chars.push(" ");
        map.push(runStart);
      }
      runStart = -1;
      chars.push(CONFUSABLE[ch] || ch);
      map.push(i);
    }
    this.norm = chars.join("");
    this.map = map;
  }

  /** Normalises a needle the same way (invisibles dropped, confusables
   *  folded, whitespace collapsed). Idempotent. */
  function probe(value) {
    var out = "";
    for (var i = 0; i < value.length; i++) {
      var ch = value[i];
      if (INVISIBLE[ch]) continue;
      out += CONFUSABLE[ch] || ch;
    }
    return out.split(/\s+/).filter(Boolean).join(" ");
  }

  /** Word-aligned matches; needle is normalised internally (raw or
   *  pre-probed input both work). Returns raw [start,end) spans. */
  NormalizedView.prototype.findSpans = function (needle) {
    needle = probe(needle);
    if (!needle) return [];
    var spans = [];
    var cursor = 0;
    for (;;) {
      var idx = this.norm.indexOf(needle, cursor);
      if (idx < 0) break;
      var end = idx + needle.length;
      // Boundary = not glued to a letter/digit/underscore; punctuation
      // (Markdown ** _ ` etc.) is transparent. Mirrors _is_word_char.
      var aligned =
        (idx === 0 || !isWordChar(this.norm[idx - 1])) &&
        (end === this.norm.length || !isWordChar(this.norm[end]));
      if (aligned) spans.push([this.map[idx], this.map[end - 1] + 1]);
      cursor = idx + 1;
    }
    return spans;
  };

  NormalizedView.prototype.count = function (needle) {
    return this.findSpans(needle).length;
  };

  // ------------------------------------------------------------- utilities

  function lineOf(text, offset) {
    var line = 1;
    for (var i = 0; i < offset; i++) if (text[i] === "\n") line++;
    return line;
  }

  function detectEol(text) {
    var crlf = (text.match(/\r\n/g) || []).length;
    var lf = (text.match(/\n/g) || []).length - crlf;
    return crlf > lf ? "\r\n" : "\n";
  }

  function shorten(value, limit) {
    limit = limit || 70;
    var collapsed = probe(value);
    return collapsed.length <= limit
      ? collapsed
      : collapsed.slice(0, limit - 1) + "…";
  }

  function contextPreview(text, span, pad) {
    pad = pad || 35;
    var start = Math.max(0, span[0] - pad);
    var end = Math.min(text.length, span[1] + pad);
    return (
      (start > 0 ? "…" : "") +
      probe(text.slice(start, end)) +
      (end < text.length ? "…" : "")
    );
  }

  /** Dice-coefficient similarity on character bigrams (0..1). */
  function similarity(a, b) {
    if (a === b) return 1;
    if (a.length < 2 || b.length < 2) return 0;
    var grams = Object.create(null);
    var i;
    for (i = 0; i < a.length - 1; i++) {
      var g = a.substr(i, 2);
      grams[g] = (grams[g] || 0) + 1;
    }
    var hits = 0;
    for (i = 0; i < b.length - 1; i++) {
      var h = b.substr(i, 2);
      if (grams[h] > 0) {
        grams[h]--;
        hits++;
      }
    }
    return (2 * hits) / (a.length + b.length - 2);
  }

  // ------------------------------------------------------------------ sha256
  // Compact synchronous SHA-256 so confirmations work identically in Node
  // and the browser without async WebCrypto plumbing.

  function sha256Hex(input) {
    if (typeof require === "function") {
      try {
        var nodeCrypto = require("crypto");
        return nodeCrypto.createHash("sha256").update(input, "utf8").digest("hex");
      } catch (ignored) {
        /* fall through to the pure-JS path */
      }
    }
    return sha256Pure(input);
  }

  function sha256Pure(inputText) {
    var K = [
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
      0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
      0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
      0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
      0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
      0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
      0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
      0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
      0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ];
    var H = [
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
      0x1f83d9ab, 0x5be0cd19,
    ];
    // UTF-8 encode.
    var bytes = [];
    for (var c = 0; c < inputText.length; c++) {
      var code = inputText.codePointAt(c);
      if (code > 0xffff) c++; // surrogate pair consumed
      if (code < 0x80) bytes.push(code);
      else if (code < 0x800) bytes.push(0xc0 | (code >> 6), 0x80 | (code & 63));
      else if (code < 0x10000)
        bytes.push(
          0xe0 | (code >> 12), 0x80 | ((code >> 6) & 63), 0x80 | (code & 63));
      else
        bytes.push(
          0xf0 | (code >> 18), 0x80 | ((code >> 12) & 63),
          0x80 | ((code >> 6) & 63), 0x80 | (code & 63));
    }
    var bitLen = bytes.length * 8;
    bytes.push(0x80);
    while (bytes.length % 64 !== 56) bytes.push(0);
    for (var s = 56; s >= 0; s -= 8) bytes.push((bitLen / Math.pow(2, s)) & 0xff);

    var rotr = function (x, n) {
      return (x >>> n) | (x << (32 - n));
    };
    var w = new Array(64);
    for (var block = 0; block < bytes.length; block += 64) {
      var t;
      for (t = 0; t < 16; t++) {
        var o = block + t * 4;
        w[t] =
          (bytes[o] << 24) | (bytes[o + 1] << 16) | (bytes[o + 2] << 8) |
          bytes[o + 3];
      }
      for (t = 16; t < 64; t++) {
        var s0 = rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3);
        var s1 = rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10);
        w[t] = (w[t - 16] + s0 + w[t - 7] + s1) | 0;
      }
      var a = H[0], b = H[1], cc = H[2], d = H[3];
      var e = H[4], f = H[5], g = H[6], h = H[7];
      for (t = 0; t < 64; t++) {
        var S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        var ch = (e & f) ^ (~e & g);
        var temp1 = (h + S1 + ch + K[t] + w[t]) | 0;
        var S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        var maj = (a & b) ^ (a & cc) ^ (b & cc);
        var temp2 = (S0 + maj) | 0;
        h = g; g = f; f = e; e = (d + temp1) | 0;
        d = cc; cc = b; b = a; a = (temp1 + temp2) | 0;
      }
      H[0] = (H[0] + a) | 0; H[1] = (H[1] + b) | 0;
      H[2] = (H[2] + cc) | 0; H[3] = (H[3] + d) | 0;
      H[4] = (H[4] + e) | 0; H[5] = (H[5] + f) | 0;
      H[6] = (H[6] + g) | 0; H[7] = (H[7] + h) | 0;
    }
    return H.map(function (x) {
      return ("00000000" + (x >>> 0).toString(16)).slice(-8);
    }).join("");
  }

  // -------------------------------------------------------------- selection

  function validateAnchor(anchor, role) {
    if (typeof anchor !== "string" || !anchor.trim()) {
      throw SelectionError("INVALID_REQUEST", "The " + role + " is empty.");
    }
    var words = anchor.trim().split(/\s+/);
    if (words.length > MAX_ANCHOR_WORDS) {
      throw SelectionError(
        "ANCHOR_TOO_LONG",
        "The " + role + " has " + words.length +
          " words; the protocol caps markers at " + MAX_ANCHOR_WORDS + " words.",
        "Shorten the marker — uniqueness comes from rare wording, not length.",
        { role: role, words: words.length, maxWords: MAX_ANCHOR_WORDS }
      );
    }
    return words;
  }

  function extendUnique(view, span, baseWords, direction) {
    var words, k, candidate;
    if (direction === "forward") {
      words = view.raw.slice(span[0], span[0] + EXTEND_SCAN_CHARS)
        .split(/\s+/).filter(Boolean);
      for (k = baseWords + 1; k <= MAX_ANCHOR_WORDS && k <= words.length; k++) {
        candidate = words.slice(0, k).join(" ");
        if (view.count(candidate) === 1) return candidate;
      }
    } else {
      words = view.raw.slice(Math.max(0, span[1] - EXTEND_SCAN_CHARS), span[1])
        .split(/\s+/).filter(Boolean);
      for (k = baseWords + 1; k <= MAX_ANCHOR_WORDS && k <= words.length; k++) {
        candidate = words.slice(-k).join(" ");
        if (view.count(candidate) === 1) return candidate;
      }
    }
    return null;
  }

  function occurrenceDetails(text, spans) {
    return spans.map(function (span, i) {
      return {
        occurrence: i + 1,
        line: lineOf(text, span[0]),
        preview: contextPreview(text, span),
      };
    });
  }

  function closestWindow(view, needle) {
    var probeWords = needle.split(" ");
    var tokens = view.raw.match(/\S+/g) || [];
    if (!tokens.length || tokens.length > 150000) return null;
    var k = Math.min(probeWords.length, tokens.length);
    var best = { ratio: 0, index: -1 };
    // Token positions for span reconstruction:
    var positions = [];
    var re = /\S+/g;
    var m;
    while ((m = re.exec(view.raw)) !== null) positions.push([m.index, m.index + m[0].length]);
    for (var i = 0; i + k <= tokens.length; i++) {
      var candidate = tokens.slice(i, i + k).join(" ");
      var ratio = similarity(candidate, needle);
      if (ratio > best.ratio) best = { ratio: ratio, index: i };
    }
    if (best.index < 0 || best.ratio < 0.55) return null;
    var span = [positions[best.index][0], positions[best.index + k - 1][1]];
    return {
      similarity: Math.round(best.ratio * 1000) / 1000,
      line: lineOf(view.raw, span[0]),
      text: view.raw.slice(span[0], span[1]),
    };
  }

  function notFound(view, anchor, role) {
    var closest = closestWindow(view, probe(anchor));
    var details = { role: role, anchor: anchor };
    var hint =
      "The marker must quote the document verbatim (whole words, including " +
      "punctuation). Whitespace and line wrapping are forgiven — wording is not.";
    if (closest) {
      details.closestMatch = closest;
      hint =
        'Closest real passage (' + Math.round(closest.similarity * 100) +
        "% similar, line " + closest.line + '): "' + shorten(closest.text, 90) +
        '". Quote the document exactly, including punctuation.';
    }
    return SelectionError(
      "ANCHOR_NOT_FOUND",
      'The ' + role + ' "' + shorten(anchor) +
        '" was not found in the document (0 occurrences).',
      hint,
      details
    );
  }

  function tightPairs(starts, ends) {
    function firstEnd(s) {
      for (var i = 0; i < ends.length; i++) {
        if (ends[i][0] >= s[0] && ends[i][1] >= s[1]) return ends[i];
      }
      return null;
    }
    function lastStart(e) {
      var candidate = null;
      for (var i = 0; i < starts.length; i++) {
        if (starts[i][0] <= e[0] && starts[i][1] <= e[1]) candidate = starts[i];
      }
      return candidate;
    }
    var pairs = [];
    starts.forEach(function (s) {
      var e = firstEnd(s);
      if (e && lastStart(e) === s) pairs.push([s, e]);
    });
    return pairs;
  }

  function ambiguityError(view, op, starts, ends, startWords, endWords) {
    var problems = [];
    var hints = [];
    var details = {};
    function describe(role, anchor, spans, baseWords, direction) {
      var suggestions = spans.map(function (span) {
        return {
          line: lineOf(view.raw, span[0]),
          anchor: extendUnique(view, span, baseWords, direction),
        };
      });
      details[role] = {
        anchor: anchor,
        occurrences: occurrenceDetails(view.raw, spans),
        suggestions: suggestions,
      };
      problems.push(
        role + ' anchor "' + shorten(anchor) + '" matches ' + spans.length +
          " locations (lines " +
          spans.slice(0, 8).map(function (s) { return lineOf(view.raw, s[0]); })
            .join(", ") + ")"
      );
      var usable = suggestions.filter(function (s) { return s.anchor; });
      if (usable.length) {
        hints.push(
          "Unique " + role + "-anchor extensions: " +
            usable.slice(0, 4).map(function (s) {
              return '"' + s.anchor + '" (line ' + s.line + ")";
            }).join("; ")
        );
      }
    }
    if (starts.length > 1) describe("start", op.startAnchor, starts, startWords.length, "forward");
    if (ends.length > 1) describe("end", op.endAnchor, ends, endWords.length, "backward");
    if (!hints.length) {
      hints.push(
        "No unique extension of <= " + MAX_ANCHOR_WORDS +
          " words exists — use contextual neighborhood matching, or pass " +
          "occurrenceStart/occurrenceEnd indices."
      );
    } else {
      hints.push(
        "Resend the edit with an extended anchor (max " + MAX_ANCHOR_WORDS +
          ' words), or use occurrenceStart/occurrenceEnd, or resolution: "pair".'
      );
    }
    return SelectionError(
      "ANCHOR_NOT_UNIQUE",
      "Selection refused — " + problems.join(" and ") +
        ". Splicing would risk replacing the wrong text.",
      hints.join(" "),
      details
    );
  }

  /**
   * Resolves an anchor operation to a confirmed selection. Never mutates.
   * @param {string} text  The document.
   * @param {object} op    { startAnchor, endAnchor, occurrenceStart?,
   *                         occurrenceEnd?, resolution? }
   * @returns {object} Selection with status "SELECTION_CONFIRMED".
   */
  function selectByAnchors(text, op) {
    if (typeof text !== "string") {
      throw SelectionError("INVALID_REQUEST", "text must be a string.");
    }
    var startWords = validateAnchor(op.startAnchor, "start anchor");
    var endWords = validateAnchor(op.endAnchor, "end anchor");
    var view = new NormalizedView(text);
    var starts = view.findSpans(probe(op.startAnchor));
    var ends = view.findSpans(probe(op.endAnchor));
    if (!starts.length) throw notFound(view, op.startAnchor, "start anchor");
    if (!ends.length) throw notFound(view, op.endAnchor, "end anchor");

    var notes = [];
    [["start", startWords], ["end", endWords]].forEach(function (pair) {
      if (pair[1].length < SUGGESTED_MIN_ANCHOR_WORDS) {
        notes.push(
          pair[0] + " anchor is only " + pair[1].length +
            " word(s); " + SUGGESTED_MIN_ANCHOR_WORDS + "-8 words is safer"
        );
      }
    });

    var startSpan, endSpan;
    var resolution = "unique_anchors";
    if (op.occurrenceStart != null || op.occurrenceEnd != null) {
      var si = (op.occurrenceStart || 1) - 1;
      var ei = (op.occurrenceEnd || 1) - 1;
      if (si < 0 || si >= starts.length || ei < 0 || ei >= ends.length) {
        throw SelectionError(
          "INVALID_REQUEST",
          "occurrence index out of range (start ×" + starts.length +
            ", end ×" + ends.length + ").",
          null,
          {
            startOccurrences: occurrenceDetails(text, starts),
            endOccurrences: occurrenceDetails(text, ends),
          }
        );
      }
      startSpan = starts[si];
      endSpan = ends[ei];
      resolution = "indexed";
    } else if (starts.length === 1 && ends.length === 1) {
      startSpan = starts[0];
      endSpan = ends[0];
    } else if (op.resolution === "pair") {
      var pairs = tightPairs(starts, ends);
      if (pairs.length === 1) {
        startSpan = pairs[0][0];
        endSpan = pairs[0][1];
        resolution = "pair";
        notes.push(
          "anchors were not individually unique; resolved as the single " +
            "innermost start→end pairing"
        );
      } else {
        throw SelectionError(
          "ANCHOR_NOT_UNIQUE",
          "Pair resolution failed: the anchors form " + pairs.length +
            " possible start→end pairings (start ×" + starts.length +
            ", end ×" + ends.length + ").",
          "Extend one anchor until unique, or pass occurrence indices.",
          {
            pairs: pairs.map(function (p) {
              return { lines: [lineOf(text, p[0][0]), lineOf(text, p[1][0])] };
            }),
          }
        );
      }
    } else {
      throw ambiguityError(view, op, starts, ends, startWords, endWords);
    }

    if (endSpan[1] < startSpan[1] || endSpan[0] < startSpan[0]) {
      throw SelectionError(
        "END_BEFORE_START",
        "The end anchor (line " + lineOf(text, endSpan[0]) +
          ") resolves before the start anchor (line " +
          lineOf(text, startSpan[0]) + ").",
        "The start anchor must be the block's first words and the end anchor " +
          "its last words, in document order."
      );
    }

    var start = startSpan[0];
    var end = endSpan[1];
    var selected = text.slice(start, end);
    return {
      status: "SELECTION_CONFIRMED",
      strategy: "anchor",
      resolution: resolution,
      start: start,
      end: end,
      text: selected,
      startLine: lineOf(text, start),
      endLine: end > start ? lineOf(text, end - 1) : lineOf(text, start),
      sha256: sha256Hex(selected),
      confidence: 1.0,
      notes: notes,
    };
  }

  // ------------------------------------------------------------- replacement

  function prepareInlineReplacement(replacement, eol) {
    var rep = replacement.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    rep = rep.replace(/^(?:[ \t]*\n)+/, ""); // leading blank lines
    rep = rep.replace(/\s+$/, "");           // trailing whitespace
    return eol === "\r\n" ? rep.replace(/\n/g, "\r\n") : rep;
  }

  function spliceInline(text, selection, replacement, eol) {
    var left = text.slice(0, selection.start);
    var right = text.slice(selection.end);
    if (replacement === "") {
      var lm = left.match(/(?:\r?\n)+$/);
      var rm = right.match(/^(?:\r?\n)+/);
      var leftRun = lm ? (lm[0].match(/\n/g) || []).length : 0;
      var rightRun = rm ? (rm[0].match(/\n/g) || []).length : 0;
      if (leftRun && rightRun) {
        var keep = Math.max(leftRun, rightRun);
        left = left.slice(0, left.length - lm[0].length);
        right = right.slice(rm[0].length);
        return left + new Array(keep + 1).join(eol) + right;
      }
      if (/[ \t]$/.test(left) && /^[ \t]/.test(right)) {
        return left + right.replace(/^[ \t]+/, "");
      }
    }
    return left + replacement + right;
  }

  /**
   * Resolves and applies one anchor edit. Throws SelectionError on any
   * ambiguity; the input string is returned untouched in that case (throw
   * happens before splicing).
   * @returns {object} { text, selection, removed, added }
   */
  function applyAnchorEdit(text, op) {
    var selection = selectByAnchors(text, op);
    if (typeof op.replace !== "string") {
      throw SelectionError("INVALID_REQUEST", '"replace" must be a string.');
    }
    var eol = detectEol(text);
    var added = prepareInlineReplacement(op.replace, eol);
    var result =
      added === selection.text
        ? text // no-op
        : spliceInline(text, selection, added, eol);
    return {
      text: result,
      selection: selection,
      removed: selection.text,
      added: added,
      noop: added === selection.text,
    };
  }

  // --------------------------------------------------------------- suggest

  /**
   * Computes the minimal statistically-unique anchor pair for text[start,end).
   * @returns {object} { startAnchor, endAnchor, startWords, endWords, lines }
   */
  function suggestAnchors(text, start, end, opts) {
    opts = opts || {};
    var minWords = Math.max(1, Math.min(
      opts.minWords || SUGGESTED_MIN_ANCHOR_WORDS, MAX_ANCHOR_WORDS));
    if (!(start >= 0 && start < end && end <= text.length)) {
      throw SelectionError("INVALID_REQUEST", "Invalid block span.");
    }
    var view = new NormalizedView(text);
    var words = text.slice(start, end).split(/\s+/).filter(Boolean);
    if (!words.length) {
      throw SelectionError("INVALID_REQUEST", "The block contains no words.");
    }
    function grow(pick) {
      for (var k = Math.min(minWords, words.length); k <= MAX_ANCHOR_WORDS; k++) {
        if (k > words.length) break;
        var candidate = pick(k).join(" ");
        if (view.count(candidate) === 1) return { anchor: candidate, words: k };
      }
      return null;
    }
    var startPick = grow(function (k) { return words.slice(0, k); });
    var endPick = grow(function (k) { return words.slice(-k); });
    if (!startPick || !endPick) {
      throw SelectionError(
        "CANNOT_UNIQUIFY",
        "No unique anchor of <= " + MAX_ANCHOR_WORDS +
          " words exists for this block.",
        "Use contextual neighborhood matching or comment-tag markers — the " +
          "block's edges are too repetitive for statistical anchors."
      );
    }
    return {
      strategy: "anchor",
      startAnchor: startPick.anchor,
      endAnchor: endPick.anchor,
      startWords: startPick.words,
      endWords: endPick.words,
      lines: [lineOf(text, start), lineOf(text, Math.max(start, end - 1))],
    };
  }

  return {
    MAX_ANCHOR_WORDS: MAX_ANCHOR_WORDS,
    SUGGESTED_MIN_ANCHOR_WORDS: SUGGESTED_MIN_ANCHOR_WORDS,
    NormalizedView: NormalizedView,
    probe: probe,
    detectEol: detectEol,
    sha256Hex: sha256Hex,
    selectByAnchors: selectByAnchors,
    applyAnchorEdit: applyAnchorEdit,
    suggestAnchors: suggestAnchors,
  };
});
