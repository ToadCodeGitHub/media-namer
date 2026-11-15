"""Local face recognition: find faces in frames and match them to people you've added.

Runs entirely on this PC with OpenCV's built-in face models:
  * YuNet  - face detector (about 230 KB)
  * SFace  - face "fingerprint" (128 numbers per face, about 37 MB)
Only the matched *names* are passed on to the AI; faces and photos never leave the PC.

Matching is deliberately cautious: a face is named only if it's clearly closer to one
person than to everyone else (sisters look alike), otherwise it stays unnamed.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable

import numpy as np

DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_FILE = "face_recognition_sface_2021dec.onnx"
MODEL_URLS = {
    DETECTOR_FILE: [
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + DETECTOR_FILE,
        "https://huggingface.co/opencv/face_detection_yunet/resolve/main/" + DETECTOR_FILE,
    ],
    RECOGNIZER_FILE: [
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/" + RECOGNIZER_FILE,
        "https://huggingface.co/opencv/face_recognition_sface/resolve/main/" + RECOGNIZER_FILE,
    ],
}
MIN_SIZES = {DETECTOR_FILE: 100_000, RECOGNIZER_FILE: 10_000_000}

DEFAULT_THRESHOLD = 0.40     # cosine similarity; OpenCV suggests 0.363 for "same person"
SISTER_MARGIN = 0.05         # best match must beat the next person by this much
MIN_FACE_PX = 24             # ignore tiny faces (unreliable)


class FaceError(Exception):
    pass


# ----------------------------------------------------------------- models

def models_present(folder: str) -> bool:
    return all(os.path.isfile(os.path.join(folder, f)) and os.path.getsize(os.path.join(folder, f)) >= MIN_SIZES[f]
               for f in MODEL_URLS)


def download_models(folder: str, progress: Callable[[str, int, int], None] | None = None,
                    cancel: threading.Event | None = None) -> None:
    """Download any missing model files (GitHub first, Hugging Face as a fallback)."""
    os.makedirs(folder, exist_ok=True)
    for name, urls in MODEL_URLS.items():
        dst = os.path.join(folder, name)
        if os.path.isfile(dst) and os.path.getsize(dst) >= MIN_SIZES[name]:
            continue
        errors = []
        for url in urls:
            part = dst + ".part"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MediaNamer"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
                    total = int(resp.headers.get("Content-Length") or 0)
                    done = 0
                    while chunk := resp.read(1 << 16):
                        if cancel is not None and cancel.is_set():
                            raise FaceError("cancelled")
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(name, done, total)
                if os.path.getsize(part) < MIN_SIZES[name]:
                    raise FaceError("download was incomplete")
                os.replace(part, dst)
                break
            except FaceError as e:
                if str(e) == "cancelled":
                    if os.path.exists(part):
                        os.remove(part)
                    raise
                errors.append(f"{url}: {e}")
            except OSError as e:
                errors.append(f"{url}: {e}")
            finally:
                if os.path.exists(part):
                    os.remove(part)
        else:
            raise FaceError(f"Couldn't download {name}:\n" + "\n".join(errors))


# ----------------------------------------------------------------- engine

@dataclass
class Face:
    box: tuple[int, int, int, int]          # x, y, w, h
    score: float
    row: object = None                      # detector output row (needed for alignment)
    embedding: np.ndarray | None = None


class FaceEngine:
    """YuNet + SFace. Not thread-safe: use it under FaceIdentifier's lock."""

    def __init__(self, folder: str, detect_score: float = 0.6):   # find more faces; matching stays strict
        try:
            import cv2
        except ImportError as e:
            raise FaceError("OpenCV isn't installed: pip install opencv-python-headless") from e
        if not models_present(folder):
            raise FaceError("Face models aren't downloaded yet (People… → Download face models).")
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(os.path.join(folder, DETECTOR_FILE), "", (320, 320),
                                                  detect_score, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(os.path.join(folder, RECOGNIZER_FILE), "")

    def decode(self, data: bytes):
        img = self.cv2.imdecode(np.frombuffer(data, np.uint8), self.cv2.IMREAD_COLOR)
        if img is None:
            raise FaceError("couldn't read image")
        return img

    def faces(self, img) -> list[Face]:
        h, w = img.shape[:2]
        self.detector.setInputSize((w, h))
        _, rows = self.detector.detect(img)
        out = []
        for r in (rows if rows is not None else []):
            x, y, fw, fh = (int(v) for v in r[:4])
            if fw < MIN_FACE_PX or fh < MIN_FACE_PX:
                continue
            face = Face((x, y, fw, fh), float(r[-1]), r)
            aligned = self.recognizer.alignCrop(img, r)
            face.embedding = normalize(self.recognizer.feature(aligned).flatten())
            out.append(face)
        return out

    def crop(self, img, face: Face):
        return self.recognizer.alignCrop(img, face.row)

    def encode_jpeg(self, img) -> bytes:
        ok, buf = self.cv2.imencode(".jpg", img, [self.cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise FaceError("couldn't encode image")
        return buf.tobytes()


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


# ----------------------------------------------------------------- people library

@dataclass
class Reference:
    id: str
    crop: str                    # path of the aligned face crop (relative to the library folder)
    embedding: np.ndarray
    source: str = ""


@dataclass
class Match:
    name: str
    score: float


class PeopleLibrary:
    """people.json + one folder of face crops per person, under the app's data folder."""

    def __init__(self, folder: str):
        self.folder = folder
        self.people: dict[str, list[Reference]] = {}
        self._load()

    @property
    def path(self) -> str:
        return os.path.join(self.folder, "people.json")

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for name, refs in (data.get("people") or {}).items():
            self.people[name] = [Reference(r["id"], r["crop"], normalize(np.array(r["emb"], dtype=np.float32)),
                                           r.get("source", "")) for r in refs]

    def save(self) -> None:
        os.makedirs(self.folder, exist_ok=True)
        data = {"version": 1, "people": {
            name: [{"id": r.id, "crop": r.crop, "emb": [round(float(x), 6) for x in r.embedding], "source": r.source}
                   for r in refs] for name, refs in self.people.items()}}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    # -- editing
    def names(self) -> list[str]:
        return sorted(self.people, key=str.casefold)

    def add_person(self, name: str) -> None:
        name = name.strip()
        if not name:
            raise FaceError("Enter a name.")
        if any(n.casefold() == name.casefold() for n in self.people):
            raise FaceError(f"{name} is already in the list.")
        self.people[name] = []
        self.save()

    def rename_person(self, old: str, new: str) -> None:
        new = new.strip()
        if not new or old not in self.people:
            return
        if new != old and any(n.casefold() == new.casefold() for n in self.people):
            raise FaceError(f"{new} is already in the list.")
        self.people[new] = self.people.pop(old)
        self.save()

    def remove_person(self, name: str) -> None:
        for r in self.people.pop(name, []):
            self._delete_crop(r)
        self.save()

    def add_reference(self, name: str, crop_jpeg: bytes, embedding: np.ndarray, source: str = "") -> Reference:
        if name not in self.people:
            raise FaceError(f"Unknown person {name}")
        rid = uuid.uuid4().hex[:10]
        rel = os.path.join(_safe_dir(name), rid + ".jpg")
        full = os.path.join(self.folder, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(crop_jpeg)
        ref = Reference(rid, rel, normalize(embedding), source)
        self.people[name].append(ref)
        self.save()
        return ref

    def remove_reference(self, name: str, rid: str) -> None:
        refs = self.people.get(name, [])
        for r in [r for r in refs if r.id == rid]:
            refs.remove(r)
            self._delete_crop(r)
        self.save()

    def _delete_crop(self, r: Reference) -> None:
        try:
            os.remove(os.path.join(self.folder, r.crop))
        except OSError:
            pass

    def crop_path(self, r: Reference) -> str:
        return os.path.join(self.folder, r.crop)

    # -- matching
    def ready(self) -> bool:
        return any(self.people.values())

    def fingerprint(self) -> str:
        """Changes whenever people or photos change (used in the description cache key)."""
        parts = sorted(f"{n}:{','.join(sorted(r.id for r in refs))}" for n, refs in self.people.items())
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]

    def match(self, embedding: np.ndarray, threshold: float = DEFAULT_THRESHOLD,
              margin: float = SISTER_MARGIN) -> Match | None:
        """Best person for one face, or None if nobody is a clear match."""
        e = normalize(embedding)
        scores = []
        for name, refs in self.people.items():
            if refs:
                scores.append((max(float(np.dot(e, r.embedding)) for r in refs), name))
        if not scores:
            return None
        scores.sort(reverse=True)
        best, name = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1.0
        if best >= threshold and best - second >= margin:
            return Match(name, best)
        return None


def _safe_dir(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip() or "person"
    return f"{keep}-{hashlib.sha1(name.encode()).hexdigest()[:6]}"


# ----------------------------------------------------------------- what the describe step uses

class FaceIdentifier:
    """Thread-safe 'who is in these frames?' shared by the describe workers."""

    def __init__(self, library: PeopleLibrary, engine_factory: Callable[[], FaceEngine],
                 threshold: float = DEFAULT_THRESHOLD):
        self.library = library
        self.threshold = threshold
        self._factory = engine_factory
        self._engine: FaceEngine | None = None
        self._lock = threading.Lock()

    def identify(self, images: list[bytes]) -> list[Match]:
        """People recognized in any of the images, best score each, in library order."""
        found: dict[str, float] = {}
        with self._lock:
            if self._engine is None:
                self._engine = self._factory()
            eng = self._engine
            for data in images:
                try:
                    img = eng.decode(data)
                    for face in eng.faces(img):
                        m = self.library.match(face.embedding, self.threshold)
                        if m and m.score > found.get(m.name, -1):
                            found[m.name] = m.score
                except FaceError:
                    continue
        order = {n: i for i, n in enumerate(self.library.names())}
        return [Match(n, s) for n, s in sorted(found.items(), key=lambda kv: order.get(kv[0], 99))]


def add_photo(engine: FaceEngine, library: PeopleLibrary, name: str, path: str) -> str:
    """Add the (largest) face in a photo as a reference for name. Returns a short note."""
    with open(path, "rb") as f:
        img = engine.decode(f.read())
    faces = engine.faces(img)
    if not faces:
        raise FaceError("no face found")
    face = max(faces, key=lambda fc: fc.box[2] * fc.box[3])
    library.add_reference(name, engine.encode_jpeg(engine.crop(img, face)), face.embedding, path)
    return "used the largest of several faces" if len(faces) > 1 else ""


def annotate(engine: FaceEngine, library: PeopleLibrary, path: str, threshold: float) -> tuple[bytes, list[str]]:
    """Draw boxes and names on a photo (for People… → Test a photo)."""
    cv2 = engine.cv2
    with open(path, "rb") as f:
        img = engine.decode(f.read())
    lines = []
    for i, face in enumerate(engine.faces(img), 1):
        m = library.match(face.embedding, threshold)
        x, y, w, h = face.box
        color = (60, 180, 75) if m else (0, 165, 255)
        label = f"{m.name} {m.score:.2f}" if m else "unknown"
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 2)
        cv2.putText(img, label, (x, max(15, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        best = sorted(((max(float(np.dot(face.embedding, r.embedding)) for r in refs), n)
                       for n, refs in library.people.items() if refs), reverse=True)[:3]
        detail = ", ".join(f"{n} {s:.2f}" for s, n in best) or "no people added yet"
        lines.append(f"Face {i}: {label}   (closest: {detail})")
    if not lines:
        lines.append("No faces found in this photo.")
    return engine.encode_jpeg(img), lines
