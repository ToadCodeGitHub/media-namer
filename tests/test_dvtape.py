"""MiniDV: recording dates from DV frames, and splitting captures by recording."""
import os
import subprocess
from datetime import datetime

import pytest

from media_namer import dates, dvtape
from media_namer.media import Tools, probe
from tests.test_core import needs_ffmpeg

TOOLS = Tools()


def dv_clip(path, seconds, when, pal=False):
    """Raw DV whose frames carry a recording date (the dv muxer stamps creation_time into them)."""
    size, rate, pix = ("720x576", "25", "yuv420p") if pal else ("720x480", "30000/1001", "yuv411p")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:d={seconds}:r={rate}",
                    "-f", "lavfi", "-i", f"sine=f=440:d={seconds}:sample_rate=48000",
                    "-c:v", "dvvideo", "-pix_fmt", pix, "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                    "-metadata", f"creation_time={when}", "-f", "dv", str(path)], check=True)


def tape(tmp_path, *parts, name="tape.avi"):
    """Concatenate DV clips like a tape with several recordings, captured into one AVI."""
    raw = tmp_path / "tape.dv"
    with open(raw, "wb") as out:
        for i, (secs, when) in enumerate(parts):
            p = tmp_path / f"part{i}.dv"
            dv_clip(p, secs, when)
            out.write(p.read_bytes())
    avi = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "dv", "-i", str(raw), "-map", "0:v", "-map", "0:a",
                    "-c", "copy", str(avi)], check=True)
    return raw, avi


def test_pack_parsing():
    frame = bytearray(dvtape.NTSC_FRAME)
    frame[0] = 0x1F                                    # header block
    vaux = 3 * 80
    frame[vaux] = 0x50                                 # section type 2 (VAUX)
    frame[vaux + 3:vaux + 8] = bytes([0x62, 0xFF, 0xE3, 0x09, 0x07])    # 23 Sep 07
    frame[vaux + 8:vaux + 13] = bytes([0x63, 0xFF, 0x80, 0xC7, 0xE1])   # 21:47:00
    assert dvtape.recording_datetime(bytes(frame)) == datetime(2007, 9, 23, 21, 47, 0)
    frame[vaux + 5] = 0xFF                             # unstamped / garbage day
    assert dvtape.recording_datetime(bytes(frame)) is None
    frame[vaux + 3:vaux + 8] = bytes([0x62, 0xFF, 0xE1, 0x12, 0x98])    # 1 Dec 1998
    assert dvtape.recording_datetime(bytes(frame)).year == 1998


def test_scene_rules():
    t = datetime(2007, 9, 23, 21, 47, 0)
    from datetime import timedelta as td
    assert dvtape.is_new_scene(None, t)
    assert not dvtape.is_new_scene(t, t + td(seconds=1))          # normal recording
    assert not dvtape.is_new_scene(t, None)                       # dropout: stay in the recording
    assert dvtape.is_new_scene(t, t + td(minutes=5))              # camera stopped, restarted later
    assert dvtape.is_new_scene(t, t - td(days=1))                 # date went backwards
    assert dvtape.scene_name("Tape1", t, 1) == "Tape1.07-09-23_21-47.00"
    assert dvtape.scene_name("Tape1", None, 3) == "Tape1.undated-003"


@needs_ffmpeg
def test_read_date_from_dv_and_avi(tmp_path):
    raw, avi = tape(tmp_path, (2, "2007-09-23T21:47:00Z"), (2, "2007-09-24T10:05:30Z"))
    assert dvtape.recording_date(TOOLS, str(raw)) == datetime(2007, 9, 23, 21, 47)
    assert dvtape.recording_date(TOOLS, str(avi)) == datetime(2007, 9, 23, 21, 47)


@needs_ffmpeg
def test_pal_frames(tmp_path):
    p = tmp_path / "pal.dv"
    dv_clip(p, 1, "2004-05-06T07:08:09Z", pal=True)
    with open(p, "rb") as f:
        assert dvtape.frame_size(f.read(80)) == dvtape.PAL_FRAME
    assert dvtape.recording_date(TOOLS, str(p)) == datetime(2004, 5, 6, 7, 8, 9)


@needs_ffmpeg
def test_split_capture_into_recordings(tmp_path):
    raw, avi = tape(tmp_path, (2, "2007-09-23T21:47:00Z"), (2, "2007-09-24T10:05:30Z"), (1, "2007-12-25T08:00:00Z"))
    before = avi.read_bytes()
    out = tmp_path / "recordings"
    scenes = dvtape.split_file(TOOLS, str(avi), str(out), prefix="Tape1")
    names = [os.path.basename(s.path) for s in scenes]
    assert names == ["Tape1.07-09-23_21-47.00.avi", "Tape1.07-09-24_10-05.30.avi", "Tape1.07-12-25_08-00.00.avi"]
    assert avi.read_bytes() == before                                     # original untouched
    assert sorted(os.listdir(out)) == sorted(names)                       # no .part files left
    for s in scenes:
        info = probe(TOOLS, s.path)
        assert [x["codec_name"] for x in info["streams"]] == ["dvvideo", "pcm_s16le"]
        assert dvtape.recording_date(TOOLS, s.path) == s.start            # tape packs survive
        dt, src = dates.find_date(TOOLS, s.path, "video", info)
        assert (dt, src) == (s.start, dates.SOURCE_TAPE)
    assert scenes[0].frames == 59 and scenes[2].frames == 29


@needs_ffmpeg
def test_tape_date_beats_wrong_file_name(tmp_path):
    raw, avi = tape(tmp_path, (1, "2005-06-07T08:09:10Z"), name="Girls.07-09-23_21-47.00.avi")
    dt, src = dates.find_date(TOOLS, str(avi), "video")
    assert (dt, src) == (datetime(2005, 6, 7, 8, 9, 10), dates.SOURCE_TAPE)


@needs_ffmpeg
def test_split_cancel_cleans_up(tmp_path):
    import threading
    raw, avi = tape(tmp_path, (3, "2007-09-23T21:47:00Z"))
    cancel = threading.Event()
    with pytest.raises(dvtape.Cancelled):
        dvtape.split_file(TOOLS, str(avi), str(tmp_path / "o"), progress=lambda n, sc: cancel.set(), cancel=cancel)
    assert os.listdir(tmp_path / "o") == []
