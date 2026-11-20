# Archive Index

A text-only local website for searching Internet Archive item metadata and downloading files.

1. Extract the ZIP and open a terminal in the folder containing server.py.
2. Install the ZIP dependency with `python -m pip install -r requirements.txt` (use `python3` on macOS/Linux).
3. Install FFmpeg with libx265 and AAC support. Ensure both `ffmpeg -version` and `ffprobe -version` work in the terminal running the server.
4. Run `python server.py` (Windows) or `python3 server.py` (macOS/Linux).
5. Open http://127.0.0.1:8000 in your browser. Press Ctrl+C in the terminal to stop.

Requires Python 3.8+ and an internet connection. No API key is required. Use `--port 8080` if port 8000 is busy.

The landing page lists visible top-level folders in `save/`, newest first. Clicking a folder name downloads the selected folder, including nested files and empty directories, inside an AES-256 encrypted ZIP. Links and temporary save folders are skipped. Open the download with 7-Zip using the configured ARCHIVE_ZIP_PASSWORD (default below), then open contents.zip. Previously encrypted files inside the folder retain their original passwords. Open /home to search. Video encoding shows estimated progress across both passes when duration is available. ZIP packaging shows the percentage of input bytes processed.

Search videos by keyword or year. Select an item title to list its files, then filter by filename or format and choose **Click** to encode videos or **Here** to save the original file byte-for-byte without FFmpeg or ZIP compression. The server saves an encrypted ZIP in the item’s `save/<item-id>/` folder (created automatically) and shows its generated name, such as `archive-<random-id>.zip`. Progress appears beside the selected file. Use **Copy filename** after saving, or **Retry** if the save fails. Keep the tab open until the save completes. With JavaScript disabled, the form opens a confirmation page. The server account needs write access and enough free space for the `save/` folder. Extract it with an AES-compatible tool such as 7-Zip and enter the password. Open the resulting `contents.zip` to find your file under its original base name. After extraction, this inner ZIP contains the readable filename and file data. The default extraction password is `Nevergiveup&0429`. To change it, set the ARCHIVE_ZIP_PASSWORD environment variable before starting the server. The new password applies to future downloads. Results and file listings are paginated in groups of 20.

The app keeps a SQLite index in `save/saved.sqlite3`. Saved files are marked **Saved** and show their ZIP filename after a refresh or restart. On startup, indexed ZIPs from the old layout move into their item folders. Unindexed loose files stay in place and are omitted from the download page. Keep the index with the ZIPs. Removing a ZIP makes that file available to save again. Tracking starts with saves made by this version.

Video saves made with **Click** use two-pass HEVC at 208 kbps, 876 × 686 at 25 fps with padding, and stereo AAC at 126 kbps and 44.1 kHz. The first video and audio tracks are selected. Silent sources get a silent stereo track. Bitrates are averages and depend on the content. Each job needs temporary disk space for the source, encoded MP4 and ZIP. The temporary files are removed when the save finishes or fails. Each option tracks its own ZIP. Existing saves appear under **Click**.
