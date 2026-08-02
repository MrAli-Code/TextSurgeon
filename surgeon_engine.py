#!/usr/bin/env python3
"""Surgeon Engine — precision text selection & replacement (Protocol v2).

This module is the selection core of Text Surgeon. It locates a target span
inside a document using one of four strategies and splices a replacement in
with byte-level care, without the caller (an AI or a human) ever having to
re-quote the full original block.

Strategies
----------
``anchor``    Statistical Anchor Marking. The block is identified by a short
              *start anchor* (its first ~5-10 words) and a short *end anchor*
              (its last ~5-10 words). Both anchors must be statistically
              unique in the document (exactly one word-aligned occurrence).
              Matching is whitespace-elastic, so re-wrapped lines still hit.
              A 1,000-line block can be replaced by quoting ~15 words.

``tags``      Comment-Tag surgical selection. The user (or a tool) places
              ``[START_EDIT]`` / ``[END_EDIT]`` marker lines in the file;
              the engine selects the region between them. Best for complex
              files where boundaries are defined manually up front.

``context``   Contextual Neighborhood Matching. The block is located by
              fuzzy-matching the lines immediately above and below it.
              Best for repetitive boilerplate where unique anchors are hard
              to find. Refuses to act when two candidate locations score
              within the ambiguity margin of each other.

``verbatim``  Protocol v1 compatibility: an exact ``search`` string that
              must occur exactly once. Still the best tool for micro-edits
              of a few words.

Safety model
------------
Selection and mutation are separate phases. ``SelectionEngine.select()``
returns a ``Selection`` whose ``status`` is ``"SELECTION_CONFIRMED"`` —
including the exact line range and a SHA-256 of the text about to be
replaced — before anything is spliced. Every ambiguity raises a
``SelectionError`` subclass with a machine-actionable ``details`` payload
(occurrence locations, auto-extended unique anchor suggestions) instead of
ever touching the wrong text. ``apply_edit_ops`` resolves *all* edits
against the original document, rejects overlaps, and only then splices,
bottom-up, so earlier splices can never shift later targets.

The module is dependency-free (standard library only, Python 3.8+) and has
no I/O: it transforms strings. Callers own files, backups, and logs.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Final, Iterable, List, Optional, Sequence, Tuple


ENGINE_VERSION = "2.2.0"

# Hard limits & defaults of the statistical-anchor contract.
MAX_ANCHOR_WORDS = 10          # the AI must never emit a longer marker
SUGGESTED_MIN_ANCHOR_WORDS = 5  # advisory lower bound (5-8 words is ideal)
EXTEND_SCAN_CHARS = 6000       # how far around an occurrence we read words

DEFAULT_START_TAG = "[START_EDIT]"
DEFAULT_END_TAG = "[END_EDIT]"

DEFAULT_MIN_SCORE = 0.85       # context strategy: minimum combined score
DEFAULT_SCORE_MARGIN = 0.05    # context strategy: winner must lead by this
DEFAULT_MAX_BLOCK_LINES = 400  # context strategy: max lines between contexts
MAX_CONTEXT_LINES = 5          # per side

_FUZZY_DIAGNOSIS_WORD_CAP = 150_000  # skip fuzzy scans on gigantic documents


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class SelectionError(Exception):
    """Base class for every refusal to select or splice.

    Attributes:
        code: Stable machine-readable error code (e.g. ``ANCHOR_NOT_UNIQUE``).
        hint: Human/AI-oriented remediation advice.
        details: Structured payload (occurrences, suggestions, indices …)
            suitable for feeding straight back to an LLM.
    """

    code = "SELECTION_ERROR"

    def __init__(
        self,
        message: str,
        *,
        hint: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.hint = hint
        self.details: Dict[str, Any] = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        """Serialises the error for JSON transport."""
        return {
            "status": "SELECTION_REJECTED",
            "code": self.code,
            "error": str(self),
            "hint": self.hint,
            "details": self.details,
        }


class InvalidRequestError(SelectionError):
    """The edit object itself is malformed (wrong keys/types/values)."""

    code = "INVALID_REQUEST"


class AnchorNotFoundError(SelectionError):
    """An anchor (or verbatim search) has zero occurrences."""

    code = "ANCHOR_NOT_FOUND"


class AnchorNotUniqueError(SelectionError):
    """An anchor matches more than once and no resolution was authorised."""

    code = "ANCHOR_NOT_UNIQUE"


class AnchorTooLongError(SelectionError):
    """An anchor exceeds the MAX_ANCHOR_WORDS contract."""

    code = "ANCHOR_TOO_LONG"


class AnchorOrderError(SelectionError):
    """The end anchor resolves before the start anchor."""

    code = "END_BEFORE_START"


class CannotUniquifyError(SelectionError):
    """No unique anchor of <= MAX_ANCHOR_WORDS words exists for the block."""

    code = "CANNOT_UNIQUIFY"


class TagSelectionError(SelectionError):
    """Comment-tag markers are missing, duplicated, or out of order."""

    code = "TAG_ERROR"


class ContextMatchError(SelectionError):
    """Neighborhood matching found nothing (or too many equal candidates)."""

    code = "CONTEXT_ERROR"


class OverlapError(SelectionError):
    """Two edits in one batch resolve to overlapping spans."""

    code = "EDITS_OVERLAP"


class GuardError(SelectionError):
    """A caller-supplied guard (hash / size / line range) failed."""

    code = "GUARD_FAILED"


# --------------------------------------------------------------------------- #
# Whitespace- and invisible-elastic text view
# --------------------------------------------------------------------------- #

# Typographically invisible / zero-width characters that humans and chat
# interfaces routinely drop, add, or swap when re-typing text — especially in
# Persian/Arabic, where word-parts are joined with a soft hyphen or a
# zero-width non-joiner that look identical on screen. These are made
# transparent for *matching* only; the raw bytes outside a replaced span are
# always preserved, and invisibles inside a replaced span are replaced with
# the block, so byte fidelity of the untouched document is never affected.
_INVISIBLE: Final = frozenset(
    "­"  # SOFT HYPHEN
    "​"  # ZERO WIDTH SPACE
    "‌"  # ZERO WIDTH NON-JOINER (ZWNJ)
    "‍"  # ZERO WIDTH JOINER (ZWJ)
    "‎"  # LEFT-TO-RIGHT MARK
    "‏"  # RIGHT-TO-LEFT MARK
    "⁠"  # WORD JOINER
    "﻿"  # ZERO WIDTH NO-BREAK SPACE / BOM
)

# Bidirectional formatting controls. These cling to plain ASCII grammar tokens
# (@@EDIT, <<<, >>>, KEY:) whenever a model or editor emits them inside an RTL
# (Persian/Arabic) paragraph, so ``"<<<"`` arrives as ``"<<<‏"`` and a
# naive ``.strip()`` no longer recognises the fence. They are ignored only
# when detecting the @@EDIT block grammar — never inside replacement bodies.
_BIDI_CONTROLS: Final = frozenset(
    "؜"  # ARABIC LETTER MARK (U+061C)
    "‪‫‬‭‮"  # LRE RLE PDF LRO RLO (U+202A–U+202E)
    "⁦⁧⁨⁩"  # LRI RLI FSI PDI (U+2066–U+2069)
)
_STRUCTURAL_IGNORE: Final = _INVISIBLE | _BIDI_CONTROLS


def _structural(line: str) -> str:
    """Returns a line stripped of bidi/invisible marks and outer whitespace.

    Used only to recognise the @@EDIT block grammar, so a fence or header
    token survives an RTL round-trip: ``"<<<" + RLM`` still reads as ``"<<<"``
    and ``"END-ANCHOR"`` wrapped in directional isolates still reads as a key.
    Replacement-body text is never passed through this — only structural
    tokens are.
    """
    return "".join(ch for ch in line if ch not in _STRUCTURAL_IGNORE).strip()

# Confusable characters folded to one canonical form *for matching only*.
# Persian and Arabic share letters that render identically but use different
# codepoints (kaf, yeh) — a document may use the Arabic form while anyone
# re-typing an anchor uses the Persian form (or vice-versa). Folding is
# strictly 1 char -> 1 char so the offset map stays exact, and it is applied
# only to the search projection: raw bytes are never rewritten, and
# uniqueness is still enforced, so a fold can only help a match or trigger a
# safe "not unique" refusal — never cause a wrong edit.
_CONFUSABLE: Final[Dict[int, str]] = {
    0x0643: "ک",  # ARABIC KAF        -> ARABIC KEHEH (Persian kaf)
    0x0649: "ی",  # ALEF MAKSURA      -> FARSI YEH
    0x064A: "ی",  # ARABIC YEH        -> FARSI YEH
    0x0629: "ه",  # TEH MARBUTA       -> HEH
    0x0623: "ا",  # ALEF WITH HAMZA ABOVE -> ALEF
    0x0625: "ا",  # ALEF WITH HAMZA BELOW -> ALEF
    0x0622: "ا",  # ALEF WITH MADDA ABOVE -> ALEF
    # "Smart" typographic quotes/apostrophes that chat models substitute for
    # plain ASCII ones (a top cause of anchors silently not matching). Persian
    # guillemets « » are intentional quotation marks and are NOT folded.
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"',  # curly double quotes
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",  # curly single quotes
    0x02BC: "'",                                          # modifier apostrophe
}
# Arabic-Indic (U+0660-0669) and Persian (U+06F0-06F9) digits -> ASCII.
for _base in (0x0660, 0x06F0):
    for _d in range(10):
        _CONFUSABLE[_base + _d] = str(_d)


def _fold(ch: str) -> str:
    """Folds one character to its canonical confusable form (or itself)."""
    return _CONFUSABLE.get(ord(ch), ch)


def _is_word_char(ch: str) -> bool:
    """True for characters that may not sit on an anchor boundary.

    A "word" character is any Unicode letter/digit plus underscore. Matches
    must begin and end on a non-word boundary, so emphasis markers and
    punctuation are transparent while genuine words stay intact.
    """
    return ch.isalnum() or ch == "_"


def _probe(value: str) -> str:
    """Normalises a needle for matching.

    Drops typographically invisible characters, folds Persian/Arabic
    confusables, and collapses every whitespace run to a single space,
    trimming the edges. Idempotent, so it is safe to apply more than once
    along a call path.
    """
    stripped = "".join(
        _CONFUSABLE.get(ord(ch), ch) for ch in value if ch not in _INVISIBLE
    )
    return " ".join(stripped.split())


class _NormalizedView:
    """A whitespace/invisible-normalised projection with offset mapping.

    ``norm`` is the document with invisible characters removed and every
    whitespace run replaced by a single space; ``_map[i]`` is the
    raw-character offset of ``norm[i]``. Anchors are matched in ``norm``
    space (so the AI never has to reproduce exact line wrapping, indentation,
    soft hyphens, or zero-width joiners) and the hits are mapped back to
    exact raw spans. Matches must be word-aligned: an anchor can never match
    inside a larger token ("the system" will not match "breathe systems").
    """

    __slots__ = ("raw", "norm", "_map")

    def __init__(self, raw: str) -> None:
        self.raw = raw
        chars: List[str] = []
        mapping: List[int] = []
        run_start = -1
        for index, ch in enumerate(raw):
            if ch in _INVISIBLE:
                continue  # transparent for matching, preserved in raw splices
            if ch.isspace():
                if run_start < 0:
                    run_start = index
                continue
            if run_start >= 0 and chars:
                chars.append(" ")
                mapping.append(run_start)
            run_start = -1
            chars.append(_CONFUSABLE.get(ord(ch), ch))  # 1:1 fold, map intact
            mapping.append(index)
        self.norm = "".join(chars)
        self._map = mapping

    def find_spans(self, probe: str) -> List[Tuple[int, int]]:
        """Returns raw ``(start, end)`` spans of word-aligned matches.

        Args:
            probe: A needle; normalised with ``_probe`` internally, so raw
                or pre-normalised input both work.

        Returns:
            Raw character spans, in document order. Empty probe matches
            nothing (an empty anchor would match everywhere).
        """
        probe = _probe(probe)
        if not probe:
            return []
        spans: List[Tuple[int, int]] = []
        norm = self.norm
        cursor = 0
        while True:
            idx = norm.find(probe, cursor)
            if idx < 0:
                break
            end = idx + len(probe)
            # Word-aligned = the match is not glued to a letter/digit on
            # either side. Punctuation (Markdown ** _ ` , : parentheses,
            # quotes) counts as a boundary, so anchors work on **bold** and
            # _italic_ spans, while "the system" still won't match inside
            # "the systematic" or identifiers like "my_system".
            left_ok = idx == 0 or not _is_word_char(norm[idx - 1])
            right_ok = end == len(norm) or not _is_word_char(norm[end])
            if left_ok and right_ok:
                spans.append((self._map[idx], self._map[end - 1] + 1))
            cursor = idx + 1
        return spans

    def count(self, probe: str) -> int:
        """Returns the number of word-aligned occurrences of ``probe``."""
        return len(self.find_spans(probe))


# --------------------------------------------------------------------------- #
# Selection result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Selection:
    """A confirmed, uniquely-resolved target span.

    Attributes:
        strategy: The strategy that produced the selection.
        resolution: How uniqueness was established (``unique_anchors``,
            ``pair``, ``indexed``, ``tags``, ``context``, ``verbatim``).
        start: Raw character offset of the span start (inclusive).
        end: Raw character offset of the span end (exclusive).
        text: The exact raw text of the span.
        start_line: 1-based first line of the span.
        end_line: 1-based last line of the span.
        sha256: SHA-256 hex digest of ``text`` (confirmation token).
        confidence: 1.0 for exact strategies; fuzzy match score for context.
        notes: Human-readable annotations (short-anchor warnings, scores …).
    """

    strategy: str
    resolution: str
    start: int
    end: int
    text: str
    start_line: int
    end_line: int
    sha256: str
    confidence: float = 1.0
    notes: Tuple[str, ...] = ()

    @property
    def status(self) -> str:
        """Selections only exist in the confirmed state."""
        return "SELECTION_CONFIRMED"

    @property
    def line_count(self) -> int:
        """Number of lines the span touches (0 for a pure insertion point)."""
        if self.start == self.end:
            return 0
        return self.end_line - self.start_line + 1

    def to_dict(self) -> Dict[str, Any]:
        """Serialises the selection (without the full text) for JSON."""
        return {
            "status": self.status,
            "strategy": self.strategy,
            "resolution": self.resolution,
            "char_span": [self.start, self.end],
            "lines": [self.start_line, self.end_line],
            "line_count": self.line_count,
            "chars": self.end - self.start,
            "sha256": self.sha256,
            "confidence": round(self.confidence, 4),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SpliceRecord:
    """One resolved-and-prepared edit, ready to (or already) spliced.

    Attributes:
        index: 1-based position of the edit in the submitted batch.
        selection: The confirmed selection that was replaced.
        removed: Exact raw text that was removed (``selection.text``).
        added: Exact text that was inserted (after whitespace preparation).
        note: Short human-readable annotation, e.g. ``anchor/pair``.
    """

    index: int
    selection: Selection
    removed: str
    added: str
    note: str


# --------------------------------------------------------------------------- #
# Edit operations (the AI-facing request schema)
# --------------------------------------------------------------------------- #

_STRATEGY_ALIASES = {
    "anchor": "anchor",
    "anchors": "anchor",
    "statistical_anchor": "anchor",
    "statistical-anchor": "anchor",
    "tags": "tags",
    "tag": "tags",
    "comment_tag": "tags",
    "comment-tag": "tags",
    "markers": "tags",
    "context": "context",
    "neighborhood": "context",
    "neighbourhood": "context",
    "contextual_neighborhood": "context",
    "contextual-neighborhood": "context",
    "fuzzy": "context",
    "verbatim": "verbatim",
    "exact": "verbatim",
    "search_replace": "verbatim",
    "search-replace": "verbatim",
}

_GUARD_KEYS = ("expected_sha256", "max_lines", "expected_line_range", "tolerance")


@dataclass
class EditOp:
    """A validated edit request for one splice.

    Only the fields relevant to ``strategy`` are consulted. ``replace`` is
    always required (empty string deletes the span).
    """

    strategy: str
    replace: str
    # anchor
    start_anchor: Optional[str] = None
    end_anchor: Optional[str] = None
    occurrence_start: Optional[int] = None
    occurrence_end: Optional[int] = None
    resolution: str = "strict"  # "strict" | "pair"
    # tags
    name: Optional[str] = None
    start_tag: Optional[str] = None
    end_tag: Optional[str] = None
    mode: str = "inner"  # "inner" | "block"
    # context
    before: Tuple[str, ...] = ()
    after: Tuple[str, ...] = ()
    target_hint: Optional[str] = None
    min_score: Optional[float] = None
    margin: Optional[float] = None
    max_block_lines: Optional[int] = None
    # verbatim
    search: Optional[str] = None
    # safety guards
    guards: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload_item(cls, item: Any, position: int) -> "EditOp":
        """Validates one decoded payload object into an ``EditOp``.

        Args:
            item: Decoded JSON value (or dict from the Markdown parser).
            position: 1-based index used in error messages.

        Returns:
            A validated ``EditOp``.

        Raises:
            InvalidRequestError: On any structural problem, with a message
                precise enough for an AI to self-correct.
        """
        label = "edit #%d" % position
        if not isinstance(item, dict):
            raise InvalidRequestError(
                "%s is not a JSON object (got %s)." % (label, type(item).__name__),
                hint="Each edit must be an object; see the Surgeon Protocol schema.",
            )

        raw_strategy = item.get("strategy")
        if raw_strategy is None:
            if "search" in item:
                strategy = "verbatim"
            elif "start_anchor" in item or "end_anchor" in item:
                strategy = "anchor"
            elif "before" in item or "after" in item:
                strategy = "context"
            else:
                raise InvalidRequestError(
                    '%s has no "strategy" key and its shape is not recognisable.'
                    % label,
                    hint='Set "strategy" to "anchor", "tags", "context" or "verbatim".',
                )
        else:
            key = str(raw_strategy).strip().lower()
            if key not in _STRATEGY_ALIASES:
                raise InvalidRequestError(
                    "%s has unknown strategy %r." % (label, raw_strategy),
                    hint='Valid strategies: "anchor", "tags", "context", "verbatim".',
                )
            strategy = _STRATEGY_ALIASES[key]

        if "replace" not in item:
            raise InvalidRequestError(
                '%s is missing the required "replace" key.' % label,
                hint='Use "replace": "" to delete the selected span.',
            )
        replace = item["replace"]
        if not isinstance(replace, str):
            raise InvalidRequestError(
                '%s: "replace" must be a string.' % label,
            )

        op = cls(strategy=strategy, replace=replace)

        def _req_str(key: str) -> str:
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise InvalidRequestError(
                    '%s: "%s" must be a non-empty string for the %s strategy.'
                    % (label, key, strategy),
                )
            return value

        if strategy == "anchor":
            op.start_anchor = _req_str("start_anchor")
            op.end_anchor = _req_str("end_anchor")
            for key in ("occurrence_start", "occurrence_end"):
                if key in item and item[key] is not None:
                    value = item[key]
                    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                        raise InvalidRequestError(
                            '%s: "%s" must be a positive integer (1-based).'
                            % (label, key),
                        )
                    setattr(op, key, value)
            resolution = str(item.get("resolution", "strict")).strip().lower()
            if resolution not in ("strict", "pair"):
                raise InvalidRequestError(
                    '%s: "resolution" must be "strict" or "pair".' % label,
                )
            op.resolution = resolution
        elif strategy == "tags":
            for key in ("name", "start_tag", "end_tag"):
                value = item.get(key)
                if value is not None:
                    if not isinstance(value, str) or not value.strip():
                        raise InvalidRequestError(
                            '%s: "%s" must be a non-empty string when given.'
                            % (label, key),
                        )
                    setattr(op, key, value.strip())
            mode = str(item.get("mode", "inner")).strip().lower()
            if mode not in ("inner", "block"):
                raise InvalidRequestError(
                    '%s: "mode" must be "inner" (keep markers) or "block" '
                    "(replace markers too)." % label,
                )
            op.mode = mode
        elif strategy == "context":
            for key in ("before", "after"):
                value = item.get(key, [])
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, list) or not all(
                    isinstance(x, str) for x in value
                ):
                    raise InvalidRequestError(
                        '%s: "%s" must be a list of strings (context lines).'
                        % (label, key),
                    )
                if len(value) > MAX_CONTEXT_LINES:
                    raise InvalidRequestError(
                        '%s: "%s" may hold at most %d lines.'
                        % (label, key, MAX_CONTEXT_LINES),
                    )
                setattr(op, key, tuple(value))
            if not op.before and not op.after:
                raise InvalidRequestError(
                    '%s: the context strategy needs "before" and/or "after" lines.'
                    % label,
                )
            hint_value = item.get("target_hint")
            if hint_value is not None:
                if not isinstance(hint_value, str) or not hint_value.strip():
                    raise InvalidRequestError(
                        '%s: "target_hint" must be a non-empty string when given.'
                        % label,
                    )
                op.target_hint = hint_value
            for key, caster in (("min_score", float), ("margin", float),
                                ("max_block_lines", int)):
                if key in item and item[key] is not None:
                    try:
                        setattr(op, key, caster(item[key]))
                    except (TypeError, ValueError):
                        raise InvalidRequestError(
                            '%s: "%s" must be a number.' % (label, key),
                        )
        else:  # verbatim
            search = item.get("search")
            if not isinstance(search, str) or search == "":
                raise InvalidRequestError(
                    '%s: "search" must be a non-empty string.' % label,
                    hint="An empty anchor would match everywhere; quote real text.",
                )
            op.search = search

        guards = item.get("guards")
        if guards is not None:
            if not isinstance(guards, dict):
                raise InvalidRequestError('%s: "guards" must be an object.' % label)
            unknown = set(guards) - set(_GUARD_KEYS)
            if unknown:
                raise InvalidRequestError(
                    '%s: unknown guard key(s): %s.'
                    % (label, ", ".join(sorted(unknown))),
                    hint="Valid guards: %s." % ", ".join(_GUARD_KEYS),
                )
            op.guards = dict(guards)
        return op


# --------------------------------------------------------------------------- #
# Selection engine
# --------------------------------------------------------------------------- #


def _line_starts(text: str) -> List[int]:
    """Returns the raw offset of the start of every line (0-based lines)."""
    starts = [0]
    find = text.find
    pos = find("\n")
    while pos != -1:
        starts.append(pos + 1)
        pos = find("\n", pos + 1)
    return starts


def detect_eol(text: str) -> str:
    """Returns the document's dominant line ending (``\\n`` or ``\\r\\n``)."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _shorten(value: str, limit: int = 70) -> str:
    """Single-line preview of a string for messages."""
    collapsed = _probe(value)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


