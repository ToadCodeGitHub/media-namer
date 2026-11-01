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
    done = Signal(int, int, str, list)        # gen, row, raw text, saved frame paths
    failed = Signal(int, int, str)
    retrying = Signal(int, int, str)
    cancelled = Signal(int, int)


class DescribeJob(QRunnable):
    def __init__(self, gen: int, row: int, item: scan.MediaItem, backend_cfg: dict,
                 context: str, tools: media.Tools, frames: int, cancel: threading.Event,
                 store: FrameStore | None = None):
        super().__init__()
        self.gen, self.row = gen, row
        self.path, self.kind = item.path, item.kind
        self.store, self.store_key = store, FrameStore.key(item.size, item.mtime_ns)
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
            prompt = describe.build_prompt(self.kind, len(images), self.context)
            backend = describe.make_backend(**self.backend_cfg)
            text = describe.with_retry(
                lambda: backend.describe(images, prompt),
                cancelled=self.cancel.is_set,
                on_retry=lambda n, d, e: s.retrying.emit(self.gen, self.row, f"retry {n} in {d:.0f}s: {e}"))
            s.done.emit(self.gen, self.row, text, saved)
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
