"""Face recognition logic, with a stand-in engine (the real ONNX models can't be fetched in CI)."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from media_namer import describe, faces

rng = np.random.default_rng(7)


def unit(v):
    return v / np.linalg.norm(v)


# Sisters look alike: share a "family" component, plus their own.
FAMILY = unit(rng.normal(size=128))
PEOPLE = {n: unit(0.55 * FAMILY + unit(rng.normal(size=128))) for n in ("Allison", "Anika", "Annaclare")}


def sample(name, noise=0.35):
    return unit(PEOPLE[name] + noise * unit(rng.normal(size=128)))


def library(tmp_path, n_refs=6):
    lib = faces.PeopleLibrary(str(tmp_path / "people"))
    for name in PEOPLE:
        lib.add_person(name)
        for _ in range(n_refs):
            lib.add_reference(name, b"\xff\xd8crop", sample(name), source=f"{name}.jpg")
    return lib


def test_sisters_are_told_apart(tmp_path):
    lib = library(tmp_path)
    hits = sum(1 for _ in range(60) for name in PEOPLE
               if (m := lib.match(sample(name))) and m.name == name)
    wrong = sum(1 for _ in range(60) for name in PEOPLE
                if (m := lib.match(sample(name))) and m.name != name)
    assert hits >= 150 and wrong == 0          # >=83% recognized, never mislabeled


def test_strangers_and_lookalikes_stay_unnamed(tmp_path):
    lib = library(tmp_path)
    strangers = [lib.match(unit(rng.normal(size=128))) for _ in range(50)]
    assert all(m is None for m in strangers)
    halfway = unit(PEOPLE["Allison"] + PEOPLE["Anika"])        # could be either sister
    assert lib.match(halfway) is None
    assert lib.match(sample("Anika"), threshold=0.99) is None  # strict setting


def test_library_persists_and_edits(tmp_path):
    lib = library(tmp_path, n_refs=2)
    fp = lib.fingerprint()
    again = faces.PeopleLibrary(str(tmp_path / "people"))
    assert again.names() == ["Allison", "Anika", "Annaclare"]
    assert len(again.people["Anika"]) == 2 and os.path.isfile(again.crop_path(again.people["Anika"][0]))
    m = again.match(sample("Anika"))
    assert m and m.name == "Anika"
    again.rename_person("Annaclare", "Annaclare R")
    assert "Annaclare R" in faces.PeopleLibrary(str(tmp_path / "people")).names()
    rid = again.people["Allison"][0].id
    crop = again.crop_path(again.people["Allison"][0])
    again.remove_reference("Allison", rid)
    assert not os.path.exists(crop) and again.fingerprint() != fp
    again.remove_person("Anika")
    assert "Anika" not in faces.PeopleLibrary(str(tmp_path / "people")).names()
    with pytest.raises(faces.FaceError):
        again.add_person("allison")                            # case-insensitive duplicate


class FakeEngine:
    """Images are bytes like b"Allison,Anika": one face per name, b"bad" fails to decode."""
    loads = 0

    def __init__(self):
        FakeEngine.loads += 1

    def decode(self, data):
        if data == b"bad":
            raise faces.FaceError("couldn't read image")
        return data

    def faces(self, img):
        out = []
        for i, n in enumerate(x for x in img.decode().split(",") if x):
            emb = sample(n) if n in PEOPLE else unit(rng.normal(size=128))
            out.append(faces.Face((0, 0, 40 + 10 * i, 40 + 10 * i), 0.9, None, emb))
        return out

    def crop(self, img, face):
        return b"crop"

    def encode_jpeg(self, img):
        return b"\xff\xd8" + (img if isinstance(img, bytes) else b"x")


def test_identifier_across_frames(tmp_path):
    lib = library(tmp_path)
    FakeEngine.loads = 0
    fid = faces.FaceIdentifier(lib, FakeEngine)
    got = fid.identify([b"Anika", b"bad", b"Stranger,Allison", b""])
    assert [m.name for m in got] == ["Allison", "Anika"]        # library order; stranger dropped
    fid.identify([b"Annaclare"])
    assert FakeEngine.loads == 1                                 # engine loaded once, reused


def test_identifier_is_thread_safe(tmp_path):
    lib = library(tmp_path)
    fid = faces.FaceIdentifier(lib, FakeEngine)
    results = []
    ts = [threading.Thread(target=lambda: results.append(fid.identify([b"Anika", b"Allison"]))) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all([m.name for m in r] == ["Allison", "Anika"] for r in results)


def test_add_photo_uses_largest_face(tmp_path):
    lib = faces.PeopleLibrary(str(tmp_path / "p"))
    lib.add_person("Anika")
    photo = tmp_path / "group.jpg"
    photo.write_bytes(b"Allison,Anika")                         # Anika's face is the larger one
    note = faces.add_photo(FakeEngine(), lib, "Anika", str(photo))
    assert "largest" in note and len(lib.people["Anika"]) == 1
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    with pytest.raises(faces.FaceError, match="no face"):
        faces.add_photo(FakeEngine(), lib, "Anika", str(empty))


def test_prompt_mentions_only_recognized_people():
    p = describe.build_prompt("video", 3, "Apple orchard, Julian", ["Allison", "Anika"])
    assert "identified these people: Allison and Anika" in p and "Do not name anyone else" in p
    assert "Julian" in p
    assert "Face recognition" not in describe.build_prompt("video", 3, "", [])
    assert "Allison, Anika and Annaclare" in describe.build_prompt("photo", 1, "", ["Allison", "Anika", "Annaclare"])


def test_engine_needs_models(tmp_path):
    with pytest.raises(faces.FaceError, match="aren't downloaded"):
        faces.FaceEngine(str(tmp_path))


def test_opencv_api_present():
    cv2 = pytest.importorskip("cv2")
    assert hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")


class ModelServer(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/missing"):
            self.send_response(404)
            self.end_headers()
            return
        size = 300 if "short" in self.path else 2048
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        self.wfile.write(b"\x08" * size)


def test_download_models_with_fallback(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ModelServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(faces, "MODEL_URLS", {"det.onnx": [f"{base}/missing/det", f"{base}/det"],
                                              "rec.onnx": [f"{base}/rec"]})
    monkeypatch.setattr(faces, "MIN_SIZES", {"det.onnx": 1000, "rec.onnx": 1000})
    seen = []
    faces.download_models(str(tmp_path / "m"), progress=lambda n, d, t: seen.append(n))
    assert sorted(os.listdir(tmp_path / "m")) == ["det.onnx", "rec.onnx"]
    assert faces.models_present(str(tmp_path / "m")) and "det.onnx" in seen
    monkeypatch.setattr(faces, "MODEL_URLS", {"x.onnx": [f"{base}/short"]})
    monkeypatch.setattr(faces, "MIN_SIZES", {"x.onnx": 1000})
    with pytest.raises(faces.FaceError, match="incomplete"):
        faces.download_models(str(tmp_path / "m2"))
    assert os.listdir(tmp_path / "m2") == []                     # no partial files
    srv.shutdown()


def test_ensure_names():
    from media_namer.naming import ensure_names
    assert ensure_names("Children picking apples", ["Allison", "Anika"]) == "Allison and Anika - Children picking apples"
    assert ensure_names("Allison picking apples", ["Allison", "Anika"]) == "Allison picking apples with Anika"
    assert ensure_names("Allison and Anika at the beach", ["Anika", "Allison"]) == "Allison and Anika at the beach"
    assert ensure_names("Girls at the beach", []) == "Girls at the beach"
    assert ensure_names("Kids", ["Allison", "Anika", "Annaclare"]) == "Allison, Anika and Annaclare - Kids"


def test_describe_job_scans_more_frames_for_faces(tmp_path, monkeypatch):
    """Faces get their own frames (8 by default) even when the AI gets only 1."""
    import subprocess
    from media_namer import media, workers
    from media_namer.media import Tools
    if not Tools().ok:
        pytest.skip("ffmpeg")
    v = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:d=4:r=10",
                    "-pix_fmt", "yuv420p", str(v)], check=True)

    class Recorder:
        def __init__(self):
            self.seen = []

        def identify(self, images):
            self.seen.append(images)
            return [faces.Match("Allison", 0.6)]

    class Backend:
        def describe(self, images, prompt):
            Backend.ai_images, Backend.prompt = len(images), prompt
            return "Girl on a swing"
    monkeypatch.setattr(describe, "make_backend", lambda **kw: Backend())
    rec = Recorder()
    from media_namer.scan import MediaItem
    item = MediaItem(path=str(v), kind="video", size=1, mtime_ns=1)
    job = workers.DescribeJob(1, 0, item, {}, "", Tools(), 1, threading.Event(), faces=rec, face_frames=8)
    out = []
    job.signals.done.connect(lambda *a: out.append(a))
    job.run()
    assert Backend.ai_images == 1                      # the AI still gets just 1 frame
    assert len(rec.seen[0]) == 8                      # faces were looked for in 8
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(rec.seen[0][0])).size == (1280, 720)   # at higher resolution
    assert "Allison" in Backend.prompt


def test_dv_frames_are_deinterlaced_and_square(tmp_path):
    import io
    import subprocess
    from PIL import Image
    from media_namer import media
    from media_namer.media import Tools
    if not Tools().ok:
        pytest.skip("ffmpeg")
    src = tmp_path / "dv.avi"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=720x480:d=2:r=30000/1001",
                    "-vf", "setsar=8/9,setfield=bff", "-flags", "+ildct+ilme", "-top", "0",
                    "-c:v", "dvvideo", "-pix_fmt", "yuv411p", str(src)], check=True)
    fr = media.video_frames(Tools(), str(src), count=2, size=1280)
    assert Image.open(io.BytesIO(fr[0])).size == (640, 480)     # 4:3, not squeezed 720x480
    hd = tmp_path / "hd.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:d=2:r=25",
                    "-pix_fmt", "yuv420p", str(hd)], check=True)
    assert Image.open(io.BytesIO(media.video_frames(Tools(), str(hd), count=1)[0])).size == (768, 432)