class SelectionEngine:
    """Resolves edit operations to confirmed selections on one document.

    The engine is bound to an immutable snapshot of the document text; all
    selections in a batch are resolved against the same snapshot so their
    coordinates are mutually consistent.
    """

    def __init__(self, text: str, *, max_anchor_words: int = MAX_ANCHOR_WORDS) -> None:
        """Builds the normalised view and line index for ``text``."""
        self.text = text
        self.max_anchor_words = max_anchor_words
        self.eol = detect_eol(text)
        self._view = _NormalizedView(text)
        self._starts = _line_starts(text)

    # ------------------------------------------------------------- utilities

    def line_of(self, offset: int) -> int:
        """Returns the 1-based line number containing raw ``offset``."""
        return self.text.count("\n", 0, offset) + 1

    def _line_span(self, line_index: int) -> Tuple[int, int]:
        """Raw span of 0-based ``line_index`` (end exclusive, incl. newline)."""
        start = self._starts[line_index]
        if line_index + 1 < len(self._starts):
            return start, self._starts[line_index + 1]
        return start, len(self.text)

    def _context_preview(self, span: Tuple[int, int], pad: int = 35) -> str:
        """Collapsed preview of a span with a little surrounding context."""
        start = max(0, span[0] - pad)
        end = min(len(self.text), span[1] + pad)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(self.text) else ""
        return prefix + _probe(self.text[start:end]) + suffix

    # ------------------------------------------------------------ public API

    def select(self, op: EditOp) -> Selection:
        """Resolves ``op`` to a confirmed selection.

        Args:
            op: A validated edit operation.

        Returns:
            The confirmed ``Selection``.

        Raises:
            SelectionError: Whenever the target cannot be resolved with
                certainty. The document is never modified by this method.
        """
        if op.strategy == "anchor":
            selection = self._select_anchor(op)
        elif op.strategy == "tags":
            selection = self._select_tags(op)
        elif op.strategy == "context":
            selection = self._select_context(op)
        elif op.strategy == "verbatim":
            selection = self._select_verbatim(op)
        else:  # pragma: no cover - EditOp validation prevents this
            raise InvalidRequestError("Unknown strategy %r." % op.strategy)
        self._check_guards(op, selection)
        return selection

    # ------------------------------------------------- strategy 1: anchors

    def _validate_anchor(self, anchor: str, role: str) -> List[str]:
        """Enforces the anchor word-count contract; returns the word list."""
        words = anchor.split()
        if not words:
            raise InvalidRequestError("The %s is empty." % role)
        if len(words) > self.max_anchor_words:
            raise AnchorTooLongError(
                "The %s has %d words; the protocol caps markers at %d words."
                % (role, len(words), self.max_anchor_words),
                hint=(
                    "Shorten the marker. Uniqueness comes from choosing rare "
                    "wording, not from long quotes."
                ),
                details={"role": role, "words": len(words),
                         "max_words": self.max_anchor_words},
            )
        return words

    def _occurrence_details(self, spans: Sequence[Tuple[int, int]]) -> List[Dict[str, Any]]:
        """Structured occurrence list (line + preview) for error payloads."""
        return [
            {
                "occurrence": i + 1,
                "line": self.line_of(span[0]),
                "preview": self._context_preview(span),
            }
            for i, span in enumerate(spans)
        ]

    def _extension_suggestions(
        self, spans: Sequence[Tuple[int, int]], base_words: int, direction: str
    ) -> List[Dict[str, Any]]:
        """Auto-extends an ambiguous anchor at each occurrence until unique.

        Args:
            spans: The raw spans where the ambiguous anchor matched.
            base_words: Word count of the anchor as submitted.
            direction: ``"forward"`` (start anchors grow with the words that
                follow) or ``"backward"`` (end anchors grow with the words
                that precede).

        Returns:
            One entry per occurrence: ``{"line", "anchor"}`` where ``anchor``
            is a unique extended candidate or ``None`` when no unique
            extension of <= max words exists at that occurrence.
        """
        suggestions: List[Dict[str, Any]] = []
        seen = set()
        for span in spans:
            candidate: Optional[str] = None
            if direction == "forward":
                tail = self.text[span[0]: span[0] + EXTEND_SCAN_CHARS]
                words = tail.split()
                for k in range(base_words + 1, self.max_anchor_words + 1):
                    if k > len(words):
                        break
                    probe = " ".join(words[:k])
                    if self._view.count(probe) == 1:
                        candidate = probe
                        break
            else:
                head = self.text[max(0, span[1] - EXTEND_SCAN_CHARS): span[1]]
                words = head.split()
                for k in range(base_words + 1, self.max_anchor_words + 1):
                    if k > len(words):
                        break
                    probe = " ".join(words[-k:])
                    if self._view.count(probe) == 1:
                        candidate = probe
                        break
            if candidate in seen:
                candidate = None
            if candidate:
                seen.add(candidate)
            suggestions.append(
                {"line": self.line_of(span[0]), "anchor": candidate}
            )
        return suggestions

    def _closest_window(self, probe: str) -> Optional[Dict[str, Any]]:
        """Finds the document word-window most similar to a missing anchor."""
        probe_words = probe.split()
        if not probe_words:
            return None
        tokens = [
            (m.group(), m.start(), m.end())
            for m in re.finditer(r"\S+", self.text)
        ]
        if not tokens or len(tokens) > _FUZZY_DIAGNOSIS_WORD_CAP:
            return None
        k = min(len(probe_words), len(tokens))
        matcher = difflib.SequenceMatcher(autojunk=False)
        matcher.set_seq2(probe)
        best_ratio, best_index = 0.0, -1
        for i in range(len(tokens) - k + 1):
            candidate = " ".join(t[0] for t in tokens[i: i + k])
            matcher.set_seq1(candidate)
            if matcher.real_quick_ratio() <= best_ratio:
                continue
            if matcher.quick_ratio() <= best_ratio:
                continue
            ratio = matcher.ratio()
            if ratio > best_ratio:
                best_ratio, best_index = ratio, i
        if best_index < 0 or best_ratio < 0.55:
            return None
        span = (tokens[best_index][1], tokens[best_index + k - 1][2])
        return {
            "similarity": round(best_ratio, 3),
            "line": self.line_of(span[0]),
            "text": self.text[span[0]: span[1]],
        }

    def _not_found(self, anchor: str, role: str) -> AnchorNotFoundError:
        """Builds a rich not-found error with the closest real passage."""
        closest = self._closest_window(_probe(anchor))
        details: Dict[str, Any] = {"role": role, "anchor": anchor}
        hint = (
            "The marker must quote the document verbatim (whole words, "
            "including punctuation). Whitespace and line wrapping are "
            "forgiven automatically — wording is not."
        )
        if closest:
            details["closest_match"] = closest
            hint = (
                'Closest real passage (%.0f%% similar, line %d): "%s". '
                "Quote the document exactly, including punctuation."
                % (
                    closest["similarity"] * 100,
                    closest["line"],
                    _shorten(closest["text"], 90),
                )
            )
        return AnchorNotFoundError(
            'The %s "%s" was not found in the document (0 occurrences).'
            % (role, _shorten(anchor)),
            hint=hint,
            details=details,
        )

    @staticmethod
    def _tight_pairs(
        starts: Sequence[Tuple[int, int]], ends: Sequence[Tuple[int, int]]
    ) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
        """Returns the both-ways-tight (innermost) start/end pairings.

        A pair (s, e) is tight when e is the first order-valid end at or
        after s, and s is the last order-valid start at or before e.
        """

        def first_end(s: Tuple[int, int]) -> Optional[Tuple[int, int]]:
            for e in ends:
                if e[0] >= s[0] and e[1] >= s[1]:
                    return e
            return None

        def last_start(e: Tuple[int, int]) -> Optional[Tuple[int, int]]:
            candidate = None
            for s in starts:
                if s[0] <= e[0] and s[1] <= e[1]:
                    candidate = s
            return candidate

        pairs = []
        for s in starts:
            e = first_end(s)
            if e is not None and last_start(e) == s:
                pairs.append((s, e))
        return pairs

    def _select_anchor(self, op: EditOp) -> Selection:
        """Implements Statistical Anchor Marking (Method 1)."""
        assert op.start_anchor is not None and op.end_anchor is not None
        start_words = self._validate_anchor(op.start_anchor, "start anchor")
        end_words = self._validate_anchor(op.end_anchor, "end anchor")

        notes: List[str] = []
        for role, words in (("start", start_words), ("end", end_words)):
            if len(words) < SUGGESTED_MIN_ANCHOR_WORDS:
                notes.append(
                    "%s anchor is only %d word(s); %d-%d words is safer"
                    % (role, len(words), SUGGESTED_MIN_ANCHOR_WORDS,
                       self.max_anchor_words - 2)
                )

        start_probe = _probe(op.start_anchor)
        end_probe = _probe(op.end_anchor)
        starts = self._view.find_spans(start_probe)
        ends = self._view.find_spans(end_probe)
        if not starts:
            raise self._not_found(op.start_anchor, "start anchor")
        if not ends:
            raise self._not_found(op.end_anchor, "end anchor")

        resolution = "unique_anchors"
        if op.occurrence_start is not None or op.occurrence_end is not None:
            s_index = (op.occurrence_start or 1) - 1
            e_index = (op.occurrence_end or 1) - 1
            if s_index >= len(starts):
                raise InvalidRequestError(
                    "occurrence_start=%d, but the start anchor matches only "
                    "%d time(s)." % (s_index + 1, len(starts)),
                    details={"occurrences": self._occurrence_details(starts)},
                )
            if e_index >= len(ends):
                raise InvalidRequestError(
                    "occurrence_end=%d, but the end anchor matches only "
                    "%d time(s)." % (e_index + 1, len(ends)),
                    details={"occurrences": self._occurrence_details(ends)},
                )
            start_span, end_span = starts[s_index], ends[e_index]
            resolution = "indexed"
            notes.append(
                "resolved by explicit occurrence index (start #%d, end #%d)"
                % (s_index + 1, e_index + 1)
            )
        elif len(starts) == 1 and len(ends) == 1:
            start_span, end_span = starts[0], ends[0]
        elif op.resolution == "pair":
            pairs = self._tight_pairs(starts, ends)
            if len(pairs) == 1:
                start_span, end_span = pairs[0]
                resolution = "pair"
                notes.append(
                    "anchors were not individually unique; resolved as the "
                    "single innermost start→end pairing"
                )
            else:
                raise AnchorNotUniqueError(
                    "Pair resolution failed: the anchors form %d possible "
                    "start→end pairings (start anchor ×%d, end anchor ×%d)."
                    % (len(pairs), len(starts), len(ends)),
                    hint=(
                        "Extend one of the anchors until it is unique, or add "
                        '"occurrence_start"/"occurrence_end" indices.'
                    ),
                    details={
                        "start_occurrences": self._occurrence_details(starts),
                        "end_occurrences": self._occurrence_details(ends),
                        "pairs": [
                            {
                                "lines": [self.line_of(s[0]), self.line_of(e[0])],
                            }
                            for s, e in pairs
                        ],
                    },
                )
        else:
            raise self._ambiguity_error(op, start_words, end_words, starts, ends)

        if end_span[1] < start_span[1] or end_span[0] < start_span[0]:
            raise AnchorOrderError(
                "The end anchor (line %d) resolves before the start anchor "
                "(line %d)." % (self.line_of(end_span[0]),
                                self.line_of(start_span[0])),
                hint=(
                    "The start anchor must be the first words of the block "
                    "and the end anchor its last words, in document order."
                ),
                details={
                    "start_line": self.line_of(start_span[0]),
                    "end_line": self.line_of(end_span[0]),
                },
            )
        return self._confirm(
            "anchor", resolution, start_span[0], end_span[1],
            confidence=1.0, notes=notes,
        )

    def _ambiguity_error(
        self,
        op: EditOp,
        start_words: List[str],
        end_words: List[str],
        starts: Sequence[Tuple[int, int]],
        ends: Sequence[Tuple[int, int]],
    ) -> AnchorNotUniqueError:
        """Builds the strict-mode ambiguity error with unique suggestions."""
        problems: List[str] = []
        details: Dict[str, Any] = {}
        hints: List[str] = []
        if len(starts) > 1:
            suggestions = self._extension_suggestions(
                starts, len(start_words), "forward"
            )
            details["start"] = {
                "anchor": op.start_anchor,
                "occurrences": self._occurrence_details(starts),
                "suggestions": suggestions,
            }
            problems.append(
                'start anchor "%s" matches %d locations (lines %s)'
                % (
                    _shorten(op.start_anchor or ""),
                    len(starts),
                    ", ".join(str(self.line_of(s[0])) for s in starts[:8]),
                )
            )
            usable = [s for s in suggestions if s["anchor"]]
            if usable:
                hints.append(
                    "Unique start-anchor extensions: "
                    + "; ".join(
                        '"%s" (line %d)' % (s["anchor"], s["line"]) for s in usable[:4]
                    )
                )
        if len(ends) > 1:
            suggestions = self._extension_suggestions(ends, len(end_words), "backward")
            details["end"] = {
                "anchor": op.end_anchor,
                "occurrences": self._occurrence_details(ends),
                "suggestions": suggestions,
            }
            problems.append(
                'end anchor "%s" matches %d locations (lines %s)'
                % (
                    _shorten(op.end_anchor or ""),
                    len(ends),
                    ", ".join(str(self.line_of(e[0])) for e in ends[:8]),
                )
            )
            usable = [s for s in suggestions if s["anchor"]]
            if usable:
                hints.append(
                    "Unique end-anchor extensions: "
                    + "; ".join(
                        '"%s" (line %d)' % (s["anchor"], s["line"]) for s in usable[:4]
                    )
                )
        if not hints:
            hints.append(
                "No unique extension of <= %d words exists — switch to the "
                '"context" strategy (quote the lines around the block) or '
                'add "occurrence_start"/"occurrence_end" indices.'
                % self.max_anchor_words
            )
        else:
            hints.append(
                'Resend the edit with an extended anchor (max %d words). '
                'Alternatives: "occurrence_start"/"occurrence_end" indices, '
                'or "resolution": "pair" when exactly one pairing is valid.'
                % self.max_anchor_words
            )
        return AnchorNotUniqueError(
            "Selection refused — " + " and ".join(problems)
            + ". Splicing would risk replacing the wrong text.",
            hint=" ".join(hints),
            details=details,
        )

    # --------------------------------------------------- strategy 2: tags

    def _select_tags(self, op: EditOp) -> Selection:
        """Implements Comment-Tag surgical selection (Method 2)."""
        start_token = op.start_tag or (
            "[START_EDIT:%s]" % op.name if op.name else DEFAULT_START_TAG
        )
        end_token = op.end_tag or (
            "[END_EDIT:%s]" % op.name if op.name else DEFAULT_END_TAG
        )
        lines = self.text.split("\n")
        start_hits = [i for i, line in enumerate(lines) if start_token in line]
        end_hits = [i for i, line in enumerate(lines) if end_token in line]

        def _tag_problem(token: str, hits: List[int], role: str) -> TagSelectionError:
            if not hits:
                return TagSelectionError(
                    "The %s marker %r was not found in the document."
                    % (role, token),
                    hint=(
                        "Insert the marker on its own line (inside a comment "
                        "for code files), or pass start_tag/end_tag/name to "
                        "match your markers."
                    ),
                    details={"token": token, "role": role, "occurrences": []},
                )
            return TagSelectionError(
                "The %s marker %r appears %d times (lines %s); it must appear "
                "exactly once."
                % (role, token, len(hits), ", ".join(str(i + 1) for i in hits[:8])),
                hint=(
                    "Use named markers — e.g. [START_EDIT:intro] … "
                    "[END_EDIT:intro] — so each region is unambiguous."
                ),
                details={
                    "token": token,
                    "role": role,
                    "occurrences": [{"line": i + 1} for i in hits],
                },
            )

        if len(start_hits) != 1:
            raise _tag_problem(start_token, start_hits, "start")
        if len(end_hits) != 1:
            raise _tag_problem(end_token, end_hits, "end")
        si, ei = start_hits[0], end_hits[0]
        if ei <= si:
            raise TagSelectionError(
                "The end marker %r (line %d) does not come after the start "
                "marker %r (line %d)." % (end_token, ei + 1, start_token, si + 1),
                details={"start_line": si + 1, "end_line": ei + 1},
            )
        if op.mode == "inner":
            sel_start = self._line_span(si)[1]  # first char after marker line
            sel_end = self._line_span(ei)[0]    # start of end-marker line
            note = "markers kept; inner region replaced"
        else:
            sel_start = self._line_span(si)[0]
            sel_end = self._line_span(ei)[1]
            note = "markers removed together with the block"
        return self._confirm(
            "tags", "tags", sel_start, sel_end, confidence=1.0, notes=[note],
        )

    # ------------------------------------------------ strategy 3: context

    def _select_context(self, op: EditOp) -> Selection:
        """Implements Contextual Neighborhood Matching (Method 3)."""
        min_score = op.min_score if op.min_score is not None else DEFAULT_MIN_SCORE
        margin = op.margin if op.margin is not None else DEFAULT_SCORE_MARGIN
        max_block = (
            op.max_block_lines
            if op.max_block_lines is not None
            else DEFAULT_MAX_BLOCK_LINES
        )
        if not 0.0 < min_score <= 1.0:
            raise InvalidRequestError('"min_score" must be within (0, 1].')

        lines = self.text.split("\n")
        real_n = len(lines)
        if real_n and lines[-1] == "" and self.text.endswith("\n"):
            real_n -= 1  # ignore the phantom line after a trailing newline
        if real_n == 0:
            raise ContextMatchError("The document is empty.")
        norm_lines = [_probe(line) for line in lines[:real_n]]
        before = [_probe(x) for x in op.before]
        after = [_probe(x) for x in op.after]

        matcher = difflib.SequenceMatcher(autojunk=False)

        def window_score(pos: int, ctx: List[str]) -> float:
            """Mean per-line similarity of ``ctx`` against lines[pos:...]."""
            total = 0.0
            for offset, want in enumerate(ctx):
                have = norm_lines[pos + offset]
                if want == have:
                    total += 1.0
                    continue
                matcher.set_seqs(have, want)
                total += matcher.ratio()
            return total / len(ctx)

        component_floor = max(0.35, 2 * min_score - 1.0)

        # Candidate block-start line indices (block starts at line i).
        if before:
            nb = len(before)
            start_candidates = [
                (i, score)
                for i in range(nb, real_n)
                for score in (window_score(i - nb, before),)
                if score >= component_floor
            ]
        else:
            start_candidates = [(0, 1.0)]  # pinned to beginning of file

        # Candidate block-end line indices (block ends at line j, inclusive).
        if after:
            na = len(after)
            end_candidates = [
                (j, score)
                for j in range(-1, real_n - na)
                for score in (window_score(j + 1, after),)
                if score >= component_floor
            ]
        else:
            end_candidates = [(real_n - 1, 1.0)]  # pinned to end of file

        hint_probe = _probe(op.target_hint) if op.target_hint else None
        scored: List[Tuple[float, int, int, Tuple[float, ...]]] = []
        for i, s_score in start_candidates:
            for j, e_score in end_candidates:
                if j < i - 1 or (j - i + 1) > max_block:
                    continue
                components = []
                if before:
                    components.append(s_score)
                if after:
                    components.append(e_score)
                if hint_probe is not None and j >= i:
                    matcher.set_seqs(norm_lines[i], hint_probe)
                    components.append(matcher.ratio())
                combined = sum(components) / len(components)
                if combined >= min_score:
                    scored.append((combined, i, j, tuple(components)))

        if not scored:
            raise ContextMatchError(
                "No location matched the supplied neighborhood (threshold "
                "%.2f)." % min_score,
                hint=(
                    "Quote the context lines exactly as they appear in the "
                    "document (1-%d lines above and below the block), or "
                    "lower min_score slightly."
                    % MAX_CONTEXT_LINES
                ),
                details={"min_score": min_score},
            )
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        best = scored[0]
        if len(scored) > 1 and (best[0] - scored[1][0]) < margin:
            rivals = [
                {
                    "score": round(score, 3),
                    "lines": [i + 1, j + 1 if j >= i else i],
                }
                for score, i, j, _ in scored[:5]
            ]
            raise ContextMatchError(
                "Ambiguous neighborhood: %d candidate locations score within "
                "%.2f of each other (best %.3f at lines %d-%d)."
                % (len(scored), margin, best[0], best[1] + 1, best[2] + 1),
                hint=(
                    "Add more context lines (up to %d per side) or a "
                    '"target_hint" (the first line of the block) to separate '
                    "the candidates." % MAX_CONTEXT_LINES
                ),
                details={"candidates": rivals, "margin": margin},
            )
        _, i, j, components = best
        sel_start = self._starts[i]
        if j < i:  # pure insertion point between the two contexts
            sel_end = sel_start
        else:
            sel_end = self._line_span(j)[1]
        notes = ["neighborhood matched with score %.3f" % best[0]]
        return self._confirm(
            "context", "context", sel_start, sel_end,
            confidence=best[0], notes=notes,
        )

    # ---------------------------------------------- strategy 0: verbatim

    def _select_verbatim(self, op: EditOp) -> Selection:
        """Implements Protocol-v1 exact search (kept for micro-edits)."""
        assert op.search is not None
        search = op.search
        count = self.text.count(search)
        note: Optional[str] = None
        if count == 0:
            # Chat UIs often swap LF/CRLF; try a lossless adaptation.
            if "\r\n" in self.text and "\r" not in search:
                adapted = search.replace("\n", "\r\n")
                if self.text.count(adapted) > 0:
                    search, count = adapted, self.text.count(adapted)
                    note = "search adapted LF→CRLF to match the file"
            elif "\r" in search and "\r" not in self.text:
                adapted = search.replace("\r\n", "\n").replace("\r", "\n")
                if self.text.count(adapted) > 0:
                    search, count = adapted, self.text.count(adapted)
                    note = "search adapted CRLF→LF to match the file"
        if count == 0:
            probe = _probe(search)
            if probe and probe in self._view.norm:
                raise AnchorNotFoundError(
                    "The exact search text was not found, but the same words "
                    "exist with different whitespace/line wrapping.",
                    hint=(
                        'Use the "anchor" strategy instead — its matching is '
                        "whitespace-elastic — or re-quote the text with the "
                        "original wrapping."
                    ),
                    details={"role": "search", "whitespace_only_mismatch": True},
                )
            raise self._not_found(search, "search text")
        if count > 1:
            spans = []
            start = 0
            while len(spans) < 50:
                idx = self.text.find(search, start)
                if idx < 0:
                    break
                spans.append((idx, idx + len(search)))
                start = idx + 1
            raise AnchorNotUniqueError(
                "The search text occurs %d times (lines %s); splicing would "
                "be ambiguous."
                % (count, ", ".join(str(self.line_of(s[0])) for s in spans[:8])),
                hint=(
                    'Extend the quote with surrounding words until unique, or '
                    'use the "anchor" strategy with occurrence indices.'
                ),
                details={"occurrences": self._occurrence_details(spans)},
            )
        idx = self.text.find(search)
        notes = [note] if note else []
        return self._confirm(
            "verbatim", "verbatim", idx, idx + len(search),
            confidence=1.0, notes=notes,
        )

    # ----------------------------------------------------------- plumbing

    def _confirm(
        self,
        strategy: str,
        resolution: str,
        start: int,
        end: int,
        *,
        confidence: float,
        notes: Iterable[str],
    ) -> Selection:
        """Builds the confirmed ``Selection`` for a resolved raw span."""
        text = self.text[start:end]
        start_line = self.line_of(start)
        end_line = self.line_of(max(start, end - 1)) if end > start else start_line
        return Selection(
            strategy=strategy,
            resolution=resolution,
            start=start,
            end=end,
            text=text,
            start_line=start_line,
            end_line=end_line,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            confidence=confidence,
            notes=tuple(n for n in notes if n),
        )

    def _check_guards(self, op: EditOp, selection: Selection) -> None:
        """Validates caller-supplied guards against a confirmed selection."""
        guards = op.guards or {}
        expected = guards.get("expected_sha256")
        if expected:
            expected = str(expected).lower()
            if len(expected) < 8 or not selection.sha256.startswith(expected):
                raise GuardError(
                    "The selected text does not match expected_sha256 — the "
                    "document changed since the selection was confirmed.",
                    hint="Re-run selection and review the new span before applying.",
                    details={
                        "expected_sha256": expected,
                        "actual_sha256": selection.sha256,
                    },
                )
        max_lines = guards.get("max_lines")
        if max_lines is not None and selection.line_count > int(max_lines):
            raise GuardError(
                "The selection spans %d lines, above the max_lines guard (%d)."
                % (selection.line_count, int(max_lines)),
                hint="Verify the anchors — the span is larger than intended.",
                details={"lines": selection.line_count, "max_lines": int(max_lines)},
            )
        line_range = guards.get("expected_line_range")
        if line_range:
            try:
                low, high = int(line_range[0]), int(line_range[1])
            except (TypeError, ValueError, IndexError):
                raise InvalidRequestError(
                    '"expected_line_range" must be [first_line, last_line].'
                )
            tolerance = int(guards.get("tolerance", 0))
            if (
                selection.start_line < low - tolerance
                or selection.end_line > high + tolerance
            ):
                raise GuardError(
                    "The selection resolved to lines %d-%d, outside the "
                    "expected range %d-%d (±%d)."
                    % (selection.start_line, selection.end_line, low, high, tolerance),
                    details={
                        "lines": [selection.start_line, selection.end_line],
                        "expected_line_range": [low, high],
                        "tolerance": tolerance,
                    },
                )

    # ------------------------------------------------- anchor suggestion

    def suggest_anchors(
        self,
        block_start: int,
        block_end: int,
        *,
        min_words: int = SUGGESTED_MIN_ANCHOR_WORDS,
    ) -> Dict[str, Any]:
        """Computes the minimal unique anchor pair for a raw span.

        This is the generation-side of the statistical-anchor contract: give
        it the block you intend to replace and it returns the shortest start
        and end anchors (``min_words``..``max_anchor_words`` words) that are
        statistically unique in the document.

        Args:
            block_start: Raw offset of the block start (at a word boundary).
            block_end: Raw offset just past the block end.
            min_words: Starting anchor length before extension.

        Returns:
            ``{"start_anchor", "end_anchor", "start_words", "end_words",
            "lines"}``.

        Raises:
            InvalidRequestError: For an empty/invalid span.
            CannotUniquifyError: When no unique anchor of <= max words
                exists at either edge (use the context strategy instead).
        """
        if not (0 <= block_start < block_end <= len(self.text)):
            raise InvalidRequestError("Invalid block span for suggest_anchors.")
        words = self.text[block_start:block_end].split()
        if not words:
            raise InvalidRequestError("The requested block contains no words.")
        min_words = max(1, min(min_words, self.max_anchor_words))

        def _grow(pick) -> Optional[Tuple[str, int]]:
            for k in range(min(min_words, len(words)), self.max_anchor_words + 1):
                if k > len(words):
                    break
                candidate = " ".join(pick(k))
                if self._view.count(candidate) == 1:
                    return candidate, k
            return None

        start = _grow(lambda k: words[:k])
        end = _grow(lambda k: words[-k:])
        missing = [
            role for role, got in (("start", start), ("end", end)) if got is None
        ]
        if missing:
            raise CannotUniquifyError(
                "No unique %s anchor of <= %d words exists for this block."
                % (" or ".join(missing), self.max_anchor_words),
                hint=(
                    'Use the "context" strategy (neighborhood lines) or '
                    '"tags" markers for this region — its edges are too '
                    "repetitive for statistical anchors."
                ),
                details={"block_lines": [self.line_of(block_start),
                                         self.line_of(max(block_start, block_end - 1))]},
            )
        assert start and end
        return {
            "strategy": "anchor",
            "start_anchor": start[0],
            "end_anchor": end[0],
            "start_words": start[1],
            "end_words": end[1],
            "lines": [self.line_of(block_start),
                      self.line_of(max(block_start, block_end - 1))],
        }

    def suggest_anchors_for_lines(
        self, first_line: int, last_line: int, **kwargs: Any
    ) -> Dict[str, Any]:
        """``suggest_anchors`` addressed by 1-based inclusive line numbers."""
        if first_line < 1 or last_line < first_line or first_line > len(self._starts):
            raise InvalidRequestError(
                "Invalid line range %d-%d (document has %d lines)."
                % (first_line, last_line, len(self._starts))
            )
        start = self._starts[first_line - 1]
        end = (
            self._line_span(min(last_line, len(self._starts)) - 1)[1]
        )
        return self.suggest_anchors(start, end, **kwargs)


