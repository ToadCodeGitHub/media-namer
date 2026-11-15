"""Finding media in a folder and pairing videos with their thumbnail images."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import dates
from .media import MediaError, Tools, kind_of, probe
from .naming import ALREADY_NAMED

STATUS_NEW = ""
STATUS_ALREADY = "already named"
STATUS_QUEUED = "queued"
STATUS_WORKING = "describing…"
STATUS_DONE = "described"
STATUS_CACHED = "described (cached)"
STATUS_ERROR = "error"
STATUS_RENAMED = "renamed"


@dataclass
class MediaItem:
    path: str
    kind: str                                     # "video" or "photo"
    companions: list[str] = field(default_factory=list)   # thumbnails that move with a video
    date: datetime | None = None
    date_source: str = ""
    description: str = ""
    new_name: str = ""
    name_overridden: bool = False
    include: bool = True
    status: str = STATUS_NEW
    error: str = ""
    size: int = 0
    mtime_ns: int = 0
    make_thumb: bool = False                      # create <new name>.jpg from a frame when renaming
    frames: list[str] = field(default_factory=list)   # cached frame JPEGs (videos)
    thumb_choice: int = -1                        # which frame becomes the thumbnail
    people: list[str] = field(default_factory=list)   # names found by face recognition

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1]


def list_media(folder: str, recursive: bool) -> list[MediaItem]:
    """All photos and videos, with same-named images attached to their video."""
    items: list[MediaItem] = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not d.startswith("$"))
        videos: dict[str, MediaItem] = {}
        photos: list[str] = []
        for name in sorted(files, key=str.casefold):
            if name.startswith("."):
                continue
            path = os.path.join(root, name)
            kind = kind_of(name)
            if kind == "video":
                stem = os.path.splitext(name)[0].casefold()
                item = MediaItem(path=path, kind="video")
                videos.setdefault(stem, item)
                items.append(item)
            elif kind == "photo":
                photos.append(path)
        for path in photos:
            stem = os.path.splitext(os.path.basename(path))[0].casefold()
            if stem in videos:
                videos[stem].companions.append(path)
            else:
                items.append(MediaItem(path=path, kind="photo"))
        if not recursive:
            break
    items.sort(key=lambda i: i.path.casefold())
    for item in items:
        try:
            st = os.stat(item.path)
            item.size, item.mtime_ns = st.st_size, st.st_mtime_ns
        except OSError:
            pass
        if ALREADY_NAMED.match(item.name):
            item.include = False
            item.status = STATUS_ALREADY
    return items


def fill_dates(tools: Tools, items: list[MediaItem],
               progress: Callable[[int, int], None] | None = None,
               cancelled: Callable[[], bool] = lambda: False) -> None:
    for i, item in enumerate(items):
        if cancelled():
            return
        info = None
        if item.kind == "video" and tools.ffprobe:
            try:
                info = probe(tools, item.path)
            except MediaError:
                info = {}
        item.date, item.date_source = dates.find_date(tools, item.path, item.kind, info)
        if progress:
            progress(i + 1, len(items))
