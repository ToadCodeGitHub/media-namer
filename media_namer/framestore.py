"""On-disk cache of the video frames grabbed for the AI, reused for previews
and thumbnails. Keyed by file size + modified time, so renames don't matter."""
from __future__ import annotations

import os
import time


class FrameStore:
    def __init__(self, folder: str):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    @staticmethod
    def key(size: int, mtime_ns: int) -> str:
        return f"{size}-{mtime_ns}"

    def paths(self, key: str) -> list[str]:
        out = []
        for i in range(12):
            p = os.path.join(self.folder, f"{key}-{i}.jpg")
            if not os.path.isfile(p):
                break
            out.append(p)
        return out

    def save(self, key: str, frames: list[bytes]) -> list[str]:
        paths = []
        for i, data in enumerate(frames):
            p = os.path.join(self.folder, f"{key}-{i}.jpg")
            tmp = f"{p}.{os.getpid()}.{id(frames)}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            paths.append(p)
        stale = os.path.join(self.folder, f"{key}-{len(frames)}.jpg")   # fewer frames than last time
        if os.path.exists(stale):
            os.remove(stale)
        return paths

    def cleanup(self, max_age_days: int = 60) -> None:
        cutoff = time.time() - max_age_days * 86400
        try:
            for name in os.listdir(self.folder):
                p = os.path.join(self.folder, name)
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
        except OSError:
            pass