# --------------------------------------------------------------------------- #
# Replacement preparation & splicing
# --------------------------------------------------------------------------- #

_LEADING_BLANK_LINES = re.compile(r"^(?:[ \t]*\n)+")


def _to_lf(value: str) -> str:
    """Normalises all line endings to LF."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _apply_eol(value: str, eol: str) -> str:
    """Converts LF-normalised text to the document's dominant EOL."""
    return value.replace("\n", eol) if eol == "\r\n" else value


def prepare_replacement(
    replacement: str,
    selection: Selection,
    doc_text: str,
    eol: str,
) -> str:
    """Adapts replacement text so the splice cannot break file structure.

    * Line endings are converted to the document's dominant EOL.
    * ``anchor`` (inline) spans start and end on non-whitespace, so leading
      blank lines and trailing whitespace on the replacement are trimmed —
      the text slots exactly where the old block sat, without doubling
      newlines at the seams.
    * ``tags``/``context`` (line) spans cover whole lines including the
      trailing newline, so the replacement is normalised to a
      newline-terminated block (or nothing at all for a deletion).
    * ``verbatim`` replacements are used untouched (v1 behaviour), except
      for the same LF/CRLF adaptation applied to the search text.

    Args:
        replacement: The raw replacement text from the edit request.
        selection: The confirmed selection being replaced.
        doc_text: Full document text (for end-of-file decisions).
        eol: The document's dominant line ending.

    Returns:
        The prepared replacement string.
    """
    if selection.strategy == "verbatim":
        if "\n" not in replacement:
            return replacement
        return _apply_eol(_to_lf(replacement), eol)

    rep = _to_lf(replacement)
    if selection.strategy == "anchor":
        rep = _LEADING_BLANK_LINES.sub("", rep)
        rep = re.sub(r"\s+\Z", "", rep)
        return _apply_eol(rep, eol)

    # Line-based strategies (tags / context): the replacement is a block of
    # whole lines. Blank-line padding inside it is intentional and preserved;
    # the only structural repair is newline termination.
    if rep.strip() == "":
        return ""  # whitespace-only replacement = clean line deletion
    at_unterminated_eof = selection.end == len(doc_text) and not doc_text.endswith(
        "\n"
    )
    if at_unterminated_eof:
        rep = rep.rstrip("\n")
    elif not rep.endswith("\n"):
        rep += "\n"
    return _apply_eol(rep, eol)


