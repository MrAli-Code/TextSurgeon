# Surgeon Protocol v2 — Edit Suggestion Schema

How an AI (or any tool) tells Text Surgeon what to change. One edit = one
selection strategy + one replacement. The engine refuses any selection it
cannot resolve to **exactly one** location — nothing is ever written on a
refusal, and every refusal carries machine-actionable repair data.

**The core economy:** never re-quote the block you are replacing. A
1,000-line block is addressed by ~15 words (two anchors). Markers are
capped at **10 words** — the engine rejects longer ones.

---

## 1. Response envelope

A model reply consists of an explanation followed by edits, in either the
Markdown block format (preferred in chat — no JSON escaping) or a JSON
array (preferred for programmatic callers):

```
<EXPLANATION>
One short paragraph: what changes and why.
</EXPLANATION>

@@EDIT anchor
START-ANCHOR: first 5-10 words of the block
END-ANCHOR: last 5-10 words of the block
<<<
Full replacement text — real newlines, no escaping.
>>>
```

Rules of the `@@EDIT` grammar:

- `@@EDIT <strategy>` starts a block; strategy defaults to `anchor`.
- Header lines are `KEY: value`. `BEFORE:`/`AFTER:` may repeat (context
  strategy, max 5 each, in top-to-bottom order).
- The replacement body sits between a line containing only `<<<` and a
  line containing only `>>>`. An **empty body deletes the selection.**
- A body that must itself contain a bare `>>>` line cannot use this
  format — use JSON for that edit.
- Multiple `@@EDIT` blocks per response are fine; they must not overlap
  and should be ordered top-to-bottom as they appear in the document.

---

## 2. Strategies

### 2.1 `anchor` — Statistical Anchor Marking *(default for blocks ≥ 2 lines)*

Reference the block by its edges only. Matching is **whitespace-elastic**
(line wrapping, indentation, and CRLF/LF differences are forgiven) and
**word-aligned** (a marker never matches inside a larger word), but
wording, capitalisation, and punctuation must be verbatim.

| Field | Type | Req | Meaning |
|---|---|---|---|
| `start_anchor` | string | ✔ | The block's **first** 5–10 words, verbatim. |
| `end_anchor` | string | ✔ | The block's **last** 5–10 words, verbatim. |
| `replace` | string | ✔ | Full replacement (`""` deletes the block). |
| `occurrence_start` | int ≥ 1 | – | Explicit 1-based occurrence index (escape hatch). |
| `occurrence_end` | int ≥ 1 | – | Same, for the end anchor. |
| `resolution` | `"strict"` \| `"pair"` | – | `"pair"` opts in to innermost-pair resolution when anchors are individually ambiguous but form exactly one valid pairing. Default `"strict"`. |

```json
{
  "strategy": "anchor",
  "start_anchor": "The migration process begins when",
  "end_anchor": "and completes the rollback safely.",
  "replace": "The migration is now a single atomic switchover…"
}
```

**Uniqueness contract.** Each anchor must occur exactly once. If the
block's first words also appear elsewhere, extend the anchor with the
words that *follow them inside the block* — `"The system is"` →
`"The system is initialized by the user"` — up to the 10-word cap. On an
ambiguous anchor the engine aborts with `ANCHOR_NOT_UNIQUE` and returns
per-occurrence **pre-computed unique extensions**, so the very next
attempt can succeed.

The selection spans from the first character of the start anchor to the
last character of the end anchor, inclusive.

### 2.2 `tags` — Comment-Tag surgical selection

For files where the user pre-marks the region:

```text
// [START_EDIT]          or          # [START_EDIT:pricing]
…region…                             …region…
// [END_EDIT]                        # [END_EDIT:pricing]
```

| Field | Type | Req | Meaning |
|---|---|---|---|
| `replace` | string | ✔ | New region content (`""` empties the region). |
| `name` | string | – | Selects `[START_EDIT:name]` / `[END_EDIT:name]`. |
| `mode` | `"inner"` \| `"block"` | – | `"inner"` (default) keeps the marker lines and replaces only what's between them; `"block"` removes markers too. |
| `start_tag` / `end_tag` | string | – | Override the marker tokens entirely. |

Markers may live inside any comment syntax — the engine matches the token
anywhere on the line. Each marker must appear exactly once (per name).

### 2.3 `context` — Contextual Neighborhood Matching

For repetitive boilerplate where no unique ≤ 10-word anchor exists.
The block is located by fuzzy-matching its neighbor lines.

| Field | Type | Req | Meaning |
|---|---|---|---|
| `before` | string[] ≤ 5 | ✱ | Lines directly **above** the block, top-to-bottom. |
| `after` | string[] ≤ 5 | ✱ | Lines directly **below** the block. |
| `replace` | string | ✔ | Full replacement for the enclosed lines. |
| `target_hint` | string | – | The block's first line — a scoring tiebreaker. |
| `min_score` | float | – | Combined similarity threshold (default 0.85). |
| `margin` | float | – | Winner must beat the runner-up by this (default 0.05). |

✱ at least one of `before`/`after`. Empty `before` pins the block start to
the beginning of file; empty `after` pins its end to the end of file.
`before` adjacent to `after` (no lines between) makes the edit a **pure
insertion** between them. If two candidate locations score within
`margin`, the engine refuses (`CONTEXT_ERROR`) rather than guess.

