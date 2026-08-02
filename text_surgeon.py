#!/usr/bin/env python3
"""Text Surgeon — surgical, AI-assisted edits for long documents.

Text Surgeon v2 implements the "Surgeon Protocol": an LLM proposes edits by
*pointing at* text instead of re-quoting it, so a 1,000-line block can be
replaced by referencing ~15 words. Selection is delegated to
``surgeon_engine`` and supports four strategies:

    anchor    Statistical Anchor Marking — a 5-10 word start anchor and a
              5-10 word end anchor, each verified to be statistically
              unique (exactly one word-aligned, whitespace-elastic match).
    tags      Comment-tag selection between user-placed [START_EDIT] /
              [END_EDIT] marker lines.
    context   Contextual neighborhood matching — fuzzy match of the lines
              around the block, for repetitive boilerplate.
    verbatim  Protocol-v1 exact search/replace, still ideal for micro-edits.

Workflow:
    1. ``--generate``   Build the Surgeon Protocol prompt from a file plus
                        your change request, ready to paste into any LLM.
    2.                  The LLM answers with an <EXPLANATION> block followed
                        by @@EDIT blocks (or a JSON edit array).
    3. ``--apply``      Feed the response back. Every selection must resolve
                        to EXACTLY ONE location (SELECTION CONFIRMED) before
                        any splice happens; the whole batch is transactional
                        and overlap-checked. The file is saved atomically
                        (with a .bak safety copy), the operation is logged to
                        ``.surgeon_memory.json``, and a post-operative
                        verification prompt is emitted for AI review.

Usage:
    python3 text_surgeon.py notes.md --generate "Make the introduction longer"
    python3 text_surgeon.py notes.md --generate "Fix typos" --out prompt.txt
    python3 text_surgeon.py notes.md --apply response.txt
    python3 text_surgeon.py notes.md --apply              # paste, end with Ctrl-D
    python3 text_surgeon.py notes.md --apply response.txt --dry-run
    python3 text_surgeon.py notes.md --generate "Tighten it" --followup
    python3 text_surgeon.py notes.md --suggest-anchors 120:180
    python3 text_surgeon.py notes.md --history

Design notes:
    * Standard library only. Styled output uses raw ANSI codes with graceful
      degradation (honours NO_COLOR, non-TTY pipes, and Windows consoles),
      so there is no dependency on ``rich``.
    * Prompts are written to STDOUT; all status/UI chrome goes to STDERR, so
      shell redirection stays clean:
      ``python3 text_surgeon.py notes.md --generate "Fix typos" > prompt.txt``
    * Exit codes: 0 = success, 1 = usage/environment error, 2 = surgical or
      payload abort (the document is never modified on an abort).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Final, Iterator, List, Optional, Sequence, Tuple

import surgeon_engine as engine
from surgeon_engine import EditOp

__version__: Final[str] = "2.2.0"

PROTOCOL_VERSION: Final[str] = "2.0"
MEMORY_FILENAME: Final[str] = ".surgeon_memory.json"
SESSION_FILENAME: Final[str] = ".surgeon_session.json"
BACKUP_SUFFIX: Final[str] = ".bak"

EXIT_OK: Final[int] = 0
EXIT_USAGE: Final[int] = 1
EXIT_SURGICAL: Final[int] = 2

_MARKDOWN_EXTENSIONS: Final[Tuple[str, ...]] = (".md", ".markdown", ".mdown", ".mkd")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class TextSurgeonError(Exception):
    """Base class for all Text Surgeon failures.

    Attributes:
        hint: Optional human-oriented suggestion for resolving the failure.
    """

    def __init__(self, message: str, hint: Optional[str] = None) -> None:
        """Initialises the error.

        Args:
            message: Human-readable description of what went wrong.
            hint: Optional remediation advice shown beneath the error.
        """
        super().__init__(message)
        self.hint: Optional[str] = hint


class SurgicalError(TextSurgeonError):
    """Raised when a splice cannot be performed safely.

    Covers the two protocol violations: a hallucinated anchor (0 occurrences
    of the "search" text) and an ambiguous anchor (more than 1 occurrence).
    The document on disk is never modified when this is raised.
    """


class PayloadError(TextSurgeonError):
    """Raised when the AI response cannot be parsed into valid edit objects."""


# --------------------------------------------------------------------------- #
# Console (zero-dependency styled output on STDERR)
# --------------------------------------------------------------------------- #


class Console:
    """Minimal ANSI console for styled status output on STDERR.

    Colour is enabled only for interactive terminals, honours the ``NO_COLOR``
    convention, and degrades to plain text everywhere else. Copy/paste
    payloads (prompts) are printed to STDOUT elsewhere; everything here
    targets STDERR so shell redirection of prompts stays clean.
    """

    _CODES: Final[Dict[str, str]] = {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "red": "\033[31m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "blue": "\033[34m",
        "magenta": "\033[35m",
        "cyan": "\033[36m",
    }

    def __init__(self) -> None:
        """Detects terminal capabilities once at start-up."""
        self.enabled: bool = self._supports_color()

    @staticmethod
    def _supports_color() -> bool:
        """Returns True when ANSI styling should be emitted."""
        if os.environ.get("NO_COLOR") is not None:
            return False
        if not sys.stderr.isatty():
            return False
        if os.name == "nt":
            return Console._enable_windows_vt()
        return True

    @staticmethod
    def _enable_windows_vt() -> bool:
        """Enables virtual-terminal processing on Windows consoles.

        Returns:
            True when ANSI sequences are safe to use.
        """
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-12)  # STD_ERROR_HANDLE
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
        except Exception:  # pragma: no cover - defensive, Windows-only path
            return False

    @staticmethod
    def width() -> int:
        """Returns the usable output width, clamped to a readable range."""
        columns = shutil.get_terminal_size(fallback=(88, 24)).columns
        return max(48, min(columns, 100))

    def _paint(self, text: str, *styles: str) -> str:
        """Wraps text in ANSI codes when styling is enabled."""
        if not self.enabled or not styles:
            return text
        prefix = "".join(self._CODES[style] for style in styles)
        return f"{prefix}{text}{self._CODES['reset']}"

    def _write(self, line: str) -> None:
        """Writes one line to STDERR with a safe non-UTF-8 fallback."""
        try:
            print(line, file=sys.stderr, flush=True)
        except UnicodeEncodeError:  # pragma: no cover - exotic consoles
            encoding = sys.stderr.encoding or "ascii"
            print(
                line.encode(encoding, "replace").decode(encoding),
                file=sys.stderr,
                flush=True,
            )

    def banner(self) -> None:
        """Prints the one-line start-up banner."""
        self._write(
            self._paint(
                f"✂ Text Surgeon v{__version__} — anchor-based select & "
                "replace (Surgeon Protocol v2)",
                "dim",
            )
        )

    def rule(self, title: str = "", style: str = "magenta") -> None:
        """Draws a horizontal rule, optionally titled."""
        width = self.width()
        if title:
            lead = f"── {title} "
            line = lead + "─" * max(0, width - len(lead))
        else:
            line = "─" * width
        self._write(self._paint(line, style, "bold"))

    def panel(self, title: str, body: str) -> None:
        """Renders a wrapped text panel (e.g. the AI's explanation)."""
        width = self.width()
        inner = width - 4
        title = title[: max(1, inner - 2)]
        lines: List[str] = []
        for paragraph in body.splitlines() or [""]:
            if not paragraph.strip():
                lines.append("")
                continue
            lines.extend(textwrap.wrap(paragraph, inner) or [""])
        self._write(
            self._paint(
                f"╭─ {title} " + "─" * max(0, width - len(title) - 5) + "╮",
                "cyan",
            )
        )
        for line in lines:
            self._write(
                self._paint("│ ", "cyan")
                + line.ljust(inner)
                + self._paint(" │", "cyan")
            )
        self._write(self._paint("╰" + "─" * (width - 2) + "╯", "cyan"))

    def raw(self, text: str, *styles: str) -> None:
        """Writes a pre-formatted line with optional styling."""
        self._write(self._paint(text, *styles))

    def info(self, message: str) -> None:
        """Prints a neutral informational line."""
        self._write(f"{self._paint('•', 'cyan')} {message}")

    def success(self, message: str) -> None:
        """Prints a success line."""
        self._write(self._paint(f"✔ {message}", "green"))

    def warn(self, message: str) -> None:
        """Prints a non-fatal warning line."""
        self._write(self._paint(f"▲ {message}", "yellow"))

    def error(self, message: str) -> None:
        """Prints an error line."""
        self._write(self._paint(f"✖ {message}", "red", "bold"))

    def hint(self, message: str) -> None:
        """Prints an indented remediation hint."""
        self._write(self._paint(f"  ↳ {message}", "yellow"))

    def detail(self, message: str) -> None:
        """Prints dim, indented secondary information."""
        self._write(self._paint(f"  {message}", "dim"))


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    """Returns the current local time as ISO-8601 with UTC offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _preview(value: str, max_chars: int = 120) -> str:
    """Collapses a string to a short single-line preview.

    Args:
        value: The string to preview.
        max_chars: Maximum length of the returned preview.

    Returns:
        A whitespace-collapsed, possibly ellipsised preview.
    """
    collapsed = " ".join(value.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 1] + "…"


def _atomic_write_json(path: str, payload: Any) -> None:
    """Atomically serialises ``payload`` as pretty-printed UTF-8 JSON.

    Writes to a temporary file in the same directory, fsyncs, then swaps it
    into place with ``os.replace`` so the target can never be half-written.

    Args:
        path: Destination file path.
        payload: JSON-serialisable object.

    Raises:
        TextSurgeonError: If the file cannot be written.
    """
    directory = os.path.dirname(path) or "."
    tmp_path: Optional[str] = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".surgeon.", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError as exc:
        raise TextSurgeonError(f"Could not write {path}: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Edit model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Edit:
    """The removed/added text pair of one executed splice.

    In Protocol v2 the AI no longer supplies the full removed text — the
    engine resolves it. ``search`` therefore holds the *actual* text that was
    removed (full undo information for the memory log) and ``replace`` the
    text that now stands in its place.
    """

    search: str
    replace: str


@dataclass(frozen=True)
class AppliedEdit:
    """Record of a single executed splice.

    Attributes:
        edit: Removed/added text exactly as applied.
        line: 1-based first line of the replaced span (original document).
        note: Annotation, e.g. ``anchor/unique_anchors``.
        end_line: 1-based last line of the replaced span.
        strategy: Selection strategy that resolved the span.
        sha256: SHA-256 of the removed text (selection confirmation token).
        confidence: 1.0 for exact strategies, match score for ``context``.
    """

    edit: Edit
    line: int
    note: Optional[str] = None
    end_line: Optional[int] = None
    strategy: Optional[str] = None
    sha256: Optional[str] = None
    confidence: float = 1.0


# --------------------------------------------------------------------------- #
# Document
# --------------------------------------------------------------------------- #


@dataclass
class Document:
    """An in-memory Markdown document bound to a file on disk.

    Attributes:
        path: Absolute path of the underlying file.
        text: Full decoded content; original line endings are preserved.
        had_bom: Whether the on-disk file began with a UTF-8 BOM.
    """

    path: str
    text: str
    had_bom: bool = False

    @classmethod
    def load(cls, raw_path: str) -> "Document":
        """Loads a document from disk with byte fidelity.

        Args:
            raw_path: User-supplied path to the file.

        Returns:
            A loaded ``Document``.

        Raises:
            TextSurgeonError: If the file is missing, unreadable, a
                directory, or not valid UTF-8.
        """
        path = os.path.abspath(raw_path)
        if not os.path.exists(path):
            raise TextSurgeonError(
                f"File not found: {path}",
                hint="Check the path — Text Surgeon never creates the target file.",
            )
        if os.path.isdir(path):
            raise TextSurgeonError(f"Path is a directory, not a file: {path}")
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except PermissionError as exc:
            raise TextSurgeonError(
                f"Permission denied reading {path}.",
                hint="Check the file's read permissions.",
            ) from exc
        except OSError as exc:
            raise TextSurgeonError(f"Could not read {path}: {exc}") from exc
        had_bom = raw.startswith(b"\xef\xbb\xbf")
        if had_bom:
            raw = raw[3:]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TextSurgeonError(
                f"{os.path.basename(path)} is not valid UTF-8 "
                f"(bad byte at offset {exc.start}).",
                hint="Text Surgeon edits UTF-8 Markdown only; convert the file first.",
            ) from exc
        return cls(path=path, text=text, had_bom=had_bom)

    @property
    def name(self) -> str:
        """Returns the file name without its directory."""
        return os.path.basename(self.path)

    @property
    def directory(self) -> str:
        """Returns the directory containing the file."""
        return os.path.dirname(self.path)

    def sha256(self) -> str:
        """Returns the SHA-256 hex digest of the current text."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def splice(
        self, edits: Sequence[EditOp]
    ) -> Tuple[str, List[AppliedEdit], List[int]]:
        """Applies a batch of edit operations transactionally.

        Every selection is resolved by ``surgeon_engine`` against the
        *original* text and must be provably unique (SELECTION CONFIRMED);
        the batch is overlap-checked and spliced bottom-up. Nothing is
        written to disk here; on any violation the exception propagates
        before the caller ever saves.

        Args:
            edits: Validated edit operations, in submission order.

        Returns:
            A tuple ``(new_text, applied, skipped_positions)`` where
            ``skipped_positions`` lists 1-based indices of no-op edits
            (replacement identical to the selected text).

        Raises:
            SurgicalError: If any selection is missing, ambiguous,
                overlapping, or guard-blocked. The message and hint carry
                the engine's machine-actionable advice (occurrence lines,
                auto-extended unique anchor suggestions).
        """
        try:
            new_text, records, skipped = engine.apply_edit_ops(self.text, edits)
        except engine.SelectionError as exc:
            index = exc.details.get("edit_index")
            prefix = f"Edit #{index}: " if index else ""
            raise SurgicalError(
                f"{prefix}{exc} [{exc.code}]", hint=exc.hint
            ) from exc
        applied = [
            AppliedEdit(
                edit=Edit(search=record.removed, replace=record.added),
                line=record.selection.start_line,
                note=record.note,
                end_line=record.selection.end_line,
                strategy=record.selection.strategy,
                sha256=record.selection.sha256,
                confidence=record.selection.confidence,
            )
            for record in records
        ]
        return new_text, applied, skipped

    def save(self, new_text: str, *, backup: bool = True) -> Optional[str]:
        """Atomically persists new content to disk.

        The content is written to a temporary file in the same directory and
        swapped into place with ``os.replace``, so the document can never be
        left half-written. Byte fidelity is preserved (UTF-8 BOM and original
        line endings are kept as-is).

        Args:
            new_text: The complete replacement content.
            backup: When True, the current file is first copied to
                ``<name>.bak``.

        Returns:
            The backup file path, or ``None`` when backup is disabled.

        Raises:
            TextSurgeonError: On permission or other I/O failures (the
                original file is left untouched).
        """
        data = new_text.encode("utf-8")
        if self.had_bom:
            data = b"\xef\xbb\xbf" + data
        backup_path: Optional[str] = None
        tmp_path: Optional[str] = None
        try:
            if backup:
                backup_path = self.path + BACKUP_SUFFIX
                shutil.copy2(self.path, backup_path)
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{self.name}.", suffix=".tmp", dir=self.directory
            )
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
            tmp_path = None
        except PermissionError as exc:
            raise TextSurgeonError(
                f"Permission denied while saving {self.path}.",
                hint="Check write permissions on the file and its directory.",
            ) from exc
        except OSError as exc:
            raise TextSurgeonError(f"Failed to save {self.path}: {exc}") from exc
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        self.text = new_text
        return backup_path