_NEWLINES_LEFT = re.compile(r"(?:\r?\n)+\Z")
_NEWLINES_RIGHT = re.compile(r"(?:\r?\n)+")


def _splice_once(text: str, selection: Selection, replacement: str, eol: str) -> str:
    """Performs one splice, healing whitespace seams on inline deletions."""
    left = text[: selection.start]
    right = text[selection.end:]
    if replacement == "" and selection.strategy in ("anchor", "verbatim"):
        left_match = _NEWLINES_LEFT.search(left)
        right_match = _NEWLINES_RIGHT.match(right)
        left_run = left_match.group().count("\n") if left_match else 0
        right_run = right_match.group().count("\n") if right_match else 0
        if left_run and right_run:
            # Keep the larger of the two runs: deleting a paragraph keeps the
            # blank-line rhythm; deleting a line keeps single spacing.
            keep = max(left_run, right_run)
            left = left[: left_match.start()]
            right = right[right_match.end():]
            return left + eol * keep + right
        if left.endswith((" ", "\t")) and right[:1] in (" ", "\t"):
            return left + right.lstrip(" \t")  # heal doubled inline spaces
    return left + replacement + right


def apply_edit_ops(
    text: str, ops: Sequence[EditOp]
) -> Tuple[str, List[SpliceRecord], List[int]]:
    """Resolves and applies a batch of edits transactionally.

    All selections are resolved against the *original* text (so their
    coordinates are consistent), overlap-checked, and then spliced from the
    bottom of the document upwards so earlier splices never invalidate later
    offsets. Any ``SelectionError`` aborts the whole batch before a single
    character has changed.

    Args:
        text: The document text.
        ops: Validated edit operations, in the AI's submission order.

    Returns:
        ``(new_text, applied, skipped_positions)`` where ``skipped_positions``
        lists 1-based indices of no-op edits (replacement equals selection).

    Raises:
        SelectionError: If any edit cannot be resolved uniquely and safely.
    """
    engine = SelectionEngine(text)
    records: List[SpliceRecord] = []
    for position, op in enumerate(ops, start=1):
        try:
            selection = engine.select(op)
        except SelectionError as exc:
            exc.details.setdefault("edit_index", position)
            raise
        replacement = prepare_replacement(op.replace, selection, text, engine.eol)
        note = "%s/%s" % (selection.strategy, selection.resolution)
        if selection.notes:
            note += " — " + "; ".join(selection.notes)
        records.append(
            SpliceRecord(
                index=position,
                selection=selection,
                removed=selection.text,
                added=replacement,
                note=note,
            )
        )

    active = [r for r in records if r.added != r.removed]
    skipped = [r.index for r in records if r.added == r.removed]

    ordered = sorted(active, key=lambda r: (r.selection.start, r.index))
    for a, b in zip(ordered, ordered[1:]):
        if b.selection.start < a.selection.end:
            raise OverlapError(
                "Edits #%d and #%d overlap (lines %d-%d and %d-%d); their "
                "combined effect would be undefined."
                % (
                    a.index, b.index,
                    a.selection.start_line, a.selection.end_line,
                    b.selection.start_line, b.selection.end_line,
                ),
                hint="Merge them into a single edit covering the whole region.",
                details={
                    "edits": [a.index, b.index],
                    "lines": [
                        [a.selection.start_line, a.selection.end_line],
                        [b.selection.start_line, b.selection.end_line],
                    ],
                },
            )

    working = text
    for record in reversed(ordered):  # bottom-up: offsets stay valid
        working = _splice_once(working, record.selection, record.added, engine.eol)
    return working, active, skipped


