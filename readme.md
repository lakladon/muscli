# Internet Archive Music Downloader

This script allows you to search for and download royalty-free music from the Internet Archive (archive.org). It provides a terminal-based interface with navigation using arrow keys and supports downloading individual files or entire collections.

## Features

- Search for music in Internet Archive collections (`audio_music`, `etree`, `opensource_audio`)
- Navigate results using arrow keys (↑/↓) and Enter
- View detailed file list for each archive (MP3, FLAC, etc.)
- Download a single file or all audio files from an archive
- Human-readable file sizes (KB, MB)
- Files are saved to `~/Music/free_archive/`
- No automatic downloads — all actions require user confirmation

## Requirements

- Python 3.6 or higher
- `requests`
- `tqdm`

## Installation

1. Install dependencies:

```bash
pip install requests tqdm
```

2. Save the script as `archive_music_downloader.py`.

## Usage

Run the script:

```bash
python archive_music_downloader.py
```

1. Enter a search query (e.g., `Bonobo`, `live grateful dead`, `Kevin MacLeod`).
2. Use **↑/↓ arrows** to navigate results.
3. Press **Enter** to open an archive.
4. In the file selection menu:
   - Type a **number** to download a specific file
   - Type **`all`** to download all audio files from the archive
   - Type **`q`** to cancel and return
5. Press **`q`** in the main list to exit.

## Notes

- Only content from open collections (public domain or Creative Commons) is accessible.
- Downloads are saved to `~/Music/free_archive/` (on Windows: `C:\Users\<user>\Music\free_archive\`).
- The script respects archive.org's terms of use and only accesses publicly available files.

## License

This tool is for educational and personal use only. Respect copyright laws and the terms of service of archive.org.
