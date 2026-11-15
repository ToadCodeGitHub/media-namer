"""Qt table model for the list of photos and videos."""
from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QStyle

from . import dates
from .naming import tidy_user_text
from .scan import STATUS_ALREADY, STATUS_ERROR, STATUS_RENAMED, MediaItem

COL_INCLUDE, COL_FILE, COL_DATE, COL_SOURCE, COL_DESC, COL_NEW, COL_STATUS = range(7)
HEADERS = ["", "File", "Date", "Date from", "Description", "New name", "Status"]

AMBER = QColor("#b26a00")
RED = QColor("#c62828")


def _muted() -> QColor:
    return QApplication.palette().color(QPalette.ColorRole.PlaceholderText)


def parse_user_date(text: str) -> datetime | None:
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class ItemModel(QAbstractTableModel):
    """Rows are MediaItems. Edits call on_changed() so names can be recomputed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[MediaItem] = []
        self.root = ""
        self.export_mode = False              # New name column shows the web copy's name (.mp4)
        self.on_changed = lambda: None
        style = QApplication.style()
        self._icons = {"video": style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay),
                       "photo": style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)}

    # -- structure
    def set_items(self, items: list[MediaItem], root: str) -> None:
        self.beginResetModel()
        self.items, self.root = items, root
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                if section == COL_NEW and self.export_mode:
                    return "Web copy name"
                return HEADERS[section]
            if role == Qt.ItemDataRole.ToolTipRole:
                return {COL_INCLUDE: "Rename this file",
                        COL_DATE: "When it was taken. Double-click to change.",
                        COL_SOURCE: "Where the date came from",
                        COL_DESC: "AI suggestion. Double-click to edit.",
                        COL_NEW: "Double-click to type your own name."}.get(section)
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == COL_INCLUDE:
            f |= Qt.ItemFlag.ItemIsUserCheckable
        if index.column() in (COL_DATE, COL_DESC, COL_NEW):
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def refresh_rows(self, rows=None) -> None:
        if not self.items:
            return
        if rows is None:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(HEADERS) - 1))
        else:
            for r in rows:
                self.dataChanged.emit(self.index(r, 0), self.index(r, len(HEADERS) - 1))

    # -- data
    def _relpath(self, item: MediaItem) -> str:
        try:
            return os.path.relpath(item.path, self.root) if self.root else item.name
        except ValueError:
            return item.path

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        col = index.column()
        R = Qt.ItemDataRole

        if role == R.DisplayRole or role == R.EditRole:
            if col == COL_FILE:
                if role != R.DisplayRole:
                    return self._relpath(it)
                suffix = "  +thumb" if it.companions else "  +new thumb" if it.make_thumb else ""
                return self._relpath(it) + suffix
            if col == COL_DATE:
                if not it.date:
                    return ""
                fmt = "%Y-%m-%d %H:%M" if role == R.DisplayRole else "%Y-%m-%d %H:%M:%S"
                return it.date.strftime(fmt)
            if col == COL_SOURCE:
                return it.date_source
            if col == COL_DESC:
                return it.description
            if col == COL_NEW:
                if self.export_mode and it.include:
                    stem = os.path.splitext(it.new_name)[0] if it.new_name else it.stem
                    return stem + (".mp4" if it.kind == "video" else it.ext.lower())
                return it.new_name
            if col == COL_STATUS:
                return it.status
        if role == R.CheckStateRole and col == COL_INCLUDE:
            return Qt.CheckState.Checked if it.include else Qt.CheckState.Unchecked
        if role == R.DecorationRole and col == COL_FILE:
            return self._icons.get(it.kind)
        if role == R.ForegroundRole:
            if col == COL_SOURCE and it.date_source == dates.SOURCE_MODIFIED:
                return QBrush(AMBER)
            if col == COL_STATUS and it.status == STATUS_ERROR:
                return QBrush(RED)
            if not it.include or it.status in (STATUS_ALREADY, STATUS_RENAMED):
                return QBrush(_muted())
        if role == R.FontRole and col == COL_NEW and it.name_overridden:
            f = QFont()
            f.setItalic(True)
            return f
        if role == R.ToolTipRole:
            if col == COL_FILE:
                tip = it.path
                if it.companions:
                    tip += "\nThumbnail renamed with it: " + ", ".join(os.path.basename(c) for c in it.companions)
                elif it.make_thumb:
                    tip += "\nA thumbnail will be made from a video frame when renaming."
                return tip
            if col == COL_SOURCE and it.date_source == dates.SOURCE_MODIFIED:
                return ("No date in the file, so this is its modified time - often when it was\n"
                        "copied, not when it was taken. Double-click the date to fix it.")
            if col == COL_STATUS and it.error:
                return it.error
            if col == COL_NEW and it.name_overridden:
                return "Typed by hand. Right-click > Use suggested name to undo."
            if col in (COL_DESC, COL_NEW):
                return self.data(index, R.DisplayRole) or None
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid():
            return False
        it = self.items[index.row()]
        col = index.column()
        if col == COL_INCLUDE and role == Qt.ItemDataRole.CheckStateRole:
            it.include = Qt.CheckState(value) == Qt.CheckState.Checked
        elif role != Qt.ItemDataRole.EditRole:
            return False
        elif col == COL_DESC:
            it.description = tidy_user_text(str(value))
        elif col == COL_NEW:
            name = tidy_user_text(str(value))
            if not name:
                it.name_overridden = False
            else:
                ext = os.path.splitext(name)[1].lower()
                if it.kind == "video" and ext == ".mp4" and it.ext.lower() != ".mp4":
                    name = os.path.splitext(name)[0]          # typed the web copy's name
                    ext = ""
                if ext != it.ext.lower():
                    name += it.ext.lower()
                it.new_name, it.name_overridden = name, True
        elif col == COL_DATE:
            dt = parse_user_date(str(value))
            if dt is None:
                return False
            it.date, it.date_source = dt, dates.SOURCE_MANUAL
        else:
            return False
        self.on_changed()
        self.refresh_rows([index.row()])
        return True


FILTER_ALL, FILTER_TODO, FILTER_REVIEW, FILTER_DONE = range(4)
FILTER_NAMES = ["All files", "To rename", "Needs a look", "Already named / renamed"]


class ItemFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = FILTER_ALL
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_mode(self, mode: int) -> None:
        self.mode = mode
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if self.mode == FILTER_ALL:
            return True
        it = self.sourceModel().items[row]
        done = it.status in (STATUS_ALREADY, STATUS_RENAMED)
        if self.mode == FILTER_DONE:
            return done
        if self.mode == FILTER_TODO:
            return it.include and not done
        # needs a look: errors, guessed dates, or no description yet
        return (not done) and (it.status == STATUS_ERROR or it.date_source == dates.SOURCE_MODIFIED
                               or (it.include and not it.description))