# --------------------------------------------------------------------------- #
# Memory engine & session store
# --------------------------------------------------------------------------- #


class MemoryEngine:
    """Append-only operation log stored beside the target document.

    Each successful splice appends one entry to ``.surgeon_memory.json``::

        {
          "timestamp": "ISO-8601",
          "intent": "User's original request",
          "file": "notes.md",
          "diff": [{"removed": "...", "added": "..."}]
        }

    The ``file`` key extends the base format so several documents in one
    directory can share a single log. A corrupt log is quarantined (renamed)
    rather than silently overwritten.
    """

    def __init__(self, directory: str, console: Optional[Console] = None) -> None:
        """Initialises the engine.

        Args:
            directory: Directory containing the target document.
            console: Optional console for non-fatal warnings.
        """
        self.path: str = os.path.join(directory, MEMORY_FILENAME)
        self._console = console

    def _warn(self, message: str) -> None:
        """Emits a non-fatal warning when a console is attached."""
        if self._console is not None:
            self._console.warn(message)

    def entries(self) -> List[Dict[str, Any]]:
        """Returns all recorded operations (empty when the log is absent).

        A structurally invalid log is quarantined to
        ``.surgeon_memory.json.corrupt-<stamp>`` and an empty log is returned.
        """
        if not os.path.exists(self.path):
            return []
        data: Any = None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, list):
            return [entry for entry in data if isinstance(entry, dict)]
        quarantine = f"{self.path}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
        try:
            os.replace(self.path, quarantine)
            self._warn(
                f"{MEMORY_FILENAME} was unreadable — quarantined as "
                f"{os.path.basename(quarantine)}; starting a fresh log."
            )
        except OSError:
            self._warn(
                f"{MEMORY_FILENAME} is unreadable and could not be "
                "quarantined; starting a fresh log."
            )
        return []

    def record(
        self, *, intent: str, target: str, applied: Sequence[AppliedEdit]
    ) -> Dict[str, Any]:
        """Appends one operation entry to the log.

        Args:
            intent: The user's original change request.
            target: File name of the edited document.
            applied: The splices that were executed, in order.

        Returns:
            The entry that was written.

        Raises:
            TextSurgeonError: If the log cannot be written.
        """
        entry: Dict[str, Any] = {
            "timestamp": _now_iso(),
            "intent": intent,
            "file": target,
            "diff": [
                {"removed": item.edit.search, "added": item.edit.replace}
                for item in applied
            ],
        }
        log = self.entries()
        log.append(entry)
        _atomic_write_json(self.path, log)
        return entry


