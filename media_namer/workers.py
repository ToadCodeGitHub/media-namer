"""Background jobs. Each emits results back to the UI thread via Qt signals."""
from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QObject, QRunnable, Signal

from . import describe, media, scan
from .framestore import FrameStore


def _debuggable() -> None:
    """Let VS Code breakpoints work inside QThreadPool threads (no-op when not debugging)."""
    import sys
    debugpy = sys.modules.get("debugpy")
    if debugpy is not None:
        try:
            debugpy.debug_this_thread()
        except Exception:  # noqa: BLE001
            pass


class ScanSignals(QObject):
    progress = Signal(int, int, int)          # gen, done, total
    finished = Signal(int, object)            # gen, list[MediaItem]
    failed = Signal(int, str)


class ScanJob(QRunnable):
    def __init__(self, gen: int, folder: str, recursive: bool, tools: media.Tools,
                 cancel: threading.Event):
        super().__init__()
        self.gen, self.folder, self.recursive, self.tools, self.cancel = gen, folder, recursive, tools, cancel
        self.signals = ScanSignals()

    def run(self):
        _debuggable()
        try:
            items = scan.list_media(self.folder, self.recursive)
            self.signals.progress.emit(self.gen, 0, len(items))
            scan.fill_dates(self.tools, items,
                            progress=lambda d, t: self.signals.progress.emit(self.gen, d, t),
                            cancelled=self.cancel.is_set)
            self.signals.finished.emit(self.gen, items)
        except Exception as e:  # noqa: BLE001
            self.signals.failed.emit(self.gen, f"{e}\n{traceback.format_exc(limit=3)}")


class DescribeSignals(QObject):
    started = Signal(int, int)                # gen, row
    done = Signal(int, int, str, list, list)  # gen, row, raw text, saved frame paths, people found
    failed = Signal(int, int, str)
    retrying = Signal(int, int, str)
    cancelled = Signal(int, int)


class DescribeJob(QRunnable):
    def __init__(self, gen: int, row: int, item: scan.MediaItem, backend_cfg: dict,
                 context: str, tools: media.Tools, frames: int, cancel: threading.Event,
                 store: FrameStore | None = None, limiter: "describe.RateLimiter | None" = None,
                 faces=None, face_frames: int = 8):
        super().__init__()
        self.gen, self.row = gen, row
        self.path, self.kind = item.path, item.kind
        self.store, self.store_key = store, FrameStore.key(item.size, item.mtime_ns)
        self.limiter = limiter
        self.faces = faces                      # FaceIdentifier, or None when face recognition is off
        self.face_frames = face_frames          # frames scanned for faces (independent of frames sent to AI)
        self.backend_cfg, self.context, self.tools = backend_cfg, context, tools
        self.frames, self.cancel = frames, cancel
        self.signals = DescribeSignals()

    def run(self):
        _debuggable()
        s = self.signals
        if self.cancel.is_set():
            s.cancelled.emit(self.gen, self.row)
            return
        s.started.emit(self.gen, self.row)
        try:
            images = media.images_for(self.tools, self.path, self.kind, self.frames)
            saved = []
            if self.kind == "video" and self.store:
                saved = self.store.save(self.store_key, images)
            people = []
            if self.faces is not None:
                try:
                    try:
                        scan = media.face_images(self.tools, self.path, self.kind, self.face_frames)
                    except media.MediaError:
                        scan = images
                    people = [m.name for m in self.faces.identify(scan)]
                except Exception as e:  # noqa: BLE001 - faces are a bonus; describe anyway
                    s.retrying.emit(self.gen, self.row, f"face recognition skipped: {e}")
            prompt = describe.build_prompt(self.kind, len(images), self.context, people)
            backend = describe.make_backend(**self.backend_cfg)
            lim = self.limiter

            def attempt():
                if lim:
                    lim.wait(self.cancel.is_set)
                return backend.describe(images, prompt)

            def on_retry(n, d, e):
                if lim and getattr(e, "retry_after", 0):
                    lim.push_back(e.retry_after)            # everyone pauses, not just this file
                s.retrying.emit(self.gen, self.row, f"waiting {d:.0f}s (retry {n}): {e}")

            text = describe.with_retry(attempt, cancelled=self.cancel.is_set, on_retry=on_retry)
            s.done.emit(self.gen, self.row, text, saved, people)
        except describe.Cancelled:
            s.cancelled.emit(self.gen, self.row)
        except (describe.DescribeError, media.MediaError) as e:
            s.failed.emit(self.gen, self.row, str(e))
        except Exception as e:  # noqa: BLE001
            s.failed.emit(self.gen, self.row, f"{type(e).__name__}: {e}")


class PreviewSignals(QObject):
    ready = Signal(str, bytes)                # path, jpeg
    failed = Signal(str, str)


class PreviewJob(QRunnable):
    def __init__(self, path: str, kind: str, tools: media.Tools):
        super().__init__()
        self.path, self.kind, self.tools = path, kind, tools
        self.signals = PreviewSignals()

    def run(self):
        _debuggable()
        try:
            self.signals.ready.emit(self.path, media.preview_image(self.tools, self.path, self.kind))
        except Exception as e:  # noqa: BLE001
            self.signals.failed.emit(self.path, str(e))


class ModelListSignals(QObject):
    ready = Signal(str, list)                 # backend kind, names
    failed = Signal(str, str)


class ModelListJob(QRunnable):
    def __init__(self, backend_cfg: dict):
        super().__init__()
        self.cfg = backend_cfg
        self.signals = ModelListSignals()

    def run(self):
        _debuggable()
        kind = self.cfg.get("kind", "")
        try:
            names = describe.make_backend(**self.cfg).list_models()
            self.signals.ready.emit(kind, names)
        except Exception as e:  # noqa: BLE001
            self.signals.failed.emit(kind, str(e))


