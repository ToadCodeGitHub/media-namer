"""Export web copies: originals untouched, encoded copies elsewhere."""
import hashlib
import os
import subprocess
import threading

import pytest

from media_namer import encode, export, proposals, renamer, scan
from media_namer.media import Tools, probe
from tests.test_core import item, needs_ffmpeg, touch

TOOLS = Tools()


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def moov_first(p):
    data = open(p, "rb").read(1 << 16)
    i, mo, md = 0, None, None
    while i + 8 <= len(data):
        size, typ = int.from_bytes(data[i:i + 4], "big"), data[i + 4:i + 8]
        if typ == b"moov" and mo is None:
            mo = i
        if typ == b"mdat" and md is None:
            md = i
        if size < 8:
            break
        i += size
    return mo is not None and (md is None or mo < md)


def raw_camera_clip(path, size="3840x2160", seconds=2, creation="2026-10-25T20:00:00Z"):
    """Like camera/raw footage: ProRes 4:2:2 10-bit, 24-bit PCM, timecode track."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:d={seconds}:r=30",
                    "-f", "lavfi", "-i", f"sine=f=440:d={seconds}:sample_rate=48000",
                    "-c:v", "prores_ks", "-profile:v", "1", "-pix_fmt", "yuv422p10le", "-c:a", "pcm_s24le",
                    "-timecode", "01:00:00:00", "-metadata", f"creation_time={creation}", str(path)], check=True)


@needs_ffmpeg
def test_encode_raw_4k_to_web_1080(tmp_path):
    src = tmp_path / "C0001.MOV"
    raw_camera_clip(src, size="2560x1440")
    before = sha(src)
    dst = tmp_path / "out" / "2026-10-25 Robot runs.mp4"
    pct = []
    encode.encode(TOOLS, str(src), str(dst), encode.EncodeOptions(max_height=1080), progress=pct.append)
    info = probe(TOOLS, str(dst))
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert (v["codec_name"], v["profile"], v["pix_fmt"], v["width"], v["height"]) == ("h264", "High", "yuv420p", 1920, 1080)
    assert [s["codec_name"] for s in a] == ["aac"] and a[0]["channels"] == 2
    assert len(info["streams"]) == 2                                   # timecode track dropped
    assert moov_first(dst)
    assert info["format"]["tags"]["creation_time"].startswith("2026-10-25T20:00:00")
    assert pct and pct[-1] == 1.0
    assert sha(src) == before and not os.path.exists(str(dst) + ".part")


@needs_ffmpeg
def test_portrait_hdr_is_upright_and_tonemapped(tmp_path):
    tmp = tmp_path / "tmp.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:d=1:r=30",
                    "-c:v", "prores_ks", "-pix_fmt", "yuv422p10le", "-color_primaries", "bt2020",
                    "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc", str(tmp)], check=True)
    src = tmp_path / "IMG_0001.MOV"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation:v:0", "90", "-i", str(tmp),
                    "-c", "copy", str(src)], check=True)
    dst = tmp_path / "o.mp4"
    rep = encode.encode(TOOLS, str(src), str(dst), encode.EncodeOptions(max_height=720))
    v = next(s for s in probe(TOOLS, str(dst))["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (720, 1280) and v["pix_fmt"] == "yuv420p"
    if rep["tonemapped"]:
        assert v.get("color_transfer") == "bt709"


@needs_ffmpeg
def test_small_and_odd_sizes_are_not_enlarged(tmp_path):
    src = tmp_path / "small.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=641x361:d=1:r=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv444p", str(src)], check=True)
    dst = tmp_path / "o.mp4"
    encode.encode(TOOLS, str(src), str(dst), encode.EncodeOptions(max_height=1080))
    v = next(s for s in probe(TOOLS, str(dst))["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["pix_fmt"]) == (640, 360, "yuv420p")


@needs_ffmpeg
def test_cancel_leaves_nothing_behind(tmp_path):
    src = tmp_path / "long.mov"
    raw_camera_clip(src, size="1280x720", seconds=6)
    dst = tmp_path / "o.mp4"
    cancel = threading.Event()
    with pytest.raises(encode.Cancelled):
        encode.encode(TOOLS, str(src), str(dst), encode.EncodeOptions(preset="medium"),
                      progress=lambda p: p > 0.05 and cancel.set(), cancel=cancel)
    assert not dst.exists() and not os.path.exists(str(dst) + ".part")


def test_encode_refuses_to_overwrite(tmp_path):
    touch(tmp_path / "o.mp4")
    touch(tmp_path / "s.mov")
    with pytest.raises(Exception, match="already exists"):
        encode.encode(TOOLS, str(tmp_path / "s.mov"), str(tmp_path / "o.mp4"), encode.EncodeOptions())


def test_plan_export(tmp_path):
    src = tmp_path / "src"
    a = item(src, "C0001.MOV", "Robot runs", companions=["C0001.jpg"])
    b = item(src / "trip", "C0002.MOV", "Robot runs")                   # different subfolder
    c = item(src, "C0003.MOV", "Robot runs")                              # same name as a -> (2)
    p = item(src, "DSC1.JPG", "Ostrich")
    d = item(src, "2018-01-01 Old.mp4", "", )
    d.include = False
    proposals.recompute([a, b, c, p, d])
    out = tmp_path / "web"
    touch(out / "trip" / "2020-05-06 Robot runs.mp4")                     # b already exported
    tasks = export.plan_export([a, b, c, p, d], str(src), str(out))
    got = [(t.kind, os.path.relpath(t.dst, out), t.skip) for t in tasks]
    assert got == [
        ("video", "2020-05-06 Robot runs.mp4", False),
        ("thumb", "2020-05-06 Robot runs.jpg", False),                   # its existing image travels with it
        ("video", os.path.join("trip", "2020-05-06 Robot runs.mp4"), True),
        ("video", "2020-05-06 Robot runs (2).mp4", False),
        ("photo", "2020-05-06 Ostrich.jpg", False),
    ]
    flat = export.plan_export([a, b], str(src), str(out), mirror=False, photos=False)
    assert [os.path.relpath(t.dst, out) for t in flat if t.kind == "video"] == [
        "2020-05-06 Robot runs.mp4", "2020-05-06 Robot runs (2).mp4"]


def test_plan_export_frame_thumbnail_only_when_asked(tmp_path):
    v = item(tmp_path / "s", "v.mov", "Cake")
    fr = tmp_path / "f.jpg"
    touch(fr)
    v.frames, v.thumb_choice = [str(fr)], 0
    proposals.recompute([v])
    assert [t.kind for t in export.plan_export([v], str(tmp_path / "s"), str(tmp_path / "o"), thumbs=False)] == ["video"]
    tasks = export.plan_export([v], str(tmp_path / "s"), str(tmp_path / "o"), thumbs=True)
    assert [(t.kind, t.src) for t in tasks] == [("video", v.path), ("thumb", str(fr))]


def test_check_folders(tmp_path):
    src, out = str(tmp_path / "src"), str(tmp_path / "src" / "web")
    assert export.check_folders(src, src, True)
    assert export.check_folders(src, out, True)                          # inside + recursive
    assert export.check_folders(src, out, False) is None
    assert export.check_folders(src, str(tmp_path), False)               # contains source
    assert export.check_folders(src, str(tmp_path / "src (web)"), True) is None
    assert export.check_folders(src, "", True)


def test_date_only_template_keeps_already_dated_names(tmp_path):
    d = item(tmp_path, "2018-01-01 Old.mp4", "")
    proposals.recompute([d], template="{date} {orig}")
    assert d.new_name == d.name


def test_dv_capture_filename_date():
    from datetime import datetime
    from media_namer import dates
    assert dates.filename_date("Girls.07-09-23_21-47.00.avi") == datetime(2007, 9, 23, 21, 47, 0)
    assert dates.filename_date("capture.99-12-31_23-59.59.avi") == datetime(1999, 12, 31, 23, 59, 59)
    assert dates.filename_date("tape1.07-13-40_21-47.00.avi") is None      # month 13: not a date
    assert dates.filename_date("IMG_20191225_083015.jpg") == datetime(2019, 12, 25, 8, 30, 15)  # unchanged


@needs_ffmpeg
def test_dv_avi_capture_to_web(tmp_path):
    """A WinDV-style capture: interlaced 720x480 DV with 4:3 non-square pixels, no date tag."""
    from datetime import datetime
    from media_namer import dates
    src = tmp_path / "Girls.07-09-23_21-47.00.avi"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=720x480:d=2:r=30000/1001",
                    "-f", "lavfi", "-i", "sine=f=440:d=2:sample_rate=48000",
                    "-vf", "setsar=8/9,setfield=bff", "-flags", "+ildct+ilme", "-top", "0",
                    "-c:v", "dvvideo", "-pix_fmt", "yuv411p", "-c:a", "pcm_s16le", str(src)], check=True)
    info = probe(TOOLS, str(src))
    assert encode.is_interlaced(info) and encode.non_square_pixels(info)
    dt, source = dates.find_date(TOOLS, str(src), "video", info)
    assert (dt, source) == (datetime(2007, 9, 23, 21, 47), dates.SOURCE_FILENAME)

    dst = tmp_path / "out" / "2007-09-23 Girls.mp4"
    encode.encode(TOOLS, str(src), str(dst), encode.EncodeOptions(), creation_time=dt)
    out = probe(TOOLS, str(dst))
    v = next(s for s in out["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (640, 480)                         # 4:3, square pixels
    assert v.get("sample_aspect_ratio", "1:1") in ("1:1", "N/A")
    assert v.get("field_order", "progressive") == "progressive"
    # the date from the file name is now inside the web copy, so rescanning finds it
    dt2, source2 = dates.find_date(TOOLS, str(dst), "video", out)
    assert source2 == dates.SOURCE_CAMERA and dt2 == datetime(2007, 9, 23, 21, 47)


@needs_ffmpeg
def test_existing_camera_date_is_not_overwritten(tmp_path):
    from datetime import datetime
    src = tmp_path / "C0001.MOV"
    raw_camera_clip(src, size="640x360", seconds=1, creation="2026-10-25T20:00:00Z")
    dst = tmp_path / "o.mp4"
    encode.encode(TOOLS, str(src), str(dst), encode.EncodeOptions(), creation_time=datetime(2001, 1, 1))
    assert probe(TOOLS, str(dst))["format"]["tags"]["creation_time"].startswith("2026-10-25T20:00:00")