class SessionStore:
    """Persists the pending intent between ``--generate`` and ``--apply``.

    Stored in ``.surgeon_session.json`` beside the document and keyed by file
    name, so ``--apply`` can log the original intent in the memory engine and
    detect when the document changed after the prompt was generated.
    """

    def __init__(self, directory: str) -> None:
        """Initialises the store.

        Args:
            directory: Directory containing the target document.
        """
        self.path: str = os.path.join(directory, SESSION_FILENAME)

    def _load(self) -> Dict[str, Any]:
        """Returns the raw session mapping, tolerating a missing/corrupt file."""
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def put(self, name: str, *, intent: str, digest: str) -> None:
        """Records a pending operation for a document.

        Args:
            name: Document file name.
            intent: The user's change request.
            digest: SHA-256 of the document at prompt-generation time.

        Raises:
            TextSurgeonError: If the session file cannot be written.
        """
        data = self._load()
        data[name] = {
            "intent": intent,
            "sha256": digest,
            "created": _now_iso(),
            "protocol": PROTOCOL_VERSION,
        }
        _atomic_write_json(self.path, data)

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        """Returns the pending operation for a document, if any."""
        value = self._load().get(name)
        return value if isinstance(value, dict) else None

    def discard(self, name: str) -> None:
        """Removes a document's pending operation after a successful apply.

        The full-prompt baseline (see ``put_baseline``) is intentionally NOT
        removed — it describes what the AI conversation has seen, which
        outlives individual apply cycles.

        Raises:
            TextSurgeonError: If the session file cannot be rewritten.
        """
        data = self._load()
        if name in data:
            del data[name]
            _atomic_write_json(self.path, data)

    _BASELINE_PREFIX = "baseline::"

    def put_baseline(self, name: str, *, digest: str) -> None:
        """Records that the FULL document was just embedded in a prompt.

        Follow-up (same-conversation) prompts use this to know which applied
        operations the AI has not seen yet, so they can summarise exactly the
        delta instead of re-embedding the document.

        Args:
            name: Document file name.
            digest: SHA-256 of the document at full-prompt generation time.
        """
        data = self._load()
        data[self._BASELINE_PREFIX + name] = {
            "sha256": digest,
            "created": _now_iso(),
            "protocol": PROTOCOL_VERSION,
        }
        _atomic_write_json(self.path, data)

    def get_baseline(self, name: str) -> Optional[Dict[str, Any]]:
        """Returns the last full-prompt baseline for a document, if any."""
        value = self._load().get(self._BASELINE_PREFIX + name)
        return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------- #
# Prompt generation
# --------------------------------------------------------------------------- #

_SURGEON_RULES: Final[str] = """\
## RESPONSE FORMAT — MANDATORY

Your ENTIRE response must be exactly two parts, in this order:

PART 1 — A short explanation of what you will change and why:

<EXPLANATION>
Your reasoning here, in plain language.
</EXPLANATION>

PART 2 — One @@EDIT block per independent change:

@@EDIT anchor
START-ANCHOR: first 5-10 words of the block, copied verbatim
END-ANCHOR: last 5-10 words of the block, copied verbatim
<<<
The complete replacement text, written naturally.
No escaping — real newlines, real quotes, real indentation.
>>>

The replacement body runs between a line containing only <<< and a line
containing only >>>. An empty body deletes the block. Nothing may follow
the final >>> except another @@EDIT block.

Write <<< and >>> as plain ASCII, each ALONE on its own line, with no other
characters. For right-to-left text (Persian, Arabic, Hebrew) this matters:
do not attach any punctuation or direction marks to the fence lines. If your
tooling makes that hard, use the JSON edit format below instead — it is the
most reliable option for right-to-left content.

## CHOOSING A STRATEGY (one per edit)

A. @@EDIT anchor — THE DEFAULT for replacing, rewriting, or deleting any
   block of 2+ lines. You reference the block by its edges only:
     * START-ANCHOR = the block's FIRST 5-10 words, verbatim.
     * END-ANCHOR   = the block's LAST 5-10 words, verbatim.
   Never re-quote the middle — that is the point of this protocol.

B. @@EDIT verbatim — for micro-edits (changing a few words inside one
   sentence). JSON-only alternative: use an anchor edit when in doubt.
   Header: none. Use the JSON format for this strategy (see below).

C. @@EDIT context — when the block's edges are repetitive boilerplate and
   no unique <=10-word anchor exists. Identify the block by its neighbors:
     BEFORE: the exact line directly above the block   (repeatable, max 5)
     AFTER: the exact line directly below the block    (repeatable, max 5)
     HINT: the block's first line                      (optional tiebreaker)

D. @@EDIT tags — only when the document already contains [START_EDIT] /
   [END_EDIT] marker lines placed by the user. Optional header
   NAME: region-name for [START_EDIT:region-name] markers. The markers
   stay; only the text between them is replaced.

## HARD RULES

1. ANCHORS ARE SHORT. 5-10 words. NEVER more than 10 words per marker.
   The engine rejects longer markers.
2. ANCHORS ARE VERBATIM. Copy wording, capitalisation, and punctuation
   exactly as they appear in the document. Whitespace and line wrapping
   are forgiven automatically — wording is not. Anchors are whole words:
   never start or end a marker in the middle of a word.
3. ANCHORS ARE UNIQUE. Before answering, verify each anchor occurs exactly
   once in the document. If the block's first words also appear elsewhere,
   extend the anchor with the words that FOLLOW them inside the block
   (up to 10) until unique — e.g. "The system is" (3 words, ambiguous)
   becomes "The system is initialized by the user". If verification fails,
   the engine aborts and reports unique alternatives; nothing is modified.
4. END AFTER START. The end anchor must come after the start anchor; the
   selection is everything from the first word of START-ANCHOR to the last
   word of END-ANCHOR, inclusive.
5. COMPLETE REPLACEMENTS. The body is the full text that will stand in
   place of the selection. Preserve document structure (headings, lists,
   code fences, indentation). Do not include the anchors' old text unless
   it should remain part of the new block.
6. NO OVERLAPS. Edits must not overlap. Order them top-to-bottom as they
   appear in the document.
7. NO INVENTION. If you cannot fulfil the request with these strategies,
   emit no @@EDIT blocks and explain why inside <EXPLANATION>.

## JSON ALTERNATIVE (for programmatic callers)

Instead of @@EDIT blocks, PART 2 may be a raw JSON array. Same rules;
newlines must then be escaped as \\n:

[
  {"strategy": "anchor",
   "start_anchor": "first 5-10 words of the block",
   "end_anchor": "last 5-10 words of the block",
   "replace": "full replacement text"},
  {"strategy": "verbatim", "search": "exact short text", "replace": "new text"},
  {"strategy": "context", "before": ["line above"], "after": ["line below"],
   "target_hint": "first line of the block", "replace": "..."},
  {"strategy": "tags", "name": "region-name", "replace": "..."}
]
"""

