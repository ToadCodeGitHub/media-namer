import io
import os
import subprocess
import time

import pytest
from PIL import Image, ImageDraw, ImageFilter

from media_namer import media, proposals, renamer, scan
from media_namer.framestore import FrameStore
from tests.test_core import TOOLS, item, needs_ffmpeg, touch


def jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def scene() -> Image.Image:
    im = Image.new("RGB", (640, 360), (110, 140, 170))
    d = ImageDraw.Draw(im)
    for i in range(0, 640, 40):
        d.rectangle([i, 100, i + 20, 260], fill=(240, 200, 40) if i % 80 else (30, 60, 30))
    d.ellipse([250, 40, 390, 180], fill=(200, 30, 30))
    return im


def test_best_frame_skips_black_flat_and_blurry():
    black = jpeg(Image.new("RGB", (640, 360), (3, 3, 3)))
    white = jpeg(Image.new("RGB", (640, 360), (250, 250, 250)))
    sharp = jpeg(scene())
    blurry = jpeg(scene().filter(ImageFilter.GaussianBlur(12)))
    assert media.best_frame([black, blurry, sharp, white]) == 2
    assert media.best_frame([black, blurry]) == 1
    assert media.best_frame([]) == -1


def test_make_thumbnail_size():
    out = media.make_thumbnail(jpeg(scene()), 320)
    im = Image.open(io.BytesIO(out))
    assert im.format == "JPEG" and im.size == (320, 180)
    small = media.make_thumbnail(jpeg(Image.new("RGB", (200, 100), "red")), 640)
    assert Image.open(io.BytesIO(small)).size == (200, 100)     # never upscales


def test_framestore(tmp_path):
    st = FrameStore(str(tmp_path / "frames"))
    k = st.key(123, 456)
    assert st.paths(k) == []
    paths = st.save(k, [b"\xff\xd8a", b"\xff\xd8b", b"\xff\xd8c"])
    assert st.paths(k) == paths and open(paths[1], "rb").read() == b"\xff\xd8b"
    st.save(k, [b"\xff\xd8z"])                                  # fewer frames: stale ones go
    assert len(st.paths(k)) == 1
    old = time.time() - 90 * 86400
    os.utime(paths[0], (old, old))
    st.cleanup(60)
    assert st.paths(k) == []


def frames_for(tmp_path, it, choice=1):
    paths = []
    for i, color in enumerate(("black", "red", "blue")):
        p = tmp_path / f"frame-{it.stem}-{i}.jpg"
        Image.new("RGB", (800, 450), color).save(p)
        paths.append(str(p))
    it.frames, it.thumb_choice = paths, choice


def test_plan_apply_undo_creates_thumbnail(tmp_path):
    media_dir = tmp_path / "m"
    v = item(media_dir, "clip.mp4", "Cake")
    v.make_thumb = True
    frames_for(tmp_path, v, choice=1)
    proposals.recompute([v])
    ops = renamer.plan([v])
    assert [(o.action, os.path.basename(o.dst)) for o in ops] == [
        ("move", "2020-05-06 Cake.mp4"), ("create", "2020-05-06 Cake.jpg")]
    assert renamer.validate(ops) == []
    log = tmp_path / "logs" / "r.csv"
    renamer.apply(ops, str(log), thumb_width=400)
    thumb = media_dir / "2020-05-06 Cake.jpg"
    im = Image.open(thumb)
    assert im.size == (400, 225) and im.getpixel((10, 10))[0] > 200          # the red frame
    assert "delete 2020-05-06 Cake.jpg" in renamer.describe_log(str(log))
    restored, problems = renamer.undo(str(log))
    assert problems == [] and not thumb.exists() and (media_dir / "clip.mp4").exists()


def test_thumbnail_name_clash_gets_counter(tmp_path):
    v = item(tmp_path, "v.mp4", "Cake")
    v.make_thumb = True
    touch(tmp_path / "2020-05-06 Cake.jpg")
    proposals.recompute([v])
    assert v.new_name == "2020-05-06 Cake (2).mp4"


def test_no_thumbnail_when_video_already_has_one(tmp_path):
    v = item(tmp_path, "v.mp4", "Cake", companions=["v.jpg"])
    v.make_thumb = True                      # the window never sets this, but plan must still be safe
    frames_for(tmp_path, v)
    proposals.recompute([v])
    assert [o.action for o in renamer.plan([v])] == ["move", "move"]


def test_missing_frame_is_reported(tmp_path):
    v = item(tmp_path, "v.mp4", "Cake")
    v.make_thumb = True
    proposals.recompute([v])
    assert any("no video frame" in p for p in renamer.validate(renamer.plan([v])))


def test_create_failure_rolls_back_renames(tmp_path):
    v = item(tmp_path / "m", "clip.mp4", "Cake")
    v.make_thumb = True
    frames_for(tmp_path, v)
    proposals.recompute([v])
    ops = renamer.plan([v])
    assert renamer.validate(ops) == []
    touch(tmp_path / "m" / "2020-05-06 Cake.jpg", b"someone else's")    # appears after checking
    with pytest.raises(renamer.RenameFailed):
        renamer.apply(ops, str(tmp_path / "logs" / "r.csv"))
    assert (tmp_path / "m" / "clip.mp4").exists()
    assert (tmp_path / "m" / "2020-05-06 Cake.jpg").read_bytes() == b"someone else's"


def test_thumbnails_only_plan(tmp_path):
    a = item(tmp_path, "2019-01-01 Done.mp4", "")
    b = item(tmp_path, "has.mp4", "", companions=["has.jpg"])
    frames_for(tmp_path, a)
    ops = renamer.plan_thumbnails_only([(0, a), (1, b)])
    assert [(o.action, os.path.basename(o.dst)) for o in ops] == [("create", "2019-01-01 Done.jpg")]


@needs_ffmpeg
def test_real_video_best_frame_avoids_black_intro(tmp_path):
    v = tmp_path / "fade.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=320x240:d=2:r=10",
                    "-f", "lavfi", "-i", "testsrc=s=320x240:d=4:r=10",
                    "-filter_complex", "[0][1]concat=n=2:v=1[v]", "-map", "[v]", "-pix_fmt", "yuv420p", str(v)],
                   check=True)
    frames = media.video_frames(TOOLS, str(v), count=3)        # at 15%, 50%, 85% -> black, pattern, pattern
    assert media.score_frame(frames[0]) < media.score_frame(frames[1])
    assert media.best_frame(frames) != 0