class FramesSignals(QObject):
    ready = Signal(str, list, int)            # video path, frame paths, best index
    failed = Signal(str, str)


class FramesJob(QRunnable):
    """Frames for a video's preview and thumbnail picker (from cache, or ffmpeg)."""

    def __init__(self, item: scan.MediaItem, tools: media.Tools, store: FrameStore, count: int):
        super().__init__()
        self.path = item.path
        self.key = FrameStore.key(item.size, item.mtime_ns)
        self.tools, self.store, self.count = tools, store, count
        self.signals = FramesSignals()

    def run(self):
        _debuggable()
        try:
            paths = self.store.paths(self.key)
            if not paths:
                paths = self.store.save(self.key, media.video_frames(self.tools, self.path, count=self.count))
            frames = []
            for p in paths:
                with open(p, "rb") as f:
                    frames.append(f.read())
            self.signals.ready.emit(self.path, paths, media.best_frame(frames))
        except Exception as e:  # noqa: BLE001
            self.signals.failed.emit(self.path, str(e))


def load_frames(item: scan.MediaItem, tools: media.Tools, store: FrameStore, count: int) -> tuple[list[str], int]:
    """Same as FramesJob, run inline (used just before renaming)."""
    key = FrameStore.key(item.size, item.mtime_ns)
    paths = store.paths(key) or store.save(key, media.video_frames(tools, item.path, count=count))
    frames = []
    for p in paths:
        with open(p, "rb") as f:
            frames.append(f.read())
    return paths, media.best_frame(frames)


class SplitSignals(QObject):
    progress = Signal(int, int, int, int)     # file index, frames done, estimated frames, recordings so far
    file_done = Signal(int, list, str)        # file index, output paths, error ("" if ok)
    finished = Signal(bool)                   # cancelled


class SplitJob(QRunnable):
    """Splits DV captures into one AVI per recording (originals untouched)."""

    def __init__(self, files: list[str], out_dir: str, tools: media.Tools, cancel: threading.Event):
        super().__init__()
        self.files, self.out_dir, self.tools, self.cancel = files, out_dir, tools, cancel
        self.signals = SplitSignals()

    def run(self):
        _debuggable()
        import os
        from . import dvtape
        s = self.signals
        for i, path in enumerate(self.files):
            if self.cancel.is_set():
                break
            est = max(1, os.path.getsize(path) // dvtape.NTSC_FRAME)
            try:
                scenes = dvtape.split_file(self.tools, path, self.out_dir,
                                           progress=lambda n, sc, i=i, est=est: s.progress.emit(i, n, est, len(sc)),
                                           cancel=self.cancel)
                s.file_done.emit(i, [sc.path for sc in scenes], "")
            except dvtape.Cancelled:
                s.file_done.emit(i, [], "cancelled")
                break
            except Exception as e:  # noqa: BLE001
                s.file_done.emit(i, [], str(e))
        s.finished.emit(self.cancel.is_set())


class ExportSignals(QObject):
    task_started = Signal(int)                # task index
    task_progress = Signal(int, float)        # task index, 0..1
    task_done = Signal(int, str, str)         # task index, result ("done"/"skipped"/"error"/"cancelled"), message
    finished = Signal(str, int, int, bool)    # log path, files written, failures, cancelled


class ExportJob(QRunnable):
    """Runs export tasks one at a time (ffmpeg already uses every CPU core)."""

    def __init__(self, tasks: list, tools: media.Tools, opts, thumb_width: int, log_path: str,
                 cancel: threading.Event):
        super().__init__()
        self.tasks, self.tools, self.opts = tasks, tools, opts
        self.thumb_width, self.log_path, self.cancel = thumb_width, log_path, cancel
        self.signals = ExportSignals()

    def run(self):
        _debuggable()
        import csv
        import os
        import shutil
        from . import encode
        from .export import PHOTO, THUMB, VIDEO

        s = self.signals
        written = failed = 0
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "w", newline="", encoding="utf-8") as f:
            log = csv.writer(f)
            log.writerow(["action", "from", "to"])
            f.flush()
            for i, t in enumerate(self.tasks):
                if self.cancel.is_set():
                    s.task_done.emit(i, "cancelled", "")
                    continue
                if t.skip or os.path.exists(t.dst):
                    s.task_done.emit(i, "skipped", "already in the output folder")
                    continue
                s.task_started.emit(i)
                part = t.dst + ".part"
                try:
                    os.makedirs(os.path.dirname(t.dst), exist_ok=True)
                    if t.kind == VIDEO:
                        encode.encode(self.tools, t.src, t.dst, self.opts, creation_time=t.date,
                                      progress=lambda p, i=i: s.task_progress.emit(i, p), cancel=self.cancel)
                    elif t.kind == THUMB:
                        with open(t.src, "rb") as src:
                            data = media.make_thumbnail(src.read(), self.thumb_width)
                        with open(part, "wb") as out:
                            out.write(data)
                        os.replace(part, t.dst)
                    elif t.kind == PHOTO:
                        shutil.copy2(t.src, part)
                        os.replace(part, t.dst)
                    log.writerow(["create", t.src, t.dst])
                    f.flush()
                    written += 1
                    s.task_done.emit(i, "done", "")
                except encode.Cancelled:
                    s.task_done.emit(i, "cancelled", "")
                except Exception as e:  # noqa: BLE001 - keep going with the next file
                    failed += 1
                    s.task_done.emit(i, "error", str(e))
                finally:
                    if os.path.exists(part):
                        try:
                            os.remove(part)
                        except OSError:
                            pass
        s.finished.emit(self.log_path, written, failed, self.cancel.is_set())