_VERIFICATION_TASK: Final[str] = """\
## YOUR TASK

Check the splices above against the original intent and answer:

1. INTENT    — Does the added text fully accomplish the request?
2. INTEGRITY — Is the Markdown well-formed at every seam (headings, lists,
               links, code fences, emphasis), with no duplicated or orphaned
               fragments where old and new text meet?
3. SCOPE     — Was anything changed beyond what the intent required?

Respond in exactly this format:

VERDICT: PASS
(or VERDICT: FAIL)

followed by one short paragraph of justification. If the verdict is FAIL,
also supply corrective @@EDIT blocks in the Surgeon Protocol format, whose
anchors quote the document as it stands NOW (after the splices above).
"""


def build_surgeon_prompt(document: Document, intent: str) -> str:
    """Renders the strict Surgeon Protocol prompt for an LLM.

    Args:
        document: The loaded target document.
        intent: The user's natural-language micro-change request.

    Returns:
        A self-contained prompt instructing the model to reply with an
        ``<EXPLANATION>`` block followed by a raw JSON array of edits.
    """
    line_count = document.text.count("\n") + 1
    header = (
        f"# SURGEON PROTOCOL v{PROTOCOL_VERSION} — ANCHOR-BASED SELECT & REPLACE\n\n"
        "You are operating as a precision text-editing engine. Below is a\n"
        "document and one edit request. You do NOT rewrite the document and\n"
        "you do NOT re-quote the text you are replacing; you return compact\n"
        "splice instructions that point at each block by short, unique\n"
        "markers. A verification engine resolves your markers, refuses any\n"
        "ambiguous selection, and splices the file mechanically.\n\n"
    )
    document_header = (
        f"## DOCUMENT — {document.name} ({line_count} lines)\n\n"
        "The document starts immediately after the <DOCUMENT> marker line and\n"
        "ends immediately before the </DOCUMENT> marker. Everything in between\n"
        "is the document, byte for byte.\n\n"
    )
    return (
        header
        + _SURGEON_RULES
        + "\n## EDIT REQUEST\n\n"
        + intent.strip()
        + "\n\n"
        + document_header
        + "<DOCUMENT>\n"
        + document.text
        + "</DOCUMENT>\n"
    )


_FOLLOWUP_FORMAT_REMINDER: Final[str] = """\
## RESPONSE FORMAT (reminder — unchanged)

<EXPLANATION>
One short paragraph.
</EXPLANATION>

@@EDIT anchor
START-ANCHOR: first 5-10 words of the block, copied verbatim
END-ANCHOR: last 5-10 words of the block, copied verbatim
<<<
The complete replacement text.
>>>

Hard rules still apply: markers are 5-10 words (NEVER more than 10),
verbatim, and must occur EXACTLY ONCE in the current document — extend an
ambiguous marker with the words that follow it inside the block. The end
anchor comes after the start anchor; edits must not overlap. Write <<< and
>>> as plain ASCII, each alone on its own line; for right-to-left text the
JSON array format is the most reliable. The engine refuses any ambiguous
selection, so verify uniqueness before answering.
"""


def ops_since_baseline(
    document: Document, baseline: Optional[Dict[str, Any]], limit: int = 8
) -> List[Dict[str, Any]]:
    """Returns memory-log operations the AI conversation has not seen.

    Args:
        document: The loaded target document.
        baseline: The last full-prompt baseline (may be ``None``).
        limit: Maximum number of operations to return (newest kept).

    Returns:
        Memory-log entries for this document applied after the baseline was
        recorded — or the most recent ``limit`` entries when no baseline
        exists (with the caller expected to caution the model).
    """
    entries = [
        e
        for e in MemoryEngine(document.directory).entries()
        if e.get("file") == document.name
    ]
    if baseline and baseline.get("created"):
        try:
            cutoff = datetime.fromisoformat(str(baseline["created"]))
            entries = [
                e
                for e in entries
                if datetime.fromisoformat(str(e.get("timestamp"))) >= cutoff
            ]
        except (ValueError, TypeError):
            entries = entries[-limit:]
    return entries[-limit:]


