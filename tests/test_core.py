import base64
import json
import os
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PIL import Image

from media_namer import dates, describe, naming, proposals, renamer, scan
from media_namer.cache import DescriptionCache
from media_namer.media import Tools, photo_jpeg, probe, video_frames

TOOLS = Tools()
needs_ffmpeg = pytest.mark.skipif(not TOOLS.ok, reason="ffmpeg not installed")


# ------------------------------------------------------------- naming

@pytest.mark.parametrize("raw,expected", [
    ("golden-retriever-playing-fetch", "Golden retriever playing fetch"),
    ('"Kids opening presents."', "Kids opening presents"),
    ("Title: Robots on a competition field\nThis video shows…", "Robots on a competition field"),
    ("**Birthday cake: candles?**", "Birthday cake candles"),
    ("A/B test <of> pipes|stuff", "A B test of pipes stuff"),
    ("", ""),
    ("   \n\n  ", ""),
    ("one two three four five six seven eight nine ten eleven twelve", "One two three four five six seven eight nine ten"),
])
def test_clean_description(raw, expected):
    assert naming.clean_description(raw) == expected


def test_build_stem_and_problems():
    assert naming.build_stem("{date} {desc}", "2019-07-04", "Beach day", "IMG_1") == "2019-07-04 Beach day"
    assert naming.build_stem("{date} {desc} ({orig})", "", "Beach", "IMG_1") == "Beach (IMG_1)"
    assert naming.build_stem("{desc}", "", "con", "x") == "_con"
    assert naming.filename_problem("ok name.mp4") is None
    assert naming.filename_problem("bad:name.mp4")
    assert naming.filename_problem("trailing.mp4 ")
    assert naming.filename_problem(".mp4")
    assert naming.filename_problem("NUL.txt")


# ------------------------------------------------------------- dates

@pytest.mark.parametrize("name,expected", [
    ("IMG_20191225_083015.jpg", datetime(2019, 12, 25, 8, 30, 15)),
    ("PXL_20230101_120000123.mp4", datetime(2023, 1, 1, 12, 0, 0)),
    ("IMG-20190704-WA0001.jpg", datetime(2019, 7, 4)),
    ("2018-06-30 party.mov", datetime(2018, 6, 30)),
    ("VID_20210230_000000.mp4", None),          # Feb 30
    ("DSC01234.JPG", None),
    ("123201912251.jpg", None),                  # digits run on, not a date
])
def test_filename_date(name, expected):
    assert dates.filename_date(name) == expected


def test_photo_exif_date(tmp_path):
    p = tmp_path / "a.jpg"
    img = Image.new("RGB", (40, 30), "red")
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "2015:08:09 10:11:12"
    img.save(p, exif=exif)
    assert dates.photo_date(str(p)) == datetime(2015, 8, 9, 10, 11, 12)
    dt, src = dates.find_date(TOOLS, str(p), "photo")
    assert (dt, src) == (datetime(2015, 8, 9, 10, 11, 12), dates.SOURCE_CAMERA)


def test_photo_no_exif_falls_back(tmp_path):
    p = tmp_path / "IMG_20200102_030405.png"
    Image.new("RGB", (10, 10)).save(p)
    assert dates.find_date(TOOLS, str(p), "photo") == (datetime(2020, 1, 2, 3, 4, 5), dates.SOURCE_FILENAME)
    q = tmp_path / "random.png"
    Image.new("RGB", (10, 10)).save(q)
    assert dates.find_date(TOOLS, str(q), "photo")[1] == dates.SOURCE_MODIFIED


def test_video_date_from_tags():
    info = {"format": {"tags": {"creation_time": "2019-07-04T18:22:01.000000Z"}}}
    expect = datetime(2019, 7, 4, 18, 22, 1, tzinfo=__import__("datetime").timezone.utc).astimezone().replace(tzinfo=None)
    assert dates.video_date(info) == expect
    apple = {"format": {"tags": {"com.apple.quicktime.creationdate": "2019-07-04T11:22:01-0700",
                                 "creation_time": "2019-07-04T18:22:01Z"}}}
    assert dates.video_date(apple) == expect
    zero = {"format": {"tags": {"creation_time": "1970-01-01T00:00:00Z"}},
            "streams": [{"tags": {"creation_time": "1904-01-01T00:00:00Z"}}]}
    assert dates.video_date(zero) is None


def make_video(path, seconds=4, creation=None, size="320x240"):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=10",
           "-pix_fmt", "yuv420p"]
    if creation:
        cmd += ["-metadata", f"creation_time={creation}"]
    subprocess.run(cmd + [str(path)], check=True)


