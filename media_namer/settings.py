"""Saved preferences and the Settings dialog."""
from __future__ import annotations

import hashlib
import os

from PySide6.QtCore import QSettings, QStandardPaths
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QSpinBox, QWidget)

from . import describe
from .naming import DEFAULT_DATE_FORMAT, DEFAULT_TEMPLATE


def data_dir() -> str:
    path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    os.makedirs(path, exist_ok=True)
    return path


def logs_dir() -> str:
    path = os.path.join(data_dir(), "rename-logs")
    os.makedirs(path, exist_ok=True)
    return path


class Prefs:
    DEFAULTS = {
        "backend": describe.OLLAMA,
        "ollama/host": describe.DEFAULT_OLLAMA_HOST,
        "ollama/model": describe.DEFAULT_OLLAMA_MODEL,
        "ollama/parallel": 1,
        "gemini/key": "",
        "gemini/model": describe.DEFAULT_GEMINI_MODEL,
        "gemini/parallel": 4,
        "ffmpeg": "",
        "ffprobe": "",
        "template": DEFAULT_TEMPLATE,
        "date_format": DEFAULT_DATE_FORMAT,
        "frames": 3,
        "folder": "",
        "recursive": True,
        "make_thumbs": False,
        "thumb_width": 640,
    }

    def __init__(self):
        self.q = QSettings()

    def get(self, key: str):
        default = self.DEFAULTS.get(key)
        value = self.q.value(key, default)
        if isinstance(default, bool):
            return value in (True, "true", "1", 1)
        if isinstance(default, int):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        return "" if value is None else str(value)

    def set(self, key: str, value) -> None:
        self.q.setValue(key, value)

    def gemini_key(self) -> str:
        return self.get("gemini/key") or os.environ.get("GEMINI_API_KEY", "")

    def context_for(self, folder: str) -> str:
        return str(self.q.value(f"context/{self._fkey(folder)}", "") or "")

    def set_context_for(self, folder: str, text: str) -> None:
        self.q.setValue(f"context/{self._fkey(folder)}", text)

    @staticmethod
    def _fkey(folder: str) -> str:
        return hashlib.sha1(os.path.normcase(os.path.abspath(folder)).encode()).hexdigest()[:16]


class SettingsDialog(QDialog):
    def __init__(self, prefs: Prefs, parent=None):
        super().__init__(parent)
        self.prefs = prefs
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        form = QFormLayout(self)

        self.ollama_host = QLineEdit(prefs.get("ollama/host"))
        self.ollama_par = self._spin(prefs.get("ollama/parallel"), 1, 8)
        self.gemini_key = QLineEdit(prefs.get("gemini/key"))
        self.gemini_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemini_key.setPlaceholderText("Blank = use the GEMINI_API_KEY environment variable")
        self.gemini_par = self._spin(prefs.get("gemini/parallel"), 1, 16)
        self.ffmpeg = self._path_row(prefs.get("ffmpeg"), "ffmpeg")
        self.ffprobe = self._path_row(prefs.get("ffprobe"), "ffprobe")
        self.template = QLineEdit(prefs.get("template"))
        self.date_format = QLineEdit(prefs.get("date_format"))
        self.frames = self._spin(prefs.get("frames"), 1, 6)
        self.thumb_width = self._spin(prefs.get("thumb_width"), 160, 768)
        self.thumb_width.setSingleStep(40)
        self.thumb_width.setSuffix(" px wide")

        form.addRow(self._heading("Ollama (runs on this computer)"))
        form.addRow("Address", self.ollama_host)
        form.addRow("Files at a time", self.ollama_par)
        form.addRow(self._heading("Gemini (sends images to Google)"))
        form.addRow("API key", self.gemini_key)
        note = QLabel("Stored in your user settings (the registry on Windows) as plain text. "
                      "An environment variable is safer.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(placeholder-text);")
        form.addRow("", note)
        form.addRow("Files at a time", self.gemini_par)
        form.addRow(self._heading("Naming"))
        form.addRow("Name template", self.template)
        form.addRow("", self._hint("{date}, {desc} (AI), {orig} (current name). Default: {date} {desc}\n"
                                   "Use {date} {orig} to just add dates to files that already have good names."))
        form.addRow("Date format", self.date_format)
        form.addRow("", self._hint("Python strftime, e.g. %Y-%m-%d or %Y-%m-%d %H%M"))
        form.addRow("Video frames sent", self.frames)
        form.addRow("Thumbnail size", self.thumb_width)
        form.addRow(self._heading("Tools (blank = find on PATH)"))
        form.addRow("ffmpeg", self.ffmpeg[0])
        form.addRow("ffprobe", self.ffprobe[0])

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @staticmethod
    def _spin(value: int, lo: int, hi: int) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(value)
        return s

    @staticmethod
    def _heading(text: str) -> QLabel:
        lbl = QLabel(f"<b>{text}</b>")
        lbl.setContentsMargins(0, 8, 0, 0)
        return lbl

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: palette(placeholder-text);")
        return lbl

    def _path_row(self, value: str, name: str):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(value)
        btn = QPushButton("Browse…")
        btn.clicked.connect(lambda: self._browse(edit, name))
        lay.addWidget(edit)
        lay.addWidget(btn)
        return w, edit

    def _browse(self, edit: QLineEdit, name: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"Find {name}", "", f"{name} (*{'.exe' if os.name == 'nt' else ''})")
        if path:
            edit.setText(path)

    def accept(self) -> None:
        p = self.prefs
        p.set("ollama/host", self.ollama_host.text().strip() or describe.DEFAULT_OLLAMA_HOST)
        p.set("ollama/parallel", self.ollama_par.value())
        p.set("gemini/key", self.gemini_key.text().strip())
        p.set("gemini/parallel", self.gemini_par.value())
        p.set("template", self.template.text().strip() or DEFAULT_TEMPLATE)
        p.set("date_format", self.date_format.text().strip() or DEFAULT_DATE_FORMAT)
        p.set("frames", self.frames.value())
        p.set("thumb_width", self.thumb_width.value())
        p.set("ffmpeg", self.ffmpeg[1].text().strip())
        p.set("ffprobe", self.ffprobe[1].text().strip())
        super().accept()