def build_followup_prompt(
    document: Document,
    intent: str,
    *,
    baseline: Optional[Dict[str, Any]] = None,
    recent_ops: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Renders the compact same-conversation prompt (no document embed).

    For tandem editing in one AI chat: the model already holds the document
    from an earlier full prompt, so re-embedding it would only burn tokens.
    Instead this prompt carries the new request, a token-cheap summary of
    every splice applied since the document was shared (so the model's
    stale copy is corrected exactly where it drifted), and a short format
    reminder.

    Args:
        document: The loaded target document.
        intent: The user's natural-language change request.
        baseline: Last full-prompt baseline from ``SessionStore`` (or None).
        recent_ops: Pre-fetched ``ops_since_baseline`` result (computed here
            when omitted).

    Returns:
        The follow-up prompt text.
    """
    if recent_ops is None:
        recent_ops = ops_since_baseline(document, baseline)
    line_count = document.text.count("\n") + 1
    blocks: List[str] = [
        f"# SURGEON PROTOCOL v{PROTOCOL_VERSION} — FOLLOW-UP EDIT "
        "(same conversation)\n",
        f'You already hold the document "{document.name}" from earlier in '
        "this conversation. It is deliberately NOT re-attached, to save "
        "tokens. Work from your copy, corrected by the change log below.\n",
        "## EDIT REQUEST\n",
        intent.strip() + "\n",
    ]
    if recent_ops:
        if baseline:
            blocks.append(
                "## DOCUMENT CHANGE LOG — your copy is out of date in "
                "exactly these places\n"
            )
            blocks.append(
                "Since the document was shared, %d surgical splice(s) were "
                "applied. Anchors must quote the document AS IT STANDS NOW "
                "(%d lines) — i.e. with these changes in effect:\n"
                % (sum(len(e.get("diff", [])) for e in recent_ops), line_count)
            )
        else:
            blocks.append("## DOCUMENT CHANGE LOG (recent operations)\n")
            blocks.append(
                "No record exists of the full document being shared in this "
                "conversation — if you do not actually hold it, say so in "
                "your EXPLANATION instead of guessing. The most recent "
                "changes were:\n"
            )
        change_number = 0
        for entry in recent_ops:
            for pair in entry.get("diff", []):
                if not isinstance(pair, dict):
                    continue
                change_number += 1
                blocks.append("### CHANGE %d\n" % change_number)
                blocks.append(
                    "[REMOVED]\n<<<<\n"
                    + _elide_block(str(pair.get("removed", "")), 16, 5)
                    + "\n>>>>\n"
                )
                added = str(pair.get("added", ""))
                if added:
                    blocks.append(
                        "[NOW READS]\n<<<<\n"
                        + _elide_block(added, 16, 5)
                        + "\n>>>>\n"
                    )
                else:
                    blocks.append("[NOW READS]\n(nothing — the text was deleted)\n")
    else:
        blocks.append(
            "## DOCUMENT STATE\n\nNo changes have been applied since the "
            "document was shared — your copy is exact (%d lines).\n"
            % line_count
        )
    blocks.append(_FOLLOWUP_FORMAT_REMINDER)
    return "\n".join(blocks)


def _elide_block(text: str, max_lines: int = 40, edge: int = 10) -> str:
    """Shows head/tail of very long blocks — the seams are what verification
    needs; re-shipping a 1,000-line body would defeat the protocol."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    omitted = len(lines) - 2 * edge
    return "\n".join(
        lines[:edge]
        + [f"    … ({omitted} unchanged-format lines omitted for brevity) …"]
        + lines[-edge:]
    )


def build_verification_prompt(
    intent: str, applied: Sequence[AppliedEdit], document_name: str
) -> str:
    """Renders the post-operative verification prompt.

    Summarises what was removed and added (long blocks elided to their
    seams), and asks the AI to confirm the original intent was met.

    Args:
        intent: The user's original change request.
        applied: The splices that were executed, in order.
        document_name: File name of the edited document.

    Returns:
        The verification prompt text.
    """
    blocks: List[str] = [
        f"# SURGEON PROTOCOL v{PROTOCOL_VERSION} — POST-OPERATIVE VERIFICATION\n",
        f'A surgical splice was just executed on "{document_name}" based on\n'
        "your edit instructions. Review the operation below and report\n"
        "whether the original intent was met.\n",
        f"ORIGINAL INTENT:\n{intent}\n",
        f"SPLICES APPLIED ({len(applied)}):\n",
    ]
    for position, item in enumerate(applied, start=1):
        span = (
            f"lines {item.line}-{item.end_line}"
            if item.end_line and item.end_line != item.line
            else f"line {item.line}"
        )
        strategy = f" via {item.strategy}" if item.strategy else ""
        blocks.append(f"### SPLICE {position} — {span}{strategy}\n")
        blocks.append(
            "[REMOVED — this exact text was taken out]\n<<<<\n"
            + _elide_block(item.edit.search)
            + "\n>>>>\n"
        )
        if item.edit.replace:
            blocks.append(
                "[ADDED — this exact text now stands in its place]\n<<<<\n"
                + _elide_block(item.edit.replace)
                + "\n>>>>\n"
            )
        else:
            blocks.append("[ADDED]\n(nothing — this splice deleted the text outright)\n")
    blocks.append(_VERIFICATION_TASK)
    return "\n".join(blocks)


# --------------------------------------------------------------------------- #
# AI response parsing
# --------------------------------------------------------------------------- #

_EXPLANATION_RE: Final["re.Pattern[str]"] = re.compile(
    r"<EXPLANATION>(.*?)</EXPLANATION>", re.IGNORECASE | re.DOTALL
)
_FENCE_LINE_RE: Final["re.Pattern[str]"] = re.compile(
    r"^[ \t]*```[^\n]*$", re.MULTILINE
)


def _strip_code_fences(text: str) -> str:
    """Removes markdown code-fence lines that models often add anyway."""
    return _FENCE_LINE_RE.sub("", text)


def _looks_like_edit_payload(value: Any) -> bool:
    """Heuristic: is this decoded JSON plausibly an edit payload?"""
    if isinstance(value, dict):
        return (
            "replace" in value
            and ("search" in value or "strategy" in value
                 or "start_anchor" in value or "before" in value
                 or "after" in value)
        ) or isinstance(value.get("edits"), list)
    if isinstance(value, list):
        return all(isinstance(item, dict) for item in value)
    return False


def _extract_json_payload(text: str) -> Any:
    """Locates and decodes the edit payload JSON inside arbitrary response text.

    Tries the whole (fence-stripped) text first, then scans forward from each
    ``[`` and ``{`` so prose before/after the JSON does not break parsing.
    Candidates that decode but are not shaped like an edit payload are
    skipped, so stray brackets in prose are harmless.

    Args:
        text: The response text with the explanation block already removed.

    Returns:
        The decoded payload (list or dict).

    Raises:
        PayloadError: When no edit-shaped JSON can be found.
    """
    cleaned = _strip_code_fences(text).strip()
    if not cleaned:
        raise PayloadError(
            "No JSON payload found in the response.",
            hint=(
                "The reply must contain a JSON array of "
                '{"search", "replace"} objects after the <EXPLANATION> block.'
            ),
        )

    def _candidates() -> Iterator[Any]:
        """Yields decodable JSON values in priority order."""
        try:
            yield json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        decoder = json.JSONDecoder()
        for pattern in (r"\[", r"\{"):
            for match in re.finditer(pattern, cleaned):
                try:
                    value, _end = decoder.raw_decode(cleaned, match.start())
                except json.JSONDecodeError:
                    continue
                yield value

    saw_json = False
    for candidate in _candidates():
        if _looks_like_edit_payload(candidate):
            return candidate
        saw_json = True
    if saw_json:
        raise PayloadError(
            "Found JSON in the response, but it is not an array of "
            '{"search", "replace"} objects.',
            hint="Ask the AI to resend exactly one JSON array of edit objects.",
        )
    raise PayloadError(
        "No valid JSON payload found in the response.",
        hint=(
            "The reply must contain a JSON array of "
            '{"search", "replace"} objects after the <EXPLANATION> block.'
        ),
    )


def parse_ai_response(raw: str) -> Tuple[Optional[str], List[EditOp]]:
    """Parses a pasted LLM response into an explanation and validated edits.

    Accepts both Protocol-v2 formats — ``@@EDIT`` blocks (preferred; no JSON
    escaping) and a JSON edit array — plus bare Protocol-v1
    ``{"search", "replace"}`` objects for backwards compatibility. Tolerates
    real-world sloppiness (missing explanation tags, markdown code fences,
    prose around the payload) while strictly validating the edits themselves.

    Args:
        raw: The full text the user pasted or saved from the LLM.

    Returns:
        ``(explanation, edits)`` — the explanation may be ``None``; an empty
        edit list means the AI explicitly declined to propose changes.

    Raises:
        PayloadError: When the response is empty, contains no payload, or
            contains malformed edit objects.
    """
    if not raw or not raw.strip():
        raise PayloadError(
            "The response is empty.",
            hint="Paste the full AI reply, including its @@EDIT blocks.",
        )
    explanation: Optional[str] = None
    remainder = raw
    match = _EXPLANATION_RE.search(raw)
    if match:
        explanation = match.group(1).strip()
        remainder = raw[: match.start()] + raw[match.end() :]

    # Detect @@EDIT even when bidi/invisible marks cling to the token in an
    # RTL response (a bare regex anchor would miss "‏@@EDIT").
    structural = "".join(ch for ch in remainder if ch not in engine._STRUCTURAL_IGNORE)
    if re.search(r"(?m)^[ \t]*@@EDIT\b", structural):
        try:
            return explanation, engine.parse_markdown_edits(remainder)
        except engine.InvalidRequestError as exc:
            # Safety net: some responses carry BOTH @@EDIT prose and a JSON
            # array. If a JSON payload is present, prefer it over failing.
            try:
                fallback = _extract_json_payload(remainder)
            except PayloadError:
                raise PayloadError(str(exc), hint=exc.hint) from exc
            if isinstance(fallback, dict):
                edits_value = fallback.get("edits")
                fallback = edits_value if isinstance(edits_value, list) else [fallback]
            try:
                return explanation, [
                    EditOp.from_payload_item(item, position)
                    for position, item in enumerate(fallback, start=1)
                ]
            except engine.InvalidRequestError:
                raise PayloadError(str(exc), hint=exc.hint) from exc

    payload = _extract_json_payload(remainder)
    if isinstance(payload, dict):
        edits_value = payload.get("edits")
        payload = edits_value if isinstance(edits_value, list) else [payload]
    try:
        edits = [
            EditOp.from_payload_item(item, position)
            for position, item in enumerate(payload, start=1)
        ]
    except engine.InvalidRequestError as exc:
        raise PayloadError(str(exc), hint=exc.hint) from exc
    return explanation, edits


# --------------------------------------------------------------------------- #
# CLI helpers
# --------------------------------------------------------------------------- #


def _warn_if_not_markdown(name: str, console: Console) -> None:
    """Warns (without blocking) when the target does not look like Markdown."""
    if not name.lower().endswith(_MARKDOWN_EXTENSIONS):
        console.warn(f"{name} does not look like a Markdown file — proceeding anyway.")


def _read_payload(source: Optional[str], console: Console) -> str:
    """Reads the AI response from a file, an inline string, or STDIN.

    Args:
        source: A file path, a literal payload (detected when it starts with
            ``[``, ``{`` or ``<``), ``-`` for STDIN, or ``None`` for STDIN.
        console: Console for the interactive paste instruction.

    Returns:
        The raw response text.

    Raises:
        TextSurgeonError: If a named response file is missing or unreadable.
    """
    if source and source != "-":
        if os.path.exists(source):
            try:
                with open(source, "r", encoding="utf-8-sig") as fh:
                    return fh.read()
            except OSError as exc:
                raise TextSurgeonError(
                    f"Could not read the response file {source}: {exc}"
                ) from exc
            except UnicodeDecodeError as exc:
                raise TextSurgeonError(
                    f"The response file {source} is not valid UTF-8."
                ) from exc
        if source.lstrip().startswith(("[", "{", "<", "@")):
            return source
        raise TextSurgeonError(
            f"Response file not found: {source}",
            hint=(
                "Pass a path to the saved AI reply, paste raw JSON directly, "
                "or omit the value to paste interactively via stdin."
            ),
        )
    if sys.stdin.isatty():
        console.info(
            "Paste the AI response below, then press Ctrl-D (macOS/Linux) "
            "or Ctrl-Z followed by Enter (Windows):"
        )
    return sys.stdin.read()


def _print_diff(
    console: Console, old: str, new: str, name: str, max_lines: int = 200
) -> None:
    """Prints a colourised unified diff of the pending change to STDERR.

    Args:
        console: Output console.
        old: Original document text.
        new: Post-splice document text.
        name: File name used in the diff headers.
        max_lines: Truncation threshold to keep huge diffs readable.
    """
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
    )
    for shown, line in enumerate(diff):
        if shown >= max_lines:
            console.detail(f"... diff truncated at {max_lines} lines ...")
            break
        text = line.rstrip("\r\n")
        if line.startswith(("+++", "---")):
            console.raw(text, "bold")
        elif line.startswith("+"):
            console.raw(text, "green")
        elif line.startswith("-"):
            console.raw(text, "red")
        elif line.startswith("@@"):
            console.raw(text, "cyan")
        else:
            console.raw(text, "dim")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_generate(args: argparse.Namespace, console: Console) -> int:
    """Executes the ``--generate`` workflow.

    Loads the document, renders the Surgeon Protocol prompt, records the
    pending intent in the session store, and emits the prompt to STDOUT or
    to ``--out``.

    Args:
        args: Parsed CLI arguments.
        console: Output console.

    Returns:
        Process exit code.

    Raises:
        TextSurgeonError: On file or prompt-output failures.
    """
    document = Document.load(args.file)
    _warn_if_not_markdown(document.name, console)
    intent = str(args.generate).strip()
    if not intent:
        raise TextSurgeonError(
            "The change request is empty.",
            hint='Describe the edit, e.g. --generate "Make the introduction longer".',
        )
    session = SessionStore(document.directory)
    if args.followup:
        baseline = session.get_baseline(document.name)
        if baseline is None:
            console.warn(
                "No full prompt has been generated for this file yet — the "
                "AI conversation may not actually hold the document. "
                "Generate a full prompt once first (drop --followup)."
            )
        prompt = build_followup_prompt(document, intent, baseline=baseline)
        full_size = len(build_surgeon_prompt(document, intent))
        console.info(
            "Follow-up prompt: ~%s tokens (saved ~%s vs the full prompt)"
            % (
                f"{max(1, len(prompt) // 4):,}",
                f"{max(0, (full_size - len(prompt)) // 4):,}",
            )
        )
    else:
        prompt = build_surgeon_prompt(document, intent)
    try:
        session.put(document.name, intent=intent, digest=document.sha256())
        if not args.followup:
            session.put_baseline(document.name, digest=document.sha256())
    except TextSurgeonError as exc:
        console.warn(
            f"Could not persist the session (pass --intent at apply time): {exc}"
        )
    line_count = document.text.count("\n") + 1
    console.info(
        f"{document.name}: {len(document.text):,} chars, {line_count:,} lines "
        f"(~{max(1, len(prompt) // 4):,} tokens in the prompt)"
    )
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8", newline="") as fh:
                fh.write(prompt)
        except OSError as exc:
            raise TextSurgeonError(
                f"Could not write the prompt to {args.out}: {exc}"
            ) from exc
        console.success(f"Surgeon Protocol prompt written to {args.out}")
    else:
        console.rule("SURGEON PROTOCOL PROMPT — copy everything below")
        print(prompt, flush=True)
        console.rule("END OF PROMPT")
    program = os.path.basename(sys.argv[0] or "text_surgeon.py")
    console.info(
        "Next: paste the prompt into your LLM, save its reply, then run: "
        f"python3 {program} {args.file} --apply <response-file>"
    )
    return EXIT_OK


