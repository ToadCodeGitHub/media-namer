"""Applying renames (and new thumbnails) safely, with a log that can undo them.

Renames run in two phases (everything to a temporary name, then to the final
name) so swaps (A->B, B->A) and case-only changes work on Windows. New
thumbnails are written last, never over an existing file. Every step is
logged before the next one starts. If anything fails, the steps already done
are reversed automatically.

Log format (CSV): action,from,to
  move,<old path>,<new path>
  create,<frame it was made from>,<new thumbnail path>
"""
from __future__ import annotations

import csv
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from .media import make_thumbnail
from .naming import filename_problem
from .scan import MediaItem

MOVE = "move"
CREATE = "create"


@dataclass
class RenameOp:
    src: str                      # file to move, or frame JPEG to make a thumbnail from
    dst: str
    item_index: int = -1          # which MediaItem it belongs to (-1 = n/a)
    action: str = MOVE


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def thumb_frame(item: MediaItem) -> str | None:
    if not item.frames:
        return None
    i = item.thumb_choice if 0 <= item.thumb_choice < len(item.frames) else len(item.frames) // 2
    return item.frames[i]


def plan(items: Iterable[MediaItem]) -> list[RenameOp]:
    ops: list[RenameOp] = []
    for idx, item in enumerate(items):
        renaming = item.include and item.new_name and item.new_name != item.name
        stem = os.path.splitext(item.new_name)[0] if renaming else item.stem
        if renaming:
            ops.append(RenameOp(item.path, os.path.join(item.folder, item.new_name), idx))
            for comp in item.companions:
                cdst = os.path.join(os.path.dirname(comp), stem + os.path.splitext(comp)[1])
                if cdst != comp:
                    ops.append(RenameOp(comp, cdst, idx))
        if renaming and item.make_thumb and not item.companions:
            ops.append(RenameOp(thumb_frame(item) or "", os.path.join(item.folder, stem + ".jpg"),
                                idx, CREATE))
    return ops


def plan_thumbnails_only(items: Iterable[tuple[int, MediaItem]]) -> list[RenameOp]:
    """'Create thumbnail now' for videos that keep their current name."""
    return [RenameOp(thumb_frame(it) or "", os.path.join(it.folder, it.stem + ".jpg"), idx, CREATE)
            for idx, it in items if it.kind == "video" and not it.companions]


def validate(ops: list[RenameOp]) -> list[str]:
    problems: list[str] = []
    sources = {_key(op.src) for op in ops if op.action == MOVE}
    seen: dict[str, str] = {}
    for op in ops:
        name = os.path.basename(op.dst)
        label = os.path.basename(op.src) if op.action == MOVE else f"thumbnail {name}"
        why = filename_problem(name)
        if why:
            problems.append(f"{label}: {why}")
        if op.action == MOVE and not os.path.exists(op.src):
            problems.append(f"{label}: file no longer exists")
        if op.action == CREATE and not (op.src and os.path.isfile(op.src)):
            problems.append(f"{label}: no video frame available to make it from")
        k = _key(op.dst)
        if k in seen:
            problems.append(f"{name}: two files would get this name "
                            f"({os.path.basename(seen[k])} and {os.path.basename(op.src)})")
        seen[k] = op.src
        if os.path.exists(op.dst) and k not in sources:
            problems.append(f"{name}: a different file with this name already exists")
    return problems


class RenameFailed(Exception):
    pass


