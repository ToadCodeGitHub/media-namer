"""People… dialog: who the face recognition should know, with reference photos."""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog,
                               QLabel, QListView, QListWidget, QListWidgetItem, QMessageBox, QProgressDialog,
                               QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget)

from . import faces


class _DownloadSignals(QObject):
    progress = Signal(str, int, int)
    finished = Signal(str)                   # "" on success, else the error


class _DownloadJob(QRunnable):
    def __init__(self, folder: str, cancel: threading.Event):
        super().__init__()
        self.folder, self.cancel = folder, cancel
        self.signals = _DownloadSignals()

    def run(self):
        try:
            faces.download_models(self.folder, lambda n, d, t: self.signals.progress.emit(n, d, t), self.cancel)
            self.signals.finished.emit("")
        except Exception as e:  # noqa: BLE001
            self.signals.finished.emit(str(e))


class PeopleDialog(QDialog):
    def __init__(self, library: faces.PeopleLibrary, models_dir: str, threshold: float, parent=None):
        super().__init__(parent)
        self.library, self.models_dir, self.threshold = library, models_dir, threshold
        self._engine: faces.FaceEngine | None = None
        self.setWindowTitle("People")
        self.resize(820, 560)
        lay = QVBoxLayout(self)

        intro = QLabel("Media Namer can recognize these people in your videos and photos, and pass only their "
                       "<b>names</b> to the AI. Faces are matched on this PC; nothing here is uploaded.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # models
        row = QHBoxLayout()
        self.models_label = QLabel()
        self.download_btn = QPushButton("Download face models (37 MB)")
        self.download_btn.clicked.connect(self.download)
        open_btn = QPushButton("Open models folder")
        open_btn.clicked.connect(lambda: (os.makedirs(self.models_dir, exist_ok=True),
                                          QDesktopServices.openUrl(QUrl.fromLocalFile(self.models_dir))))
        row.addWidget(self.models_label, 1)
        row.addWidget(self.download_btn)
        row.addWidget(open_btn)
        lay.addLayout(row)

        # people | photos
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("<b>People</b>"))
        self.people_list = QListWidget()
        self.people_list.currentItemChanged.connect(lambda *_: self.show_photos())
        ll.addWidget(self.people_list, 1)
        b = QHBoxLayout()
        for text, slot in (("Add…", self.add_person), ("Rename…", self.rename_person), ("Remove", self.remove_person)):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            b.addWidget(btn)
        ll.addLayout(b)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.photos_title = QLabel("<b>Reference faces</b>")
        rl.addWidget(self.photos_title)
        self.photo_list = QListWidget()
        self.photo_list.setViewMode(QListView.ViewMode.IconMode)
        self.photo_list.setIconSize(QSize(96, 96))
        self.photo_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.photo_list.setSpacing(6)
        self.photo_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        rl.addWidget(self.photo_list, 1)
        b = QHBoxLayout()
        self.add_photos_btn = QPushButton("Add photos…")
        self.add_photos_btn.clicked.connect(self.add_photos)
        rm = QPushButton("Remove selected")
        rm.clicked.connect(self.remove_photos)
        test = QPushButton("Test a photo…")
        test.setToolTip("Pick any photo to see which faces are found and how well they match")
        test.clicked.connect(self.test_photo)
        b.addWidget(self.add_photos_btn)
        b.addWidget(rm)
        b.addStretch(1)
        b.addWidget(test)
        rl.addLayout(b)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 3)
        lay.addWidget(split, 1)

        tips = QLabel("Tips: add 5-10 clear photos per person, one face per photo works best. Kids change a lot, "
                      "so include photos from each age your videos cover. A face that doesn't clearly match one "
                      "person is left unnamed - the AI is never told to guess.")
        tips.setWordWrap(True)
        tips.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(tips)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.refresh()

    # -- helpers
    def refresh(self, select: str | None = None) -> None:
        ok = faces.models_present(self.models_dir)
        self.models_label.setText("Face models: <span style='color:#2e7d32'>ready</span>" if ok else
                                  "Face models: <span style='color:#b26a00'>not downloaded</span>")
        self.download_btn.setVisible(not ok)
        current = select or (self.people_list.currentItem().data(Qt.ItemDataRole.UserRole)
                             if self.people_list.currentItem() else None)
        self.people_list.clear()
        for name in self.library.names():
            n = len(self.library.people[name])
            item = QListWidgetItem(f"{name}  ({n} photo{'s' if n != 1 else ''})")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.people_list.addItem(item)
            if name == current:
                self.people_list.setCurrentItem(item)
        if self.people_list.currentItem() is None and self.people_list.count():
            self.people_list.setCurrentRow(0)
        self.show_photos()

    def current_name(self) -> str | None:
        it = self.people_list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def show_photos(self) -> None:
        self.photo_list.clear()
        name = self.current_name()
        self.add_photos_btn.setEnabled(bool(name))
        self.photos_title.setText(f"<b>Reference faces for {name}</b>" if name else "<b>Reference faces</b>")
        for r in self.library.people.get(name, []) if name else []:
            pm = QPixmap(self.library.crop_path(r))
            item = QListWidgetItem(QIcon(pm), "" if not pm.isNull() else "(missing)")
            item.setData(Qt.ItemDataRole.UserRole, r.id)
            item.setToolTip(r.source or "")
            self.photo_list.addItem(item)

    def engine(self) -> faces.FaceEngine | None:
        if self._engine is None:
            try:
                self._engine = faces.FaceEngine(self.models_dir)
            except faces.FaceError as e:
                QMessageBox.warning(self, "Face recognition", str(e))
                return None
        return self._engine

    # -- models
    def download(self) -> None:
        cancel = threading.Event()
        dlg = QProgressDialog("Downloading face models…", "Cancel", 0, 100, self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.canceled.connect(cancel.set)
        job = _DownloadJob(self.models_dir, cancel)
        done = {}

        def progress(name, d, t):
            dlg.setLabelText(f"Downloading {name}…")
            dlg.setValue(int(100 * d / t) if t else 0)

        def finished(err):
            done["err"] = err
            dlg.close()
        job.signals.progress.connect(progress)
        job.signals.finished.connect(finished)
        self._job = job
        QThreadPool.globalInstance().start(job)
        dlg.exec()
        QThreadPool.globalInstance().waitForDone(100)
        while "err" not in done:
            QApplication.processEvents()
        if done["err"] and done["err"] != "cancelled":
            QMessageBox.warning(self, "Download failed",
                                done["err"] + f"\n\nYou can also download the two .onnx files by hand into:\n{self.models_dir}")
        self.refresh()

    # -- people
    def add_person(self) -> None:
        name, ok = QInputDialog.getText(self, "Add person", "Name (as it should appear in file names):")
        if ok and name.strip():
            try:
                self.library.add_person(name)
            except faces.FaceError as e:
                QMessageBox.warning(self, "Add person", str(e))
                return
            self.refresh(select=name.strip())

    def rename_person(self) -> None:
        old = self.current_name()
        if not old:
            return
        new, ok = QInputDialog.getText(self, "Rename", "New name:", text=old)
        if ok and new.strip() and new.strip() != old:
            try:
                self.library.rename_person(old, new)
            except faces.FaceError as e:
                QMessageBox.warning(self, "Rename", str(e))
                return
            self.refresh(select=new.strip())

    def remove_person(self) -> None:
        name = self.current_name()
        if name and QMessageBox.question(self, "Remove", f"Remove {name} and their reference faces?") \
                == QMessageBox.StandardButton.Yes:
            self.library.remove_person(name)
            self.refresh()

    # -- photos
    def add_photos(self) -> None:
        name = self.current_name()
        if not name:
            return
        eng = self.engine()
        if eng is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, f"Photos of {name}", os.path.expanduser("~"),
                                                "Images (*.jpg *.jpeg *.png *.webp *.bmp)")
        if not paths:
            return
        dlg = QProgressDialog(f"Adding photos of {name}…", "Cancel", 0, len(paths), self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        added, notes = 0, []
        for i, p in enumerate(paths):
            if dlg.wasCanceled():
                break
            try:
                note = faces.add_photo(eng, self.library, name, p)
                added += 1
                if note:
                    notes.append(f"{os.path.basename(p)}: {note}")
            except (faces.FaceError, OSError) as e:
                notes.append(f"{os.path.basename(p)}: {e}")
            dlg.setValue(i + 1)
            QApplication.processEvents()
        dlg.close()
        self.refresh(select=name)
        if notes:
            box = QMessageBox(QMessageBox.Icon.Information, "Photos added",
                              f"Added {added} of {len(paths)} photo(s).", parent=self)
            box.setDetailedText("\n".join(notes))
            box.exec()

    def remove_photos(self) -> None:
        name = self.current_name()
        for item in self.photo_list.selectedItems():
            self.library.remove_reference(name, item.data(Qt.ItemDataRole.UserRole))
        self.refresh(select=name)

    def test_photo(self) -> None:
        eng = self.engine()
        if eng is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Test a photo", os.path.expanduser("~"),
                                              "Images (*.jpg *.jpeg *.png *.webp *.bmp)")
        if not path:
            return
        try:
            jpeg, lines = faces.annotate(eng, self.library, path, self.threshold)
        except (faces.FaceError, OSError) as e:
            QMessageBox.warning(self, "Test a photo", str(e))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Test: {os.path.basename(path)}")
        v = QVBoxLayout(dlg)
        pic = QLabel()
        pm = QPixmap()
        pm.loadFromData(jpeg)
        pic.setPixmap(pm.scaled(900, 600, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        area = QScrollArea()
        area.setWidget(pic)
        area.setWidgetResizable(True)
        v.addWidget(area, 1)
        txt = QLabel("\n".join(lines) + f"\n\nA name is used when its score is at least {self.threshold:.2f} "
                     "and clearly above the next person.")
        txt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(txt)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dlg.reject)
        v.addWidget(close)
        dlg.resize(940, 720)
        dlg.exec()