def cmd_apply(args: argparse.Namespace, console: Console) -> int:
    """Executes the ``--apply`` workflow.

    Parses the AI response, verifies every anchor (exactly-once rule),
    splices transactionally, saves atomically with a backup, logs to the
    memory engine, and emits the verification prompt.

    Args:
        args: Parsed CLI arguments.
        console: Output console.

    Returns:
        Process exit code.

    Raises:
        SurgicalError: If any anchor is hallucinated or ambiguous.
        PayloadError: If the response cannot be parsed into edits.
        TextSurgeonError: On file I/O failures.
    """
    document = Document.load(args.file)
    _warn_if_not_markdown(document.name, console)
    raw = _read_payload(args.apply_source, console)
    explanation, edits = parse_ai_response(raw)
    if explanation:
        console.panel("AI EXPLANATION", explanation)
    if not edits:
        console.warn(
            "The AI proposed no edits (empty array) — see its explanation. "
            "Nothing to do."
        )
        return EXIT_SURGICAL

    session = SessionStore(document.directory)
    pending = session.get(document.name)
    intent: str = (
        args.intent
        or (str(pending.get("intent", "")) if pending else "")
        or "(intent not recorded)"
    )
    if intent == "(intent not recorded)":
        console.warn(
            "No pending session and no --intent given; the memory log will "
            "record '(intent not recorded)'."
        )
    if pending and pending.get("sha256") and pending["sha256"] != document.sha256():
        console.warn(
            "The document changed after the prompt was generated — anchors "
            "may be stale. The exactly-once check below still protects you."
        )

    original = document.text
    new_text, applied, skipped = document.splice(edits)
    for position in skipped:
        console.warn(
            f"Edit #{position}: replacement is identical to the selection — "
            "skipped as a no-op."
        )
    if not applied:
        console.warn("All edits were no-ops; the document is unchanged.")
        return EXIT_SURGICAL

    for position, item in enumerate(applied, start=1):
        span = (
            f"lines {item.line}-{item.end_line}"
            if item.end_line and item.end_line != item.line
            else f"line {item.line}"
        )
        confidence = (
            f", confidence {item.confidence:.2f}" if item.confidence < 1.0 else ""
        )
        digest = f" sha256 {item.sha256[:12]}…" if item.sha256 else ""
        console.success(
            f"SELECTION CONFIRMED — edit #{position} [{item.note or item.strategy}] "
            f"{span}: -{len(item.edit.search):,} chars, "
            f"+{len(item.edit.replace):,} chars{confidence}{digest}"
        )

    if args.dry_run:
        console.rule(f"DRY RUN — proposed changes to {document.name}", "cyan")
        _print_diff(console, original, new_text, document.name)
        console.info("Dry run complete — nothing was written.")
        return EXIT_OK

    backup_path = document.save(new_text, backup=not args.no_backup)
    backup_note = (
        f"  (backup: {os.path.basename(backup_path)})" if backup_path else ""
    )
    console.success(f"Spliced {len(applied)} edit(s) into {document.name}.{backup_note}")

    try:
        MemoryEngine(document.directory, console).record(
            intent=intent, target=document.name, applied=applied
        )
        console.detail(f"operation logged to {MEMORY_FILENAME}")
    except TextSurgeonError as exc:
        console.warn(f"The edit was saved, but the memory log failed: {exc}")
    try:
        session.discard(document.name)
    except TextSurgeonError as exc:
        console.warn(f"Could not update the session file: {exc}")

    verification = build_verification_prompt(intent, applied, document.name)
    console.rule("VERIFICATION PROMPT — paste this back to the AI")
    print(verification, flush=True)
    console.rule("END OF VERIFICATION PROMPT")
    return EXIT_OK