def apply(ops: list[RenameOp], log_path: str, progress: Callable[[int, int], None] | None = None,
          thumb_width: int = 640) -> None:
    """Do everything or nothing. Raises RenameFailed after rolling back."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    token = secrets.token_hex(4)
    moves = [op for op in ops if op.action == MOVE]
    creates = [op for op in ops if op.action == CREATE]
    done: list[tuple[str, str, str]] = []
    total = len(moves) * 2 + len(creates)
    step = 0
    with open(log_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["action", "from", "to"])
        f.flush()

        def record(action: str, a: str, b: str) -> None:
            nonlocal step
            done.append((action, a, b))
            w.writerow([action, a, b])
            f.flush()
            step += 1
            if progress:
                progress(step, total)

        try:
            temps = []
            for i, op in enumerate(moves):
                tmp = f"{op.src}.mn-{token}-{i}.tmp"
                os.rename(op.src, tmp)
                record(MOVE, op.src, tmp)
                temps.append(tmp)
            for op, tmp in zip(moves, temps):
                os.rename(tmp, op.dst)
                record(MOVE, tmp, op.dst)
            for op in creates:
                with open(op.src, "rb") as src:
                    data = make_thumbnail(src.read(), thumb_width)
                with open(op.dst, "xb") as out:          # 'x': never overwrite
                    try:
                        out.write(data)
                    except OSError:
                        out.close()
                        os.remove(op.dst)
                        raise
                record(CREATE, op.src, op.dst)
        except OSError as e:
            failed = _reverse(done)
            w.writerow(["#rolled-back", "", ""])
            note = f" Could not restore {len(failed)} file(s); see the log." if failed else ""
            raise RenameFailed(f"{e}. All changes were undone.{note}") from e


def _reverse(steps: list[tuple[str, str, str]]) -> list[str]:
    failed = []
    for action, a, b in reversed(steps):
        try:
            if action == CREATE:
                if os.path.exists(b):
                    os.remove(b)
            elif os.path.exists(b) and not os.path.exists(a):
                os.rename(b, a)
            elif not (os.path.exists(a) and not os.path.exists(b)):
                failed.append(b)
        except OSError:
            failed.append(b)
    return failed


def read_log(log_path: str) -> list[tuple[str, str, str]]:
    """Steps from a log as (action, from, to); empty if it was rolled back."""
    with open(log_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    steps = []
    for row in rows[1:]:
        if row and row[0] == "#rolled-back":
            return []
        if len(row) >= 3 and row[0] in (MOVE, CREATE):
            steps.append((row[0], row[1], row[2]))
    return steps


def undo(log_path: str) -> tuple[int, list[str]]:
    """Put things back as they were before the logged run. Returns (restored, problems)."""
    if log_is_undone(log_path):
        return 0, []
    restored = 0
    problems = []
    for action, a, b in reversed(read_log(log_path)):
        if action == CREATE:
            try:
                if os.path.exists(b):
                    os.remove(b)
                    restored += 1
            except OSError as e:
                problems.append(f"{os.path.basename(b)}: {e}")
            continue
        if os.path.exists(a) and not os.path.exists(b):
            continue                                  # already back
        if not os.path.exists(b):
            problems.append(f"{os.path.basename(b)} is missing")
            continue
        if os.path.exists(a) and _key(a) != _key(b):
            problems.append(f"{os.path.basename(a)} already exists; left {os.path.basename(b)} alone")
            continue
        try:
            os.rename(b, a)
            if not b.endswith(".tmp"):
                restored += 1
        except OSError as e:
            problems.append(f"{os.path.basename(b)}: {e}")
    if not problems:
        with open(log_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["#undone", datetime.now().isoformat(timespec="seconds"), ""])
    return restored, problems


def log_is_undone(log_path: str) -> bool:
    try:
        with open(log_path, encoding="utf-8") as f:
            return any(line.startswith(("#undone", "#rolled-back")) for line in f)
    except OSError:
        return False


def describe_log(log_path: str) -> list[str]:
    """Human-readable lines for the undo confirmation."""
    steps = read_log(log_path)
    original = {b: a for act, a, b in steps if act == MOVE and b.endswith(".tmp")}
    out = []
    for act, a, b in steps:
        if act == MOVE and not b.endswith(".tmp"):
            out.append(f"{os.path.basename(b)}  →  {os.path.basename(original.get(a, a))}")
        elif act == CREATE:
            out.append(f"delete thumbnail {os.path.basename(b)}")
    return out
