"""MiniDV tools: read the recording date stamped in every DV frame, and split captures by recording.

A camcorder writes the date and time into each DV frame (VAUX and subcode
"packs" 0x62 = date, 0x63 = time). ffprobe doesn't show them, so they're read
here directly. That's how WinDV-style tools split a tape into one file per
recording and name each file by when it was shot.

DV frame = 10 (NTSC) or 12 (PAL) DIF sequences x 150 blocks x 80 bytes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import BinaryIO, Callable, Iterator

from .media import MediaError, Tools

NTSC_FRAME = 120_000
PAL_FRAME = 144_000
BLOCK = 80
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

SCENE_GAP = timedelta(seconds=3)   # a jump bigger than this (or backwards) starts a new recording


class Cancelled(Exception):
    pass


def frame_size(header: bytes) -> int:
    """Frame size from the first DIF block (header section): DSF bit set means PAL."""
    if len(header) < 4 or (header[0] >> 5) != 0:
        raise MediaError("not a DV stream (no DIF header block)")
    return PAL_FRAME if header[3] & 0x80 else NTSC_FRAME


def _bcd(b: int, mask: int) -> int | None:
    b &= mask
    hi, lo = b >> 4, b & 0x0F
    return None if hi > 9 or lo > 9 else hi * 10 + lo


def _packs(frame: bytes) -> Iterator[bytes]:
    """5-byte packs from the VAUX and subcode blocks of the first two DIF sequences."""
    seq = 150 * BLOCK
    for s in range(2):
        base = s * seq
        for blk in range(1, 6):                       # blocks 1-2 subcode, 3-5 VAUX
            b = frame[base + blk * BLOCK: base + (blk + 1) * BLOCK]
            if len(b) < BLOCK:
                return
            kind = b[0] >> 5
            if kind == 2:                             # VAUX: 15 packs from byte 3
                for off in range(3, 78, 5):
                    yield b[off:off + 5]
            elif kind == 1:                           # subcode: 6 sync blocks, pack at +3 of each 8
                for i in range(6):
                    off = 3 + i * 8 + 3
                    yield b[off:off + 5]


def recording_datetime(frame: bytes) -> datetime | None:
    """The date and time this frame was recorded, or None if the camera didn't stamp it."""
    date = time = None
    for pk in _packs(frame):
        if len(pk) < 5:
            continue
        if pk[0] == 0x62 and date is None:
            d, m, y = _bcd(pk[2], 0x3F), _bcd(pk[3], 0x1F), _bcd(pk[4], 0xFF)
            if d and m and y is not None and 1 <= m <= 12 and 1 <= d <= 31:
                date = (2000 + y if y < 80 else 1900 + y, m, d)
        elif pk[0] == 0x63 and time is None:
            s, mi, h = _bcd(pk[2], 0x7F), _bcd(pk[3], 0x7F), _bcd(pk[4], 0x3F)
            if s is not None and mi is not None and h is not None and s < 60 and mi < 60 and h < 24:
                time = (h, mi, s)
        if date and time:
            break
    if not date:
        return None
    try:
        return datetime(*date, *(time or (0, 0, 0)))
    except ValueError:
        return None


# ----------------------------------------------------------------- reading frames

