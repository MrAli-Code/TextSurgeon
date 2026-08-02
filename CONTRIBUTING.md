# Contributing to Text Surgeon

Thanks for scrubbing in. 🧤

## Ground rules (the project's vital signs)

1. **Zero dependencies is a feature.** The Python side is standard library only; the JS port runs in Node *and* the browser with no `npm install`. PRs that add a runtime dependency will be asked to find a stdlib way first.
2. **The flat layout is deliberate.** All runtime files (`*.py`, `surgeon_anchor.js`, `surgeon_ui.html`, `Start-Text-Surgeon.bat`) must stay side-by-side in one folder — the one-click launcher and the web server both resolve siblings relative to their own directory. Docs and CI config may live in subfolders; code may not move.
3. **Refusal over guessing.** The engine never writes on an ambiguous selection. New strategies or matching rules must preserve the uniqueness contract and the two-phase (select → confirm → splice) model, and should return machine-actionable repair data on failure.
4. **The engine owns byte hygiene.** CRLF/LF, BOM, final newline, indentation and seam whitespace are engine responsibilities. Never delegate them to model output.
5. **Python 3.8+ compatibility.** No syntax or stdlib APIs newer than 3.8 in runtime code.

## Running the tests

```bash
python3 -m unittest             # 115 tests — engine, CLI, web workflow
node test_surgeon_anchor.js     #  18 tests — JS port parity
```

Both suites must pass on your machine before you open a PR. CI runs them on Linux and Windows across multiple Python and Node versions.

## Making changes

- **Engine behavior** lives in `surgeon_engine.py`; mirror any anchor-strategy change in `surgeon_anchor.js` (the JS port intentionally tracks the Python semantics and error codes 1:1) and cover it in both test suites.
- **Protocol changes** (new fields, strategies, error codes) must be documented in `SCHEMA.md` in the same PR, and should remain backward compatible — v1 `{"search": …, "replace": …}` objects still apply today; keep it that way.
- **UI text** in `surgeon_ui.html` is bilingual. If you add a string, add both the English and the Farsi variant, and check the layout in RTL mode.
- **User-visible changes** get a line in `CHANGELOG.md`.

## Reporting bugs

The most useful bug report for a selection engine is a *reproduction document*:

1. A minimal input file (or snippet),
2. the exact `@@EDIT` block (or JSON edit) that misbehaved,
3. what the engine said (`SELECTION CONFIRMED` / error code + details),
4. what you expected instead.

Multilingual edge cases (RTL text, ZWNJ, confusables, mixed digit systems) are first-class citizens here — please don't shy away from filing them.