def cmd_suggest_anchors(args: argparse.Namespace, console: Console) -> int:
    """Executes the ``--suggest-anchors`` workflow.

    Computes the minimal statistically-unique start/end anchor pair for a
    line range and prints it as JSON — ready to drop into an anchor edit.

    Args:
        args: Parsed CLI arguments (``suggest_anchors`` holds "FIRST:LAST").
        console: Output console.

    Returns:
        Process exit code.

    Raises:
        TextSurgeonError: On an invalid range argument.
        SurgicalError: When no unique <=10-word anchor exists for the block.
    """
    document = Document.load(args.file)
    raw_range = str(args.suggest_anchors).replace("-", ":")
    try:
        first_text, _, last_text = raw_range.partition(":")
        first, last = int(first_text), int(last_text or first_text)
    except ValueError:
        raise TextSurgeonError(
            f"Invalid line range {args.suggest_anchors!r}.",
            hint='Use FIRST:LAST (1-based, inclusive), e.g. --suggest-anchors "120:180".',
        )
    selector = engine.SelectionEngine(document.text)
    try:
        result = selector.suggest_anchors_for_lines(first, last)
    except engine.SelectionError as exc:
        raise SurgicalError(f"{exc} [{exc.code}]", hint=exc.hint) from exc
    console.info(
        f"Unique anchors for {document.name} lines {first}-{last} "
        f"({result['start_words']}+{result['end_words']} words):"
    )
    print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    return EXIT_OK


def cmd_history(args: argparse.Namespace, console: Console) -> int:
    """Executes the ``--history`` workflow.

    Shows the most recent memory-log entries for the target document.

    Args:
        args: Parsed CLI arguments.
        console: Output console.

    Returns:
        Process exit code.
    """
    target = os.path.abspath(args.file)
    name = os.path.basename(target)
    memory = MemoryEngine(os.path.dirname(target), console)
    entries = [e for e in memory.entries() if e.get("file") in (None, name)]
    if not entries:
        console.info(f"No operations recorded for {name}.")
        return EXIT_OK
    limit = max(1, args.limit)
    shown = entries[-limit:]
    console.info(
        f"{len(entries)} operation(s) recorded for {name} — showing the last "
        f"{len(shown)}:"
    )
    for entry in shown:
        diffs = [d for d in entry.get("diff", []) if isinstance(d, dict)]
        removed = sum(len(str(d.get("removed", ""))) for d in diffs)
        added = sum(len(str(d.get("added", ""))) for d in diffs)
        intent_preview = _preview(str(entry.get("intent", "?")), 70)
        console.raw(f"  {entry.get('timestamp', '????')}  \"{intent_preview}\"", "bold")
        console.detail(f"    {len(diffs)} splice(s), -{removed} chars, +{added} chars")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Argument parsing & entry point
# --------------------------------------------------------------------------- #


def cmd_agent_generate(args: argparse.Namespace, console: Console) -> int:
    """Executes the Agent Mode prompt generation workflow."""
    import surgeon_agent as agent
    workspace_dir = os.path.abspath(args.project_dir or os.getcwd())
    goal = str(args.goal or args.generate or "").strip()
    if not goal:
        raise TextSurgeonError("A goal or task description is required for Agent Mode.")
    prompt = agent.AgentPromptBuilder.build_task_prompt(
        goal=goal,
        workspace_root=workspace_dir,
        runtime=getattr(args, "runtime", "python") or "python",
        include_workspace_files=True,
    )
    if args.out:
        out_path = os.path.abspath(args.out)
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(prompt)
            console.success(f"Agent task prompt written to {os.path.basename(out_path)} (~{len(prompt)//4} tokens).")
        except OSError as exc:
            raise TextSurgeonError(f"Could not write prompt to {out_path}: {exc}") from exc
    else:
        console.rule("SURGEON AGENT PROMPT — copy everything below")
        print(prompt, flush=True)
        console.rule("END OF PROMPT")
    return EXIT_OK


def cmd_agent_apply(args: argparse.Namespace, console: Console) -> int:
    """Executes the Agent Mode response application and execution workflow."""
    import surgeon_agent as agent
    workspace_dir = os.path.abspath(args.project_dir or os.getcwd())
    raw_response = _read_payload(args.apply_source, console)
    plan = agent.parse_agent_response(raw_response)
    ws = agent.AgentWorkspace(workspace_dir)

    console.info(f"Parsed Agent Plan: {len(plan.file_actions)} file action(s), {len(plan.setup_commands)} setup command(s).")
    if plan.explanation:
        console.detail(f"AI Plan: {plan.explanation}")

    if args.dry_run:
        preview = ws.preview_plan(plan)
        console.rule(f"AGENT DRY RUN — {workspace_dir}", "cyan")
        for ch in preview["changes"]:
            console.info(f"[{ch['action'].upper()}] {ch['file']}")
            if ch.get("diff"):
                _print_diff(console, "", ch["diff"], ch["file"])
        console.info("Dry run complete — no files written.")
        return EXIT_OK

    apply_res = ws.apply_plan(plan, backup=not args.no_backup)
    console.success(f"Applied {len(apply_res['written_files'])} file(s) in {workspace_dir}.")

    should_run = bool(args.run or plan.run_command or plan.setup_commands)
    if should_run:
        timeout = getattr(args, "timeout", agent.DEFAULT_EXECUTION_TIMEOUT) or agent.DEFAULT_EXECUTION_TIMEOUT
        for setup_cmd in plan.setup_commands:
            console.info(f"Running setup: {setup_cmd}")
            s_res = ws.execute_command(setup_cmd, timeout=timeout)
            if not s_res.success:
                console.error(f"Setup command failed: {setup_cmd}")
                print(s_res.summary(), file=sys.stderr)
                return EXIT_SURGICAL
        cmd_to_run = args.run if (isinstance(args.run, str) and args.run.strip() and args.run is not True) else plan.run_command
        if cmd_to_run:
            console.info(f"Executing: {cmd_to_run}")
            exec_res = ws.execute_command(cmd_to_run, timeout=timeout, expected_artifacts=plan.expected_artifacts)
            if exec_res.success:
                console.success(f"Execution succeeded (exit code 0 in {exec_res.duration_sec:.2f}s).")
            else:
                console.error(f"Execution failed with exit code {exec_res.exit_code}.")
            if exec_res.stdout:
                console.rule("STDOUT")
                print(exec_res.stdout)
            if exec_res.stderr:
                console.rule("STDERR", "red")
                print(exec_res.stderr, file=sys.stderr)

            verif_prompt = agent.AgentPromptBuilder.build_verification_prompt(
                goal=getattr(args, "goal", "") or plan.explanation or "Task execution",
                plan=plan,
                result=exec_res,
                workspace_root=workspace_dir,
            )
            console.rule("AGENT VERIFICATION PROMPT — paste this back to the AI")
            print(verif_prompt, flush=True)
            console.rule("END OF VERIFICATION PROMPT")
            return EXIT_OK if exec_res.success else EXIT_SURGICAL

    return EXIT_OK


def cmd_agent_new_project(args: argparse.Namespace, console: Console) -> int:
    """Creates a new safe project workspace."""
    import surgeon_agent as agent
    name = str(args.new_project or "").strip()
    runtime = getattr(args, "runtime", "python") or "python"
    created = agent.ProjectManager.create_project(
        name=name,
        runtime=runtime,
        description=getattr(args, "goal", "") or "",
    )
    console.success(f"Safe project '{created['name']}' created successfully at:")
    console.detail(f"Path: {created['path']}")
    console.detail(f"Runtime: {created['runtime']}")
    return EXIT_OK


def cmd_agent_list_projects(args: argparse.Namespace, console: Console) -> int:
    """Lists all safe projects in the projects library."""
    import surgeon_agent as agent
    projects = agent.ProjectManager.list_projects()
    root = agent.ProjectManager.get_projects_root()
    console.rule(f"SAFE PROJECTS LIBRARY — {root}", "cyan")
    if not projects:
        console.info("No projects found yet. Create one with: --agent --new-project <name>")
        return EXIT_OK

    for p in projects:
        console.info(f"📁 {p['name']} ({p['runtime']}) — {p['total_files']} files, {p['backups_count']} backups")
        console.detail(f"   Path: {p['path']}")
        console.detail(f"   Last modified: {p['last_modified']}")
    return EXIT_OK