def select_edit_ops(text: str, ops: Sequence[EditOp]) -> List[Selection]:
    """Phase-one only: resolves every edit and returns confirmed selections.

    Nothing is modified. Use this to show the user (or the AI) exactly what
    *would* be replaced — line ranges, sizes, SHA-256 confirmation tokens —
    before committing with ``apply_edit_ops``.
    """
    engine = SelectionEngine(text)
    selections: List[Selection] = []
    for position, op in enumerate(ops, start=1):
        try:
            selections.append(engine.select(op))
        except SelectionError as exc:
            exc.details.setdefault("edit_index", position)
            raise
    return selections


# --------------------------------------------------------------------------- #
# Markdown edit-block parsing (JSON-free authoring format)
# --------------------------------------------------------------------------- #

_HEADER_KEY_MAP = {
    "START-ANCHOR": "start_anchor",
    "END-ANCHOR": "end_anchor",
    "OCCURRENCE-START": "occurrence_start",
    "OCCURRENCE-END": "occurrence_end",
    "RESOLUTION": "resolution",
    "NAME": "name",
    "MODE": "mode",
    "START-TAG": "start_tag",
    "END-TAG": "end_tag",
    "BEFORE": "before",
    "AFTER": "after",
    "HINT": "target_hint",
    "TARGET-HINT": "target_hint",
    "MIN-SCORE": "min_score",
    "MARGIN": "margin",
    "MAX-BLOCK-LINES": "max_block_lines",
}
_INT_KEYS = {"occurrence_start", "occurrence_end", "max_block_lines"}
_FLOAT_KEYS = {"min_score", "margin"}
_LIST_KEYS = {"before", "after"}


