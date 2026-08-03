#!/usr/bin/env python3
"""Text Surgeon Web — a local browser UI for text_surgeon.py.

Serves a single-page application (``surgeon_ui.html``) plus a small JSON API
on ``127.0.0.1`` so the whole Semantic Search & Replace workflow — generate
prompt, paste AI response, preview, apply, verify — happens in the browser
with full Unicode/RTL support (Farsi, Arabic, ...), which Windows terminals
handle poorly.

Zero third-party dependencies: built on ``http.server`` from the standard
library, so it runs on any machine with Python 3.8+ — no pip, no venv.

Usage:
    python surgeon_web.py                 # starts server, opens the browser
    python surgeon_web.py --port 9000
    python surgeon_web.py --no-browser

API overview (all JSON):
    GET  /                → the UI
    GET  /api/state       → document stats, pending session, memory count
    GET  /api/files       → tracked-file workspace with live per-file badges
    GET  /api/recent      → recently used files (legacy path list)
    GET  /api/history     → memory-log entries for one document (per file)
    POST /api/generate    → build the Surgeon Protocol prompt (+ session)
    POST /api/apply       → parse response, splice (dry-run or real), log
    POST /api/pick        → native OS "open file" dialog (Tk); returns a path
    POST /api/browse      → server-side folder listing (in-page fallback)
    POST /api/files/forget→ drop a file from the workspace (bookmark only)
    POST /api/shutdown    → stop the server (the UI's Quit button)
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import socket
import subprocess
import sys
import threading
import traceback
import urllib
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse


APP_DIR: str = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

import surgeon_agent as agent  # noqa: E402  (multi-file agent engine)
import text_surgeon as ts  # noqa: E402  (co-located core engine)

__version__: str = "2.3.0"

UI_FILENAME: str = "surgeon_ui.html"
STATE_FILENAME: str = ".surgeon_web_state.json"
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8765
MAX_RECENT: int = 40
MAX_BODY_BYTES: int = 64 * 1024 * 1024
MARKDOWN_EXTENSIONS: Tuple[str, ...] = (".md", ".markdown", ".mdown", ".mkd")

# Native OS "open file" dialog, run in a short-lived subprocess so a missing
# Tk/display can never crash the server and GUI threading rules are respected
# (the dialog owns its process's main thread). Prints the chosen absolute
# path to stdout, or nothing when the user cancels.
_PICKER_SCRIPT: str = r"""
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as exc:
    sys.stderr.write("no-tk: %s" % exc)
    sys.exit(3)