def cmd_agent_backups(args: argparse.Namespace, console: Console) -> int:
    """Lists backup snapshots for the designated workspace."""
    import surgeon_agent as agent
    workspace_dir = os.path.abspath(args.project_dir or os.getcwd())
    ws = agent.AgentWorkspace(workspace_dir)
    backups = ws.list_backups()
    console.rule(f"WORKSPACE BACKUP SNAPSHOTS — {workspace_dir}", "cyan")
    if not backups:
        console.info("No backups found in .surgeon/backups/.")
        return EXIT_OK

    for b in backups:
        b_id = b.get("backup_id", "unknown")
        ts_str = b.get("timestamp", "")
        exp = b.get("explanation", "")
        files = b.get("files", [])
        console.info(f"💾 Snapshot [{b_id}] — {ts_str}")
        if exp:
            console.detail(f"   Goal: {exp}")
        console.detail(f"   Backed up files: {', '.join(files) if files else 'None'}")
    return EXIT_OK


def cmd_agent_restore(args: argparse.Namespace, console: Console) -> int:
    """Restores a backup snapshot."""
    import surgeon_agent as agent
    workspace_dir = os.path.abspath(args.project_dir or os.getcwd())
    backup_id = str(args.restore or "").strip()
    ws = agent.AgentWorkspace(workspace_dir)
    res = ws.restore_backup(backup_id)
    console.success(f"Restored snapshot [{backup_id}] successfully.")
    for f in res.get("restored_files", []):
        console.detail(f"Restored: {f}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Argument parsing & entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Builds the CLI argument parser.

    Returns:
        A configured ``argparse.ArgumentParser``.
    """
    parser = argparse.ArgumentParser(
        prog="text_surgeon.py",
        description=(
            "Text Surgeon — surgical, AI-assisted edits for documents & multi-file workspaces "
            "via the anchor-based Surgeon Protocol v2 and Agent Mode."
        ),
        epilog=textwrap.dedent(
            """\
            examples (Document Mode):
              %(prog)s notes.md --generate "Make the introduction longer"
              %(prog)s notes.md --generate "Tighten section 2" --out prompt.txt
              %(prog)s notes.md --apply response.txt
              %(prog)s notes.md --apply response.txt --dry-run
              %(prog)s notes.md --suggest-anchors 120:180
              %(prog)s notes.md --history --limit 10

            examples (Agent Mode):
              %(prog)s --agent --goal "Build presentation generator" --project-dir ./my_project
              %(prog)s --agent --project-dir ./my_project --apply ai_reply.txt --run

            exit codes:
              0  success      1  usage or I-O error      2  surgical or payload abort
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("file", nargs="?", default=None, help="path to the target Markdown document (Document Mode)")
    parser.add_argument("--agent", action="store_true", help="enable Agent Mode (multi-file workspace agent)")
    parser.add_argument("--project-dir", "-d", metavar="DIR", help="project workspace directory for Agent Mode (default: current dir)")
    parser.add_argument("--goal", metavar="TEXT", help="task/goal description for Agent Mode")
    parser.add_argument("--runtime", default="python", help="runtime environment for Agent Mode (python, node, bash)")
    parser.add_argument("--run", nargs="?", const=True, default=None, metavar="CMD", help="execute command in workspace during Agent Mode apply")
    parser.add_argument("--timeout", type=int, default=180, help="command execution timeout in seconds")
    parser.add_argument("--new-project", metavar="NAME", help="create a new safe project folder in projects library")
    parser.add_argument("--list-projects", action="store_true", help="list all projects in safe projects library")
    parser.add_argument("--backups", action="store_true", help="list backup snapshots in project .surgeon/backups/")
    parser.add_argument("--restore", metavar="BACKUP_ID", help="restore workspace from a backup snapshot ID")

    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument(
        "--generate",
        metavar="INTENT",
        help="generate a Surgeon Protocol prompt for this change request",
    )
    mode.add_argument(
        "--apply",
        nargs="?",
        const="-",
        default=None,
        dest="apply_source",
        metavar="RESPONSE",
        help=(
            "apply an AI response: pass a file path or raw JSON, or omit the "
            "value to paste via stdin"
        ),
    )
    mode.add_argument(
        "--suggest-anchors",
        metavar="FIRST:LAST",
        help=(
            "print the minimal statistically-unique start/end anchors for a "
            "1-based inclusive line range"
        ),
    )
    mode.add_argument(
        "--history",
        action="store_true",
        help="show recorded operations for this document",
    )
    parser.add_argument(
        "--intent",
        metavar="TEXT",
        help="record/override the intent stored in the memory log (with --apply)",
    )
    parser.add_argument(
        "--followup",
        action="store_true",
        help=(
            "with --generate: build a compact same-conversation prompt that "
            "does NOT re-embed the document (the AI already holds it); "
            "changes applied since the last full prompt are summarised"
        ),
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="write the generated prompt to a file instead of stdout (with --generate)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and preview the splice without writing (with --apply)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the .bak safety copy (with --apply)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        metavar="N",
        help="number of history entries to show (default: 5)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def _warn_ignored_flags(args: argparse.Namespace, console: Console) -> None:
    """Flags options that have no effect in the selected mode."""
    if args.generate is not None:
        for flag, active in (
            ("--dry-run", args.dry_run),
            ("--no-backup", args.no_backup),
            ("--intent", bool(args.intent)),
        ):
            if active:
                console.warn(f"{flag} has no effect with --generate; ignored.")
    if args.apply_source is not None and args.out:
        console.warn("--out has no effect with --apply; ignored.")
    if args.generate is None and args.followup:
        console.warn("--followup only has effect with --generate; ignored.")
    if args.history and any(
        [args.out, args.dry_run, args.no_backup, bool(args.intent)]
    ):
        console.warn(
            "--out/--dry-run/--no-backup/--intent have no effect with "
            "--history; ignored."
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 success, 1 usage/environment error,
        2 surgical/payload abort, 130 interrupted).
    """
    console = Console()
    args = build_parser().parse_args(argv)
    console.banner()
    _warn_ignored_flags(args, console)
    try:
        if args.list_projects:
            return cmd_agent_list_projects(args, console)
        if args.new_project:
            return cmd_agent_new_project(args, console)
        if args.backups:
            return cmd_agent_backups(args, console)
        if args.restore:
            return cmd_agent_restore(args, console)

        if args.agent:
            if args.apply_source is not None:
                return cmd_agent_apply(args, console)
            if args.goal or args.generate:
                return cmd_agent_generate(args, console)
            raise TextSurgeonError("Agent Mode requires either --goal <TEXT>, --apply <FILE>, --new-project <NAME>, or --list-projects.")

        if not args.file:
            raise TextSurgeonError(
                "Missing target file.",
                hint="Pass a document path for Document Mode (e.g. text_surgeon.py notes.md --generate '...'), or use --agent for workspace mode.",
            )

        if args.generate is not None:
            return cmd_generate(args, console)
        if args.apply_source is not None:
            return cmd_apply(args, console)
        if args.suggest_anchors is not None:
            return cmd_suggest_anchors(args, console)
        if args.history:
            return cmd_history(args, console)
        raise TextSurgeonError("No action specified. Use --generate, --apply, --suggest-anchors, or --history.")
    except (SurgicalError, PayloadError) as exc:
        label = "SURGICAL ABORT" if isinstance(exc, SurgicalError) else "PAYLOAD REJECTED"
        console.error(f"{label} — {exc}")
        if exc.hint:
            console.hint(exc.hint)
        console.info("The document on disk was NOT modified.")
        return EXIT_SURGICAL
    except TextSurgeonError as exc:
        console.error(str(exc))
        if exc.hint:
            console.hint(exc.hint)
        return EXIT_USAGE
    except KeyboardInterrupt:
        console.warn("Interrupted — no changes written.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