def parse_markdown_edits(text: str) -> List[EditOp]:
    """Parses ``@@EDIT`` blocks — the escape-free authoring format.

    Long multi-line replacements inside JSON strings force the AI to escape
    every newline and quote, which is where real-world responses most often
    corrupt. The Markdown block format sidesteps escaping entirely::

        @@EDIT anchor
        START-ANCHOR: The migration process begins when
        END-ANCHOR: and completes the rollback safely.
        <<<
        ...replacement text, written naturally...
        >>>

    Header keys per strategy match the JSON schema (``BEFORE:``/``AFTER:``
    may repeat for the context strategy). The replacement body runs between
    a line containing only ``<<<`` and a line containing only ``>>>``; an
    empty body deletes the span. A body that itself needs a bare ``>>>``
    line must use the JSON format instead.

    Args:
        text: Response text (explanation already removed by the caller).

    Returns:
        Validated ``EditOp`` objects in order of appearance.

    Raises:
        InvalidRequestError: On malformed blocks (missing fences, bad keys).
    """
    lines = _to_lf(text).split("\n")
    ops: List[EditOp] = []
    index = 0
    position = 0
    fence_hint = (
        "Put <<< and >>> each ALONE on their own line, as plain ASCII, with "
        "nothing else on the line (no bidi marks). For right-to-left text the "
        "JSON edit format is the most reliable alternative."
    )

    def _is_open_fence(clean: str) -> bool:
        """``<<<`` — tolerating sloppy repetition like ``<<<<``."""
        return len(clean) >= 3 and set(clean) == {"<"}

    def _is_close_fence(clean: str) -> bool:
        """``>>>`` — tolerating sloppy repetition like ``>>>>``."""
        return len(clean) >= 3 and set(clean) == {">"}

    def _is_code_fence(clean: str) -> bool:
        """Markdown code fences models love to wrap the whole block in."""
        return clean.startswith("```") or clean.startswith("~~~")

    while index < len(lines):
        if not _structural(lines[index]).startswith("@@EDIT"):
            index += 1
            continue
        position += 1
        label = "edit #%d" % position
        strategy = _structural(lines[index])[len("@@EDIT"):].strip() or "anchor"
        item: Dict[str, Any] = {"strategy": strategy}
        index += 1
        # Header lines until the opening fence (bidi-tolerant recognition).
        while index < len(lines) and not _is_open_fence(_structural(lines[index])):
            clean = _structural(lines[index])
            if clean and not _is_code_fence(clean):
                if clean.startswith("@@EDIT"):
                    raise InvalidRequestError(
                        "%s has no '<<<' replacement fence before the next "
                        "@@EDIT block." % label,
                        hint=fence_hint,
                    )
                if _is_close_fence(clean) or ":" not in clean:
                    raise InvalidRequestError(
                        "%s: expected a 'KEY: value' header or the '<<<' fence, "
                        "but found %r. The opening fence is probably missing or "
                        "merged with text." % (label, clean[:60]),
                        hint=fence_hint,
                    )
                raw_key, _, raw_value = clean.partition(":")
                key_token = raw_key.strip().upper().replace("_", "-")
                if key_token not in _HEADER_KEY_MAP:
                    raise InvalidRequestError(
                        "%s: unknown header key %r." % (label, raw_key.strip()),
                        hint="Valid keys: %s." % ", ".join(sorted(_HEADER_KEY_MAP)),
                    )
                key = _HEADER_KEY_MAP[key_token]
                value: Any = raw_value.strip()
                if key in _INT_KEYS:
                    try:
                        value = int(value)
                    except ValueError:
                        raise InvalidRequestError(
                            "%s: %s must be an integer." % (label, raw_key.strip()),
                        )
                elif key in _FLOAT_KEYS:
                    try:
                        value = float(value)
                    except ValueError:
                        raise InvalidRequestError(
                            "%s: %s must be a number." % (label, raw_key.strip()),
                        )
                if key in _LIST_KEYS:
                    item.setdefault(key, []).append(value)
                else:
                    item[key] = value
            index += 1
        if index >= len(lines):
            raise InvalidRequestError(
                "%s has no '<<<' replacement fence." % label,
                hint=fence_hint,
            )
        index += 1  # consume '<<<'
        body: List[str] = []
        closed = False
        while index < len(lines):
            if _is_close_fence(_structural(lines[index])):
                closed = True
                index += 1
                break
            body.append(lines[index])
            index += 1
        if not closed:
            raise InvalidRequestError(
                "%s: the replacement body was never closed with '>>>'." % label,
                hint=fence_hint,
            )
        item["replace"] = "\n".join(body)
        ops.append(EditOp.from_payload_item(item, position))
    return ops