@needs_ffmpeg
def test_real_video_frames_and_date(tmp_path):
    v = tmp_path / "clip.mov"
    make_video(v, creation="2021-12-25T16:00:00Z")
    info = probe(TOOLS, str(v))
    frames = video_frames(TOOLS, str(v), count=3, info=info)
    assert len(frames) == 3 and all(f[:2] == b"\xff\xd8" for f in frames)
    dt, src = dates.find_date(TOOLS, str(v), "video", info)
    assert src == dates.SOURCE_CAMERA and dt.year == 2021


@needs_ffmpeg
def test_short_video_single_frame(tmp_path):
    v = tmp_path / "tiny.mp4"
    make_video(v, seconds=1)
    assert len(video_frames(TOOLS, str(v), count=3)) == 1


def test_photo_jpeg_downscales_and_rotates(tmp_path):
    p = tmp_path / "big.png"
    Image.new("RGBA", (3000, 1000), (0, 0, 255, 128)).save(p)
    data = photo_jpeg(str(p), size=1024)
    from io import BytesIO
    im = Image.open(BytesIO(data))
    assert im.format == "JPEG" and max(im.size) == 1024


# ------------------------------------------------------------- scan

def touch(p, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_scan_pairs_thumbnails(tmp_path):
    touch(tmp_path / "Robot.mp4")
    touch(tmp_path / "robot.JPG")              # thumbnail, different case
    touch(tmp_path / "beach.jpg")
    touch(tmp_path / "notes.txt")
    touch(tmp_path / ".hidden.mp4")
    touch(tmp_path / "2019-07-04 Done.mp4")
    touch(tmp_path / "sub" / "deep.mov")
    items = scan.list_media(str(tmp_path), recursive=False)
    names = [i.name for i in items]
    assert names == ["2019-07-04 Done.mp4", "beach.jpg", "Robot.mp4"]
    robot = items[2]
    assert [os.path.basename(c) for c in robot.companions] == ["robot.JPG"]
    assert items[0].status == scan.STATUS_ALREADY and not items[0].include
    assert len(scan.list_media(str(tmp_path), recursive=True)) == 4


# ------------------------------------------------------------- proposals

def item(tmp_path, name, desc="", date=datetime(2020, 5, 6), companions=()):
    p = tmp_path / name
    touch(p)
    comps = []
    for c in companions:
        touch(tmp_path / c)
        comps.append(str(tmp_path / c))
    return scan.MediaItem(path=str(p), kind=scan.kind_of(name) if hasattr(scan, "kind_of") else "video",
                          description=desc, date=date, companions=comps)


def test_recompute_dedupes_and_respects_existing(tmp_path):
    a = item(tmp_path, "a.MP4", "Beach day")
    b = item(tmp_path, "b.mp4", "Beach day")
    touch(tmp_path / "2020-05-06 Beach day (3).mp4")        # unrelated existing file
    c = item(tmp_path, "c.mp4", "Beach day")
    d = item(tmp_path, "d.mp4", "")                           # no description yet
    proposals.recompute([a, b, c, d])
    assert a.new_name == "2020-05-06 Beach day.mp4"           # extension lower-cased
    assert b.new_name == "2020-05-06 Beach day (2).mp4"
    assert c.new_name == "2020-05-06 Beach day (4).mp4"
    assert d.new_name == ""


def test_recompute_companion_clash(tmp_path):
    v = item(tmp_path, "v.mp4", "Cake", companions=["v.jpg"])
    touch(tmp_path / "2020-05-06 Cake.jpg")                   # would collide with the thumbnail
    proposals.recompute([v])
    assert v.new_name == "2020-05-06 Cake (2).mp4"


def test_recompute_swap_is_allowed(tmp_path):
    # x gets y's current name while y is renamed away: no counter needed.
    x = item(tmp_path, "x.mp4", "Party", date=None)
    y = item(tmp_path, "Party.mp4", "Picnic", date=None)
    proposals.recompute([x, y])
    assert (x.new_name, y.new_name) == ("Party.mp4", "Picnic.mp4")


def test_csv_round_trip(tmp_path):
    a = item(tmp_path, "a.mp4", "Beach")
    b = item(tmp_path, "b.mp4", "Snow")
    proposals.recompute([a, b])
    csv_path = tmp_path / "plan.csv"
    proposals.export_csv([a, b], str(csv_path))
    text = csv_path.read_text(encoding="utf-8-sig")
    text = text.replace("Snow", "Snow fort").replace(",yes,2020-05-06 00:00:00,", ",no,7/4/2019,", 1)
    csv_path.write_text(text, encoding="utf-8-sig")
    assert proposals.import_csv([a, b], str(csv_path)) == 2
    proposals.recompute([a, b])
    assert a.include is False and a.date == datetime(2019, 7, 4) and a.date_source == dates.SOURCE_MANUAL
    assert b.description == "Snow fort" and b.new_name == "2020-05-06 Snow fort.mp4"


# ------------------------------------------------------------- renamer

def test_apply_and_undo_with_swap_and_companion(tmp_path):
    touch(tmp_path / "a.mp4", b"A")
    touch(tmp_path / "a.jpg", b"Athumb")
    touch(tmp_path / "b.mp4", b"B")
    ops = [renamer.RenameOp(str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")),
           renamer.RenameOp(str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")),
           renamer.RenameOp(str(tmp_path / "b.mp4"), str(tmp_path / "a.mp4"))]
    assert renamer.validate(ops) == []
    log = tmp_path / "logs" / "run.csv"
    renamer.apply(ops, str(log))
    assert (tmp_path / "b.mp4").read_bytes() == b"A"
    assert (tmp_path / "a.mp4").read_bytes() == b"B"
    assert (tmp_path / "b.jpg").read_bytes() == b"Athumb"
    assert not list(tmp_path.glob("*.tmp"))
    restored, problems = renamer.undo(str(log))
    assert problems == [] and restored == 3
    assert (tmp_path / "a.mp4").read_bytes() == b"A" and (tmp_path / "a.jpg").exists()
    assert renamer.log_is_undone(str(log))
    assert renamer.undo(str(log)) == (0, [])                   # second undo is harmless


def test_validate_catches_problems(tmp_path):
    touch(tmp_path / "a.mp4")
    touch(tmp_path / "b.mp4")
    touch(tmp_path / "taken.mp4")
    ops = [renamer.RenameOp(str(tmp_path / "a.mp4"), str(tmp_path / "same.mp4")),
           renamer.RenameOp(str(tmp_path / "b.mp4"), str(tmp_path / "same.mp4"))]
    assert any("two files" in p for p in renamer.validate(ops))
    ops = [renamer.RenameOp(str(tmp_path / "a.mp4"), str(tmp_path / "taken.mp4"))]
    assert any("already exists" in p for p in renamer.validate(ops))
    ops = [renamer.RenameOp(str(tmp_path / "a.mp4"), str(tmp_path / "bad?.mp4")),
           renamer.RenameOp(str(tmp_path / "gone.mp4"), str(tmp_path / "x.mp4"))]
    probs = renamer.validate(ops)
    assert any("< > :" in p for p in probs) and any("no longer exists" in p for p in probs)


def test_apply_rolls_back_on_failure(tmp_path, monkeypatch):
    touch(tmp_path / "a.mp4", b"A")
    touch(tmp_path / "b.mp4", b"B")
    ops = [renamer.RenameOp(str(tmp_path / "a.mp4"), str(tmp_path / "x.mp4")),
           renamer.RenameOp(str(tmp_path / "b.mp4"), str(tmp_path / "y.mp4"))]
    real = os.rename
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 4:                                   # fail during phase 2
            raise PermissionError("file is open in another program")
        real(a, b)
    monkeypatch.setattr(renamer.os, "rename", flaky)
    log = tmp_path / "logs" / "run.csv"
    with pytest.raises(renamer.RenameFailed, match="undone"):
        renamer.apply(ops, str(log))
    monkeypatch.setattr(renamer.os, "rename", real)
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["a.mp4", "b.mp4"]
    assert (tmp_path / "a.mp4").read_bytes() == b"A"
    assert renamer.read_log(str(log)) == []                   # nothing left to undo


def test_plan_uses_items(tmp_path):
    v = item(tmp_path, "v.mp4", "Cake", companions=["v.png"])
    s = item(tmp_path, "s.mp4", "Same")
    proposals.recompute([v, s])
    s.new_name = s.name                                      # unchanged -> no op
    ops = renamer.plan([v, s])
    assert [(os.path.basename(o.src), os.path.basename(o.dst)) for o in ops] == [
        ("v.mp4", "2020-05-06 Cake.mp4"), ("v.png", "2020-05-06 Cake.png")]


# ------------------------------------------------------------- cache

def test_cache(tmp_path):
    path = tmp_path / "c" / "cache.json"
    c = DescriptionCache(str(path))
    k = c.key(10, 20, "ollama", "m", "ctx")
    assert c.key(10, 20, "ollama", "m", "other") != k
    c.put(k, "Beach day")
    c.save()
    assert DescriptionCache(str(path)).get(k) == "Beach day"


# ------------------------------------------------------------- AI backends

class FakeOllama(BaseHTTPRequestHandler):
    calls = []
    fail_first = 0

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": "qwen2.5vl:7b"}, {"name": "llava:13b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.calls.append(body)
        if FakeOllama.fail_first > 0:
            FakeOllama.fail_first -= 1
            return self._send(503, {"error": "busy"})
        if body["model"] == "missing":
            return self._send(404, {"error": "model 'missing' not found"})
        n = len(body["messages"][0]["images"])
        self._send(200, {"message": {"role": "assistant", "content": f"robot-runs-{n}-frames."}})


@pytest.fixture
def ollama_server():
    srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    FakeOllama.calls, FakeOllama.fail_first = [], 0
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_ollama_backend(ollama_server):
    be = describe.OllamaBackend(ollama_server, "qwen2.5vl:7b")
    assert be.list_models() == ["llava:13b", "qwen2.5vl:7b"]
    prompt = describe.build_prompt("video", 3, "FIRST robotics, La Grange")
    out = be.describe([b"\xff\xd8a", b"\xff\xd8b", b"\xff\xd8c"], prompt)
    assert naming.clean_description(out) == "Robot runs 3 frames"
    sent = FakeOllama.calls[-1]["messages"][0]
    assert base64.b64decode(sent["images"][0]) == b"\xff\xd8a" and "La Grange" in sent["content"]


def test_ollama_retry_and_errors(ollama_server):
    be = describe.OllamaBackend(ollama_server, "qwen2.5vl:7b")
    FakeOllama.fail_first = 2
    retries = []
    out = describe.with_retry(lambda: be.describe([b"x"], "p"), sleep=lambda s: None,
                              on_retry=lambda n, d, e: retries.append(n))
    assert out.startswith("robot") and retries == [1, 2]
    with pytest.raises(describe.DescribeError, match="not found"):
        describe.with_retry(lambda: describe.OllamaBackend(ollama_server, "missing").describe([b"x"], "p"),
                            sleep=lambda s: None)
    with pytest.raises(describe.DescribeError, match="isn't running"):
        describe.OllamaBackend("http://127.0.0.1:9", "m").describe([b"x"], "p")


def test_retry_cancel():
    with pytest.raises(describe.Cancelled):
        describe.with_retry(lambda: (_ for _ in ()).throw(describe.TransientError("x")),
                            sleep=lambda s: None, cancelled=lambda: True)


def test_gemini_backend_with_fake_client(monkeypatch):
    from google.genai import errors
    be = describe.GeminiBackend("fake-key", "gemini-3.5-flash-lite")
    seen = {}

    class Resp:
        text = "Kids opening presents."

    class Models:
        def generate_content(self, model, contents, config):
            seen["model"], seen["contents"] = model, contents
            return Resp()

        def list(self):
            M = type("M", (), {})
            a, b = M(), M()
            a.name, a.supported_actions = "models/gemini-3.5-flash-lite", ["generateContent"]
            b.name, b.supported_actions = "models/text-embedding-004", ["embedContent"]
            return [a, b]

    be._client = type("C", (), {"models": Models()})()
    assert be.describe([b"\xff\xd8x"], "prompt") == "Kids opening presents."
    assert seen["model"] == "gemini-3.5-flash-lite" and seen["contents"][-1] == "prompt"
    assert be.list_models() == ["gemini-3.5-flash-lite"]

    def boom(code):
        def f(**kw):
            raise errors.APIError(code, {"error": {"message": "nope", "status": "X"}})
        return f
    be._client.models.generate_content = boom(429)
    with pytest.raises(describe.TransientError):
        be.describe([b"x"], "p")
    be._client.models.generate_content = boom(400)
    with pytest.raises(describe.DescribeError) as ei:
        be.describe([b"x"], "p")
    assert not isinstance(ei.value, describe.TransientError)


def test_gemini_needs_key():
    with pytest.raises(describe.DescribeError, match="API key"):
        describe.GeminiBackend("", "m")


def test_finds_winget_ffmpeg_not_on_path(tmp_path, monkeypatch):
    from media_namer import media as m
    pkg = tmp_path / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ffmpeg-8.0-full_build" / "bin"
    pkg.mkdir(parents=True)
    for exe in ("ffmpeg.exe", "ffprobe.exe"):
        (pkg / exe).write_bytes(b"MZ")
    monkeypatch.setattr(m.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    t = m.Tools()
    assert t.ok and t.ffmpeg == str(pkg / "ffmpeg.exe")


def test_template_without_desc_needs_no_ai(tmp_path):
    a = item(tmp_path, "Allison feeding ostrich.mp4", "", date=datetime(2024, 12, 26, 11, 52))
    b = item(tmp_path, "La Grange 10 Robot Runs.MP4", "", date=datetime(2019, 4, 16))
    proposals.recompute([a, b], template="{date} {orig}")
    assert a.new_name == "2024-12-26 Allison feeding ostrich.mp4"
    assert b.new_name == "2019-04-16 La Grange 10 Robot Runs.mp4"
    ops = renamer.plan([a, b])
    assert len(ops) == 2 and renamer.validate(ops) == []
