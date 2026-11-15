"""End-to-end: scan -> describe (fake Ollama) -> edit -> rename -> undo, driven through the real window."""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM") or "offscreen"
from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from media_namer import app as appmod  # noqa: E402
from media_namer.media import Tools  # noqa: E402
from media_namer.table import COL_DESC, COL_NEW  # noqa: E402

pytestmark = pytest.mark.skipif(not Tools().ok, reason="ffmpeg not installed")


class Fake(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send({"models": [{"name": "qwen2.5vl:7b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.calls += 1
        msg = body["messages"][0]
        text = ("Robots driving on a competition field." if "home video" in msg["content"]
                else "golden-retriever-playing-fetch")
        if "La Grange" in msg["content"] and "home video" in msg["content"]:
            text = "Title: La Grange robot runs"
        time.sleep(0.05)
        self._send({"message": {"content": text}})

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def wait(app, cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def video(path, creation=None):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
           "-pix_fmt", "yuv420p"]
    if creation:
        cmd += ["-metadata", f"creation_time={creation}"]
    subprocess.run(cmd + [str(path)], check=True)


def test_full_flow(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    video(media / "IMG_4417.MOV", "2026-10-25T20:00:00Z")
    Image.new("RGB", (64, 36), "blue").save(media / "IMG_4417.jpg")          # thumbnail for the video
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "2019:07:04 10:00:00"
    Image.new("RGB", (300, 200), "green").save(media / "DSC0001.jpg", exif=exif)
    Image.new("RGB", (300, 200), "green").save(media / "DSC0002.jpg", exif=exif)   # same desc+date -> (2)
    video(media / "2018-01-01 Already done.mp4")
    (media / "sub").mkdir()
    Image.new("RGB", (50, 50)).save(media / "sub" / "scan.png")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("MNTest")
    app.setApplicationName(f"MNTest{os.getpid()}")
    monkeypatch.setattr(appmod, "logs_dir", lambda: str(tmp_path / "logs"))
    os.makedirs(tmp_path / "logs", exist_ok=True)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))

    w = appmod.MainWindow()
    w.prefs.set("ollama/host", f"http://127.0.0.1:{srv.server_port}")
    w.prefs.set("ollama/parallel", 2)
    w.show()
    w.folder_edit.setText(str(media))
    w.recursive.setChecked(True)
    w.scan_folder()
    wait(app, lambda: not w.scanning)
    items = {i.name: i for i in w.model.items}
    assert sorted(items) == ["2018-01-01 Already done.mp4", "DSC0001.jpg", "DSC0002.jpg", "IMG_4417.MOV", "scan.png"]
    assert len(items["IMG_4417.MOV"].companions) == 1
    assert items["IMG_4417.MOV"].date_source == "camera" and items["DSC0001.jpg"].date.year == 2019
    assert items["scan.png"].date_source == "modified time"
    assert not items["2018-01-01 Already done.mp4"].include

    # refresh model list (background job)
    w.refresh_models()
    wait(app, lambda: "models available" in w.status_label.text())

    w.context_edit.setText("FIRST robotics competition at La Grange")
    w.describe_missing()
    wait(app, lambda: w.pending == 0)
    assert Fake.calls == 4, Fake.calls
    assert items["IMG_4417.MOV"].description == "La Grange robot runs"
    assert items["IMG_4417.MOV"].new_name.endswith(" La Grange robot runs.mov")
    assert items["DSC0001.jpg"].new_name == "2019-07-04 Golden retriever playing fetch.jpg"
    assert items["DSC0002.jpg"].new_name == "2019-07-04 Golden retriever playing fetch (2).jpg"
    w.selection_changed()
    app.processEvents()

    # screenshot for a visual check
    w.resize(1400, 640)
    for r in range(w.proxy.rowCount()):
        if w.proxy.index(r, 1).data().startswith("IMG_4417"):
            w.table.selectRow(r)
    wait(app, lambda: getattr(w, "_pixmap", None) is not None)
    app.processEvents()
    w.grab().save(str(tmp_path.parent / "screenshot.png"))

    # edit through the model like a user would
    src = w.model
    row = src.items.index(items["scan.png"])
    assert src.setData(src.index(row, COL_DESC), "Old receipt / scan")
    assert items["scan.png"].description == "Old receipt scan"
    row2 = src.items.index(items["DSC0002.jpg"])
    assert src.setData(src.index(row2, COL_NEW), "Buddy at the park")
    assert items["DSC0002.jpg"].new_name == "Buddy at the park.jpg" and items["DSC0002.jpg"].name_overridden

    # cache: a rescan keeps descriptions without calling the AI
    calls = Fake.calls
    w.scan_folder()
    wait(app, lambda: not w.scanning)
    assert Fake.calls == calls
    items = {i.name: i for i in w.model.items}
    assert items["DSC0001.jpg"].description == "Golden retriever playing fetch"
    src = w.model
    src.setData(src.index(src.items.index(items["DSC0002.jpg"]), COL_NEW), "Buddy at the park")

    w.apply_renames()
    names = sorted(p.name for p in media.iterdir() if p.is_file())
    assert "Buddy at the park.jpg" in names and "2019-07-04 Golden retriever playing fetch.jpg" in names
    vid = [n for n in names if n.endswith("La Grange robot runs.mov")]
    assert vid and vid[0][:-4] + ".jpg" in names, names                  # thumbnail followed the video
    assert "IMG_4417.MOV" not in names
    assert w.apply_btn.isEnabled() is False

    # undo last
    w.undo_last()
    wait(app, lambda: not w.scanning)
    names = sorted(p.name for p in media.iterdir() if p.is_file())
    assert names == sorted(["2018-01-01 Already done.mp4", "DSC0001.jpg", "DSC0002.jpg", "IMG_4417.MOV", "IMG_4417.jpg"])
    assert w._latest_log() is None

    # Ollama not running -> one clear warning, batch stops
    w.prefs.set("ollama/host", "http://127.0.0.1:9")
    w.describe_rows(list(range(len(w.model.items))), force=True)
    wait(app, lambda: w.pending == 0)
    assert len(warnings) == 1 and "isn't running" in warnings[0]
    w.close()
    srv.shutdown()


def test_thumbnail_flow(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    video(media / "IMG_5000.MOV", "2026-10-25T20:00:00Z")
    video(media / "2018-01-01 Already done.mp4")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("MNTest")
    app.setApplicationName(f"MNTestThumb{os.getpid()}")
    monkeypatch.setattr(appmod, "logs_dir", lambda: str(tmp_path / "logs"))
    os.makedirs(tmp_path / "logs", exist_ok=True)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    w = appmod.MainWindow()
    w.prefs.set("ollama/host", f"http://127.0.0.1:{srv.server_port}")
    w.prefs.set("thumb_width", 240)
    w.show()
    w.resize(1400, 680)
    w.folder_edit.setText(str(media))
    w.scan_folder()
    wait(app, lambda: not w.scanning)
    w.thumbs_check.setChecked(True)
    items = {i.name: i for i in w.model.items}
    vid, done = items["IMG_5000.MOV"], items["2018-01-01 Already done.mp4"]
    assert vid.make_thumb and not done.make_thumb
    assert "+new thumb" in w.model.data(w.model.index(w.model.items.index(vid), 1))

    w.describe_missing()
    wait(app, lambda: w.pending == 0)
    assert len(vid.frames) == 3 and 0 <= vid.thumb_choice < 3

    # select the video: frame strip shows 3 frames; pick the first one
    for r in range(w.proxy.rowCount()):
        if w.proxy.index(r, 1).data().startswith("IMG_5000"):
            w.table.selectRow(r)
    app.processEvents()
    assert w.frame_strip.isVisible() and sum(b.isVisible() for b in w.frame_buttons) == 3
    w.frame_buttons[0].click()
    app.processEvents()
    assert vid.thumb_choice == 0 and w.frame_buttons[0].isChecked()
    w.grab().save(str(tmp_path.parent / "screenshot-thumbs.png"))

    w.apply_renames()
    names = sorted(p.name for p in media.iterdir())
    thumb = [n for n in names if n.endswith(".jpg")]
    assert thumb == [vid.name[:-4] + ".jpg"], names
    im = Image.open(media / thumb[0])
    assert im.width == 240
    expected = Image.open(vid.frames[0]).resize(im.size).convert("L").getpixel((120, 80))
    assert abs(im.convert("L").getpixel((120, 80)) - expected) < 25          # it's frame 0
    assert vid.companions and not vid.make_thumb

    # undo removes the thumbnail and restores the name
    w.undo_last()
    wait(app, lambda: not w.scanning)
    assert sorted(p.name for p in media.iterdir()) == ["2018-01-01 Already done.mp4", "IMG_5000.MOV"]

    # thumbnail for an already-named video, without renaming it
    row = next(i for i, it in enumerate(w.model.items) if it.name.startswith("2018"))
    w.create_thumbnails_now([row])
    assert (media / "2018-01-01 Already done.jpg").exists()
    assert (media / "2018-01-01 Already done.mp4").exists()
    w.close()
    srv.shutdown()


def test_export_flow(tmp_path, monkeypatch):
    import hashlib
    from media_namer.media import probe
    from tests.test_export import raw_camera_clip

    src = tmp_path / "Video Staging area"
    src.mkdir()
    raw_camera_clip(src / "C0001.MOV", size="2560x1440", seconds=2, creation="2024-12-26T19:52:00Z")
    raw_camera_clip(src / "La Grange 10 Robot Runs.MOV", size="1280x720", seconds=2, creation="2019-04-16T20:49:00Z")
    Image.new("RGB", (320, 180), "orange").save(src / "La Grange 10 Robot Runs.jpg")
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "2024:12:26 12:00:00"
    Image.new("RGB", (300, 200), "green").save(src / "DSC0001.jpg", exif=exif)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in src.iterdir()}

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("MNTest")
    app.setApplicationName(f"MNTestExport{os.getpid()}")
    monkeypatch.setattr(appmod, "logs_dir", lambda: str(tmp_path / "logs"))
    os.makedirs(tmp_path / "logs", exist_ok=True)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    infos = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: infos.append(a[2]))

    w = appmod.MainWindow()
    w.prefs.set("ollama/host", f"http://127.0.0.1:{srv.server_port}")
    w.prefs.set("template", "{date} {orig}")
    w.show()
    w.resize(1400, 680)
    w.folder_edit.setText(str(src))
    w.scan_folder()
    wait(app, lambda: not w.scanning)
    w.action_combo.setCurrentIndex(w.action_combo.findData("export"))
    assert w.export_row.isVisible()
    assert w.out_edit.text() == str(tmp_path / "Video Staging area (web)")       # default: next to the source
    w.size_combo.setCurrentIndex(w.size_combo.findData(720))
    w.thumbs_check.setChecked(True)
    assert w.apply_btn.text() == "Export 3 files…"

    w.run_action()
    assert w.exporting
    wait(app, lambda: not w.exporting, timeout=120)
    w.grab().save(str(tmp_path.parent / "screenshot-export.png"))

    out = tmp_path / "Video Staging area (web)"
    got = sorted(p.name for p in out.iterdir())
    assert got == ["2019-04-16 La Grange 10 Robot Runs.jpg", "2019-04-16 La Grange 10 Robot Runs.mp4",
                   "2024-12-26 C0001.jpg", "2024-12-26 C0001.mp4", "2024-12-26 DSC0001.jpg"], got
    v = next(s for s in probe(w.tools, str(out / "2024-12-26 C0001.mp4"))["streams"] if s["codec_type"] == "video")
    assert (v["codec_name"], v["pix_fmt"], v["width"], v["height"]) == ("h264", "yuv420p", 1280, 720)
    assert Image.open(out / "2019-04-16 La Grange 10 Robot Runs.jpg").getpixel((5, 5))[0] > 200   # its own orange thumb
    # originals untouched: same names, same bytes
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in src.iterdir()} == before
    assert all(it.status == "exported" for it in w.model.items)
    assert "Exported 5 files" in w.status_label.text()

    # running again skips what's there
    w.run_action()
    assert infos and "already in the output folder" in infos[-1]

    # undo deletes the copies, leaves originals
    w.undo_last()
    assert list(out.iterdir()) == []
    assert {p.name for p in src.iterdir()} == set(before)
    w.close()
    srv.shutdown()


def test_split_dv_flow(tmp_path, monkeypatch):
    from tests.test_dvtape import tape
    raw, avi = tape(tmp_path, (2, "2007-09-23T21:47:00Z"), (2, "2007-09-24T10:05:30Z"), name="Tape1.avi")
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("MNTest")
    app.setApplicationName(f"MNTestSplit{os.getpid()}")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    w = appmod.MainWindow()
    w.show()
    out = tmp_path / "recordings"
    w.split_dv([str(avi)], str(out))
    assert w.splitting
    wait(app, lambda: not w.splitting and not w.scanning, timeout=60)
    assert sorted(os.listdir(out)) == ["Tape1.07-09-23_21-47.00.avi", "Tape1.07-09-24_10-05.30.avi"]
    # it offered to scan the recordings; dates come from the tape itself
    assert w.model.root == str(out)
    got = sorted((it.name, it.date.strftime("%Y-%m-%d %H:%M:%S"), it.date_source) for it in w.model.items)
    assert got == [("Tape1.07-09-23_21-47.00.avi", "2007-09-23 21:47:00", "tape"),
                   ("Tape1.07-09-24_10-05.30.avi", "2007-09-24 10:05:30", "tape")]
    w.close()


def test_face_names_reach_the_ai(tmp_path, monkeypatch):
    from media_namer import faces as facemod
    from tests.test_faces import FakeEngine, library

    media = tmp_path / "media"
    media.mkdir()
    video(media / "IMG_6000.MOV", "2023-10-01T18:00:00Z")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("MNTest")
    app.setApplicationName(f"MNTestFaces{os.getpid()}")

    class FramesShowSisters(FakeEngine):            # every frame shows Allison and Anika
        def decode(self, data):
            return b"Allison,Anika"
    monkeypatch.setattr(facemod, "models_present", lambda folder: True)
    monkeypatch.setattr(facemod, "FaceEngine", lambda folder: FramesShowSisters())

    w = appmod.MainWindow()
    w.people = library(tmp_path)                    # Allison, Anika, Annaclare with reference faces
    w.prefs.set("ollama/host", f"http://127.0.0.1:{srv.server_port}")
    w._update_faces_check()
    assert w.faces_check.isEnabled()
    w.faces_check.setChecked(True)
    w.folder_edit.setText(str(media))
    w.scan_folder()
    wait(app, lambda: not w.scanning)
    Fake.calls = 0
    sent = []
    orig = Fake.do_POST

    def capture(handler):
        body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
        sent.append(body["messages"][0]["content"])
        b = json.dumps({"message": {"content": "Allison and Anika picking apples in orchard"}}).encode()
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(b)))
        handler.end_headers()
        handler.wfile.write(b)
    monkeypatch.setattr(Fake, "do_POST", capture)
    w.describe_missing()
    wait(app, lambda: w.pending == 0)
    it = w.model.items[0]
    assert "Face recognition identified these people: Allison and Anika" in sent[0]
    assert "Annaclare" not in sent[0]                # not in the frames, so not mentioned
    assert it.people == ["Allison", "Anika"]
    assert it.new_name == "2023-10-01 Allison and Anika picking apples in orchard.mov" or \
        it.new_name.endswith(" Allison and Anika picking apples in orchard.mov")
    # turning faces off changes the cache key, so it would ask again
    k_on = w._cache_key(it)
    w.faces_check.setChecked(False)
    assert w._cache_key(it) != k_on
    w.close()
    srv.shutdown()


def test_people_dialog_smoke(tmp_path):
    from media_namer.people_dialog import PeopleDialog
    from tests.test_faces import library
    app = QApplication.instance() or QApplication([])
    lib = library(tmp_path, n_refs=2)
    d = PeopleDialog(lib, str(tmp_path / "models"), 0.40)
    assert d.people_list.count() == 3 and d.current_name() == "Allison"
    assert d.photo_list.count() == 2
    assert d.download_btn.isVisibleTo(d)             # models missing -> offer download
    d.people_list.setCurrentRow(1)
    assert d.current_name() == "Anika"
    d.resize(820, 560)
    d.show()
    app.processEvents()
    d.grab().save(str(tmp_path.parent / "screenshot-people.png"))
    d.close()
