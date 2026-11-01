# Media Namer

A desktop app (Python + PySide6/Qt) that gives family photos and videos dated,
descriptive names, such as:

    IMG_4417.MOV   →   2026-10-25 La Grange robot runs.mov
    IMG_4417.jpg   →   2026-10-25 La Grange robot runs.jpg      (thumbnail follows its video)
    DSC0001.jpg    →   2019-07-04 Golden retriever playing fetch.jpg

The AI suggests a description. You review and edit the names in a table
before anything changes. Every rename is logged and can be undone.
Optionally, it also creates a `.jpg` thumbnail for each video from the best
frame (you can pick a different one).

These names work with the family video site's gallery, which shows a file's
name as its title, with underscores turned into spaces.

## Setup (Windows)

1. **Python 3.11 or newer**, then from this folder:
   ```
   pip install -e .[gemini,heic]
   ```
   (`gemini` adds Google's SDK and `heic` adds iPhone photo support; both are optional.)
2. **ffmpeg**, needed for videos:
   ```
   winget install Gyan.FFmpeg
   ```
   Open a new terminal afterwards so it's on your PATH. You can also set the
   path in Settings.
3. **Choose an AI:**
   - **Ollama (recommended for family media):** images never leave your PC.
     Install it from ollama.com, then run:
     ```
     ollama pull qwen2.5vl:7b
     ```
     Any vision model works. The ↻ button lists the ones you have installed.
     A GPU with about 8 GB of VRAM makes it quick; on CPU alone it works, but slowly.
   - **Gemini:** fast and cheap, but **images are sent to Google**, and on the
     free tier Google may use them to improve its products. Get a key at
     aistudio.google.com and set it as an environment variable:
     ```
     setx GEMINI_API_KEY "your-key"
     ```
     You can also paste the key into Settings, but it's stored there as plain
     text. The default model is `gemini-3.5-flash-lite`; ↻ lists the models
     your key can use.

Run it:
```
media-namer
```
or `python -m media_namer`.

## Using it

1. **Pick a folder** and click **Scan**. The app reads each file's date
   (nothing is renamed yet).
2. Type a **Context** line for anything the AI can't see, such as names,
   places or events ("FIRST robotics competition at La Grange", "Grandma
   Ruth's 80th"). It's remembered for each folder.
3. Click **Describe all**. Each video sends 3 frames (start, middle and end);
   each photo sends a downscaled copy.
4. **Review the table:**
   - Double-click a **Description**, **Date**, or **New name** to edit it. A
     name you type yourself is shown in *italics* and the AI won't change it.
     To get the suggestion back, right-click and choose **Use suggested name**.
   - **Amber "modified time"** means the file has no recorded date, so the
     date shown is probably when the file was copied. Fix those by hand.
   - **Space** checks or unchecks the selected rows. Use **Show → Needs a
     look** to see errors, guessed dates, and files without a description.
   - **Describe selected** asks the AI again for the selected rows.
5. Click **Rename N files…**. The app checks the whole batch first and
   renames nothing if any problem is found.

### Thumbnails

Tick **Create thumbnails** to make a `.jpg` for every video that doesn't
already have one. The thumbnail gets the video's new name
(`2026-10-25 La Grange robot runs.jpg`), which is what the family video
site's gallery looks for.

- The frames come from the ones already grabbed for the AI, so no extra
  video reads are needed. A video that wasn't described is read once when
  you rename.
- The app automatically picks the **best** frame: the one that's well
  exposed, has contrast and is sharp, which skips black fade-ins and motion
  blur. Select a video to see its frames under the preview, and click a
  different one to use that instead.
- Thumbnails are written during **Rename**, never over an existing file, and
  are logged, so **Undo deletes them**.
- To make a thumbnail for a video that already has a good name, right-click it
  and choose **Create thumbnail now (keep name)**.
- The thumbnail width is set in Settings (640 px by default).

**Undo:** use File → Undo last rename, or pick an older log. Logs are saved in
`%APPDATA%\MediaNamer\MediaNamer\rename-logs\`.

**Spreadsheet editing:** use File → Export list to CSV, edit the Description,
New name, Date or include columns in Excel, then use File → Import edits.

## Rules

| What | How |
|---|---|
| Date | Camera metadata first: EXIF for photos, `creation_time` for videos (or Apple's local-time tag when present). Then a date in the file name (`IMG_20191225_083015`, `PXL_…`, `IMG-20190704-WA0001`). Last, the file's modified time, shown in amber. |
| Name | Template `{date} {desc}`; `{orig}` (the original name) is also available. The date format is `%Y-%m-%d`. Both can be changed in Settings. The extension is lower-cased. |
| Thumbnails | An image with the same name as a video in the same folder (`clip.mp4` + `clip.jpg`) is renamed together with the video and isn't described separately. With **Create thumbnails** on, videos without one get a new `.jpg` from the chosen frame. |
| Already named | Files that start with `YYYY-MM-DD ` are listed but unchecked. |
| Name clashes | Duplicates get ` (2)`, ` (3)`, and so on. Swaps (A→B, B→A) and case-only changes work. |
| Safety | Windows-illegal characters are removed. Renames happen in two phases, and every step is logged. If any rename fails, everything already done is reversed. |
| Cost | Descriptions are cached by file size and modified time, so rescanning or renaming doesn't ask the AI again. Changing the model or the context does. |

## Working in VS Code

1. Open the `media-namer` folder and install the recommended extensions
   (Python, Pylance, Python Debugger) when VS Code offers them.
2. **Terminal → Run Task… → Install dependencies.** This creates `.venv`
   and installs everything in editable mode. Then run **Python: Select
   Interpreter** and choose `.venv` if it isn't picked automatically.
3. **F5 → "Media Namer"** runs the app under the debugger. Breakpoints work
   in the background workers too (`workers.py`); each Qt worker thread
   registers itself with the debugger.
4. To use Gemini while debugging, copy `.env.example` to `.env`, add your
   key, and launch **"Media Namer (Gemini key from .env)"**. `.env` is
   git-ignored.
5. **Testing** sidebar: the tests are discovered automatically. GUI tests run
   offscreen, so no windows pop up. **Ctrl+Shift+P → Run Test Task** runs
   them all, and "Debug current test file" debugs the open test.
6. **Ctrl+Shift+B** builds `dist\MediaNamer.exe` with PyInstaller.

## Building a single .exe

Use the build task above, or run:
```
pip install pyinstaller
pyinstaller --noconsole --onefile --name MediaNamer run_media_namer.py
```
ffmpeg isn't bundled into the exe, so it still needs to be installed.

## For developers

```
pip install -e .[gemini,heic,dev]
pytest
```

The 49 tests use real ffmpeg-generated videos, EXIF-dated photos, a fake
Ollama HTTP server, best-frame scoring (including a real video that fades
in from black), and two full GUI runs under `QT_QPA_PLATFORM=offscreen`:
scan → describe → edit → rename → undo, and the thumbnail flow (pick a frame
→ rename → undo → create a thumbnail only).

| Module | Role |
|---|---|
| `scan.py` | Walks the folder and pairs thumbnails with videos; defines `MediaItem` |
| `dates.py` | Reads dates from EXIF, ffprobe tags, or the file name, falling back to the modified time |
| `media.py` | ffmpeg/ffprobe calls, frame grabs, and photo downscaling |
| `describe.py` | Prompt, `OllamaBackend` / `GeminiBackend`, and retry with backoff |
| `naming.py`, `proposals.py` | Cleans up descriptions, fills in the template, de-duplicates names, and imports/exports CSV |
| `renamer.py` | Plans, validates, applies (in two phases, with rollback), and undoes renames |
| `framestore.py` | Caches video frames in `%APPDATA%\MediaNamer\MediaNamer\frames` (shared by the AI, the preview and thumbnails; cleaned after 60 days) |
| `cache.py` | Caches descriptions in `%APPDATA%\MediaNamer\MediaNamer\descriptions.json` |
| `table.py`, `workers.py`, `settings.py`, `app.py` | The Qt model and filter, background jobs (`QThreadPool`), Settings dialog, and main window |

To add a backend, give it `describe(images: list[bytes], prompt) -> str` and
`list_models()`, and raise `TransientError` for errors that should be retried.
Then add it to `make_backend()` and the backend dropdown.
