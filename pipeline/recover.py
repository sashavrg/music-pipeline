#!/usr/bin/env python3
"""
slskd-recover.py — Batch re-download lost albums via slskd REST API.

Reads LOST_ALBUMS.md, checks the beets library, searches slskd for each
missing album, scores results by quality and upload speed, and queues the
best match. Runs continuously with rate limiting. Resumes after interruption.

Usage:
  slskd-recover.py [--dry-run] [--reset] [--stats] [--report]

  --dry-run   Search and score but don't queue any downloads
  --reset     Clear progress file and start from scratch
  --stats     Print progress summary and exit
  --report    Write not_found/no_quality albums to /var/log/slskd-recover-missing.md and exit
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Optional

from . import config as cfg
from . import db as pipeline_db

# ── Configuration ─────────────────────────────────────────────────────────────

SLSKD_URL        = cfg.SLSKD_URL
LOST_ALBUMS_FILE = cfg.LOST_ALBUMS_FILE
BEETS_DB         = cfg.BEETS_DB
LOG_FILE         = str(cfg.RECOVER_LOG)
PROGRESS_FILE    = str(cfg.RECOVER_PROGRESS_FILE)

MIN_UPLOAD_SPEED = cfg.MIN_UPLOAD_SPEED   # bytes/s  — 2 MB/s floor
MAX_PENDING_DL   = cfg.MAX_PENDING_DL     # pause queueing above this many active transfers
SEARCH_DELAY     = cfg.SEARCH_DELAY       # seconds between searches (be a good citizen)
POLL_INTERVAL    = cfg.POLL_INTERVAL      # seconds between result-poll attempts
SEARCH_TIMEOUT   = cfg.SEARCH_TIMEOUT     # seconds before giving up on a search
QUEUE_POLL_WAIT  = cfg.QUEUE_POLL_WAIT    # seconds to wait when download queue is full

# Format priority scores. Anything scoring <= 0 is rejected outright.
# MP3 score is conditional on bitrate check (size/length).
FORMAT_SCORES = {
    "flac":  500,
    "wav":   450,
    "aiff":  450,
    "aif":   450,
    "ape":   420,
    "wv":    420,
    "alac":  420,
    "opus":  300,
    "mp3":   200,
    # everything else → not in dict → rejected
}

LOSSLESS_MIN_SCORE = 400   # formats at or above this are lossless
MP3_MIN_KBPS       = 310   # reject MP3 below this average bitrate

# Bit-depth and sample-rate bonuses applied to lossless files
DEPTH_BONUS = {16: 0, 24: 30, 32: 30}
RATE_BONUS  = {44100: 0, 48000: 10, 88200: 20, 96000: 25, 192000: 25}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class FolderResult:
    username:     str
    directory:    str
    files:        list    # raw file dicts from slskd API
    fmt:          str     # dominant format extension
    score:        int     # composite quality+speed score
    upload_speed: int     # bytes/s
    file_count:   int


# ── Logging ───────────────────────────────────────────────────────────────────

_log_fh = None

def log(msg: str, level: str = "INFO"):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{timestamp} [{level}] {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()

def setup_logging():
    global _log_fh
    _log_fh = cfg.open_log_file(LOG_FILE)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def api_get(path: str):
    req = urllib.request.Request(SLSKD_URL + path, headers=pipeline_db.slskd_headers())
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def api_post(path: str, body) -> Optional[dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        SLSKD_URL + path, data=data, method="POST",
        headers=pipeline_db.slskd_headers({"Content-Type": "application/json"}),
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read()
            return json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} on POST {path}: {e.read().decode(errors='replace')}") from e

def api_delete(path: str):
    req = urllib.request.Request(
        SLSKD_URL + path, method="DELETE", headers=pipeline_db.slskd_headers(),
    )
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception:
        pass


# ── LOST_ALBUMS.md parser ─────────────────────────────────────────────────────

# Quality/format tags to strip from folder-name headers
_FORMAT_TAG_RE = re.compile(
    r'\s*[-–]\s*(WEB|FLAC|MP3|320|Lossless|Hi[- ]?Res|24[- ]?bit|CD|SACD|Vinyl'
    r'|Remaster(?:ed)?|Expanded|Bonus|Limited|Deluxe)\S*$',
    re.IGNORECASE,
)
_BRACKET_TAG_RE = re.compile(
    r'\s*[\[\(](WEB|FLAC|MP3|320|Lossless|Hi[- ]?Res|24[- ]?bit|CD|SACD|Vinyl'
    r'|Remaster(?:ed)?|Expanded|Bonus|Limited|Deluxe)[^\]\)]*[\]\)]$',
    re.IGNORECASE,
)

def _clean_header(s: str) -> str:
    """Strip trailing format/quality noise from a folder-name header."""
    s = _FORMAT_TAG_RE.sub("", s).strip()
    s = _BRACKET_TAG_RE.sub("", s).strip()
    return s

def _split_artist_album(header: str) -> tuple[str, str]:
    """
    Try to split 'Artist - Album' into (artist, album).
    Returns ('', header) when artist can't be determined.
    """
    header = _clean_header(header)
    if " - " in header:
        artist, album = header.split(" - ", 1)
        artist, album = artist.strip(), album.strip()
        # Bare year prefix → treat whole string as album
        if re.fullmatch(r"\d{4}", artist):
            return ("", header)
        return (artist, album)
    return ("", header)

def parse_lost_albums(filepath: str) -> list[tuple[str, str]]:
    """
    Parse LOST_ALBUMS.md → deduplicated list of (artist, album) tuples.
    artist is '' for entries where it could not be determined.
    """
    albums: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    current_artist: Optional[str] = None
    in_section = False

    with open(filepath, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip()

            if line.startswith("## Albums by Artist"):
                in_section = True
                continue
            if not in_section:
                continue

            # Header line: **Name** (N album(s))
            m = re.match(r"^\*\*(.+?)\*\*\s*\(\d+ albums?\)", line)
            if m:
                current_artist = m.group(1).strip()
                continue

            # Album entry: "  - Name"
            m = re.match(r"^  - (.+)$", line)
            if m and current_artist is not None:
                entry = m.group(1).strip()
                if entry == "(root folder - no album structure)":
                    artist, album = _split_artist_album(current_artist)
                else:
                    artist = current_artist
                    album  = _clean_header(entry)

                artist = artist.strip()
                album  = album.strip()
                if not album:
                    continue

                key = (artist.lower(), album.lower())
                if key not in seen:
                    seen.add(key)
                    albums.append((artist, album))

    return albums


# ── Beets library check via SQLite ────────────────────────────────────────────

def _beets_paths(artist: str, album: str) -> list[str]:
    """Query beets library.db for tracks matching artist+album."""
    try:
        conn = sqlite3.connect(BEETS_DB, timeout=10)
        conds, params = [], []
        if artist:
            conds.append("(LOWER(albumartist) LIKE ? OR LOWER(artist) LIKE ?)")
            params += [f"%{artist.lower()}%", f"%{artist.lower()}%"]
        if album:
            conds.append("LOWER(album) LIKE ?")
            params.append(f"%{album.lower()}%")
        where = " AND ".join(conds) if conds else "1=1"
        rows = conn.execute(f"SELECT path FROM items WHERE {where}", params).fetchall()
        conn.close()
        result = []
        for (p,) in rows:
            if isinstance(p, bytes):
                p = p.decode("utf-8", errors="replace")
            result.append(p)
        return result
    except Exception:
        return []

LIBRARY_ROOT = str(cfg.LIBRARY_ROOT)
AUDIO_EXTS   = {".flac", ".mp3", ".opus", ".wav", ".aiff", ".aif", ".ape", ".wv", ".m4a", ".ogg"}

def _fs_track_count(artist: str, album: str) -> int:
    """
    Scan the library filesystem for a matching Artist/Album folder.
    Uses fuzzy substring matching on directory names so minor naming
    differences don't cause false misses.
    Returns the number of audio files found, or 0 if no folder matched.
    """
    if not os.path.isdir(LIBRARY_ROOT):
        return 0

    artist_l = artist.lower().strip() if artist else ""
    album_l  = album.lower().strip()

    for artist_dir in os.scandir(LIBRARY_ROOT):
        if not artist_dir.is_dir():
            continue
        # Artist match: either no artist known, or dirname contains artist name
        if artist_l and artist_l not in artist_dir.name.lower():
            continue
        for album_dir in os.scandir(artist_dir.path):
            if not album_dir.is_dir():
                continue
            # Album match: dirname contains album name (ignoring year/extra tags)
            dir_clean = re.sub(r"\s*[\(\[].*?[\)\]]", "", album_dir.name).strip().lower()
            if album_l not in album_dir.name.lower() and album_l not in dir_clean:
                continue
            # Count audio files inside
            count = sum(
                1 for f in os.scandir(album_dir.path)
                if f.is_file() and os.path.splitext(f.name)[1].lower() in AUDIO_EXTS
            )
            if count > 0:
                return count
    return 0

def count_existing_tracks(artist: str, album: str) -> int:
    """
    Return track count from beets DB (files confirmed on disk) OR filesystem scan,
    whichever is higher. Filesystem scan catches albums not yet imported into beets.
    """
    beets_count = sum(1 for p in _beets_paths(artist, album) if os.path.exists(p))
    fs_count    = _fs_track_count(artist, album)
    return max(beets_count, fs_count)


# ── slskd search ──────────────────────────────────────────────────────────────

_QUERY_STRIP_RE = re.compile(r"[',&\"\(\)\[\]\{\}/\\]+")

def _sanitize_query(query: str) -> str:
    """slskd's EF Core layer raises optimistic-concurrency errors on queries
    containing apostrophes, commas, and similar punctuation, returning 0
    responses for searches that would otherwise succeed. Strip them before
    sending."""
    cleaned = _QUERY_STRIP_RE.sub(" ", query)
    return re.sub(r"\s+", " ", cleaned).strip()


def slskd_search(query: str) -> Optional[list]:
    """
    Submit a search and poll until complete (or timeout).
    Returns list of response objects, or None on API error.
    """
    query = _sanitize_query(query)
    try:
        result = api_post("/api/v0/searches", {
            "searchText":               query,
            "fileLimit":                10000,
            "filterResponses":          True,
            "minimumResponseFileCount": 1,
        })
    except Exception as e:
        log(f"Search creation failed: {e}", "WARN")
        return None

    search_id = result.get("id")
    if not search_id:
        return None

    deadline = time.monotonic() + SEARCH_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            data = api_get(f"/api/v0/searches/{search_id}?includeResponses=true")
        except Exception:
            continue
        if data.get("isComplete"):
            api_delete(f"/api/v0/searches/{search_id}")
            return data.get("responses", [])

    # Timed out — return whatever arrived
    try:
        data = api_get(f"/api/v0/searches/{search_id}?includeResponses=true")
        api_delete(f"/api/v0/searches/{search_id}")
        return data.get("responses", [])
    except Exception:
        return []


# ── Quality scoring ───────────────────────────────────────────────────────────

def _ext(filename: str) -> str:
    return PureWindowsPath(filename).suffix.lstrip(".").lower()

def _file_score(f: dict) -> int:
    """Score a single file. Returns -1 if the file should be rejected."""
    ext  = _ext(f.get("filename", ""))
    base = FORMAT_SCORES.get(ext, -1)
    if base <= 0:
        return -1

    # MP3: verify bitrate from size / length
    if ext == "mp3":
        size, length = f.get("size", 0), f.get("length", 0)
        if length <= 0 or (size * 8 / length / 1000) < MP3_MIN_KBPS:
            return -1
        return base

    # Lossless: add depth and sample-rate bonus
    if base >= LOSSLESS_MIN_SCORE:
        depth   = f.get("bitDepth") or 16
        rate    = f.get("sampleRate") or 44100
        d_bonus = DEPTH_BONUS.get(depth, 0)
        # Snap to nearest known rate
        closest = min(RATE_BONUS, key=lambda r: abs(r - rate))
        r_bonus = RATE_BONUS[closest]
        return base + d_bonus + r_bonus

    return base

def _score_folder(audio_files: list, upload_speed: int) -> int:
    """
    Score a folder of audio files. Returns -1 if it fails minimum requirements.
    Uses the weakest file's score as the base (no mixed-quality folders).
    """
    if upload_speed < MIN_UPLOAD_SPEED or not audio_files:
        return -1

    file_scores = [_file_score(f) for f in audio_files]
    valid        = [s for s in file_scores if s >= 0]
    if not valid:
        return -1

    base         = min(valid)                              # weakest link
    speed_bonus  = min(int(upload_speed / MIN_UPLOAD_SPEED * 5), 50)  # up to +50
    count_bonus  = min(len(audio_files) * 2, 40)           # up to +40

    return base + speed_bonus + count_bonus

def find_best_folder(responses: list) -> Optional[FolderResult]:
    """Pick the highest-scoring (username, directory) folder from all responses."""
    best_score = -1
    best: Optional[FolderResult] = None

    for resp in responses:
        username     = resp.get("username", "")
        upload_speed = resp.get("uploadSpeed", 0)
        files        = resp.get("files", [])

        if upload_speed < MIN_UPLOAD_SPEED:
            continue

        # Group files by parent directory
        folders: dict[str, list] = {}
        for f in files:
            d = str(PureWindowsPath(f.get("filename", "")).parent)
            folders.setdefault(d, []).append(f)

        for directory, dir_files in folders.items():
            audio = [f for f in dir_files if _ext(f.get("filename", "")) in FORMAT_SCORES]
            if not audio:
                continue

            score = _score_folder(audio, upload_speed)
            if score <= best_score:
                continue

            exts = [_ext(f.get("filename", "")) for f in audio]
            fmt  = max(set(exts), key=exts.count)

            best_score = score
            best = FolderResult(
                username=username, directory=directory, files=audio,
                fmt=fmt, score=score, upload_speed=upload_speed,
                file_count=len(audio),
            )

    return best


def find_all_folders(responses: list) -> list:
    """Return all qualifying FolderResult objects sorted by score desc.
    Used for multi-source filling where different peers may have different tracks."""
    results = []
    seen: set[tuple[str, str]] = set()

    for resp in responses:
        username     = resp.get("username", "")
        upload_speed = resp.get("uploadSpeed", 0)
        files        = resp.get("files", [])

        if upload_speed < MIN_UPLOAD_SPEED:
            continue

        folders: dict[str, list] = {}
        for f in files:
            d = str(PureWindowsPath(f.get("filename", "")).parent)
            folders.setdefault(d, []).append(f)

        for directory, dir_files in folders.items():
            key = (username, directory)
            if key in seen:
                continue
            seen.add(key)

            audio = [f for f in dir_files if _ext(f.get("filename", "")) in FORMAT_SCORES]
            if not audio:
                continue

            score = _score_folder(audio, upload_speed)
            if score < 0:
                continue

            exts = [_ext(f.get("filename", "")) for f in audio]
            fmt  = max(set(exts), key=exts.count)

            results.append(FolderResult(
                username=username, directory=directory, files=audio,
                fmt=fmt, score=score, upload_speed=upload_speed,
                file_count=len(audio),
            ))

    return sorted(results, key=lambda r: r.score, reverse=True)


# ── Download queueing ─────────────────────────────────────────────────────────

def queue_download(folder: FolderResult) -> bool:
    payload = [{"filename": f["filename"], "size": f.get("size", 0)} for f in folder.files]
    try:
        api_post(f"/api/v0/transfers/downloads/{urllib.parse.quote(folder.username, safe='')}", payload)
        return True
    except Exception as e:
        log(f"Queue failed ({folder.username}): {e}", "WARN")
        return False
def pending_download_count() -> int:
    """Count transfers that are queued or actively downloading."""
    try:
        data = api_get("/api/v0/transfers/downloads")
        count = 0
        for user_data in data:
            for d in user_data.get("directories", []):
                for f in d.get("files", []):
                    state = f.get("state", "")
                    if any(s in state for s in ("Queued", "InProgress", "Requested", "Initializing")):
                        count += 1
        return count
    except Exception:
        return 0


# ── Progress tracking ─────────────────────────────────────────────────────────

def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE) as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}

def save_progress(p: dict):
    with open(PROGRESS_FILE, "w") as fh:
        json.dump(p, fh, indent=2, ensure_ascii=False)

def _pkey(artist: str, album: str) -> str:
    return f"{artist.lower()}|||{album.lower()}"


# ── Stats summary ─────────────────────────────────────────────────────────────

def print_stats(albums: list, progress: dict):
    total = len(albums)
    counts: dict[str, int] = {}
    for artist, album in albums:
        status = progress.get(_pkey(artist, album), {}).get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
    done = total - counts.get("pending", total)
    print(f"\nProgress: {done}/{total} albums processed")
    for status, n in sorted(counts.items()):
        print(f"  {status:20s} {n}")
    print()


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    dry_run   = "--dry-run" in sys.argv
    do_reset  = "--reset"   in sys.argv
    do_stats  = "--stats"   in sys.argv
    do_report = "--report"  in sys.argv

    setup_logging()

    albums   = parse_lost_albums(LOST_ALBUMS_FILE)
    progress = {} if do_reset else load_progress()

    if do_stats:
        print_stats(albums, progress)
        return

    if do_report:
        out = str(cfg.RECOVER_MISSING_REPORT)
        not_found  = sorted(v["label"] for v in progress.values() if v.get("status") == "not_found")
        no_quality = sorted(v["label"] for v in progress.values() if v.get("status") == "no_quality")
        with open(out, "w") as f:
            f.write(f"# Albums not recovered by slskd-recover\n\n")
            f.write(f"## Not found on Soulseek ({len(not_found)})\n\n")
            for label in not_found:
                f.write(f"- {label}\n")
            f.write(f"\n## Found but no results met quality bar ({len(no_quality)})\n\n")
            for label in no_quality:
                f.write(f"- {label}\n")
        print(f"Written to {out} ({len(not_found)} not found, {len(no_quality)} no quality)")
        return

    log("=" * 60)
    log(f"slskd-recover starting {'(DRY RUN) ' if dry_run else ''}"
        f"— {len(albums)} albums in list")

    counts = {"queued": 0, "skipped": 0, "not_found": 0, "no_quality": 0, "error": 0}

    for idx, (artist, album) in enumerate(albums, 1):
        label = f"{artist} – {album}" if artist else album
        key   = _pkey(artist, album)

        # Resume: skip already-handled (except errors which get retried)
        prev = progress.get(key, {})
        if prev.get("status") and prev["status"] not in ("error", "pending"):
            continue

        log(f"[{idx}/{len(albums)}] {label}")

        # ── Library check ─────────────────────────────────────────────────
        n_existing = count_existing_tracks(artist, album)
        if n_existing:
            log(f"  library: {n_existing} tracks on disk", "DEBUG")

        # ── Search ────────────────────────────────────────────────────────
        query = f"{artist} {album}".strip() if artist else album
        # Soulseek works best with short queries; trim at a word boundary
        if len(query) > 60:
            query = query[:60].rsplit(" ", 1)[0]

        responses = slskd_search(query)

        if responses is None:
            log("  search error", "WARN")
            progress[key] = {"status": "error", "label": label}
            save_progress(progress)
            counts["error"] += 1
            time.sleep(SEARCH_DELAY)
            continue

        if not responses:
            log("  no results")
            progress[key] = {"status": "not_found", "label": label}
            save_progress(progress)
            counts["not_found"] += 1
            time.sleep(SEARCH_DELAY)
            continue

        # ── Score results ─────────────────────────────────────────────────
        best = find_best_folder(responses)

        if best is None:
            log(f"  no match meeting quality/speed criteria ({len(responses)} responses)")
            progress[key] = {"status": "no_quality", "label": label,
                             "responses": len(responses)}
            save_progress(progress)
            counts["no_quality"] += 1
            time.sleep(SEARCH_DELAY)
            continue

        # ── Skip if library already complete ──────────────────────────────
        if n_existing > 0 and n_existing >= best.file_count:
            log(f"  SKIP — library has {n_existing} tracks, best match has {best.file_count}")
            progress[key] = {"status": "skipped", "label": label}
            save_progress(progress)
            counts["skipped"] += 1
            time.sleep(SEARCH_DELAY)
            continue

        speed_mb = best.upload_speed / 1_000_000
        log(f"  best: {best.username} | {best.fmt.upper()} | "
            f"{best.file_count} files | {speed_mb:.1f} MB/s | score={best.score}")
        if n_existing:
            log(f"  (merging: library has {n_existing}, queuing {best.file_count})")

        # ── Pause if download queue is saturated ──────────────────────────
        pending = pending_download_count()
        if pending >= MAX_PENDING_DL:
            log(f"  queue at {pending}/{MAX_PENDING_DL} — waiting for space...")
            while pending_download_count() >= MAX_PENDING_DL:
                time.sleep(QUEUE_POLL_WAIT)
            log("  queue drained, resuming")

        # ── Queue download ────────────────────────────────────────────────
        if dry_run:
            log("  [DRY RUN] would queue download")
            progress[key] = {"status": "dry_run", "label": label,
                             "username": best.username, "format": best.fmt,
                             "files": best.file_count}
        else:
            if queue_download(best):
                log(f"  queued {best.file_count} files from {best.username}")
                progress[key] = {"status": "queued", "label": label,
                                 "username": best.username, "format": best.fmt,
                                 "files": best.file_count, "score": best.score}
                counts["queued"] += 1
            else:
                progress[key] = {"status": "error", "label": label}
                counts["error"] += 1

        save_progress(progress)
        time.sleep(SEARCH_DELAY)

    log("=" * 60)
    log(f"Finished — queued={counts['queued']} skipped={counts['skipped']} "
        f"not_found={counts['not_found']} no_quality={counts['no_quality']} "
        f"errors={counts['error']}")


if __name__ == "__main__":
    main()
