"""Turning AI text and dates into safe, consistent file names."""
from __future__ import annotations

import os
import re
from datetime import datetime

# Characters Windows forbids in file names, plus control characters.
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_PREFIX = re.compile(r"^(title|description|filename|caption|answer)\s*[:\-]\s*", re.I)

# A file that already starts with YYYY-MM-DD is treated as already named.
ALREADY_NAMED = re.compile(r"^(19|20)\d{2}-\d{2}-\d{2}[ _]")

DEFAULT_TEMPLATE = "{date} {desc}"
DEFAULT_DATE_FORMAT = "%Y-%m-%d"
MAX_STEM = 120


def clean_description(text: str | None, max_words: int = 10, max_len: int = 70) -> str:
    """Normalize a model reply into a short sentence-case phrase usable in a file name."""
    if not text:
        return ""
    line = next((ln for ln in text.strip().splitlines() if ln.strip()), "")
    line = line.strip().strip("\"'`*#").strip()
    line = _PREFIX.sub("", line)
    line = line.replace("_", " ")
    if " " not in line and "-" in line:          # slug style: golden-retriever-fetch
        line = line.replace("-", " ")
    line = _INVALID.sub(" ", line)
    line = re.sub(r"\s+", " ", line).strip()
    line = re.sub(r"[\s.,;:!?]+$", "", line)       # trailing punctuation
    line = line.strip("\"'`*")
    words = line.split(" ")[:max_words]
    line = " ".join(words)
    if len(line) > max_len:
        cut = line[:max_len]
        line = cut.rsplit(" ", 1)[0] if " " in cut else cut
    line = line.strip(" .-")
    if line:
        line = line[0].upper() + line[1:]
    return line


def tidy_user_text(text: str) -> str:
    """Light cleanup for text typed by hand: keep the wording, drop illegal characters."""
    text = _INVALID.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip(" .")


def format_date(dt: datetime | None, fmt: str = DEFAULT_DATE_FORMAT) -> str:
    return dt.strftime(fmt) if dt else ""


def build_stem(template: str, date_str: str, desc: str, orig_stem: str) -> str:
    """Fill the template ({date}, {desc}, {orig}) and make the result safe."""
    stem = (template or DEFAULT_TEMPLATE)
    stem = stem.replace("{date}", date_str).replace("{desc}", desc).replace("{orig}", orig_stem)
    stem = _INVALID.sub(" ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if len(stem) > MAX_STEM:
        stem = stem[:MAX_STEM].rstrip(" .")
    if stem.split(".")[0].upper() in _RESERVED:
        stem = "_" + stem
    return stem


def filename_problem(name: str) -> str | None:
    """Return a reason the file name is unusable on Windows, or None if it's fine."""
    if not name or not name.strip():
        return "name is empty"
    if _INVALID.search(name):
        return 'name contains one of  < > : " / \\ | ? *'
    if name != name.rstrip(" ."):
        return "name ends with a space or dot"
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if not stem.strip() or name.startswith("."):
        return "name has no text before the extension"
    if stem.split(".")[0].upper() in _RESERVED:
        return "name is reserved by Windows"
    if len(name) > 200:
        return "name is too long"
    return None


def with_counter(stem: str, n: int) -> str:
    return f"{stem} ({n})"
