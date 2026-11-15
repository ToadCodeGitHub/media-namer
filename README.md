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
   pip install -e .[gemini,heic,faces]
   ```
   (`gemini` adds Google's SDK, `heic` adds iPhone photo support, and `faces`
   adds OpenCV for face recognition. All three are optional.)
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

### Gemini quota errors

Gemini limits how many requests your key can make, both per minute and per
day. The free tier's limits are low, and AI Studio shows yours at
aistudio.google.com/rate-limit. Each photo or video uses one request.

- **Per-minute limit:** the app spaces out requests to stay under
  **Max requests per minute** (Settings; 10 by default). If Google still
  says to slow down, every worker waits the time Google asks for and then
  retries.
- **Daily limit:** describing stops with a message. The limit resets at
  midnight Pacific time. Descriptions already received are saved, so
  **Describe all** later picks up where it stopped.
- **"No free quota" for a model:** pick another model with ↻, or enable
  billing on the key.
- To avoid quotas altogether, use Ollama.

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

### Export web copies (originals untouched)

For raw camera footage, set **Action** (next to the main button) to **Export
web copies**. Your original files keep their names and stay where they are.
The app writes new files to a separate folder:

    Video Staging area\C0001.MOV                     (untouched)
    Video Staging area (web)\2024-12-26 Allison feeding ostrich.mp4
    Video Staging area (web)\2024-12-26 Allison feeding ostrich.jpg

That folder is ready to upload to the family video site's B2 bucket.

- **Output folder:** defaults to `<folder> (web)` next to the source folder,
  and is remembered for each source folder. It must be outside the scanned
  folder.
- **Size:** 720p, 1080p (the default), 1440p, 4K or Original. The limit
  applies to the short side, so portrait videos stay portrait. Smaller
  videos are never enlarged.
- **Format:** every copy is H.264 (High profile, 8-bit 4:2:0) with AAC
  stereo audio and fast start, which plays in every browser and on every
  phone.
  - 10-bit and 4:2:2 footage is converted to 8-bit 4:2:0.
  - PCM camera audio is converted to AAC.
  - Timecode and data tracks are dropped.
  - Phone HDR (HLG or PQ) is converted to normal brightness so it doesn't
    look washed out.
  - Camera dates are kept, so rescanning the copies still finds the date.
    If the original has no date inside it (DV captures), the date the app
    found, from the file name or typed by hand, is written into the copy.
  - DV and other interlaced video is deinterlaced (no comb lines on motion),
    and non-square DV/DVD pixels are converted, so 720×480 4:3 becomes
    640×480.
- **Quality and speed** are in Settings → Web copies. CRF 23 with the
  veryfast speed is the default; use CRF 20 for near-original quality, or a
  slower speed for slightly smaller files that take 2 to 4 times longer.
- **Thumbnails:** a video's existing `.jpg` comes along, resized. With
  **Create thumbnails** ticked, videos without one get one from the chosen
  frame.
- **Photos** are copied as-is with their new names; untick **Copy photos**
  to leave them out. **Keep subfolders** recreates the folder layout.
- **Already exported:** files already in the output folder are skipped, so
  you can add new footage and export again.
- **Stop:** cancels the export. The file being encoded is discarded, and
  finished files are kept.
- **Undo:** File → Undo last rename or export deletes the copies.

Videos are encoded one at a time, because ffmpeg already uses every CPU core.

### Recognizing family members

Click **People…**, add each person, and give the app 5 to 10 clear photos of
them, one face per photo works best. Kids change a lot, so include photos from
every age your videos cover. The first time, click **Download face models**
(about 37 MB, from OpenCV's model library on GitHub). Then tick **Recognize
faces**.

When describing, Media Namer finds faces in the frames and matches them to
your people **on this PC**. Only the matched names go to the AI, as text:
"Face recognition identified these people: Allison and Anika. Use their names
in the title. Do not name anyone else." So you get "Allison and Anika picking
apples in orchard". This works with Gemini or Ollama.

- **Cautious by design:** a face is named only if it scores above the
  strictness setting (Settings → Face recognition, 0.40 by default) **and**
  clearly beats the next person, since sisters look alike. Otherwise it
  stays unnamed, and the AI is never asked to guess.
- **Test a photo…** (in People) shows each face it finds, with its scores
  for every person, so you can tune the strictness for your family.
- **More frames for faces:** faces are looked for in 8 frames per video, at up
  to 1280 px (Settings → Frames scanned per video). This is separate from the
  frames sent to the AI, so you can send Ollama just 1 frame for speed and
  still find faces across the whole clip. DV frames are deinterlaced and
  corrected to 4:3 first.
- **Names always reach the title:** if the AI leaves recognized names out,
  the app adds them ("Allison and Anika - Children picking apples", or
  "... with Anika"). You can turn this off in Settings.
- Small, blurry or turned-away faces are still often missed. Those clips get
  a description without names.
- Leave names out of the **Context** line when using faces, or the AI will
  use them whether or not those people appear.
- Reference faces are stored in `%APPDATA%\MediaNamer\MediaNamer\people`.
  Changing people, photos or strictness makes the app ask the AI again,
  rather than reusing a cached description.

### MiniDV tapes

A camcorder stamps the recording date and time into **every DV frame**.
Media Namer reads it directly. ffprobe can't, which is why the dates seemed
to be missing.

- **Dates:** when scanning, any DV file (`.avi`, `.dv`) gets its date from
  the tape. The Date from column shows **tape**. This takes priority over a
  date in the file name.
- **File → Split DV capture into recordings…** takes one long tape capture,
  from any capture program, and writes one AVI per recording. A new file
  starts wherever the camera was stopped and restarted. Files are named the
  WinDV way, `Tape1.07-09-23_21-47.00.avi`, which is the recording date.
  The DV video and audio are copied untouched, the date packs are kept, and
  the original capture isn't changed. Recordings shorter than half a second
  are merged into the previous one.
- **Typical workflow:**
  1. Capture the whole tape to one file with your usual program.
  2. Split it into recordings.
  3. Scan the recordings folder.
  4. Describe the recordings, or name them `{date} {orig}`.
  5. Export web copies.

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
| Date | Camera metadata first: EXIF for photos, `creation_time` for videos (or Apple's local-time tag when present). Then a date in the file name (`IMG_20191225_083015`, `PXL_…`, `IMG-20190704-WA0001`, or a DV capture name like `Girls.07-09-23_21-47.00.avi`, which is WinDV's `YY-MM-DD_HH-MM.SS` recording date from the tape). Last, the file's modified time, shown in amber. |
| Name | Template `{date} {desc}`; `{orig}` (the original name) is also available. A template without `{desc}`, such as `{date} {orig}`, names files immediately with no AI, which suits files that already have good names. The date format is `%Y-%m-%d`. Both can be changed in Settings. The extension is lower-cased. |
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

The 96 tests use real ffmpeg-generated videos, EXIF-dated photos, a fake
Ollama HTTP server, best-frame scoring (including a real video that fades
in from black), and two full GUI runs under `QT_QPA_PLATFORM=offscreen`:
scan → describe → edit → rename → undo, and the thumbnail flow (pick a frame
→ rename → undo → create a thumbnail only).
The export tests encode camera-style ProRes 4:2:2 10-bit + PCM + timecode
sources, portrait HLG HDR, and odd-sized clips, then check the codec,
pixel format, size, fast start and dates. They also check that cancelling
leaves no partial files, and that a GUI export leaves the originals
byte-for-byte unchanged.

| Module | Role |
|---|---|
| `scan.py` | Walks the folder and pairs thumbnails with videos; defines `MediaItem` |
| `dates.py` | Reads dates from EXIF, ffprobe tags, or the file name, falling back to the modified time |
| `media.py` | ffmpeg/ffprobe calls, frame grabs, and photo downscaling |
| `describe.py` | Prompt, `OllamaBackend` / `GeminiBackend`, and retry with backoff |
| `naming.py`, `proposals.py` | Cleans up descriptions, fills in the template, de-duplicates names, and imports/exports CSV |
| `renamer.py` | Plans, validates, applies (in two phases, with rollback), and undoes renames |
| `framestore.py` | Caches video frames in `%APPDATA%\MediaNamer\MediaNamer\frames` (shared by the AI, the preview and thumbnails; cleaned after 60 days) |
| `dvtape.py` | Reads the DV recording-date packs (VAUX/subcode 0x62/0x63) from frames, and splits captures by recording into type-2 AVIs |
| `faces.py`, `people_dialog.py` | Face detection (YuNet) and matching (SFace) with OpenCV, the people library, cautious matching with a margin between look-alikes, model download, and the People dialog |
| `encode.py` | Builds and runs the ffmpeg web-copy encode (scale, tone-map, H.264/AAC, faststart), with progress and cancel |
| `export.py` | Plans an export: output names, mirrored folders, de-duplication, skipping existing files, thumbnails and photos |
| `cache.py` | Caches descriptions in `%APPDATA%\MediaNamer\MediaNamer\descriptions.json` |
| `table.py`, `workers.py`, `settings.py`, `app.py` | The Qt model and filter, background jobs (`QThreadPool`), Settings dialog, and main window |

To add a backend, give it `describe(images: list[bytes], prompt) -> str` and
`list_models()`, and raise `TransientError` for errors that should be retried.
Then add it to `make_backend()` and the backend dropdown.
