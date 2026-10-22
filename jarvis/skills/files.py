"""Reading files and folders — the part of an assistant people actually use daily.

Text extraction is local and quiet: python-docx for Word, PyPDF2 for PDFs, openpyxl for
spreadsheets, plain read for everything else. Deletion is the one destructive operation and
goes through the spoken confirmation gate, into the Recycle Bin (recoverable).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger("skills.files")

MAX_CHARS = 6000          # what we hand the model to summarise
MAX_LIST = 60

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".log", ".json", ".csv", ".py", ".ps1",
                 ".bat", ".ini", ".yaml", ".yml", ".html", ".xml", ".ts", ".js"}


# ------------------------------------------------------------------------ reading
def read_text(path: str, max_chars: int = MAX_CHARS) -> tuple[str, str]:
    """Return (text, note). Note explains truncation or unsupported formats."""
    target = Path(path).expanduser()
    if not target.exists():
        return "", f"{path} doesn't exist."
    if target.is_dir():
        return "", f"{path} is a folder, not a file."
    suffix = target.suffix.lower()
    try:
        if suffix == ".docx":
            import docx

            document = docx.Document(str(target))
            parts = [p.text for p in document.paragraphs if p.text.strip()]
            for table in document.tables:
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        parts.append(" | ".join(cells))
            text = "\n".join(parts)
        elif suffix == ".pdf":
            from PyPDF2 import PdfReader

            reader = PdfReader(str(target))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:25])
        elif suffix in (".xlsx", ".xlsm"):
            import openpyxl

            workbook = openpyxl.load_workbook(str(target), read_only=True, data_only=True)
            lines: list[str] = []
            for sheet in workbook.worksheets[:5]:
                lines.append(f"[sheet: {sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        lines.append(" | ".join(cells))
            text = "\n".join(lines)
        elif suffix == ".pptx":
            from pptx import Presentation

            presentation = Presentation(str(target))
            lines = []
            for index, slide in enumerate(presentation.slides, 1):
                lines.append(f"[slide {index}]")
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False) and shape.text.strip():
                        lines.append(shape.text.strip())
            text = "\n".join(lines)
        elif suffix in TEXT_SUFFIXES or suffix == "":
            text = target.read_text(encoding="utf-8", errors="replace")
        else:
            return "", f"I can't read {suffix} files yet, sir."
    except Exception as exc:  # noqa: BLE001 - a broken file is not a crash
        log.warning("read %s failed: %s", target, exc)
        return "", f"I couldn't read {target.name}: {exc}"

    text = text.strip()
    if not text:
        return "", f"{target.name} has no readable text."
    note = ""
    if len(text) > max_chars:
        note = f" (first {max_chars} of {len(text)} characters)"
        text = text[:max_chars]
    return text, note


def read_file_for_model(path: str) -> str:
    text, note = read_text(path)
    if not text:
        return note or "Nothing to read, sir."
    target = Path(path).expanduser()
    return f"Contents of {target.name}{note}:\n{text}"


def list_folder(path: str) -> str:
    target = Path(path).expanduser()
    if not target.exists():
        return f"{path} doesn't exist, sir."
    if not target.is_dir():
        return f"{path} is a file, not a folder, sir."
    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return f"I'm not allowed to look inside {path}, sir."
    names = []
    for entry in entries[:MAX_LIST]:
        names.append(entry.name + ("/" if entry.is_dir() else ""))
    more = f" (and {len(entries) - MAX_LIST} more)" if len(entries) > MAX_LIST else ""
    return f"{target} contains: " + ", ".join(names) + more


# ------------------------------------------------------------------------ deleting
def delete_to_trash(path: str) -> str:
    """Move a file to the Recycle Bin. Called only after spoken confirmation."""
    target = Path(path).expanduser()
    if not target.exists():
        return f"{path} doesn't exist, sir."
    try:
        from send2trash import send2trash

        send2trash(str(target))
        log.info("moved to recycle bin: %s", target)
        return f"{target.name} is in the Recycle Bin, sir. It can be restored from there."
    except Exception as exc:  # noqa: BLE001
        log.warning("delete failed: %s", exc)
        return f"I couldn't delete {target.name}, sir: {exc}"


def request_delete(path: str) -> str:
    """Ask out loud before deleting; the gate runs the deletion on 'yes'."""
    from ..safety import request

    target = Path(path).expanduser()
    if not target.exists():
        return f"{path} doesn't exist, sir."
    kind = "folder" if target.is_dir() else "file"
    return request(
        f"move {target.name} to the Recycle Bin",
        f"That {kind} is at {target}.",
        lambda: delete_to_trash(str(target)),
    )


# Directories that are large and never contain the documents people ask for.
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "AppData", "Windows",
    "Program Files", "Program Files (x86)", "ProgramData", "$Recycle.Bin", "Temp",
    "site-packages", ".cache", ".dsh", "OneDrive", "Dropbox", "Music", "Videos",
}


def find_first(name: str, budget_s: float = 2.5) -> str | None:
    """Find a file by (part of) its name in the user's common folders.

    Walks with a time budget and prunes huge directories: an unbounded `Path.home()`
    search took ~10 s per call on this laptop and made 'delete my cv' feel broken.
    """
    needle = name.lower().strip()
    if not needle:
        return None
    roots = []
    for folder in ("Desktop", "Documents", "Downloads", "Pictures"):
        candidate = Path.home() / folder
        if candidate.is_dir():
            roots.append(candidate)
    deadline = time.monotonic() + budget_s
    for root in roots:
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                if time.monotonic() > deadline:
                    log.debug("file search for %r hit its time budget", name)
                    return None
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
                for filename in filenames:
                    if needle in filename.lower():
                        return str(Path(dirpath) / filename)
        except Exception:  # noqa: BLE001 - permission errors are expected while walking
            continue
    return None


def resolve_path(name: str) -> str | None:
    """Resolve a spoken alias ('my cv folder') or a plain path."""
    from ..memory import Memory

    alias = Memory().get_alias(name)
    if alias:
        return alias
    candidate = Path(name).expanduser()
    if candidate.exists():
        return str(candidate)
    return find_first(name)