```json
{
  "strategy": "context",
  "before": ["  name: beta"],
  "after": ["resource block:", "  name: gamma"],
  "target_hint": "  size: small",
  "replace": "  size: large\n  retries: 9\n"
}
```

### 2.4 `verbatim` — Protocol-v1 exact match *(micro-edits)*

Still the best tool for changing a few words inside one sentence.

| Field | Type | Req | Meaning |
|---|---|---|---|
| `search` | string | ✔ | Exact text, must occur exactly once. |
| `replace` | string | ✔ | Replacement. |

Bare v1 objects (`{"search": …, "replace": …}` with no `strategy`) are
accepted unchanged for backwards compatibility.

---

## 3. Guards (optional, any strategy)

Caller-supplied tripwires checked *after* selection, *before* splicing:

```json
"guards": {
  "expected_sha256": "3f6ac1b2e8d9…",      // ≥8 hex chars; prefix of the selection's SHA-256
  "max_lines": 400,                        // refuse selections larger than this
  "expected_line_range": [120, 180],       // selection must fall inside…
  "tolerance": 10                          // …±tolerance lines
}
```

`expected_sha256` powers the two-phase workflow: confirm a selection
(phase 1), pin its hash, apply later (phase 2) — if the file drifted in
between, the splice is refused with `GUARD_FAILED`.

---

## 4. Selection confirmation & errors

**Phase 1 — select** (`select_edit_ops`, `--dry-run`, or the web preview)
returns, per edit:

```json
{
  "status": "SELECTION_CONFIRMED",
  "strategy": "anchor",
  "resolution": "unique_anchors",
  "lines": [42, 118],
  "line_count": 77,
  "chars": 4210,
  "sha256": "3f6ac1b2e8d9…",
  "confidence": 1.0,
  "notes": []
}
```

**Phase 2 — apply** resolves every edit against the original snapshot,
rejects overlaps, then splices bottom-up (later spans first) so earlier
splices can never shift later targets. The batch is transactional: any
error aborts everything before a single character changes.

| Code | Meaning | Repair data in `details` |
|---|---|---|
| `ANCHOR_NOT_FOUND` | 0 occurrences | closest real passage + similarity + line |
| `ANCHOR_NOT_UNIQUE` | ≥ 2 occurrences | every occurrence (line, preview) + **unique extended anchors per occurrence** |
| `ANCHOR_TOO_LONG` | marker > 10 words | word counts |
| `END_BEFORE_START` | anchors out of order | both lines |
| `CANNOT_UNIQUIFY` | no ≤ 10-word unique anchor exists | block lines; use `context`/`tags` |
| `TAG_ERROR` | marker missing/duplicated/out of order | marker lines |
| `CONTEXT_ERROR` | neighborhood not found or ambiguous | scored candidates |
| `EDITS_OVERLAP` | two edits share territory | edit indices + line ranges |
| `GUARD_FAILED` | hash/size/range tripwire fired | expected vs. actual |
| `INVALID_REQUEST` | malformed edit object | field-level message |

---

## 5. Matching robustness (anchors & context)

Anchor/context matching is deliberately forgiving so a human or model can
quote text without reproducing invisible detail — while the file's exact
bytes are never rewritten outside the replaced span, and uniqueness is
always enforced (a fold can only help a match or trigger a safe refusal):

- **Whitespace-elastic** — line wrapping, indentation, and repeated spaces
  are ignored; an anchor written on one line matches text wrapped over many.
- **Invisible-elastic** — soft hyphens (U+00AD), ZWNJ/ZWJ (U+200C/D),
  zero-width spaces, bidi marks (LRM/RLM), word joiners, and stray BOMs are
  transparent. A Persian word joined with a soft hyphen matches an anchor
  typed with a ZWNJ, or with no joiner at all.
- **Confusable-folded** — Persian/Arabic look-alikes are unified for
  matching: Arabic kaf (U+0643) ↔ Persian keheh (U+06A9), Arabic yeh
  (U+064A) / alef-maksura (U+0649) ↔ farsi yeh (U+06CC), teh-marbuta ↔ heh,
  hamza-alef forms → alef, and Arabic-Indic/Persian digits ↔ ASCII.
- **Word-aligned** — a match may not begin or end inside a letter/digit run,
  so `the system` never matches inside `the systematic`, and Markdown
  emphasis (`**bold**`, `_italic_`, `` `code` ``) or punctuation glued to a
  word acts as a boundary rather than blocking the anchor.

## 6. Whitespace & structure warranty

The engine — never the AI — owns byte hygiene:

- File EOL style (LF / CRLF) is detected and enforced on the replacement;
  a UTF-8 BOM and a missing final newline are preserved as found.
- `anchor` spans run non-whitespace → non-whitespace, so leading blank
  lines / trailing whitespace on the replacement are trimmed — no doubled
  blank lines at the seams.
- Deleting with `replace: ""` heals the seam: paragraph deletions keep
  the blank-line rhythm, single-line deletions keep single spacing, and
  mid-sentence deletions collapse doubled spaces.
- `tags`/`context` replacements are whole-line blocks; the engine ensures
  newline termination and otherwise preserves your blank-line padding.
