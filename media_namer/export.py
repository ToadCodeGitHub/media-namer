"""Planning 'Export web copies': originals stay untouched; new files go to another folder.

For each checked item:
  video  -> <out>/<sub>/<new name>.mp4   (re-encoded)
            <out>/<sub>/<new name>.jpg   (thumbnail: its existing image, or a chosen frame)
  photo  -> <out>/<sub>/<new name>.<ext> (copied as-is; optional)

<sub> mirrors the source folder layout (optional). Files that already exist in
the output are skipped, so an export can be re-run after adding new videos.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from .scan import MediaItem

VIDEO, THUMB, PHOTO = "video", "thumb", "photo"


@dataclass
class ExportTask:
    item_index: int
    kind: str                 # VIDEO, THUMB or PHOTO
    src: str                  # source video/photo, or image/frame the thumbnail comes from
    dst: str
    skip: bool = False        # already exists in the output folder
    date: datetime | None = None   # written into the copy if the source has no date tag


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def is_inside(child: str, parent: str) -> bool:
    c, p = _key(child), _key(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def output_stem(item: MediaItem) -> str:
    if item.include and item.new_name:
        return os.path.splitext(item.new_name)[0]
    return item.stem


def plan_export(items: list[MediaItem], src_root: str, out_root: str, *, mirror: bool = True,
                photos: bool = True, thumbs: bool = True) -> list[ExportTask]:
    tasks: list[ExportTask] = []
    used: set[str] = set()

    def target(folder: str, stem: str, ext: str, siblings: tuple[str, ...] = ()) -> tuple[str, str]:
        """Unique stem in folder (also checking sibling extensions, e.g. the .jpg next to a .mp4)."""
        n, s = 1, stem
        while True:
            keys = [_key(os.path.join(folder, s + e)) for e in (ext, *siblings)]
            if not any(k in used for k in keys):
                used.update(keys)
                return os.path.join(folder, s + ext), s
            n += 1
            s = f"{stem} ({n})"

    for idx, it in enumerate(items):
        if not it.include:
            continue
        if it.kind == "photo" and not photos:
            continue
        rel = os.path.relpath(it.folder, src_root) if mirror else "."
        folder = os.path.normpath(os.path.join(out_root, rel))
        stem = output_stem(it)
        if it.kind == "video":
            thumb_src = None
            if it.companions:                       # its own thumbnail image always comes along
                thumb_src = it.companions[0]
            elif thumbs and it.frames:
                i = it.thumb_choice if 0 <= it.thumb_choice < len(it.frames) else len(it.frames) // 2
                thumb_src = it.frames[i]
            dst, s = target(folder, stem, ".mp4", (".jpg",) if thumb_src else ())
            tasks.append(ExportTask(idx, VIDEO, it.path, dst, skip=os.path.exists(dst), date=it.date))
            if thumb_src:
                tdst = os.path.join(folder, s + ".jpg")
                tasks.append(ExportTask(idx, THUMB, thumb_src, tdst, skip=os.path.exists(tdst)))
        else:
            dst, _ = target(folder, stem, it.ext.lower())
            tasks.append(ExportTask(idx, PHOTO, it.path, dst, skip=os.path.exists(dst)))
    return tasks


def check_folders(src_root: str, out_root: str, recursive: bool) -> str | None:
    """Reason the output folder can't be used, or None."""
    if not out_root:
        return "Choose an output folder."
    if _key(out_root) == _key(src_root):
        return "The output folder must be different from the folder being scanned."
    if recursive and is_inside(out_root, src_root):
        return ("The output folder is inside the scanned folder, and 'Include subfolders' is on, so the "
                "copies would be scanned next time. Pick a folder outside it.")
    if is_inside(src_root, out_root):
        return "The output folder can't contain the folder being scanned."
    return None