initialdir = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
try:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        root.update()
    except Exception:
        pass
    path = filedialog.askopenfilename(
        title="Text Surgeon — choose a file to edit",
        initialdir=initialdir,
        filetypes=[
            ("Text & Markdown", "*.md *.markdown *.mdown *.mkd *.txt"),
            ("Markdown", "*.md *.markdown *.mdown *.mkd"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
except Exception as exc:
    sys.stderr.write("dialog-failed: %s" % exc)
    sys.exit(4)
if path:
    sys.stdout.write(path)
"""

_PICKER_FOLDER_SCRIPT: str = r"""
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as exc:
    sys.stderr.write("no-tk: %s" % exc)
    sys.exit(3)
initialdir = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
try:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        root.update()
    except Exception:
        pass
    path = filedialog.askdirectory(
        title="Text Surgeon — Choose a Project Workspace Folder",
        initialdir=initialdir,
    )
    root.destroy()
except Exception as exc:
    sys.stderr.write("dialog-failed: %s" % exc)
    sys.exit(4)
if path:
    sys.stdout.write(path)
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve_path(raw: str) -> str:
    """Expands ``~`` and environment variables and returns an absolute path.

    Args:
        raw: A user-supplied path (may contain ``~`` or ``%VAR%``/``$VAR``).

    Returns:
        The absolute, expanded path.
    """
    return os.path.abspath(os.path.expanduser(os.path.expandvars(raw.strip())))


def _state_path() -> str:
    """Returns the path of the small app-state file (the file workspace)."""
    return os.path.join(APP_DIR, STATE_FILENAME)


def _load_workspace() -> List[Dict[str, Any]]:
    """Returns the tracked-files list (most recently opened first).

    Each entry is ``{"path", "name", "last_opened"}``. Legacy state that
    stored a bare ``recent`` path list is migrated on read.
    """
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    files = data.get("files")
    if isinstance(files, list):
        clean = []
        for item in files:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                clean.append(
                    {
                        "path": item["path"],
                        "name": item.get("name") or os.path.basename(item["path"]),
                        "last_opened": item.get("last_opened"),
                    }
                )
        return clean[:MAX_RECENT]
    legacy = data.get("recent")  # migrate old {"recent": [paths]}
    if isinstance(legacy, list):
        return [
            {"path": p, "name": os.path.basename(p), "last_opened": None}
            for p in legacy
            if isinstance(p, str)
        ][:MAX_RECENT]
    return []


def _save_workspace(files: List[Dict[str, Any]]) -> None:
    """Persists the tracked-files list (best effort, atomic)."""
    try:
        ts._atomic_write_json(_state_path(), {"files": files[:MAX_RECENT]})
    except Exception:  # non-fatal bookkeeping
        pass


def _touch_file(path: str) -> None:
    """Moves ``path`` to the front of the workspace with a fresh timestamp."""
    files = [f for f in _load_workspace() if f["path"] != path]
    files.insert(
        0,
        {"path": path, "name": os.path.basename(path), "last_opened": ts._now_iso()},
    )
    _save_workspace(files)


def _forget_file(path: str) -> bool:
    """Removes ``path`` from the workspace. Returns True if it was present."""
    files = _load_workspace()
    kept = [f for f in files if f["path"] != path]
    if len(kept) == len(files):
        return False
    _save_workspace(kept)
    return True


def _load_recent() -> List[str]:
    """Backward-compatible path list for the legacy ``/api/recent`` route."""
    return [f["path"] for f in _load_workspace()]


def _remember_recent(path: str) -> None:
    """Compatibility shim — records a file into the workspace."""
    _touch_file(path)


def _history_entries(memory: "ts.MemoryEngine", name: str) -> List[Dict[str, Any]]:
    """Returns memory-log entries belonging to exactly one document.

    Memories are kept strictly per file: only entries whose ``file`` matches
    ``name`` are returned, so two documents that share a directory never see
    each other's history.
    """
    return [e for e in memory.entries() if e.get("file") == name]


def _file_card(path: str, entry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Builds the workspace-list payload for one tracked file (live stats)."""
    name = os.path.basename(path)
    card: Dict[str, Any] = {
        "path": path,
        "name": name,
        "last_opened": entry.get("last_opened") if entry else None,
        "exists": os.path.isfile(path),
        "pending": None,
        "pending_intent": None,
        "history_count": 0,
        "has_backup": False,
    }
    if not card["exists"]:
        return card
    directory = os.path.dirname(path)
    pending = ts.SessionStore(directory).get(name)
    if pending and pending.get("intent"):
        card["pending"] = True
        card["pending_intent"] = pending["intent"]
    card["history_count"] = len(_history_entries(ts.MemoryEngine(directory), name))
    card["has_backup"] = os.path.isfile(path + ts.BACKUP_SUFFIX)
    return card


def _pick_file_native(initialdir: Optional[str]) -> Dict[str, Any]:
    """Opens the OS "open file" dialog and returns the chosen path."""
    args = [sys.executable, "-c", _PICKER_SCRIPT, initialdir or ""]
    try:
        proc = subprocess.run(
            args, capture_output=True, timeout=600, text=True, encoding="utf-8"
        )
    except subprocess.TimeoutExpired:
        return {"cancelled": True}
    except OSError as exc:
        return {"error": "Could not launch the file dialog: %s" % exc,
                "fallback": True}
    if proc.returncode in (3, 4):
        return {
            "error": "No native file dialog is available on this machine.",
            "fallback": True,
        }
    path = (proc.stdout or "").strip()
    if not path:
        return {"cancelled": True}
    return {"path": _resolve_path(path)}


def _pick_folder_native(initialdir: Optional[str]) -> Dict[str, Any]:
    """Opens the OS "choose directory" dialog and returns the chosen folder path."""
    args = [sys.executable, "-c", _PICKER_FOLDER_SCRIPT, initialdir or ""]
    try:
        proc = subprocess.run(
            args, capture_output=True, timeout=600, text=True, encoding="utf-8"
        )
    except subprocess.TimeoutExpired:
        return {"cancelled": True}
    except OSError as exc:
        return {"error": "Could not launch the folder dialog: %s" % exc, "fallback": True}
    if proc.returncode in (3, 4):
        return {
            "error": "No native folder dialog is available on this machine.",
            "fallback": True,
        }
    path = (proc.stdout or "").strip()
    if not path:
        return {"cancelled": True}
    return {"path": _resolve_path(path)}


def _document_stats(document: "ts.Document") -> Dict[str, Any]:
    """Returns display statistics for a loaded document."""
    return {
        "chars": len(document.text),
        "lines": document.text.count("\n") + 1,
        "tokens": max(1, len(document.text) // 4),
    }


def _splices_payload(applied: Sequence["ts.AppliedEdit"]) -> List[Dict[str, Any]]:
    """Serialises applied splices for the UI."""
    return [
        {
            "line": item.line,
            "removed": item.edit.search,
            "added": item.edit.replace,
            "note": item.note,
        }
        for item in applied
    ]


def _unified_diff(old: str, new: str, name: str, max_chars: int = 120_000) -> str:
    """Builds a unified diff string, truncated for very large changes."""
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n... (diff truncated) ..."
    return diff


def _error_body(exc: Exception, kind: str) -> Dict[str, Any]:
    """Builds the standard JSON error payload."""
    return {"error": str(exc), "hint": getattr(exc, "hint", None), "kind": kind}


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #


class SurgeonRequestHandler(BaseHTTPRequestHandler):
    """Routes HTTP requests for the Text Surgeon web UI and JSON API."""

    server_version = f"TextSurgeonWeb/{__version__}"

    # ------------------------------------------------------------------ I/O

    def log_message(self, fmt: str, *args: Any) -> None:
        """Writes a terse request log line to STDERR."""
        sys.stderr.write("[web] %s\n" % (fmt % args))

    def _send_bytes(self, body: bytes, status: int, content_type: str) -> None:
        """Sends a complete response with standard headers."""
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # client went away
            pass

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        """Sends a JSON response (UTF-8, non-ASCII preserved)."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, status, "application/json; charset=utf-8")

    def _send_ui(self) -> None:
        """Serves the single-page UI from disk (live-editable)."""
        ui_path = os.path.join(APP_DIR, UI_FILENAME)
        try:
            with open(ui_path, "rb") as fh:
                self._send_bytes(fh.read(), 200, "text/html; charset=utf-8")
        except OSError:
            message = (
                "<h1>Text Surgeon</h1><p>UI file missing: put "
                f"<code>{UI_FILENAME}</code> next to surgeon_web.py.</p>"
            )
            self._send_bytes(message.encode("utf-8"), 500, "text/html; charset=utf-8")

    def _read_json(self) -> Dict[str, Any]:
        """Reads and validates the request body as a JSON object.

        Raises:
            ValueError: If the body is missing, oversized, or not an object.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("Empty request body.")
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body too large.")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def _query(self) -> Dict[str, str]:
        """Returns the query string as a flat dict (first value wins)."""
        parsed = parse_qs(urlparse(self.path).query)
        return {key: values[0] for key, values in parsed.items() if values}

    # ------------------------------------------------------------- dispatch

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        """Dispatches GET requests."""
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        """Dispatches POST requests."""
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        """Routes a request and maps engine exceptions to HTTP statuses."""
        path = urlparse(self.path).path
        try:
            if method == "GET":
                if path == "/":
                    return self._send_ui()
                if path == "/favicon.ico":
                    return self._send_bytes(b"", 204, "image/x-icon")
                if path == "/api/state":
                    return self._api_state()
                if path == "/api/recent":
                    return self._send_json({"recent": _load_recent()})
                if path == "/api/files":
                    return self._api_files()
                if path == "/api/history":
                    return self._api_history()
                if path == "/api/agent/state":
                    return self._api_agent_state()
                if path == "/api/agent/projects":
                    return self._api_agent_projects_get()
                if path == "/api/agent/backups":
                    return self._api_agent_backups()
                if path == "/api/agent/artifact":
                    return self._api_agent_artifact()
                if path == "/api/agent/file":
                    return self._api_agent_file_get()
                if path == "/api/agent/env":
                    return self._api_agent_env_get()
                if path == "/api/agent/export":
                    return self._api_agent_export()
                if path == "/api/agent/models":
                    return self._api_agent_models()
                if path == "/api/agent/rounds":
                    return self._api_agent_rounds()
                if path == "/api/agent/skills":
                    return self._api_agent_skills_get()
                if path == "/api/agent/keys/status":
                    return self._api_agent_keys_status()
                if path == "/api/agent/memory":
                    return self._api_agent_memory_get()
            else:
                if path == "/api/generate":
                    return self._api_generate()
                if path == "/api/apply":
                    return self._api_apply()
                if path == "/api/browse":
                    return self._api_browse()
                if path == "/api/pick":
                    return self._api_pick()
                if path == "/api/files/forget":
                    return self._api_forget()
                if path == "/api/shutdown":
                    return self._api_shutdown()
                if path == "/api/agent/pick_folder":
                    return self._api_agent_pick_folder()
                if path == "/api/agent/projects":
                    return self._api_agent_projects_post()
                if path == "/api/agent/generate":
                    return self._api_agent_generate()
                if path == "/api/agent/preview":
                    return self._api_agent_preview()
                if path == "/api/agent/apply":
                    return self._api_agent_apply()
                if path == "/api/agent/run":
                    return self._api_agent_run()
                if path == "/api/agent/restore":
                    return self._api_agent_restore()
                if path == "/api/agent/file":
                    return self._api_agent_file_post()
                if path == "/api/agent/file/delete":
                    return self._api_agent_file_delete()
                if path == "/api/agent/env":
                    return self._api_agent_env_post()
                if path == "/api/agent/llm_query":
                    return self._api_agent_llm_query()
                if path == "/api/agent/autopilot":
                    return self._api_agent_autopilot()
                if path == "/api/agent/skills/detect":
                    return self._api_agent_skills_detect()
                if path == "/api/agent/skills/import":
                    return self._api_agent_skills_import()
                if path == "/api/agent/skills/create":
                    return self._api_agent_skills_create()
                if path == "/api/agent/install_packages":
                    return self._api_agent_install_packages()
                if path == "/api/agent/keys/status":
                    return self._api_agent_keys_status()
                if path == "/api/agent/memory/synthesize":
                    return self._api_agent_memory_synthesize()
                if path == "/api/agent/memory/clear":
                    return self._api_agent_memory_clear()
                if path == "/api/agent/memory/save":
                    return self._api_agent_memory_save()
            self._send_json({"error": "Not found.", "kind": "request"}, 404)

        except agent.AgentError as exc:
            self._send_json(_error_body(exc, "agent"), 422)
        except ts.SurgicalError as exc:
            self._send_json(_error_body(exc, "surgical"), 422)
        except ts.PayloadError as exc:
            self._send_json(_error_body(exc, "payload"), 422)
        except ts.TextSurgeonError as exc:
            self._send_json(_error_body(exc, "file"), 400)
        except (ValueError, KeyError) as exc:
            self._send_json({"error": str(exc), "kind": "request"}, 400)
        except Exception as exc:  # pragma: no cover - defensive
            traceback.print_exc()
            self._send_json(
                {"error": f"Internal server error: {exc}", "kind": "internal"}, 500
            )

    # ------------------------------------------------------------ endpoints

    def _require_file(self, raw: Optional[str]) -> "ts.Document":
        """Resolves and loads the target document or raises a clean error."""
        if not raw or not raw.strip():
            raise ValueError("No file path provided.")
        return ts.Document.load(_resolve_path(raw))

    def _api_state(self) -> None:
        """GET /api/state?file=...&remember=1 → document status snapshot."""
        query = self._query()
        raw = query.get("file", "")
        if not raw.strip():
            return self._send_json({"exists": False})
        path = _resolve_path(raw)
        if not os.path.isfile(path):
            return self._send_json({"exists": False, "path": path})
        document = ts.Document.load(path)
        session = ts.SessionStore(document.directory)
        pending = session.get(document.name)
        memory = ts.MemoryEngine(document.directory)
        history_count = len(_history_entries(memory, document.name))
        if query.get("remember") == "1":
            _touch_file(path)
        self._send_json(
            {
                "exists": True,
                "path": document.path,
                "name": document.name,
                "directory": document.directory,
                "is_markdown": document.name.lower().endswith(MARKDOWN_EXTENSIONS),
                "stats": _document_stats(document),
                "pending": pending,
                "history_count": history_count,
                "has_backup": os.path.isfile(document.path + ts.BACKUP_SUFFIX),
                "has_baseline": session.get_baseline(document.name) is not None,
            }
        )

    def _api_files(self) -> None:
        """GET /api/files → the tracked-file workspace with live per-file stats."""
        cards = [_file_card(f["path"], f) for f in _load_workspace()]
        self._send_json({"files": cards})

    def _api_pick(self) -> None:
        """POST /api/pick {initialdir?} → native OS open-file dialog result.

        On success the chosen file is added to the workspace and its full
        state snapshot is returned so the UI can open it in one round-trip.
        """
        try:
            data = self._read_json()
        except ValueError:
            data = {}
        initialdir = str(data.get("initialdir") or "").strip()
        if initialdir:
            initialdir = _resolve_path(initialdir)
        elif _load_workspace():
            initialdir = os.path.dirname(_load_workspace()[0]["path"])
        result = _pick_file_native(initialdir or None)
        if "path" in result:
            _touch_file(result["path"])
        self._send_json(result)

    def _api_forget(self) -> None:
        """POST /api/files/forget {file} → drop a file from the workspace.

        Only the workspace bookmark is removed; the document, its backup, and
        its memory log on disk are left untouched.
        """
        data = self._read_json()
        raw = str(data.get("file") or "").strip()
        if not raw:
            raise ValueError("No file path provided.")
        removed = _forget_file(_resolve_path(raw))
        self._send_json({"removed": removed, "files": [
            _file_card(f["path"], f) for f in _load_workspace()
        ]})

    def _api_generate(self) -> None:
        """POST /api/generate {file, intent, mode?} → Surgeon Protocol prompt.

        ``mode`` is ``"full"`` (default — embeds the document, for a fresh AI
        conversation) or ``"followup"`` (compact same-conversation prompt:
        no document embed, applied-change summary instead).
        """
        data = self._read_json()
        document = self._require_file(data.get("file"))
        intent = str(data.get("intent") or "").strip()
        if not intent:
            raise ValueError("The change request (intent) is empty.")
        mode = str(data.get("mode") or "full").strip().lower()
        if mode not in ("full", "followup"):
            raise ValueError('mode must be "full" or "followup".')
        session = ts.SessionStore(document.directory)
        saved_tokens = 0
        baseline_missing = False
        if mode == "followup":
            baseline = session.get_baseline(document.name)
            baseline_missing = baseline is None
            prompt = ts.build_followup_prompt(document, intent, baseline=baseline)
            full_size = len(ts.build_surgeon_prompt(document, intent))
            saved_tokens = max(0, (full_size - len(prompt)) // 4)
        else:
            prompt = ts.build_surgeon_prompt(document, intent)
        try:
            session.put(document.name, intent=intent, digest=document.sha256())
            if mode == "full":
                session.put_baseline(document.name, digest=document.sha256())
        except ts.TextSurgeonError:
            pass  # session is a convenience; the prompt still stands
        _touch_file(document.path)
        self._send_json(
            {
                "prompt": prompt,
                "intent": intent,
                "mode": mode,
                "stats": _document_stats(document),
                "prompt_tokens": max(1, len(prompt) // 4),
                "saved_tokens": saved_tokens,
                "baseline_missing": baseline_missing,
            }
        )

    def _api_apply(self) -> None:
        """POST /api/apply {file, response, dry_run, intent?, backup?}."""
        data = self._read_json()
        document = self._require_file(data.get("file"))
        raw_response = str(data.get("response") or "")
        dry_run = bool(data.get("dry_run"))
        backup = data.get("backup", True) is not False

        explanation, edits = ts.parse_ai_response(raw_response)
        if not edits:
            return self._send_json(
                {
                    "error": "The AI proposed no edits (empty array).",
                    "hint": "Read its explanation — it may have declined the request.",
                    "kind": "empty",
                    "explanation": explanation,
                },
                422,
            )

        session = ts.SessionStore(document.directory)
        pending = session.get(document.name)
        intent = (
            str(data.get("intent") or "").strip()
            or (str(pending.get("intent", "")) if pending else "")
            or "(intent not recorded)"
        )
        stale = bool(
            pending
            and pending.get("sha256")
            and pending["sha256"] != document.sha256()
        )

        original = document.text
        new_text, applied, skipped = document.splice(edits)
        if not applied:
            return self._send_json(
                {
                    "error": "All edits were no-ops (search equals replace).",
                    "hint": "Ask the AI for a real change.",
                    "kind": "noop",
                    "explanation": explanation,
                },
                422,
            )

        diff = _unified_diff(original, new_text, document.name)
        base: Dict[str, Any] = {
            "explanation": explanation,
            "intent": intent,
            "stale": stale,
            "skipped": skipped,
            "splices": _splices_payload(applied),
            "diff": diff,
        }

        if dry_run:
            base["mode"] = "preview"
            return self._send_json(base)

        backup_path = document.save(new_text, backup=backup)
        warnings: List[str] = []
        try:
            ts.MemoryEngine(document.directory).record(
                intent=intent, target=document.name, applied=applied
            )
        except ts.TextSurgeonError as exc:
            warnings.append(f"The edit was saved, but the memory log failed: {exc}")
        try:
            session.discard(document.name)
        except ts.TextSurgeonError:
            pass
        _touch_file(document.path)

        base["mode"] = "applied"
        base["backup"] = os.path.basename(backup_path) if backup_path else None
        base["verification"] = ts.build_verification_prompt(
            intent, applied, document.name
        )
        base["stats"] = _document_stats(document)
        base["warnings"] = warnings
        self._send_json(base)

    def _api_history(self) -> None:
        """GET /api/history?file=...&limit=N → memory entries, newest first."""
        query = self._query()
        raw = query.get("file", "")
        if not raw.strip():
            raise ValueError("No file path provided.")
        path = _resolve_path(raw)
        name = os.path.basename(path)
        try:
            limit = max(1, min(100, int(query.get("limit", "20"))))
        except ValueError:
            limit = 20
        memory = ts.MemoryEngine(os.path.dirname(path))
        entries = _history_entries(memory, name)
        recent_first = list(reversed(entries))[:limit]
        payload = []
        for entry in recent_first:
            diffs = [d for d in entry.get("diff", []) if isinstance(d, dict)]
            payload.append(
                {
                    "timestamp": entry.get("timestamp"),
                    "intent": entry.get("intent"),
                    "splices": len(diffs),
                    "removed_chars": sum(len(str(d.get("removed", ""))) for d in diffs),
                    "added_chars": sum(len(str(d.get("added", ""))) for d in diffs),
                    "diff": [
                        {
                            "removed": str(d.get("removed", ""))[:400],
                            "added": str(d.get("added", ""))[:400],
                        }
                        for d in diffs
                    ],
                }
            )
        self._send_json({"total": len(entries), "entries": payload})

    def _api_browse(self) -> None:
        """POST /api/browse {dir} → folders and Markdown files for the picker."""
        data = self._read_json()
        raw = str(data.get("dir") or "").strip()
        directory = _resolve_path(raw) if raw else os.path.expanduser("~")
        if not os.path.isdir(directory):
            raise ValueError(f"Not a folder: {directory}")
        dirs: List[str] = []
        files: List[Dict[str, Any]] = []
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir():
                            dirs.append(entry.name)
                        elif entry.name.lower().endswith(MARKDOWN_EXTENSIONS):
                            files.append(
                                {"name": entry.name, "size": entry.stat().st_size}
                            )
                    except OSError:
                        continue
        except PermissionError:
            raise ValueError(f"Permission denied: {directory}")
        parent = os.path.dirname(directory)
        self._send_json(
            {
                "dir": directory,
                "parent": parent if parent != directory else None,
                "dirs": sorted(dirs, key=str.casefold),
                "files": sorted(files, key=lambda f: str.casefold(f["name"])),
            }
        )

    # -------------------------------------------------------- Agent Endpoints

    def _api_agent_state(self) -> None:
        """GET /api/agent/state?dir=... → scans project workspace files and stats."""
        query = self._query()
        raw_dir = query.get("dir", "").strip()
        if not raw_dir:
            projs = agent.ProjectManager.list_projects()
            if projs:
                workspace_dir = projs[0]["path"]
            else:
                workspace_dir = agent.ProjectManager.get_projects_root()
        else:
            workspace_dir = _resolve_path(raw_dir)

        ws = agent.AgentWorkspace(workspace_dir)
        scan = ws.scan()
        backups = ws.list_backups()
        meta = ws.get_project_meta()
        default_keys = agent.KeyManager.load_default_keys()
        self._send_json(
            {
                "exists": os.path.isdir(workspace_dir),
                "workspace": scan,
                "workspace_root": workspace_dir,
                "dir": workspace_dir,
                "dir_name": os.path.basename(workspace_dir),
                "files": scan.get("files", []),
                "total_files": scan.get("total_files", 0),
                "total_lines": scan.get("total_lines", 0),
                "est_tokens": scan.get("est_tokens", 0),
                "backups_count": len(backups),
                "project": meta,
                "default_keys": default_keys,
                "default_keys_count": len(default_keys),
                "has_default_keys": bool(default_keys),
            }
        )

    def _api_agent_pick_folder(self) -> None:
        """POST /api/agent/pick_folder {initialdir?} → native OS folder picker."""
        try:
            data = self._read_json()
        except ValueError:
            data = {}
        initialdir = str(data.get("initialdir") or "").strip()
        if initialdir:
            initialdir = _resolve_path(initialdir)
        else:
            initialdir = os.getcwd()
        result = _pick_folder_native(initialdir)
        if "path" in result:
            scan = agent.scan_workspace_tree(result["path"])
            result["workspace"] = scan
            result["dir_name"] = os.path.basename(result["path"])
        self._send_json(result)

    def _api_agent_generate(self) -> None:
        """POST /api/agent/generate {goal, dir/workspace_dir, runtime?, include_files?, include_memory?, skills?, auto_detect_skills?}."""
        data = self._read_json()
        goal = str(data.get("goal") or "").strip()
        if not goal:
            raise ValueError("The goal / task description is required.")
        raw_dir = str(data.get("dir") or data.get("workspace_dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        runtime = str(data.get("runtime") or "python").strip()
        include_files = data.get("include_files", True) is not False
        include_memory = data.get("include_memory", True) is not False
        skill_names = data.get("skills") or data.get("skill_names")
        auto_detect_skills = data.get("auto_detect_skills", True) is not False

        prompt = agent.AgentPromptBuilder.build_task_prompt(
            goal=goal,
            workspace_root=workspace_dir,
            runtime=runtime,
            include_workspace_files=include_files,
            include_memory=include_memory,
            skill_names=skill_names,
            auto_detect_skills=auto_detect_skills,
        )
        tokens = max(1, len(prompt) // 4)
        self._send_json(
            {
                "prompt": prompt,
                "tokens": tokens,
                "prompt_tokens": tokens,
                "goal": goal,
                "dir": workspace_dir,
                "workspace_dir": workspace_dir,
                "runtime": runtime,
                "include_memory": include_memory,
                "skills": skill_names or [],
            }
        )

    def _api_agent_preview(self) -> None:
        """POST /api/agent/preview {response, dir/workspace_dir} → parses plan & computes diffs."""
        data = self._read_json()
        response_text = str(data.get("response") or "")
        raw_dir = str(data.get("dir") or data.get("workspace_dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()

        plan = agent.parse_agent_response(response_text)
        ws = agent.AgentWorkspace(workspace_dir)
        preview = ws.preview_plan(plan)

        self._send_json(
            {
                "plan": preview,
                "changes": preview.get("changes", []),
                "explanation": plan.explanation,
                "file_count": len(plan.file_actions),
                "setup_commands": plan.setup_commands,
                "run_command": plan.run_command,
                "expected_artifacts": plan.expected_artifacts,
                "workspace_root": workspace_dir,
            }
        )

    def _api_agent_apply(self) -> None:
        """POST /api/agent/apply {response, dir/workspace_dir, execute/auto_run?, timeout?, goal?}."""
        data = self._read_json()
        response_text = str(data.get("response") or "")
        raw_dir = str(data.get("dir") or data.get("workspace_dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        should_execute = bool(data.get("execute", data.get("auto_run", True)))
        timeout = int(data.get("timeout") or agent.DEFAULT_EXECUTION_TIMEOUT)
        goal = str(data.get("goal") or "").strip()

        plan = agent.parse_agent_response(response_text)
        ws = agent.AgentWorkspace(workspace_dir)

        # 1. Apply file changes transactionally
        apply_result = ws.apply_plan(plan, backup=True)

        exec_result: Optional[agent.ExecutionResult] = None
        setup_results: List[Dict[str, Any]] = []
        verification_prompt: Optional[str] = None

        if should_execute:
            # 2. Run setup commands if specified
            for setup_cmd in plan.setup_commands:
                res = ws.execute_command(setup_cmd, timeout=timeout)
                setup_results.append(
                    {
                        "command": setup_cmd,
                        "exit_code": res.exit_code,
                        "stdout": res.stdout,
                        "stderr": res.stderr,
                        "success": res.success,
                    }
                )
                if not res.success:
                    exec_result = res
                    break

            # 3. Run primary command if setup passed (or if no setup)
            if (not exec_result or exec_result.success) and plan.run_command:
                exec_result = ws.execute_command(
                    plan.run_command,
                    timeout=timeout,
                    expected_artifacts=plan.expected_artifacts,
                )

            # 4. Build post-execution verification prompt
            if exec_result:
                verification_prompt = agent.AgentPromptBuilder.build_verification_prompt(
                    goal=goal or (plan.explanation or "Task execution"),
                    plan=plan,
                    result=exec_result,
                    workspace_root=workspace_dir,
                )

        exec_dict: Optional[Dict[str, Any]] = None
        artifacts_meta: List[Dict[str, Any]] = []
        if exec_result:
            exec_dict = {
                "command": exec_result.command,
                "exit_code": exec_result.exit_code,
                "stdout": exec_result.stdout,
                "stderr": exec_result.stderr,
                "duration_sec": exec_result.duration_sec,
                "artifacts_found": exec_result.artifacts_found,
                "timed_out": exec_result.timed_out,
                "success": exec_result.success,
                "summary": exec_result.summary(),
            }
            for art in exec_result.artifacts_found:
                safe_art = agent.resolve_safe_workspace_path(workspace_dir, art)
                sz = os.path.getsize(safe_art) if os.path.isfile(safe_art) else 0
                artifacts_meta.append({"name": os.path.basename(art), "path": art, "size": sz})

        scan = ws.scan()
        backups = ws.list_backups()

        self._send_json(
            {
                "success": True,
                "apply_result": apply_result,
                "written_files": apply_result.get("written_files", []),
                "backup_id": apply_result.get("backup_id"),
                "setup_results": setup_results,
                "execution_result": exec_dict,
                "execution": exec_dict,
                "artifacts": artifacts_meta,
                "verification_prompt": verification_prompt,
                "workspace": scan,
                "workspace_root": workspace_dir,
                "backups_count": len(backups),
            }
        )

    def _api_agent_run(self) -> None:
        """POST /api/agent/run {command, dir, goal?, timeout?, expected_artifacts?}."""
        data = self._read_json()
        command = str(data.get("command") or "").strip()
        if not command:
            raise ValueError("No command specified to run.")
        raw_dir = str(data.get("dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        goal = str(data.get("goal") or "").strip()
        timeout = int(data.get("timeout") or agent.DEFAULT_EXECUTION_TIMEOUT)
        artifacts = data.get("expected_artifacts") or []

        ws = agent.AgentWorkspace(workspace_dir)
        exec_result = ws.execute_command(
            command,
            timeout=timeout,
            expected_artifacts=artifacts if isinstance(artifacts, list) else None,
        )

        plan_dummy = agent.AgentPlan(
            explanation=goal or "Custom execution",
            run_command=command,
            expected_artifacts=artifacts if isinstance(artifacts, list) else [],
        )

        verification_prompt = agent.AgentPromptBuilder.build_verification_prompt(
            goal=goal or f"Run `{command}`",
            plan=plan_dummy,
            result=exec_result,
            workspace_root=workspace_dir,
        )

        exec_dict = {
            "command": exec_result.command,
            "exit_code": exec_result.exit_code,
            "stdout": exec_result.stdout,
            "stderr": exec_result.stderr,
            "duration_sec": exec_result.duration_sec,
            "artifacts_found": exec_result.artifacts_found,
            "timed_out": exec_result.timed_out,
            "success": exec_result.success,
            "summary": exec_result.summary(),
        }

        scan = ws.scan()

        self._send_json(
            {
                "execution_result": exec_dict,
                "verification_prompt": verification_prompt,
                "workspace": scan,
            }
        )

    def _api_agent_artifact(self) -> None:
        """GET /api/agent/artifact?dir=...&file=... → serves generated artifact file."""
        query = self._query()
        raw_dir = query.get("dir", "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        raw_file = query.get("file", "").strip()
        if not raw_file:
            raise ValueError("No artifact file specified.")

        safe_path = agent.resolve_safe_workspace_path(workspace_dir, raw_file)
        if not os.path.isfile(safe_path):
            return self._send_json({"error": "Artifact file not found."}, 404)

        ext = os.path.splitext(safe_path)[1].lower()
        mime_map = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".csv": "text/csv; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".pdf": "application/pdf",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".zip": "application/zip",
        }
        content_type = mime_map.get(ext, "application/octet-stream")

        try:
            with open(safe_path, "rb") as fh:
                data = fh.read()
            self._send_bytes(data, 200, content_type)
        except OSError as exc:
            self._send_json({"error": f"Failed to read file: {exc}"}, 500)

    def _api_agent_projects_get(self) -> None:
        """GET /api/agent/projects → lists safe projects in projects directory."""
        query = self._query()
        base_dir = query.get("base_dir")
        projects = agent.ProjectManager.list_projects(base_dir)
        projects_root = agent.ProjectManager.get_projects_root(base_dir)
        self._send_json({"projects": projects, "projects_root": projects_root})

    def _api_agent_projects_post(self) -> None:
        """POST /api/agent/projects {name, runtime?, description?, base_dir?} → creates safe project."""
        data = self._read_json()
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("Project name is required.")
        runtime = str(data.get("runtime") or "python").strip()
        description = str(data.get("description") or "").strip()
        base_dir = data.get("base_dir")

        created = agent.ProjectManager.create_project(
            name=name,
            base_dir=base_dir,
            runtime=runtime,
            description=description,
        )
        self._send_json({"success": True, "project": created})

    def _api_agent_backups(self) -> None:
        """GET /api/agent/backups?dir=... → lists organized backups for project workspace."""
        query = self._query()
        raw_dir = query.get("dir", "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        ws = agent.AgentWorkspace(workspace_dir)
        backups = ws.list_backups()
        self._send_json({"backups": backups, "workspace": workspace_dir, "total": len(backups)})

    def _api_agent_restore(self) -> None:
        """POST /api/agent/restore {backup_id, dir, file?} → restores from snapshot."""
        data = self._read_json()
        backup_id = str(data.get("backup_id") or "").strip()
        if not backup_id:
            raise ValueError("Backup ID is required.")
        raw_dir = str(data.get("dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        target_file = data.get("file")

        ws = agent.AgentWorkspace(workspace_dir)
        res = ws.restore_backup(backup_id=backup_id, target_file=target_file)
        scan = ws.scan()
        self._send_json({
            "success": True,
            "restore": res,
            "backup_id": backup_id,
            "restored_files": res.get("restored_files", []),
            "workspace": scan,
        })

    def _api_agent_file_get(self) -> None:
        """GET /api/agent/file?dir=...&file=... → returns content of a workspace file."""
        query = self._query()
        raw_dir = query.get("dir", "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        raw_file = query.get("file", "").strip()
        if not raw_file:
            raise ValueError("No file specified.")
        ws = agent.AgentWorkspace(workspace_dir)
        content = ws.read_file(raw_file)
        self._send_json({"file": raw_file, "content": content, "size": len(content.encode("utf-8"))})

    def _api_agent_file_post(self) -> None:
        """POST /api/agent/file {dir, file, content} → atomically writes a workspace file."""
        data = self._read_json()
        raw_dir = str(data.get("dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        raw_file = str(data.get("file") or "").strip()
        if not raw_file:
            raise ValueError("No file path specified.")
        content = str(data.get("content") or "")
        ws = agent.AgentWorkspace(workspace_dir)
        res = ws.write_file(raw_file, content)
        scan = ws.scan()
        self._send_json({"success": True, "result": res, "workspace": scan})

    def _api_agent_file_delete(self) -> None:
        """POST /api/agent/file/delete {dir, file} → safely removes a file from workspace."""
        data = self._read_json()
        raw_dir = str(data.get("dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        raw_file = str(data.get("file") or "").strip()
        if not raw_file:
            raise ValueError("No file path specified.")
        ws = agent.AgentWorkspace(workspace_dir)
        deleted = ws.delete_file(raw_file)
        scan = ws.scan()
        self._send_json({"success": True, "deleted": deleted, "file": raw_file, "workspace": scan})

    def _api_agent_env_get(self) -> None:
        """GET /api/agent/env?dir=... → returns workspace .env key-value variables."""
        query = self._query()
        raw_dir = query.get("dir", "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        ws = agent.AgentWorkspace(workspace_dir)
        env_vars = ws.get_env()
        self._send_json({"env": env_vars, "workspace": workspace_dir})

    def _api_agent_env_post(self) -> None:
        """POST /api/agent/env {dir, env} → saves workspace .env key-value variables."""
        data = self._read_json()
        raw_dir = str(data.get("dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        env_vars = data.get("env") or {}
        if not isinstance(env_vars, dict):
            raise ValueError("Expected an object for 'env'.")
        ws = agent.AgentWorkspace(workspace_dir)
        ws.set_env({str(k): str(v) for k, v in env_vars.items()})
        self._send_json({"success": True, "env": ws.get_env()})

    def _api_agent_export(self) -> None:
        """GET /api/agent/export?dir=... → downloads workspace as clean .zip archive."""
        query = self._query()
        raw_dir = query.get("dir", "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        ws = agent.AgentWorkspace(workspace_dir)
        zip_bytes = ws.export_zip()
        proj_name = os.path.basename(workspace_dir) or "project"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{proj_name}.zip"')
        self.send_header("Content-Length", str(len(zip_bytes)))
        self.end_headers()
        self.wfile.write(zip_bytes)

    def _api_agent_models(self) -> None:
        """GET /api/agent/models?base_url=... → lists available local Ollama models."""
        query = self._query()
        base_url = query.get("base_url", "http://localhost:11434")
        models = agent.LLMClient.list_ollama_models(base_url)
        self._send_json({"models": models, "base_url": base_url})

    def _api_agent_rounds(self) -> None:
        """GET /api/agent/rounds?dir=... → returns multi-round agent history."""
        query = self._query()
        raw_dir = query.get("dir", "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        ws = agent.AgentWorkspace(workspace_dir)
        rounds = ws.get_round_history()
        self._send_json({"rounds": rounds, "workspace": workspace_dir, "total": len(rounds)})

    def _api_agent_llm_query(self) -> None:
        """POST /api/agent/llm_query {prompt, config} → queries configured LLM provider directly."""
        data = self._read_json()
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("Prompt is empty.")
        cfg_dict = data.get("config") or {}
        raw_keys = cfg_dict.get("api_keys") or []
        if isinstance(raw_keys, str):
            raw_keys = agent.KeyManager.parse_keys(raw_keys)
        config = agent.LLMConfig(
            provider=str(cfg_dict.get("provider") or "ollama"),
            model=str(cfg_dict.get("model") or "llama3"),
            api_key=str(cfg_dict.get("api_key") or ""),
            api_keys=list(raw_keys),
            base_url=str(cfg_dict.get("base_url") or "http://localhost:11434"),
            temperature=float(cfg_dict.get("temperature") or 0.2),
            max_tokens=int(cfg_dict.get("max_tokens") or 4096),
        )
        system_prompt = data.get("system_prompt")
        reply = agent.LLMClient.send_prompt(prompt, config, system_prompt=system_prompt)
        self._send_json({"reply": reply, "model": config.model, "provider": config.provider})

    def _api_agent_autopilot(self) -> None:
        """POST /api/agent/autopilot {dir, goal, config, runtime?, max_rounds?, skills?, auto_detect_skills?} → multi-round self-repair."""
        data = self._read_json()
        raw_dir = str(data.get("dir") or data.get("workspace_dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        goal = str(data.get("goal") or "").strip()
        if not goal:
            raise ValueError("Goal is required for Auto-Pilot.")
        cfg_dict = data.get("config") or data
        raw_keys = cfg_dict.get("api_keys") or []
        if not raw_keys and cfg_dict.get("api_key"):
            raw_keys = [cfg_dict.get("api_key")]
        if isinstance(raw_keys, str):
            raw_keys = agent.KeyManager.parse_keys(raw_keys)
        config = agent.LLMConfig(
            provider=str(cfg_dict.get("provider") or "ollama"),
            model=str(cfg_dict.get("model") or "llama3"),
            api_key=str(cfg_dict.get("api_key") or (raw_keys[0] if raw_keys else "")),
            api_keys=list(raw_keys),
            base_url=str(cfg_dict.get("base_url") or "http://localhost:11434"),
            temperature=float(cfg_dict.get("temperature") or 0.2),
            max_tokens=int(cfg_dict.get("max_tokens") or 4096),
        )
        runtime = str(data.get("runtime") or "python").strip()
        max_rounds = max(1, min(10, int(data.get("max_rounds") or 3)))
        include_memory = data.get("include_memory", True) is not False
        skills = data.get("skills") or data.get("skill_names")
        auto_detect_skills = data.get("auto_detect_skills", True) is not False

        loop_res = agent.AutoPilotEngine.run_autonomous_loop(
            workspace_root=workspace_dir,
            goal=goal,
            llm_config=config,
            runtime=runtime,
            max_rounds=max_rounds,
            include_memory=include_memory,
            skill_names=skills,
            auto_detect_skills=auto_detect_skills,
        )
        ws = agent.AgentWorkspace(workspace_dir)
        scan = ws.scan()
        self._send_json({
            "success": loop_res.get("status") == "success",
            "status": loop_res.get("status"),
            "rounds_completed": loop_res.get("rounds_completed", 0),
            "rounds": loop_res.get("rounds", []),
            "final_verification": loop_res.get("final_verification", ""),
            "final_artifacts": loop_res.get("final_artifacts", []),
            "result": loop_res,
            "workspace": scan,
        })

    def _api_agent_memory_get(self) -> None:
        """GET /api/agent/memory?dir=... → returns Short-Term and Long-Term project memory."""
        query = self._query()
        raw_dir = query.get("dir", "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        mem_engine = agent.ProjectMemoryEngine(workspace_dir)
        short_term = mem_engine.load_short_term()
        long_term = mem_engine.load_long_term()
        md_path = mem_engine.memory_md_file
        md_text = ""
        if os.path.isfile(md_path):
            try:
                with open(md_path, "r", encoding="utf-8") as fh:
                    md_text = fh.read()
            except OSError:
                pass
        self._send_json({
            "workspace": workspace_dir,
            "short_term": short_term,
            "short_term_count": len(short_term),
            "long_term": long_term,
            "memory_md": md_text,
        })

    def _api_agent_memory_synthesize(self) -> None:
        """POST /api/agent/memory/synthesize {dir, config, instructions?} → runs AI memory consolidation."""
        data = self._read_json()
        raw_dir = str(data.get("dir") or data.get("workspace_dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        cfg_dict = data.get("config") or {}
        raw_keys = cfg_dict.get("api_keys") or []
        if not raw_keys and cfg_dict.get("api_key"):
            raw_keys = [cfg_dict.get("api_key")]
        if isinstance(raw_keys, str):
            raw_keys = agent.KeyManager.parse_keys(raw_keys)
        config = agent.LLMConfig(
            provider=str(cfg_dict.get("provider") or "ollama"),
            model=str(cfg_dict.get("model") or "llama3"),
            api_key=str(cfg_dict.get("api_key") or (raw_keys[0] if raw_keys else "")),
            api_keys=list(raw_keys),
            base_url=str(cfg_dict.get("base_url") or "http://localhost:11434"),
            temperature=float(cfg_dict.get("temperature") or 0.2),
            max_tokens=int(cfg_dict.get("max_tokens") or 4096),
        )
        custom_instructions = str(data.get("instructions") or data.get("custom_instructions") or "").strip()

        mem_engine = agent.ProjectMemoryEngine(workspace_dir)
        synthesized_ltm = mem_engine.synthesize_memory(config, custom_instructions=custom_instructions)
        md_path = mem_engine.memory_md_file
        md_text = ""
        if os.path.isfile(md_path):
            try:
                with open(md_path, "r", encoding="utf-8") as fh:
                    md_text = fh.read()
            except OSError:
                pass

        self._send_json({
            "success": True,
            "workspace": workspace_dir,
            "long_term": synthesized_ltm,
            "memory_md": md_text,
            "short_term_count": len(mem_engine.load_short_term()),
        })

    def _api_agent_memory_clear(self) -> None:
        """POST /api/agent/memory/clear {dir, target?} → clears short-term memory buffer."""
        data = self._read_json()
        raw_dir = str(data.get("dir") or data.get("workspace_dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        target = str(data.get("target") or "short_term").strip()

        mem_engine = agent.ProjectMemoryEngine(workspace_dir)
        if target in ("short_term", "all"):
            mem_engine.clear_short_term()
        self._send_json({
            "success": True,
            "cleared": True,
            "workspace": workspace_dir,
            "short_term_count": len(mem_engine.load_short_term()),
        })

    def _api_agent_memory_save(self) -> None:
        """POST /api/agent/memory/save {dir, long_term} → persists manual edits to long-term memory."""
        data = self._read_json()
        raw_dir = str(data.get("dir") or data.get("workspace_dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        ltm = data.get("long_term") or {}
        if not isinstance(ltm, dict):
            raise ValueError("Invalid long_term memory payload format.")

        mem_engine = agent.ProjectMemoryEngine(workspace_dir)
        mem_engine.save_long_term(ltm)
        self._send_json({
            "success": True,
            "saved": True,
            "workspace": workspace_dir,
            "long_term": mem_engine.load_long_term(),
        })

    def _api_agent_skills_get(self) -> None:
        """GET /api/agent/skills?dir=... → lists all available skills."""
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        raw_dir = params.get("dir", [""])[0].strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else None
        skills = agent.SkillManager.discover_skills(workspace_dir)
        self._send_json({
            "skills": [s.to_dict() for s in skills],
            "total": len(skills),
        })

    def _api_agent_skills_detect(self) -> None:
        """POST /api/agent/skills/detect {goal, dir?} → returns matching skills for goal."""
        data = self._read_json()
        goal = str(data.get("goal") or "").strip()
        raw_dir = str(data.get("dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else None
        matched = agent.SkillManager.detect_skills(goal, workspace_dir)
        self._send_json({
            "skills": [s.to_dict() for s in matched],
            "matched_skills": [s.to_dict() for s in matched],
            "total": len(matched),
        })


    def _api_agent_skills_import(self) -> None:
        """POST /api/agent/skills/import {url, dir?} → downloads and imports skill from URL."""
        data = self._read_json()
        url = str(data.get("url") or "").strip()
        if not url:
            raise ValueError("URL is required.")
        raw_dir = str(data.get("dir") or "").strip()
        target_dir = _resolve_path(raw_dir) if raw_dir else None
        skill = agent.SkillManager.import_from_url(url, target_dir)
        self._send_json({
            "success": True,
            "skill": skill.to_dict(),
        })

    def _api_agent_skills_create(self) -> None:
        """POST /api/agent/skills/create {name, title, description, content, keywords, packages, dir?}."""
        data = self._read_json()
        name = str(data.get("name") or "").strip()
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        content = str(data.get("content") or "").strip()
        keywords = data.get("keywords") or []
        packages = data.get("packages") or []
        raw_dir = str(data.get("dir") or "").strip()
        target_dir = _resolve_path(raw_dir) if raw_dir else None

        skill = agent.SkillManager.create_skill(
            name=name,
            title=title,
            description=description,
            content=content,
            keywords=keywords,
            packages=packages,
            target_dir=target_dir,
        )
        self._send_json({
            "success": True,
            "skill": skill.to_dict(),
        })

    def _api_agent_keys_status(self) -> None:
        """GET/POST /api/agent/keys/status → returns key health status and pool summary."""
        provider = "gemini"
        keys_list: List[str] = []
        if self.command == "POST":
            data = self._read_json()
            provider = str(data.get("provider") or "gemini").strip().lower()
            raw_keys = data.get("api_keys") if "api_keys" in data else data.get("keys", [])
            keys_list = agent.KeyManager.parse_keys(raw_keys)
        else:
            query = urlparse(self.path).query
            params = parse_qs(query)
            provider = params.get("provider", ["gemini"])[0].strip().lower()
            raw_keys = params.get("keys", [""])[0].strip()
            keys_list = agent.KeyManager.parse_keys(raw_keys)

        if not keys_list:
            keys_list = agent.KeyManager.load_default_keys()

        status = agent.KeyManager.get_status(provider, keys_list)
        summary = agent.KeyManager.get_pool_status(keys_list, provider)
        self._send_json({
            "provider": provider,
            "keys": status,
            "summary": summary,
            "default_keys_count": len(keys_list),
        })


    def _api_agent_install_packages(self) -> None:
        """POST /api/agent/install_packages {dir, packages} → runs pip install inside workspace."""
        data = self._read_json()
        raw_dir = str(data.get("dir") or "").strip()
        workspace_dir = _resolve_path(raw_dir) if raw_dir else os.getcwd()
        packages = data.get("packages") or []
        if isinstance(packages, str):
            packages = [p.strip() for p in packages.split(",") if p.strip()]
        ws = agent.AgentWorkspace(workspace_dir)
        res = ws.install_dependencies(packages)
        self._send_json({
            "success": res.success,
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "duration_sec": res.duration_sec,
        })

    def _api_shutdown(self) -> None:
        """POST /api/shutdown → stops the server after responding."""
        self._send_json({"success": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        hard_exit = threading.Timer(2.0, os._exit, args=(0,))
        hard_exit.daemon = True
        hard_exit.start()


class _Server(ThreadingHTTPServer):
    """Threading HTTP server with daemon workers for clean shutdown."""

    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _pick_port(host: str, preferred: int, tries: int = 40) -> int:
    """Finds a free TCP port, starting at ``preferred``.

    Args:
        host: Interface to bind.
        preferred: First port to try.
        tries: How many sequential ports to probe.

    Returns:
        A bindable port number.

    Raises:
        OSError: If no port in the range is free.
    """
    last_error: Optional[OSError] = None
    for port in range(preferred, preferred + tries):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, port))
            return port
        except OSError as exc:
            last_error = exc
        finally:
            probe.close()
    raise last_error or OSError("No free port found.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Starts the local web server (and, by default, the browser).

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="surgeon_web.py",
        description="Local browser UI for Text Surgeon (standard library only).",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="preferred port (default 8765)"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open the browser"
    )
    args = parser.parse_args(argv)

    ui_path = os.path.join(APP_DIR, UI_FILENAME)
    if not os.path.isfile(ui_path):
        print(f"WARNING: {UI_FILENAME} not found next to surgeon_web.py", file=sys.stderr)

    try:
        port = _pick_port(DEFAULT_HOST, args.port)
    except OSError as exc:
        print(f"Could not find a free port near {args.port}: {exc}", file=sys.stderr)
        return 1

    server = _Server((DEFAULT_HOST, port), SurgeonRequestHandler)
    url = f"http://{DEFAULT_HOST}:{port}/"
    print(f"Text Surgeon Web v{__version__}", file=sys.stderr)
    print(f"  Serving:  {url}", file=sys.stderr)
    print(f"  Engine:   {os.path.join(APP_DIR, 'text_surgeon.py')}", file=sys.stderr)
    print("  Stop:     Ctrl+C here, or the Quit button in the page.", file=sys.stderr)

    if not args.no_browser:
        opener = threading.Timer(0.6, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("Server stopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
