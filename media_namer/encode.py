"""Re-encoding videos into web-friendly MP4 (H.264 + AAC, faststart).

What "web-friendly" means here:
  * H.264 High profile, 8-bit 4:2:0 (yuv420p): plays in every browser and phone.
    Raw/pro footage is often 10-bit or 4:2:2, which browsers can't play.
  * AAC stereo audio (PCM from cameras isn't playable in browsers).
  * moov atom at the front (+faststart), so playback starts before the download finishes.
  * Optional downscale (never upscale), keeping portrait videos portrait.
  * HDR (HLG / PQ from phones) tone-mapped to SDR, so it doesn't look washed out.
  * Camera metadata (creation_time) kept, so dates still work on the copies.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .media import MediaError, Tools, duration_of, probe

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

PRESETS = ["veryfast", "faster", "fast", "medium", "slow"]
SIZES = [("Original size", 0), ("4K (2160p)", 2160), ("1440p", 1440), ("1080p", 1080), ("720p", 720)]
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}


class Cancelled(Exception):
    pass


@dataclass
class EncodeOptions:
    max_height: int = 1080          # short side limit; 0 = keep original size
    crf: int = 23                   # 18 = near-transparent, 23 = good default, 28 = small
    preset: str = "veryfast"
    audio_kbps: int = 160


_filter_cache: dict[str, set[str]] = {}


def available_filters(tools: Tools) -> set[str]:
    if tools.ffmpeg not in _filter_cache:
        try:
            out = subprocess.run([tools.ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                                 timeout=30, creationflags=_NO_WINDOW).stdout.decode(errors="replace")
            names = {ln.split()[1] for ln in out.splitlines() if len(ln.split()) > 2 and "->" in ln}
        except (OSError, subprocess.TimeoutExpired):
            names = set()
        _filter_cache[tools.ffmpeg] = names
    return _filter_cache[tools.ffmpeg]


def _video_stream(info: dict) -> dict | None:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            return s
    return None


def _has_audio(info: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


def is_hdr(info: dict) -> bool:
    v = _video_stream(info) or {}
    return v.get("color_transfer") in HDR_TRANSFERS


def is_interlaced(info: dict) -> bool:
    """DV tape, broadcast and many camcorder recordings are interlaced (combing on motion)."""
    v = _video_stream(info) or {}
    if v.get("field_order") in ("tt", "bb", "tb", "bt"):
        return True
    # DV-in-AVI doesn't record field order ("unknown"), but camcorder DV is interlaced.
    return v.get("codec_name") == "dvvideo" and v.get("field_order") != "progressive"


def non_square_pixels(info: dict) -> bool:
    """DV and DVD video use non-square pixels (e.g. 720x480 shown as 4:3)."""
    sar = (_video_stream(info) or {}).get("sample_aspect_ratio") or "1:1"
    return sar not in ("1:1", "0:1", "N/A")


def video_filter(opts: EncodeOptions, hdr: bool, can_tonemap: bool,
                 interlaced: bool = False, square: bool = False) -> str:
    """Filter chain. ffmpeg auto-rotates first, so iw/ih are the upright size here."""
    chain = []
    if interlaced:
        chain.append("bwdif=mode=send_frame:deint=interlaced")   # deinterlace flagged frames, keep frame rate
    if square:
        chain.append("scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1")   # square pixels for browsers
    if hdr and can_tonemap:
        chain.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                     "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")
    m = opts.max_height
    if m:
        # Limit the short side (landscape: height, portrait: width); even sizes for 4:2:0.
        chain.append(f"scale=w='if(gte(iw,ih),-2,trunc(min(iw,{m})/2)*2)'"
                     f":h='if(gte(iw,ih),trunc(min(ih,{m})/2)*2,-2)'")
    else:
        chain.append("scale=w='trunc(iw/2)*2':h='trunc(ih/2)*2'")
    chain.append("format=yuv420p")
    return ",".join(chain)


def _has_date_tag(info: dict) -> bool:
    tags = {k.lower() for k in info.get("format", {}).get("tags", {})}
    return bool(tags & {"creation_time", "com.apple.quicktime.creationdate", "date"})


def build_command(tools: Tools, src: str, dst: str, info: dict, opts: EncodeOptions,
                  creation_time: "datetime | None" = None) -> list[str]:
    hdr = is_hdr(info)
    can_tonemap = {"zscale", "tonemap"} <= available_filters(tools)
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-i", src,
           "-map", "0:v:0", "-map", "0:a:0?",          # first video + first audio only (skip timecode/data)
           "-map_metadata", "0",                        # keep creation_time etc.
           *(["-metadata", "creation_time=" + creation_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")]
             if creation_time and not _has_date_tag(info) else []),   # e.g. DV captures with the date only in the name
           "-vf", video_filter(opts, hdr, can_tonemap, is_interlaced(info), non_square_pixels(info)),
           "-c:v", "libx264", "-preset", opts.preset, "-crf", str(opts.crf),
           "-profile:v", "high", "-pix_fmt", "yuv420p"]
    if hdr and can_tonemap:
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if _has_audio(info):
        cmd += ["-c:a", "aac", "-b:a", f"{opts.audio_kbps}k", "-ac", "2"]
    cmd += ["-movflags", "+faststart", "-write_tmcd", "0",    # no timecode track (cameras add one)
            "-progress", "pipe:1", "-nostats", "-f", "mp4", dst]
    return cmd


def encode(tools: Tools, src: str, dst: str, opts: EncodeOptions,
           creation_time: "datetime | None" = None,
           progress: Callable[[float], None] | None = None,
           cancel: threading.Event | None = None) -> dict:
    """Encode src to dst. Writes dst.part first, so dst only ever appears complete.

    Returns a small report: {"hdr": bool, "tonemapped": bool}.
    Raises MediaError on failure, Cancelled if cancel is set.
    """
    if not tools.ok:
        raise MediaError("ffmpeg not found")
    if os.path.exists(dst):
        raise MediaError(f"{os.path.basename(dst)} already exists")
    info = probe(tools, src)
    if _video_stream(info) is None:
        raise MediaError("no video stream")
    duration = duration_of(info)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    part = dst + ".part"
    cmd = build_command(tools, src, part, info, opts, creation_time)
    err = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                creationflags=_NO_WINDOW)
        assert proc.stdout is not None
        for raw in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                break
            line = raw.decode(errors="replace").strip()
            if progress and duration > 0 and line.startswith("out_time_us="):
                try:
                    progress(min(0.999, int(line.split("=", 1)[1]) / 1e6 / duration))
                except ValueError:
                    pass
        proc.wait()
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if proc.returncode != 0:
            err.seek(0)
            msg = err.read().decode(errors="replace").strip().splitlines()
            raise MediaError("ffmpeg failed: " + (msg[-1] if msg else f"exit code {proc.returncode}"))
        os.replace(part, dst)
        if progress:
            progress(1.0)
        hdr = is_hdr(info)
        return {"hdr": hdr, "tonemapped": hdr and {"zscale", "tonemap"} <= available_filters(tools)}
    finally:
        err.close()
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
