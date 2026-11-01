"""File types, ffprobe/ffmpeg access, and image preparation."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys

from PIL import Image, ImageOps

try:  # Optional: lets Pillow open iPhone .heic photos.
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".3gp", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif"}

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class MediaError(Exception):
    pass


class Tools:
    """Locations of ffmpeg and ffprobe. Blank means 'look on PATH'."""

    def __init__(self, ffmpeg: str = "", ffprobe: str = ""):
        self.ffmpeg = self._find(ffmpeg, "ffmpeg")
        self.ffprobe = self._find(ffprobe, "ffprobe")

    @staticmethod
    def _find(configured: str, name: str) -> str | None:
        if configured:
            return configured if os.path.isfile(configured) else None
        found = shutil.which(name)
        if found:
            return found
        # Installs that aren't on this process's PATH yet (e.g. winget ran after VS Code started).
        for cand in Tools._windows_candidates(name):
            if os.path.isfile(cand):
                return cand
        return None

    @staticmethod
    def _windows_candidates(name: str) -> list[str]:
        import glob
        exe = name + ".exe"
        local = os.environ.get("LOCALAPPDATA", "")
        home = os.environ.get("USERPROFILE", "")
        out = []
        if local:
            winget = os.path.join(local, "Microsoft", "WinGet")
            out.append(os.path.join(winget, "Links", exe))
            out += sorted(glob.glob(os.path.join(winget, "Packages", "*FFmpeg*", "*", "bin", exe)), reverse=True)
            out += sorted(glob.glob(os.path.join(winget, "Packages", "*FFmpeg*", "bin", exe)), reverse=True)
        if home:
            out.append(os.path.join(home, "scoop", "shims", exe))
        out += [os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "chocolatey", "bin", exe),
                os.path.join(r"C:\ffmpeg", "bin", exe),
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin", exe)]
        return out

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)


def kind_of(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "photo"
    return None


def probe(tools: Tools, path: str) -> dict:
    if not tools.ffprobe:
        raise MediaError("ffprobe not found")
    try:
        out = subprocess.run(
            [tools.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, timeout=60, creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as e:
        raise MediaError("ffprobe timed out") from e
    if out.returncode != 0:
        raise MediaError(out.stderr.decode(errors="replace").strip() or "ffprobe failed")
    return json.loads(out.stdout.decode(errors="replace") or "{}")


def duration_of(info: dict) -> float:
    try:
        return float(info.get("format", {}).get("duration") or 0)
    except ValueError:
        return 0.0


def video_frames(tools: Tools, path: str, count: int = 3, size: int = 768,
                 info: dict | None = None) -> list[bytes]:
    """JPEG frames spread across the video (start, middle, end)."""
    if not tools.ffmpeg:
        raise MediaError("ffmpeg not found")
    info = info if info is not None else probe(tools, path)
    dur = duration_of(info)
    if dur > 2 and count > 1:
        times = [dur * (0.15 + 0.7 * i / (count - 1)) for i in range(count)]
    elif dur > 2:
        times = [dur / 2]
    else:
        times = [0.0]
    frames = []
    for t in times:
        frame = _grab_frame(tools, path, t, size)
        if frame:
            frames.append(frame)
    if not frames and times != [0.0]:
        frame = _grab_frame(tools, path, 0.0, size)
        if frame:
            frames.append(frame)
    if not frames:
        raise MediaError("could not read any frames from the video")
    return frames


def _grab_frame(tools: Tools, path: str, t: float, size: int) -> bytes | None:
    try:
        out = subprocess.run(
            [tools.ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
             "-vf", f"scale=w={size}:h={size}:force_original_aspect_ratio=decrease",
             "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-"],
            capture_output=True, timeout=60, creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return None
    data = out.stdout
    return data if out.returncode == 0 and data[:2] == b"\xff\xd8" else None


def photo_jpeg(path: str, size: int = 1024) -> bytes:
    """Photo as an upright, downscaled JPEG (smaller uploads, faster models)."""
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((size, size))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            return buf.getvalue()
    except Exception as e:  # noqa: BLE001 - Pillow raises many types
        hint = " (install pillow-heif for HEIC)" if path.lower().endswith((".heic", ".heif")) and not HEIC_SUPPORT else ""
        raise MediaError(f"could not open image: {e}{hint}") from e


def images_for(tools: Tools, path: str, kind: str, frames: int = 3) -> list[bytes]:
    if kind == "video":
        return video_frames(tools, path, count=frames)
    return [photo_jpeg(path)]


def preview_image(tools: Tools, path: str, kind: str) -> bytes:
    if kind == "video":
        return video_frames(tools, path, count=1, size=640)[0]
    return photo_jpeg(path, size=640)


# ----------------------------------------------------------------- thumbnails

def score_frame(jpeg: bytes) -> float:
    """Higher is a better thumbnail: well exposed, contrasty, and sharp.

    Black/white fades and motion-blurred frames score low.
    """
    from PIL import ImageFilter, ImageStat
    with Image.open(io.BytesIO(jpeg)) as im:
        g = im.convert("L")
        g.thumbnail((320, 320))
        stat = ImageStat.Stat(g)
        mean, std = stat.mean[0], stat.stddev[0]
        edges = ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).mean[0]
    exposure = 1.0 - min(1.0, abs(mean - 120) / 120)       # 1 at mid-grey, 0 at black/white
    if mean < 18 or mean > 240:
        exposure *= 0.1
    return exposure * (std + 1) * (edges + 1)


def best_frame(frames: list[bytes]) -> int:
    if not frames:
        return -1
    scores = [score_frame(f) for f in frames]
    return max(range(len(frames)), key=scores.__getitem__)


def make_thumbnail(jpeg: bytes, width: int = 640) -> bytes:
    with Image.open(io.BytesIO(jpeg)) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85, optimize=True, progressive=True)
        return buf.getvalue()
