"""Working out the proposed new name for every item, without collisions."""
from __future__ import annotations

import csv
import os

from .naming import DEFAULT_DATE_FORMAT, DEFAULT_TEMPLATE, build_stem, format_date, with_counter
from .scan import MediaItem


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def recompute(items: list[MediaItem], template: str = DEFAULT_TEMPLATE,
              date_format: str = DEFAULT_DATE_FORMAT) -> None:
    """Fill item.new_name for items with a description; add ' (2)' etc. on clashes.

    A clash is another item's proposed name, or an existing file that isn't
    itself being renamed. Names typed by hand are kept as-is (the final check
    before renaming reports any clash).
    """
    sources = set()                       # files that will move out of the way
    for it in items:
        if it.include and (it.description or (it.name_overridden and it.new_name)):
            sources.add(_key(it.path))
            sources.update(_key(c) for c in it.companions)

    used: set[str] = set()
    for it in items:                      # reserve hand-typed names first
        if it.include and it.name_overridden and it.new_name:
            used.add(_key(os.path.join(it.folder, it.new_name)))

    for it in items:
        if it.name_overridden:
            continue
        if not it.description:
            it.new_name = ""
            continue
        stem = build_stem(template, format_date(it.date, date_format), it.description, it.stem)
        ext = it.ext.lower()
        n = 1
        candidate = stem
        while True:
            names = [candidate + ext] + [candidate + os.path.splitext(c)[1] for c in it.companions]
            if it.make_thumb:
                names.append(candidate + ".jpg")
            keys = [_key(os.path.join(it.folder, nm)) for nm in names]
            own = {_key(it.path), *(_key(c) for c in it.companions)}
            clash = any(k in used and k not in own for k in keys) or any(
                os.path.exists(os.path.join(it.folder, nm)) and k not in sources
                for nm, k in zip(names, keys))
            if not clash:
                break
            n += 1
            candidate = with_counter(stem, n)
        it.new_name = candidate + ext
        if it.include:
            used.update(keys)


CSV_FIELDS = ["path", "include", "date", "date_source", "description", "new_name"]


def export_csv(items: list[MediaItem], path: str, date_format: str = DEFAULT_DATE_FORMAT) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        for it in items:
            w.writerow([it.path, "yes" if it.include else "no",
                        it.date.strftime("%Y-%m-%d %H:%M:%S") if it.date else "",
                        it.date_source, it.description, it.new_name])


def import_csv(items: list[MediaItem], path: str) -> int:
    """Apply edits from a CSV (matched by path). Returns how many rows matched."""
    from datetime import datetime
    from .dates import SOURCE_MANUAL

    by_path = {_key(it.path): it for it in items}
    matched = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            it = by_path.get(_key(row.get("path", "")))
            if not it:
                continue
            matched += 1
            inc = (row.get("include") or "").strip().lower()
            if inc:
                it.include = inc in ("yes", "y", "true", "1", "x")
            d = (row.get("date") or "").strip()
            if d:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
                    try:
                        new = datetime.strptime(d, fmt)
                    except ValueError:
                        continue
                    if not it.date or new.date() != it.date.date():
                        it.date, it.date_source = new, SOURCE_MANUAL
                    break
            if row.get("description") is not None:
                it.description = row["description"].strip()
            name = (row.get("new_name") or "").strip()
            if name and name != it.new_name:
                it.new_name, it.name_overridden = name, True
    return matched
