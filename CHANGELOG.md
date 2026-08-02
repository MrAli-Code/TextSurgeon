# Changelog

All notable changes to Text Surgeon are documented here.
The format loosely follows [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## [2.2.0]

### Added
- **Two prompt modes** in Step 1 — a big token saver:
  - **New chat** — the prompt includes the whole document (use for a fresh AI conversation).
  - **Same chat** — a compact prompt that does *not* re-send the document, because the AI already has it from earlier in the conversation. For a large file this cuts thousands of tokens per follow-up (e.g. ~5,500 → ~290 tokens).
- In *Same chat* mode, any edits applied since the document was shared are summarized in the prompt, so the AI's copy stays correct.
- CLI: `--followup` flag for `--generate`.

## [2.1.2]

### Changed
- The verification prompt is now shown as **Step 3**, directly under the green "Edit applied" banner (previously it sat below the splice cards and looked missing on large edits).

### Fixed
- **Resilience to real-world AI replies:** right-to-left responses that attach invisible direction marks to the `<<<` / `>>>` fences now parse correctly; whole-block ``` code fences and sloppy `<<<<` / `>>>>` fences are tolerated; if an `@@EDIT` block is still malformed, the app falls back to a JSON array in the same reply.
- **Smart-quote safety:** curly quotes/apostrophes (`“”` `‘’`) substituted by models no longer break anchor matching. Persian guillemets « » are preserved (never folded) because they are real quotation marks.

## [2.1.0]

### Added
- **Native file picker** — "Choose file…" opens the operating system's real Open-File dialog, with automatic fallback to the built-in folder browser on machines without a desktop dialog.
- **Files workspace** — every opened file is remembered in a "Your files" list with per-file badges (operation count, pending change, backup, missing) and one-click resume of the pending change request.
- **Separate memories per file** — each document keeps its own operation history; switching files never mixes logs.
- **International text robustness** (matters a lot for Farsi/Arabic): anchor matching transparently handles invisible characters (soft hyphens, ZWNJ/ZWJ, zero-width spaces, bidi marks), Persian/Arabic confusables (kaf/keheh, yeh forms, teh-marbuta, digit systems), and Markdown emphasis glued to anchored words.

## [2.0.0] — Surgeon Protocol 2.0

### Changed
- **The AI no longer re-quotes the text it wants to change.** It points at a block with two short markers and the engine does the rest.

### Added
- Four selection strategies:
  - `anchor` — Statistical Anchor Marking (default): first 5–10 + last 5–10 words, each verified unique; ambiguous markers abort with pre-computed unique extensions.
  - `tags` — edit only between user-placed `[START_EDIT]` / `[END_EDIT]` marker lines.
  - `context` — fuzzy match of the 1–5 lines around the block, for repetitive boilerplate.
  - `verbatim` — v1-style exact search/replace for micro-edits.
- **Safety model:** selection and mutation are separate phases; every edit gets a `SELECTION CONFIRMED` line (line range, size, SHA-256) before the file is written; batches are transactional, overlap-checked, and spliced bottom-up.
- Whitespace, indentation, CRLF/LF, BOM, and final-newline fidelity handled by the engine, never by the AI.
- Full schema in `SCHEMA.md`. v1 JSON responses still accepted.

## [1.0.0]

- Initial release: exact search/replace protocol (JSON), atomic writes with `.bak` backups, operation log in `.surgeon_memory.json`.