def _raw_dv_process(tools: Tools, path: str) -> subprocess.Popen:
    """Untouched DV frames from any container (AVI type 1/2, MOV, .dv) on stdout.

    Uses the rawvideo muxer, not the dv muxer: the dv muxer rewrites the
    date packs, which would destroy what we're trying to read.
    """
    if not tools.ffmpeg:
        raise MediaError("ffmpeg not found")
    return subprocess.Popen(
        [tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", path,
         "-map", "0:v:0", "-c", "copy", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW, bufsize=1 << 20)


def iter_frames(stream: BinaryIO) -> Iterator[bytes]:
    head = stream.read(BLOCK)
    if not head:
        return
    size = frame_size(head)
    rest = stream.read(size - len(head))
    frame = head + rest
    while len(frame) == size:
        yield frame
        frame = stream.read(size)


def open_frames(tools: Tools, path: str) -> tuple[Iterator[bytes], Callable[[], None]]:
    """(frame iterator, close function) for a DV file."""
    if path.lower().endswith(".dv"):
        f = open(path, "rb")
        return iter_frames(f), f.close
    proc = _raw_dv_process(tools, path)

    def close():
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    return iter_frames(proc.stdout), close


def recording_date(tools: Tools, path: str, max_frames: int = 300) -> datetime | None:
    """First recording date found in the file (looks at up to ~10 seconds)."""
    try:
        frames, close = open_frames(tools, path)
    except (OSError, MediaError):
        return None
    try:
        for i, fr in enumerate(frames):
            dt = recording_datetime(fr)
            if dt:
                return dt
            if i >= max_frames:
                break
    except (OSError, MediaError):
        return None
    finally:
        close()
    return None


# ----------------------------------------------------------------- splitting into recordings

@dataclass
class Scene:
    start: datetime | None
    frames: int = 0
    path: str = ""


def is_new_scene(prev: datetime | None, cur: datetime | None) -> bool:
    if cur is None:
        return False                        # unstamped frames stay with the current recording
    if prev is None:
        return True
    return cur < prev or cur - prev > SCENE_GAP


def scene_name(prefix: str, start: datetime | None, n: int) -> str:
    if start is None:
        return f"{prefix}.undated-{n:03d}"
    return f"{prefix}.{start:%y-%m-%d_%H-%M.%S}"     # WinDV style; Media Namer reads this date


class _Writer:
    """Pipes DV frames into ffmpeg, which wraps them in a type-2 AVI (DV video + PCM audio)."""

    def __init__(self, tools: Tools, dst: str, start: datetime | None):
        self.dst, self.part = dst, dst + ".part"
        meta = ["-metadata", f"date={start:%Y-%m-%dT%H:%M:%S}"] if start else []
        self.proc = subprocess.Popen(
            [tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "dv", "-i", "-",
             "-map", "0:v:0", "-map", "0:a?", "-c", "copy", *meta, "-f", "avi", self.part],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW)

    def write(self, frame: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(frame)

    def finish(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        err = self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""
        if self.proc.wait() != 0:
            self.abort()
            raise MediaError(f"writing {os.path.basename(self.dst)} failed: {err.strip()[-200:]}")
        os.replace(self.part, self.dst)

    def abort(self) -> None:
        if self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except OSError:
                pass
            self.proc.kill()
            self.proc.wait()
        if os.path.exists(self.part):
            os.remove(self.part)


def _unique(path: str) -> str:
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        n += 1
        path = f"{stem} ({n}){ext}"
    return path


def split_frames(tools: Tools, frames: Iterator[bytes], out_dir: str, prefix: str,
                 progress: Callable[[int, list[Scene]], None] | None = None,
                 cancel: threading.Event | None = None, min_frames: int = 15) -> list[Scene]:
    """Write one AVI per recording. Recordings shorter than min_frames are merged into the previous one."""
    os.makedirs(out_dir, exist_ok=True)
    scenes: list[Scene] = []
    writer: _Writer | None = None
    prev: datetime | None = None
    count = 0
    try:
        for fr in frames:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            dt = recording_datetime(fr)
            start_new = writer is None or (is_new_scene(prev, dt) and scenes[-1].frames >= min_frames)
            if start_new:
                if writer is not None:
                    writer.finish()
                scene = Scene(dt)
                scene.path = _unique(os.path.join(out_dir, scene_name(prefix, dt, len(scenes) + 1) + ".avi"))
                writer = _Writer(tools, scene.path, dt)
                scenes.append(scene)
            writer.write(fr)
            scenes[-1].frames += 1
            if dt is not None:
                prev = dt
                if scenes[-1].start is None:
                    scenes[-1].start = dt
            count += 1
            if progress and count % 30 == 0:
                progress(count, scenes)
        if writer is not None:
            writer.finish()
            writer = None
    except BaseException:
        if writer is not None:
            writer.abort()
        raise
    if progress:
        progress(count, scenes)
    return scenes


def split_file(tools: Tools, path: str, out_dir: str, prefix: str | None = None,
               progress: Callable[[int, list[Scene]], None] | None = None,
               cancel: threading.Event | None = None) -> list[Scene]:
    """Split an existing capture into one file per recording. The original is not changed."""
    prefix = prefix or os.path.splitext(os.path.basename(path))[0].split(".")[0]
    frames, close = open_frames(tools, path)
    try:
        return split_frames(tools, frames, out_dir, prefix, progress, cancel)
    finally:
        close()
