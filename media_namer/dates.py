"""Finding when a photo or video was taken.

Order of trust: camera metadata, then a date in the file name, then the file's
modified time (often the copy date, so the UI flags it).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from PIL import Image

from .media import MediaError, Tools, probe

SOURCE_CAMERA = "camera"
SOURCE_TAPE = "tape"            # recording date stamped in DV frames by the camcorder
SOURCE_FILENAME = "file name"
SOURCE_MODIFIED = "modified time"
SOURCE_MANUAL = "manual"

_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 36867
_DATETIME_DIGITIZED = 36868
_DATETIME = 306

_FILENAME_DATE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)"
)
# DV capture tools (WinDV and similar) append the tape's recording date: name.YY-MM-DD_HH-MM.SS
_DV_CAPTURE = re.compile(r"(?<!\d)(\d{2})-(\d{2})-(\d{2})_(\d{2})-(\d{2})[.\-](\d{2})(?!\d)")
_FILENAME_TIME = re.compile(r"^[-_. T]?([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d)(?:\d{3})?(?!\d)")


def _plausible(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    naive = dt.replace(tzinfo=None)
    if naive.year < 1980 or naive > datetime.now() + timedelta(days=2):
        return None
    return dt


def parse_exif(value) -> datetime | None:
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    value = str(value).strip().strip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return _plausible(datetime.strptime(value[:19] if "%S" in fmt else value[:16], fmt))
        except ValueError:
            continue
    return None


def photo_date(path: str) -> datetime | None:
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            sub = exif.get_ifd(_EXIF_IFD)
            for tag in (_DATETIME_ORIGINAL, _DATETIME_DIGITIZED):
                dt = parse_exif(sub.get(tag)) if sub.get(tag) else None
                if dt:
                    return dt
            if exif.get(_DATETIME):
                return parse_exif(exif.get(_DATETIME))
    except Exception:  # noqa: BLE001
        return None
    return None


def parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)  # UTC -> this computer's local time
    return _plausible(dt)


def video_date(info: dict) -> datetime | None:
    fmt_tags = {k.lower(): v for k, v in info.get("format", {}).get("tags", {}).items()}
    # iPhones store local time with offset here; it's more accurate than creation_time.
    for key in ("com.apple.quicktime.creationdate", "creation_time", "date"):
        if key in fmt_tags:
            dt = parse_iso(fmt_tags[key])
            if dt:
                return dt
    for stream in info.get("streams", []):
        tags = {k.lower(): v for k, v in stream.get("tags", {}).items()}
        if "creation_time" in tags:
            dt = parse_iso(tags["creation_time"])
            if dt:
                return dt
    return None


def filename_date(name: str) -> datetime | None:
    stem = os.path.splitext(os.path.basename(name))[0]
    m = _FILENAME_DATE.search(stem)
    if not m:
        dv = _DV_CAPTURE.search(stem)
        if not dv:
            return None
        yy, mo, d, hh, mi, ss = (int(g) for g in dv.groups())
        year = 2000 + yy if yy < 80 else 1900 + yy       # DV camcorders: 1995 onwards
        try:
            return _plausible(datetime(year, mo, d, hh, mi, ss))
        except ValueError:
            return None
    y, mo, d = (int(g) for g in m.groups())
    hh = mm = ss = 0
    t = _FILENAME_TIME.match(stem[m.end():])
    if t:
        hh, mm, ss = (int(g) for g in t.groups())
    try:
        return _plausible(datetime(y, mo, d, hh, mm, ss))
    except ValueError:
        return None


def find_date(tools: Tools, path: str, kind: str, info: dict | None = None) -> tuple[datetime | None, str]:
    dt = None
    if kind == "photo":
        dt = photo_date(path)
    elif kind == "video" and tools.ffprobe:
        try:
            info = info if info is not None else probe(tools, path)
            if any(s.get("codec_name") == "dvvideo" for s in info.get("streams", [])):
                from .dvtape import recording_date
                tape = recording_date(tools, path)
                if tape:
                    return tape, SOURCE_TAPE
            dt = video_date(info)
        except MediaError:
            dt = None
    if dt:
        return dt, SOURCE_CAMERA
    dt = filename_date(path)
    if dt:
        return dt, SOURCE_FILENAME
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)), SOURCE_MODIFIED
    except OSError:
        return None, ""
