# Archive Index

A text-only local website for searching Internet Archive item metadata and downloading files.

1. Extract the ZIP and open a terminal in the folder containing server.py.
2. Install the ZIP dependency with `python -m pip install -r requirements.txt` (use `python3` on macOS/Linux).
3. Run `python server.py` (Windows) or `python3 server.py` (macOS/Linux).
4. Open http://127.0.0.1:8000 in your browser. Press Ctrl+C in the terminal to stop.

Requires Python 3.8+ and an internet connection. No API key is required. Use `--port 8080` if port 8000 is busy.

Search by keyword, media type, or year. Select an item title to list its files, then filter by filename or format and choose **Save to server**. The server saves an encrypted ZIP in the project’s `save/` folder (created automatically) and shows its generated name, such as `archive-<random-id>.zip`. Progress appears beside the selected file. Use **Copy filename** after saving, or **Retry** if the save fails. Keep the tab open until the save completes. With JavaScript disabled, the form opens a confirmation page. The server account needs write access and enough free space for the `save/` folder. Extract it with an AES-compatible tool such as 7-Zip and enter the password. Open the resulting `contents.zip` to find your file under its original name. After extraction, this inner ZIP contains the readable filename and file data. The default extraction password is `Nevergiveup&0429`. To change it, set the ARCHIVE_ZIP_PASSWORD environment variable before starting the server. The new password applies to future downloads. Results and file listings are paginated in groups of 20.
