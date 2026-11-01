"""Media Namer main window."""
from __future__ import annotations

import glob
import os
import sys
import threading
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QThreadPool, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QProgressBar, QProgressDialog, QPushButton, QSizePolicy,
                               QSplitter, QTableView, QToolButton, QVBoxLayout, QWidget)

from . import __version__, describe, media, proposals, renamer
from .cache import DescriptionCache
from .framestore import FrameStore
from .naming import clean_description
from .scan import (STATUS_ALREADY, STATUS_CACHED, STATUS_DONE, STATUS_ERROR, STATUS_QUEUED,
                   STATUS_RENAMED, STATUS_WORKING, MediaItem)
from .settings import Prefs, SettingsDialog, data_dir, logs_dir
from .table import (COL_DATE, COL_DESC, COL_FILE, COL_INCLUDE, COL_NEW, COL_SOURCE, COL_STATUS,
                    FILTER_NAMES, ItemFilter, ItemModel)
from .workers import DescribeJob, FramesJob, ModelListJob, PreviewJob, ScanJob, load_frames


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Media Namer")
        self.resize(1280, 760)
        self.prefs = Prefs()
        self.cache = DescriptionCache(os.path.join(data_dir(), "descriptions.json"))
        self.tools = media.Tools(self.prefs.get("ffmpeg"), self.prefs.get("ffprobe"))
        self.frame_store = FrameStore(os.path.join(data_dir(), "frames"))
        self.frame_store.cleanup()

        self.gen = 0                                   # bumps on each scan; stale results are ignored
        self.scan_cancel = threading.Event()
        self.ai_cancel = threading.Event()
        self.ai_pool = QThreadPool(self)
        self.preview_pool = QThreadPool(self)
        self.preview_pool.setMaxThreadCount(2)
        self.pending = 0                               # describe jobs not yet finished
        self.batch_total = 0
        self.batch_failed = 0
        self.scanning = False
        self.previews: dict[str, bytes] = {}
        self.current_preview = ""
        self.jobs = []                                 # keep job signal objects alive

        self.model = ItemModel(self)
        self.model.on_changed = self.recompute
        self.proxy = ItemFilter(self)
        self.proxy.setSourceModel(self.model)

        self._build_ui()
        self._load_prefs()
        self.thumbs_check.toggled.connect(self._thumbs_toggled)
        self._update_tools_note()
        self._update_buttons()

    # ================================================================ UI
    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)

        # Row 1: folder
        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText(r"Folder of photos and videos, e.g. D:\Organized_Media")
        self.folder_edit.returnPressed.connect(self.scan_folder)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_folder)
        self.recursive = QCheckBox("Include subfolders")
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.setDefault(True)
        self.scan_btn.clicked.connect(self.scan_folder)
        row.addWidget(QLabel("Folder"))
        row.addWidget(self.folder_edit, 1)
        row.addWidget(browse)
        row.addWidget(self.recursive)
        row.addWidget(self.scan_btn)
        outer.addLayout(row)

        # Row 2: AI
        row = QHBoxLayout()
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("Ollama (this PC)", describe.OLLAMA)
        self.backend_combo.addItem("Gemini (Google)", describe.GEMINI)
        self.backend_combo.currentIndexChanged.connect(self._backend_changed)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(220)
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        refresh = QToolButton()
        refresh.setText("↻")
        refresh.setToolTip("List available models")
        refresh.clicked.connect(self.refresh_models)
        self.privacy = QLabel()
        self.context_edit = QLineEdit()
        self.context_edit.setPlaceholderText("Context for the AI (optional), e.g. FIRST robotics competition at La Grange")
        self.context_edit.setToolTip("Names, places or events the AI can't know from the pictures.\n"
                                     "Remembered per folder.")
        row.addWidget(QLabel("AI"))
        row.addWidget(self.backend_combo)
        row.addWidget(self.model_combo)
        row.addWidget(refresh)
        row.addWidget(self.privacy)
        row.addSpacing(12)
        row.addWidget(self.context_edit, 1)
        outer.addLayout(row)

        # Row 3: actions
        row = QHBoxLayout()
        self.describe_btn = QPushButton("Describe all")
        self.describe_btn.setToolTip("Get AI descriptions for every checked file that doesn't have one yet")
        self.describe_btn.clicked.connect(self.describe_missing)
        self.describe_sel_btn = QPushButton("Describe selected")
        self.describe_sel_btn.setToolTip("Ask the AI again for the selected rows")
        self.describe_sel_btn.clicked.connect(lambda: self.describe_rows(self.selected_rows(), force=True))
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_work)
        self.apply_btn = QPushButton("Rename checked files…")
        self.apply_btn.clicked.connect(self.apply_renames)
        self.thumbs_check = QCheckBox("Create thumbnails")
        self.thumbs_check.setToolTip("For videos without a thumbnail image, save the best frame as a .jpg\n"
                                     "with the same name when renaming. Click a frame in the preview\n"
                                     "pane to choose a different one.")
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(FILTER_NAMES)
        self.filter_combo.currentIndexChanged.connect(self.proxy.set_mode)
        row.addWidget(self.describe_btn)
        row.addWidget(self.describe_sel_btn)
        row.addWidget(self.stop_btn)
        row.addSpacing(16)
        row.addWidget(self.thumbs_check)
        row.addStretch(1)
        row.addWidget(QLabel("Show"))
        row.addWidget(self.filter_combo)
        row.addSpacing(12)
        row.addWidget(self.apply_btn)
        outer.addLayout(row)

        # Table + preview
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_FILE, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                   QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)   # keep "(2).jpg" visible
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.table_menu)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_INCLUDE, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(COL_INCLUDE, 28)
        for col, w in ((COL_FILE, 210), (COL_DATE, 118), (COL_SOURCE, 92), (COL_DESC, 230),
                       (COL_STATUS, 120)):
            hh.resizeSection(col, w)
        hh.setSectionResizeMode(COL_NEW, QHeaderView.ResizeMode.Stretch)
        hh.setStretchLastSection(False)
        self.table.selectionModel().selectionChanged.connect(self.selection_changed)

        side = QWidget()
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(6, 0, 0, 0)
        self.preview = QLabel("Select a file to preview it")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(280, 200)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview.setStyleSheet("background: palette(base); border: 1px solid palette(mid); border-radius: 4px;")
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        open_btn = QPushButton("Open file")
        open_btn.clicked.connect(self.open_selected)

        self.frame_strip = QWidget()
        fs = QHBoxLayout(self.frame_strip)
        fs.setContentsMargins(0, 0, 0, 0)
        fs.setSpacing(4)
        self.frame_label = QLabel("Thumbnail")
        fs.addWidget(self.frame_label)
        self.frame_group = QButtonGroup(self)
        self.frame_group.setExclusive(True)
        self.frame_buttons: list[QToolButton] = []
        for i in range(6):
            b = QToolButton()
            b.setCheckable(True)
            b.setIconSize(QSize(88, 50))
            b.setToolTip("Use this frame as the thumbnail")
            b.setStyleSheet("QToolButton { border: 2px solid transparent; border-radius: 3px; padding: 1px; }"
                            "QToolButton:checked { border-color: palette(highlight); }")
            self.frame_group.addButton(b, i)
            self.frame_buttons.append(b)
            fs.addWidget(b)
        fs.addStretch(1)
        self.frame_group.idClicked.connect(self._frame_picked)
        self.frame_strip.setVisible(False)

        side_lay.addWidget(self.preview, 3)
        side_lay.addWidget(self.frame_strip)
        side_lay.addWidget(self.details, 2)
        side_lay.addWidget(open_btn)

        self.splitter = QSplitter()
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(side)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        outer.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

        # Status bar
        self.status_label = QLabel()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)

        # Menus
        m = self.menuBar().addMenu("&File")
        self._action(m, "&Open folder…", self.browse_folder, QKeySequence.StandardKey.Open)
        self._action(m, "Re&scan", self.scan_folder, QKeySequence("F5"))
        m.addSeparator()
        self._action(m, "&Export list to CSV…", self.export_csv)
        self._action(m, "&Import edits from CSV…", self.import_csv)
        m.addSeparator()
        self._action(m, "Undo &last rename…", self.undo_last)
        self._action(m, "&Undo a rename from a log…", self.undo_from_file)
        self._action(m, "Open rename &logs folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(logs_dir())))
        m.addSeparator()
        self._action(m, "Se&ttings…", self.open_settings, QKeySequence("Ctrl+,"))
        m.addSeparator()
        self._action(m, "E&xit", self.close, QKeySequence.StandardKey.Quit)
        h = self.menuBar().addMenu("&Help")
        self._action(h, "&About", lambda: QMessageBox.about(
            self, "Media Namer",
            f"<b>Media Namer {__version__}</b><p>Dated, descriptive names for family photos and videos. "
            "AI suggests; you review; every rename is logged and can be undone.</p>"))

        toggle = QAction(self)
        toggle.setShortcut(QKeySequence(Qt.Key.Key_Space))
        toggle.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        toggle.triggered.connect(lambda: self.set_include(None))
        self.table.addAction(toggle)

    def _action(self, menu: QMenu, text: str, slot, shortcut=None) -> QAction:
        a = menu.addAction(text)
        a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(shortcut)
        return a

    # ================================================================ prefs
    def _load_prefs(self) -> None:
        p = self.prefs
        self.folder_edit.setText(p.get("folder"))
        self.recursive.setChecked(p.get("recursive"))
        self.thumbs_check.setChecked(p.get("make_thumbs"))
        idx = self.backend_combo.findData(p.get("backend"))
        self.backend_combo.setCurrentIndex(max(0, idx))
        self._backend_changed()
        self.context_edit.setText(p.context_for(p.get("folder")) if p.get("folder") else "")
        geo = p.q.value("window/geometry")
        if geo:
            self.restoreGeometry(geo)
        state = p.q.value("window/splitter")
        if state:
            self.splitter.restoreState(state)
        cols = p.q.value("window/columns")
        if cols:
            hh = self.table.horizontalHeader()
            hh.restoreState(cols)
            hh.setSectionResizeMode(COL_NEW, QHeaderView.ResizeMode.Stretch)
            hh.setSectionResizeMode(COL_INCLUDE, QHeaderView.ResizeMode.Fixed)

    def _save_prefs(self) -> None:
        p = self.prefs
        p.set("folder", self.folder_edit.text().strip())
        p.set("recursive", self.recursive.isChecked())
        p.set("backend", self.backend())
        p.set(f"{self.backend()}/model", self.model_combo.currentText().strip())
        if self.model.root:
            p.set_context_for(self.model.root, self.context_edit.text().strip())
        p.q.setValue("window/geometry", self.saveGeometry())
        p.q.setValue("window/splitter", self.splitter.saveState())
        p.q.setValue("window/columns", self.table.horizontalHeader().saveState())

    def backend(self) -> str:
        return self.backend_combo.currentData()

    def backend_cfg(self) -> dict:
        kind = self.backend()
        model = self.model_combo.currentText().strip()
        return {"kind": kind, "ollama_host": self.prefs.get("ollama/host"), "ollama_model": model,
                "gemini_key": self.prefs.gemini_key(), "gemini_model": model}

    def _backend_changed(self) -> None:
        prev = getattr(self, "_last_backend", None)
        if prev and self.model_combo.currentText().strip():
            self.prefs.set(f"{prev}/model", self.model_combo.currentText().strip())
        kind = self.backend()
        self._last_backend = kind
        saved = self.prefs.get(f"{kind}/model")
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem(saved)
        self.model_combo.setCurrentText(saved)
        self.model_combo.blockSignals(False)
        if kind == describe.GEMINI:
            self.privacy.setText("⚠ sends images to Google")
            self.privacy.setStyleSheet("color: #b26a00;")
            self.privacy.setToolTip("Video frames and photos are uploaded to Google's Gemini API.\n"
                                    "On the free tier Google may use them to improve its products.")
        else:
            self.privacy.setText("🔒 stays on this PC")
            self.privacy.setStyleSheet("color: #2e7d32;")
            self.privacy.setToolTip("Images go only to Ollama on this computer (or the address in Settings).")

    def refresh_models(self) -> None:
        cfg = self.backend_cfg()
        job = ModelListJob(cfg)
        job.signals.ready.connect(self._models_ready)
        job.signals.failed.connect(lambda k, e: QMessageBox.warning(self, "Couldn't list models", e))
        self.jobs.append(job)
        QThreadPool.globalInstance().start(job)
        self.status_label.setText("Asking for the list of models…")

    def _models_ready(self, kind: str, names: list) -> None:
        if kind != self.backend():
            return
        current = self.model_combo.currentText()
        self.model_combo.clear()
        self.model_combo.addItems(names)
        self.model_combo.setCurrentText(current if current in names or not names else names[0])
        if names:
            self.status_label.setText(f"{len(names)} models available")
        elif kind == describe.OLLAMA:
            self.status_label.setText("No Ollama models installed. Run:  ollama pull qwen2.5vl:7b")
        else:
            self.status_label.setText("No models returned")

    def open_settings(self) -> None:
        if SettingsDialog(self.prefs, self).exec():
            self.tools = media.Tools(self.prefs.get("ffmpeg"), self.prefs.get("ffprobe"))
            self._update_tools_note()
            self.recompute()

    def _update_tools_note(self) -> None:
        if not self.tools.ok:
            self.status_label.setText("ffmpeg/ffprobe not found - videos can't be read. "
                                      "Install with:  winget install Gyan.FFmpeg   (or set the path in Settings)")

    # ================================================================ scanning
    def browse_folder(self) -> None:
        start = self.folder_edit.text() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", start)
        if folder:
            self.folder_edit.setText(os.path.normpath(folder))
            self.scan_folder()

    def scan_folder(self) -> None:
        folder = self.folder_edit.text().strip().strip('"')
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "Folder not found", f"Can't open this folder:\n{folder}")
            return
        if self.model.root:
            self.prefs.set_context_for(self.model.root, self.context_edit.text().strip())
        self.stop_work()
        self.gen += 1
        self.scan_cancel = threading.Event()
        self.scanning = True
        self.model.set_items([], "")
        self.previews.clear()
        self.context_edit.setText(self.prefs.context_for(folder))
        self.prefs.set("folder", folder)
        self.prefs.set("recursive", self.recursive.isChecked())
        job = ScanJob(self.gen, folder, self.recursive.isChecked(), self.tools, self.scan_cancel)
        job.signals.progress.connect(self._scan_progress)
        job.signals.finished.connect(lambda g, items: self._scan_done(g, items, folder))
        job.signals.failed.connect(self._scan_failed)
        self.jobs = [job]
        QThreadPool.globalInstance().start(job)
        self.status_label.setText(f"Scanning {folder}…")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self._update_buttons()

    def _scan_progress(self, gen: int, done: int, total: int) -> None:
        if gen != self.gen:
            return
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.status_label.setText(f"Reading dates… {done} of {total}")

    def _scan_done(self, gen: int, items: list[MediaItem], folder: str) -> None:
        if gen != self.gen:
            return
        self.scanning = False
        self.progress.setVisible(False)
        self._apply_cache(items)
        self.model.set_items(items, folder)
        self.recompute()
        self._update_buttons()
        videos = sum(1 for i in items if i.kind == "video")
        already = sum(1 for i in items if i.status == STATUS_ALREADY)
        guessed = sum(1 for i in items if i.date_source == "modified time")
        msg = f"{len(items)} files ({videos} videos, {len(items) - videos} photos)"
        if already:
            msg += f" - {already} already named"
        if guessed:
            msg += f" - {guessed} with no recorded date (amber)"
        self.status_label.setText(msg)
        if not self.tools.ok and videos:
            self._update_tools_note()

    def _scan_failed(self, gen: int, error: str) -> None:
        if gen != self.gen:
            return
        self.scanning = False
        self.progress.setVisible(False)
        self._update_buttons()
        QMessageBox.critical(self, "Scan failed", error)

    def _cache_key(self, it: MediaItem) -> str:
        return self.cache.key(it.size, it.mtime_ns, self.backend(), self.model_combo.currentText().strip(),
                              self.context_edit.text())

    def _apply_cache(self, items: list[MediaItem]) -> None:
        for it in items:
            if it.status == STATUS_ALREADY or it.description:
                continue
            hit = self.cache.get(self._cache_key(it))
            if hit:
                it.description, it.status = hit, STATUS_CACHED

    # ================================================================ naming
    def _thumbs_toggled(self, on: bool) -> None:
        self.prefs.set("make_thumbs", on)
        self.recompute()
        rows = self.selected_rows()
        if len(rows) == 1:
            self._show_frames(self.model.items[rows[0]])

    def _mark_thumbs(self) -> None:
        on = self.thumbs_check.isChecked()
        for it in self.model.items:
            it.make_thumb = (on and it.kind == "video" and not it.companions
                             and it.status not in (STATUS_ALREADY, STATUS_RENAMED))

    def recompute(self) -> None:
        self._mark_thumbs()
        proposals.recompute(self.model.items, self.prefs.get("template"), self.prefs.get("date_format"))
        self.model.refresh_rows()
        self._update_buttons()
        self._show_details()

    # ================================================================ describing
    def selected_rows(self) -> list[int]:
        rows = {self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()}
        return sorted(rows)

    def describe_missing(self) -> None:
        todo = [r for r, it in enumerate(self.model.items)
                if it.include and not it.description and it.status not in (STATUS_ALREADY, STATUS_RENAMED)]
        if not todo:
            self.status_label.setText("Every checked file already has a description. "
                                      "Select rows and use Describe selected to ask again.")
            return
        self.describe_rows(todo)

    def describe_rows(self, rows: list[int], force: bool = False) -> None:
        if not rows or self.scanning:
            return
        cfg = self.backend_cfg()
        if not cfg["ollama_model" if cfg["kind"] == describe.OLLAMA else "gemini_model"]:
            QMessageBox.warning(self, "No model", "Choose a model first (↻ lists the ones available).")
            return
        if cfg["kind"] == describe.GEMINI and not cfg["gemini_key"]:
            QMessageBox.warning(self, "No API key",
                                "Add a Gemini API key in Settings, or set the GEMINI_API_KEY environment variable.")
            return
        if any(self.model.items[r].kind == "video" for r in rows) and not self.tools.ok:
            QMessageBox.warning(self, "ffmpeg needed", "Videos need ffmpeg and ffprobe.\n\n"
                                "Install with:  winget install Gyan.FFmpeg\nthen restart Media Namer.")
            return
        context = self.context_edit.text().strip()
        if self.model.root:
            self.prefs.set_context_for(self.model.root, context)
        if self.pending == 0:
            self.ai_cancel = threading.Event()
            self.batch_total = self.batch_failed = 0
            self.jobs = [j for j in self.jobs if not isinstance(j, DescribeJob)]
        parallel = self.prefs.get(f"{cfg['kind']}/parallel")
        self.ai_pool.setMaxThreadCount(max(1, parallel))
        frames = self.prefs.get("frames")
        queued = 0
        for r in rows:
            it = self.model.items[r]
            if it.status in (STATUS_QUEUED, STATUS_WORKING):
                continue
            if not force:
                hit = self.cache.get(self._cache_key(it))
                if hit:
                    it.description, it.status, it.error = hit, STATUS_CACHED, ""
                    continue
            it.status, it.error = STATUS_QUEUED, ""
            job = DescribeJob(self.gen, r, it, cfg, context, self.tools, frames, self.ai_cancel,
                              store=self.frame_store)
            s = job.signals
            s.started.connect(self._job_started)
            s.done.connect(self._job_done)
            s.failed.connect(self._job_failed)
            s.retrying.connect(self._job_retrying)
            s.cancelled.connect(self._job_cancelled)
            self.jobs.append(job)
            self.ai_pool.start(job)
            queued += 1
        self.pending += queued
        self.batch_total += queued
        self.recompute()
        self._batch_progress()

    def _row_ok(self, gen: int, row: int) -> bool:
        return gen == self.gen and 0 <= row < len(self.model.items)

    def _job_started(self, gen: int, row: int) -> None:
        if self._row_ok(gen, row):
            self.model.items[row].status = STATUS_WORKING
            self.model.refresh_rows([row])

    def _job_retrying(self, gen: int, row: int, msg: str) -> None:
        if self._row_ok(gen, row):
            self.model.items[row].error = msg
            self.status_label.setText(f"{self.model.items[row].name}: {msg}")

    def _job_done(self, gen: int, row: int, text: str, frames: list) -> None:
        if not self._row_ok(gen, row):
            return
        it = self.model.items[row]
        if frames:
            it.thumb_choice = -1                 # new frames: pick the best again
            self._set_frames(it, frames)
            if it.path == self.current_preview:
                self._show_frames(it)
        desc = clean_description(text)
        if desc:
            it.description, it.status, it.error = desc, STATUS_DONE, ""
            it.name_overridden = False
            self.cache.put(self._cache_key(it), desc)
        else:
            it.status, it.error = STATUS_ERROR, f"The AI's answer couldn't be used: {text[:200]!r}"
            self.batch_failed += 1
        self._job_finished()

    def _job_failed(self, gen: int, row: int, error: str) -> None:
        if self._row_ok(gen, row):
            it = self.model.items[row]
            it.status, it.error = STATUS_ERROR, error
            self.batch_failed += 1
            self._job_finished()
            # A setup problem (wrong model, Ollama not running) will fail every file - stop early.
            setup = ("isn't running", "not found", "API key", "HTTP 401", "HTTP 403", "HTTP 404")
            if any(x in error for x in setup) and not self.ai_cancel.is_set():
                self.stop_work()
                QMessageBox.warning(self, "Describing stopped", error)

    def _job_cancelled(self, gen: int, row: int) -> None:
        if self._row_ok(gen, row):
            it = self.model.items[row]
            if it.status in (STATUS_QUEUED, STATUS_WORKING):
                it.status = ""
            self._job_finished()

    def _job_finished(self) -> None:
        self.pending = max(0, self.pending - 1)
        if self.pending == 0:
            self.cache.save()
        self.recompute()
        self._batch_progress()

    def _batch_progress(self) -> None:
        if self.pending > 0:
            done = self.batch_total - self.pending
            self.progress.setRange(0, self.batch_total)
            self.progress.setValue(done)
            self.progress.setVisible(True)
            self.status_label.setText(f"Describing… {done} of {self.batch_total}"
                                      + (f" ({self.batch_failed} failed)" if self.batch_failed else ""))
        elif self.batch_total:
            self.progress.setVisible(False)
            ok = self.batch_total - self.batch_failed
            self.status_label.setText(f"Described {ok} of {self.batch_total}."
                                      + (f" {self.batch_failed} failed - hover the Status column for details."
                                         if self.batch_failed else " Review the names, then click Rename."))
            self.batch_total = 0
        self._update_buttons()

    def stop_work(self) -> None:
        self.scan_cancel.set()
        self.ai_cancel.set()
        self.ai_pool.clear()                     # drop jobs that haven't started
        stuck = [i for i in self.model.items if i.status == STATUS_QUEUED]
        for it in stuck:
            it.status = ""
        self.pending = max(0, self.pending - len(stuck))
        self.model.refresh_rows()
        self._batch_progress()

    # ================================================================ table actions
    def table_menu(self, pos) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        menu = QMenu(self)
        menu.addAction("Describe again", lambda: self.describe_rows(rows, force=True))
        menu.addAction("Use suggested name", lambda: self._reset_names(rows))
        if any(self.model.items[r].kind == "video" and not self.model.items[r].companions for r in rows):
            menu.addAction("Create thumbnail now (keep name)", lambda: self.create_thumbnails_now(rows))
        menu.addSeparator()
        menu.addAction("Check", lambda: self.set_include(True))
        menu.addAction("Uncheck", lambda: self.set_include(False))
        menu.addSeparator()
        if len(rows) == 1:
            menu.addAction("Open file", self.open_selected)
            menu.addAction("Show in folder", lambda: self._show_in_folder(self.model.items[rows[0]].path))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def set_include(self, value: bool | None) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        if value is None:
            value = not all(self.model.items[r].include for r in rows)
        for r in rows:
            self.model.items[r].include = value
        self.recompute()

    def _reset_names(self, rows: list[int]) -> None:
        for r in rows:
            self.model.items[r].name_overridden = False
        self.recompute()

    def open_selected(self) -> None:
        rows = self.selected_rows()
        if rows:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.model.items[rows[0]].path))

    @staticmethod
    def _show_in_folder(path: str) -> None:
        if sys.platform == "win32":
            import subprocess
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    # ================================================================ preview
    def selection_changed(self, *_):
        self._show_details()
        self._update_buttons()
        rows = self.selected_rows()
        if len(rows) != 1:
            return
        it = self.model.items[rows[0]]
        self.current_preview = it.path
        if it.kind == "photo" and it.path in self.previews:
            self.frame_strip.setVisible(False)
            self._set_preview(self.previews[it.path])
            return
        if it.kind == "video":
            if it.frames:
                self._show_frames(it)
                return
            self.frame_strip.setVisible(False)
            if not self.tools.ok:
                self.preview.setText("Install ffmpeg to preview videos")
                return
            self.preview.setText("Loading…")
            job = FramesJob(it, self.tools, self.frame_store, self.prefs.get("frames"))
            job.signals.ready.connect(self._frames_ready)
            job.signals.failed.connect(lambda p, e: p == self.current_preview and self.preview.setText(f"No preview\n{e}"))
            self.preview_jobs = (getattr(self, "preview_jobs", []) + [job])[-50:]
            self.preview_pool.start(job)
            return
        self.frame_strip.setVisible(False)
        self.preview.setText("Loading…")
        job = PreviewJob(it.path, it.kind, self.tools)
        job.signals.ready.connect(self._preview_ready)
        job.signals.failed.connect(lambda p, e: p == self.current_preview and self.preview.setText(f"No preview\n{e}"))
        self.preview_jobs = (getattr(self, "preview_jobs", []) + [job])[-50:]
        self.preview_pool.start(job)

    def _item_by_path(self, path: str) -> MediaItem | None:
        return next((it for it in self.model.items if it.path == path), None)

    def _set_frames(self, it: MediaItem, paths: list[str], best: int | None = None) -> None:
        it.frames = list(paths)
        if not (0 <= it.thumb_choice < len(paths)):
            if best is None:
                try:
                    data = []
                    for p in paths:
                        with open(p, "rb") as f:
                            data.append(f.read())
                    best = media.best_frame(data)
                except OSError:
                    best = len(paths) // 2
            it.thumb_choice = best

    def _frames_ready(self, path: str, paths: list, best: int) -> None:
        it = self._item_by_path(path)
        if not it:
            return
        self._set_frames(it, paths, best)
        if path == self.current_preview:
            self._show_frames(it)

    def _show_frames(self, it: MediaItem) -> None:
        if it.kind != "video" or not it.frames:
            self.frame_strip.setVisible(False)
            return
        self.frame_label.setText("Thumbnail" if it.make_thumb else "Frames")
        for i, b in enumerate(self.frame_buttons):
            if i < len(it.frames):
                pm = QPixmap(it.frames[i])
                b.setIcon(QIcon(pm.scaled(b.iconSize(), Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)))
                b.setChecked(i == it.thumb_choice)
                b.setVisible(True)
            else:
                b.setVisible(False)
        self.frame_strip.setVisible(True)
        try:
            with open(it.frames[max(0, it.thumb_choice)], "rb") as f:
                self._set_preview(f.read())
        except OSError:
            pass

    def _frame_picked(self, i: int) -> None:
        rows = self.selected_rows()
        if len(rows) != 1:
            return
        it = self.model.items[rows[0]]
        if i < len(it.frames):
            it.thumb_choice = i
            self._show_frames(it)
            self._show_details()

    def _preview_ready(self, path: str, data: bytes) -> None:
        if len(self.previews) > 300:
            self.previews.clear()
        self.previews[path] = data
        if path == self.current_preview:
            self._set_preview(data)

    def _set_preview(self, data: bytes) -> None:
        pm = QPixmap()
        pm.loadFromData(data)
        self._pixmap = pm
        self._fit_preview()

    def _fit_preview(self) -> None:
        pm = getattr(self, "_pixmap", None)
        if pm and not pm.isNull():
            self.preview.setPixmap(pm.scaled(self.preview.size() * 0.98, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._fit_preview)

    def _show_details(self) -> None:
        rows = self.selected_rows()
        if len(rows) != 1:
            self.details.setText(f"{len(rows)} files selected" if rows else "")
            return
        it = self.model.items[rows[0]]
        lines = [f"<b>{_esc(it.name)}</b>", _esc(it.folder), ""]
        if it.date:
            lines.append(f"Taken: {it.date:%Y-%m-%d %H:%M} <i>(from {it.date_source})</i>")
        if it.companions:
            lines.append("Thumbnail: " + ", ".join(_esc(os.path.basename(c)) for c in it.companions))
        elif it.make_thumb:
            lines.append("Thumbnail: <i>will be made from the highlighted frame when renaming</i>")
        if it.new_name and it.new_name != it.name:
            lines.append(f"New name: <b>{_esc(it.new_name)}</b>")
        if it.error:
            lines.append(f"<span style='color:#c62828'>{_esc(it.error)}</span>")
        self.details.setText("<br>".join(lines))

    # ================================================================ renaming
    def _update_buttons(self) -> None:
        busy = self.pending > 0 or self.scanning
        has = bool(self.model.items)
        self.describe_btn.setEnabled(has and not self.scanning)
        self.describe_sel_btn.setEnabled(has and not self.scanning and bool(self.selected_rows()))
        self.stop_btn.setEnabled(busy)
        n = len(renamer.plan(self.model.items)) if has else 0
        n_thumbs = sum(1 for it in self.model.items
                       if it.make_thumb and it.include and it.new_name and it.new_name != it.name)
        files = sum(1 for it in self.model.items if it.include and it.new_name and it.new_name != it.name)
        self.apply_btn.setEnabled(files > 0 and not busy)
        self.apply_btn.setText(f"Rename {files} file{'s' if files != 1 else ''}…" if files else "Rename checked files…")
        self.apply_btn.setToolTip((f"{n} changes, including {n_thumbs} new thumbnail(s)" if n_thumbs
                                   else f"{n} renames including thumbnails") if n else "")

    def _ensure_frames(self, items: list[MediaItem]) -> bool:
        """Grab frames for videos that need a thumbnail but haven't been previewed or described."""
        need = [it for it in items if not it.frames]
        if not need:
            return True
        if not self.tools.ok:
            QMessageBox.warning(self, "ffmpeg needed", "Making thumbnails needs ffmpeg.")
            return False
        dlg = QProgressDialog("Reading video frames…", "Cancel", 0, len(need), self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(300)
        for i, it in enumerate(need):
            if dlg.wasCanceled():
                return False
            try:
                paths, best = load_frames(it, self.tools, self.frame_store, self.prefs.get("frames"))
                self._set_frames(it, paths, best)
            except media.MediaError as e:
                it.error = f"No thumbnail: {e}"
            dlg.setValue(i + 1)
            QApplication.processEvents()
        dlg.close()
        return True

    def _run_ops(self, ops: list[renamer.RenameOp]) -> str | None:
        """Apply ops with a progress dialog. Returns the log path, or None on failure."""
        log = os.path.join(logs_dir(), f"rename-{datetime.now():%Y%m%d-%H%M%S}.csv")
        dlg = QProgressDialog("Renaming…", "", 0, max(1, len(ops) * 2), self)
        dlg.setCancelButton(None)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(300)

        def progress(done, total):
            dlg.setMaximum(total)
            dlg.setValue(done)
            QApplication.processEvents()
        try:
            renamer.apply(ops, log, progress, thumb_width=self.prefs.get("thumb_width"))
        except renamer.RenameFailed as e:
            dlg.close()
            QMessageBox.critical(self, "Rename failed", f"{e}\n\nLog: {log}")
            return None
        dlg.close()
        for op in ops:                      # new thumbnails become companions
            if op.action == renamer.CREATE and 0 <= op.item_index < len(self.model.items):
                self.model.items[op.item_index].companions.append(op.dst)
        return log

    def create_thumbnails_now(self, rows: list[int]) -> None:
        targets = [(r, self.model.items[r]) for r in rows
                   if self.model.items[r].kind == "video" and not self.model.items[r].companions]
        if not targets or not self._ensure_frames([it for _, it in targets]):
            return
        ops = renamer.plan_thumbnails_only(targets)
        problems = renamer.validate(ops)
        if problems:
            box = QMessageBox(QMessageBox.Icon.Warning, "Can't create thumbnails",
                              f"{len(problems)} problem(s). Nothing was created.", parent=self)
            box.setDetailedText("\n".join(problems))
            box.exec()
            return
        log = self._run_ops(ops)
        if log:
            self.recompute()
            self.status_label.setText(f"Created {len(ops)} thumbnail(s). Undo with File › Undo last rename.")

    def apply_renames(self) -> None:
        wanting = [it for it in self.model.items
                   if it.make_thumb and it.include and it.new_name and it.new_name != it.name]
        if wanting and not self._ensure_frames(wanting):
            return
        ops = renamer.plan(self.model.items)
        if not ops:
            return
        problems = renamer.validate(ops)
        if problems:
            box = QMessageBox(QMessageBox.Icon.Warning, "Can't rename yet",
                              f"{len(problems)} problem(s) need fixing first. Nothing was renamed.", parent=self)
            box.setDetailedText("\n".join(problems))
            box.exec()
            return
        moves = [op for op in ops if op.action == renamer.MOVE]
        files = len({op.item_index for op in moves})
        thumbs = len(moves) - files
        new_thumbs = len(ops) - len(moves)
        msg = f"Rename {files} file{'s' if files != 1 else ''}"
        if thumbs:
            msg += f" and {thumbs} thumbnail{'s' if thumbs != 1 else ''}"
        msg += "?"
        if new_thumbs:
            msg += f"\n\nAlso create {new_thumbs} new thumbnail{'s' if new_thumbs != 1 else ''}."
        msg += "\n\nA log is saved so you can undo this from File › Undo last rename."
        if QMessageBox.question(self, "Rename files", msg) != QMessageBox.StandardButton.Yes:
            return
        log = self._run_ops(ops)
        if not log:
            return
        by_src = {os.path.normcase(op.src): op.dst for op in moves}
        for it in self.model.items:
            new = by_src.get(os.path.normcase(it.path))
            if new:
                self.previews.pop(it.path, None)
                it.path = new
                it.companions = [by_src.get(os.path.normcase(c), c) for c in it.companions]
                it.status, it.include, it.name_overridden, it.new_name = STATUS_RENAMED, False, False, ""
        self.recompute()
        extra = f" and created {new_thumbs} thumbnail(s)" if new_thumbs else ""
        self.status_label.setText(f"Renamed {files} files{extra}. Log: {log}")

    def _latest_log(self) -> str | None:
        logs = sorted(glob.glob(os.path.join(logs_dir(), "rename-*.csv")), reverse=True)
        for path in logs:
            if not renamer.log_is_undone(path) and renamer.read_log(path):
                return path
        return None

    def undo_last(self) -> None:
        log = self._latest_log()
        if not log:
            QMessageBox.information(self, "Undo", "There's no rename to undo.")
            return
        self._undo(log)

    def undo_from_file(self) -> None:
        log, _ = QFileDialog.getOpenFileName(self, "Choose a rename log", logs_dir(), "Rename logs (*.csv)")
        if log:
            self._undo(log)

    def _undo(self, log: str) -> None:
        if renamer.log_is_undone(log):
            QMessageBox.information(self, "Undo", "That rename was already undone.")
            return
        steps = renamer.describe_log(log)
        sample = "\n".join(steps[:8])
        more = f"\n…and {len(steps) - 8} more" if len(steps) > 8 else ""
        when = os.path.basename(log)[7:22].replace("-", " ")
        if QMessageBox.question(self, "Undo rename",
                                f"Undo {len(steps)} change(s) from {when}?\n\n{sample}{more}") \
                != QMessageBox.StandardButton.Yes:
            return
        restored, problems = renamer.undo(log)
        if problems:
            box = QMessageBox(QMessageBox.Icon.Warning, "Undo finished with problems",
                              f"Restored {restored} file(s). {len(problems)} couldn't be restored.", parent=self)
            box.setDetailedText("\n".join(problems))
            box.exec()
        else:
            self.status_label.setText(f"Restored {restored} original file name(s).")
        if self.model.root:
            self.scan_folder()

    # ================================================================ CSV
    def export_csv(self) -> None:
        if not self.model.items:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export list", os.path.join(
            os.path.expanduser("~"), "media-names.csv"), "CSV (*.csv)")
        if path:
            proposals.export_csv(self.model.items, path)
            self.status_label.setText(f"Saved {path}. Edit Description or New name, then File › Import edits.")

    def import_csv(self) -> None:
        if not self.model.items:
            QMessageBox.information(self, "Import", "Scan the folder first, then import your edits.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import edits", os.path.expanduser("~"), "CSV (*.csv)")
        if not path:
            return
        try:
            n = proposals.import_csv(self.model.items, path)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "Import failed", str(e))
            return
        self.recompute()
        self.status_label.setText(f"Imported edits for {n} file(s).")

    # ================================================================ close
    def closeEvent(self, e):
        if self.pending:
            if QMessageBox.question(self, "Quit", "Descriptions are still running. Stop and quit?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.stop_work()
        self.ai_pool.waitForDone(3000)
        self.cache.save()
        self._save_prefs()
        super().closeEvent(e)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> int:
    if sys.platform == "win32":
        try:  # taskbar groups by app, not by python.exe
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MediaNamer.MediaNamer")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    app.setOrganizationName("MediaNamer")
    app.setApplicationName("MediaNamer")
    app.setApplicationDisplayName("Media Namer")
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    return app.exec()
