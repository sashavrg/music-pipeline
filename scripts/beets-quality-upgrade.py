#!/usr/bin/env python3
import argparse
import os
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import mutagen

LIB_DB = "/root/.config/beets/library.db"
LIB_ROOT = "/mnt/storage/share/media/music/music"
QUARANTINE_UNPARSED = Path("/mnt/scratch/slskd/quarantine/unparsed")
QUARANTINE_INCOMPLETE = Path("/mnt/scratch/slskd/quarantine/incomplete")
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac", ".aiff", ".wma"}


def log(msg: str):
    print(msg, flush=True)


def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s*\([^)]*\)$", "", s).strip()
    s = s.replace("&", " and ")
    s = re.sub(r"[-_:;,.!/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def score(format_name: str, bitrate: int, samplerate: int, bitdepth: int) -> int:
    fmt_rank = {
        "flac": 6,
        "alac": 6,
        "wav": 6,
        "aiff": 6,
        "m4a": 4,
        "aac": 4,
        "opus": 4,
        "ogg": 3,
        "mp3": 2,
        "wma": 1,
    }.get((format_name or "").lower(), 0)
    return fmt_rank * 10000 + int((bitrate or 0) / 1000) * 10 + int((samplerate or 0) / 10) + int(bitdepth or 0) * 100


def get_first(tags, keys):
    for k in keys:
        if k in tags and tags[k]:
            v = tags[k]
            if isinstance(v, list):
                return str(v[0]).strip()
            return str(v).strip()
    return ""


def parse_int_first(v):
    if v is None:
        return 0
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    m = re.search(r"(\d+)", str(v))
    return int(m.group(1)) if m else 0


def decode_path(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def audio_files(folder: Path):
    out = []
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith("._") and not p.name.startswith("."):
            out.append(p)
    return sorted(out)


def parse_folder_guess(folder: Path):
    name = folder.name
    parent = folder.parent.name
    guessed_artist = ""
    guessed_album = ""
    if " - " in name:
        a, b = name.split(" - ", 1)
        guessed_artist = a.strip()
        guessed_album = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", b).strip()
    elif " - " in parent:
        a, b = parent.split(" - ", 1)
        guessed_artist = a.strip()
        guessed_album = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", b).strip()
    else:
        guessed_artist = parent
        guessed_album = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", name).strip()
    guessed_album = re.sub(r"^\d{4}\s*-\s*", "", guessed_album).strip()
    return guessed_artist, guessed_album


def fallback_track_from_filename(p: Path):
    stem = p.stem
    m = re.match(r"^\s*(\d{1,2})[\s._-]+(.+)$", stem)
    trk = int(m.group(1)) if m else 0
    title = m.group(2).strip() if m else stem
    fmt = p.suffix.lower().lstrip(".")
    return {
        "path": str(p),
        "artist": "",
        "album": "",
        "title": title,
        "track": trk,
        "disc": 0,
        "track_total": 0,
        "format": fmt,
        "bitrate": 0,
        "samplerate": 0,
        "bitdepth": 0,
        "q": score(fmt, 0, 0, 0),
        "source": "fallback",
    }


def incoming_tracks(folder: Path):
    tracks = []
    artist_votes = []
    album_votes = []

    for p in audio_files(folder):
        try:
            mf = mutagen.File(str(p), easy=False)
            if mf is None:
                tracks.append(fallback_track_from_filename(p))
                continue

            tags = mf.tags or {}
            info = getattr(mf, "info", None)
            artist = get_first(tags, ["albumartist", "TPE2", "aART", "artist", "TPE1", "\xa9ART"]) or get_first(
                tags, ["artist", "TPE1", "\xa9ART"]
            )
            album = get_first(tags, ["album", "TALB", "\xa9alb"])
            title = get_first(tags, ["title", "TIT2", "\xa9nam"]) or p.stem
            track = parse_int_first(get_first(tags, ["tracknumber", "TRCK", "trkn"]))
            track_total = parse_int_first(get_first(tags, ["tracktotal", "TOTALTRACKS"]))
            if track_total == 0:
                trks = get_first(tags, ["tracknumber", "TRCK"])
                m = re.search(r"\d+\s*/\s*(\d+)", trks)
                if m:
                    track_total = int(m.group(1))
            disc = parse_int_first(get_first(tags, ["discnumber", "TPOS", "disk", "disknumber"]))
            bitrate = int(getattr(info, "bitrate", 0) or 0)
            samplerate = int(getattr(info, "sample_rate", 0) or 0)
            bitdepth = int(getattr(info, "bits_per_sample", 0) or 0)
            fmt = p.suffix.lower().lstrip(".")
            if bitdepth == 0 and fmt == "flac":
                bitdepth = 16
            q = score(fmt, bitrate, samplerate, bitdepth)
            tracks.append(
                {
                    "path": str(p),
                    "artist": artist,
                    "album": album,
                    "title": title,
                    "track": track,
                    "disc": disc,
                    "track_total": track_total,
                    "format": fmt,
                    "bitrate": bitrate,
                    "samplerate": samplerate,
                    "bitdepth": bitdepth,
                    "q": q,
                    "source": "tags",
                }
            )
            if artist:
                artist_votes.append(artist)
            if album:
                album_votes.append(album)
        except Exception:
            tracks.append(fallback_track_from_filename(p))

    guessed_artist = Counter(artist_votes).most_common(1)[0][0] if artist_votes else ""
    guessed_album = Counter(album_votes).most_common(1)[0][0] if album_votes else ""

    fa, fb = parse_folder_guess(folder)
    guessed_artist = guessed_artist or fa
    guessed_album = guessed_album or fb

    # fill missing artist/album for fallback rows
    for t in tracks:
        if not t["artist"]:
            t["artist"] = guessed_artist
        if not t["album"]:
            t["album"] = guessed_album

    return tracks, guessed_artist, guessed_album


def load_library_candidates(conn, artist_guess: str, album_guess: str):
    cur = conn.cursor()
    rows = []
    ag = norm(artist_guess)
    alb = norm(album_guess)

    cur.execute(
        """
        SELECT i.id, i.path, i.title, i.track, i.disc, i.format, i.bitrate, i.samplerate, i.bitdepth,
               COALESCE(a.albumartist, i.artist), i.album
        FROM items i
        LEFT JOIN albums a ON i.album_id = a.id
        WHERE lower(i.album) LIKE ?
        """,
        (f"%{alb[:40]}%",),
    )
    rows.extend(cur.fetchall())

    if ag:
        cur.execute(
            """
            SELECT i.id, i.path, i.title, i.track, i.disc, i.format, i.bitrate, i.samplerate, i.bitdepth,
                   COALESCE(a.albumartist, i.artist), i.album
            FROM items i
            LEFT JOIN albums a ON i.album_id = a.id
            WHERE lower(COALESCE(a.albumartist, i.artist)) LIKE ?
              AND lower(i.album) LIKE ?
            """,
            (f"%{ag[:40]}%", f"%{alb[:40]}%"),
        )
        rows.extend(cur.fetchall())

    uniq = {}
    for r in rows:
        uniq[r[0]] = r

    out = []
    for r in uniq.values():
        item_id, path, title, track, disc, fmt, bitrate, samplerate, bitdepth, alb_artist, album = r
        out.append(
            {
                "id": int(item_id),
                "path": decode_path(path),
                "title": title or "",
                "track": int(track or 0),
                "disc": int(disc or 0),
                "format": (fmt or "").lower(),
                "bitrate": int(bitrate or 0),
                "samplerate": int(samplerate or 0),
                "bitdepth": int(bitdepth or 0),
                "artist": alb_artist or "",
                "album": album or "",
            }
        )
    return out




def canon_title(title: str, artist: str = "") -> str:
    t = norm(title)
    a = norm(artist)
    if a and t.startswith(a + " "):
        t = t[len(a):].strip()
    t = re.sub(r"^\d{1,2}\s+", "", t).strip()
    return t


def track_key(rec):
    return (int(rec.get("disc") or 0), int(rec.get("track") or 0), canon_title(rec.get("title") or "", rec.get("artist") or ""))


def delete_library_items(conn, items):
    cur = conn.cursor()
    deleted_ids = []
    for it in items:
        p = Path(it["path"])
        if str(p).startswith(LIB_ROOT) and p.exists():
            try:
                p.unlink()
            except Exception as e:
                log(f"[WARN] failed deleting file {p}: {e}")
        try:
            cur.execute("DELETE FROM items WHERE id = ?", (it["id"],))
            deleted_ids.append(it["id"])
        except Exception as e:
            log(f"[WARN] failed deleting db row for id={it['id']}: {e}")

    cur.execute("DELETE FROM albums WHERE id NOT IN (SELECT DISTINCT album_id FROM items WHERE album_id IS NOT NULL)")
    conn.commit()
    return deleted_ids


def quarantine_unparsed(folder: Path):
    QUARANTINE_UNPARSED.mkdir(parents=True, exist_ok=True)
    target = QUARANTINE_UNPARSED / folder.name
    if target.exists():
        base = target
        i = 1
        while target.exists():
            target = Path(str(base) + f"__{i}")
            i += 1
    shutil.move(str(folder), str(target))
    return target




def quarantine_incomplete(folder: Path):
    QUARANTINE_INCOMPLETE.mkdir(parents=True, exist_ok=True)
    target = QUARANTINE_INCOMPLETE / folder.name
    if target.exists():
        base = target
        i = 1
        while target.exists():
            target = Path(str(base) + f"__{i}")
            i += 1
    shutil.move(str(folder), str(target))
    return target


def album_looks_incomplete(tracks):
    nums = sorted({int(t.get("track") or 0) for t in tracks if int(t.get("track") or 0) > 0})
    if len(nums) < 3:
        return False, "too-few-tracknums"

    discs = {int(t.get("disc") or 0) for t in tracks if int(t.get("disc") or 0) > 0}
    if len(discs) > 1:
        return False, "multi-disc-skip"

    totals = [int(t.get("track_total") or 0) for t in tracks if int(t.get("track_total") or 0) > 0]
    expected = max(totals) if totals else 0

    if expected > 0 and len(nums) < expected:
        missing = [n for n in range(1, expected + 1) if n not in nums]
        return True, f"tag-total present={len(nums)}/{expected} missing={missing[:12]}"

    maxn = max(nums)
    gaps = [n for n in range(1, maxn + 1) if n not in nums]
    if maxn >= 6 and gaps and (len(nums) / maxn) < 0.9:
        return True, f"gap-heuristic present={len(nums)}/{maxn} missing={gaps[:12]}"

    return False, "ok"


def process_folder(conn, folder: Path):
    tracks, g_artist, g_album = incoming_tracks(folder)
    if not tracks:
        try:
            q = quarantine_unparsed(folder)
            return "quarantine-empty", 0, 0, 0, str(q)
        except Exception:
            return "skip-empty", 0, 0, 0, ""

    incomplete, reason = album_looks_incomplete(tracks)
    if incomplete:
        try:
            q = quarantine_incomplete(folder)
            return "quarantine-incomplete", len(tracks), 0, 0, f"{q} reason={reason}"
        except Exception:
            return "skip-keep-incoming", len(tracks), 0, 0, ""

    cand = load_library_candidates(conn, g_artist, g_album)
    existing_by_key = defaultdict(list)
    existing_by_title = defaultdict(list)
    for c in cand:
        existing_by_key[track_key(c)].append(c)
        existing_by_title[canon_title(c.get("title") or "", c.get("artist") or "")].append(c)

    has_new = False
    has_upgrade = False
    downgrade_or_equal = 0
    new_count = 0
    incoming_drop = []
    items_to_delete = {}

    for t in tracks:
        k = track_key(t)
        ex = existing_by_key.get(k, [])

        if not ex and t["track"] > 0:
            # disc flexibility: treat incoming disc 0 as wildcard; existing disc 0/1 equivalent for single-disc albums
            ex = [
                x
                for x in cand
                if int(x.get("track") or 0) == int(t.get("track") or 0)
                and (int(t.get("disc") or 0) == 0 or int(x.get("disc") or 0) in {0, int(t.get("disc") or 0)})
            ]

        if not ex:
            # title-only fallback inside candidate album
            ex = existing_by_title.get(canon_title(t.get("title") or "", t.get("artist") or ""), [])

        if not ex:
            has_new = True
            new_count += 1
            continue

        best_existing = sorted(
            ex,
            key=lambda x: score(x["format"], x["bitrate"], x["samplerate"], x["bitdepth"]),
            reverse=True,
        )[0]
        ex_score = score(best_existing["format"], best_existing["bitrate"], best_existing["samplerate"], best_existing["bitdepth"])

        if t["q"] > ex_score:
            has_upgrade = True
            items_to_delete[best_existing["id"]] = best_existing
        else:
            downgrade_or_equal += 1
            incoming_drop.append(t['path'])


    # Drop incoming files that are not better than existing library copies.
    dropped = 0
    for fp in incoming_drop:
        try:
            pp = Path(fp)
            if pp.exists() and str(pp).startswith(str(folder)):
                pp.unlink()
                dropped += 1
        except Exception:
            pass

    # If we removed everything from folder, delete folder and skip import.
    remaining_audio = [x for x in folder.rglob('*') if x.is_file() and x.suffix.lower() in AUDIO_EXTS and not x.name.startswith('._') and not x.name.startswith('.')]
    if not remaining_audio:
        try:
            shutil.rmtree(folder)
            return "skip-delete-incoming", len(tracks), 0, downgrade_or_equal, ""
        except Exception:
            return "skip-keep-incoming", len(tracks), 0, downgrade_or_equal, ""

    if not has_new and not has_upgrade:
        try:
            shutil.rmtree(folder)
            return "skip-delete-incoming", len(tracks), 0, downgrade_or_equal, ""
        except Exception:
            return "skip-keep-incoming", len(tracks), 0, downgrade_or_equal, ""

    deleted_count = 0
    if has_upgrade and items_to_delete:
        deleted_count = len(delete_library_items(conn, list(items_to_delete.values())))

    return "keep-for-import", len(tracks), deleted_count, downgrade_or_equal, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean-list", required=True)
    args = ap.parse_args()

    p = Path(args.clean_list)
    if not p.exists() or p.stat().st_size == 0:
        log("[INFO] no clean-list entries")
        return 0

    folders = [Path(x.strip()) for x in p.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
    folders = [f for f in folders if f.exists() and f.is_dir()]
    if not folders:
        p.write_text("", encoding="utf-8")
        log("[INFO] no existing folders from clean-list")
        return 0

    conn = sqlite3.connect(LIB_DB)
    kept = []
    stats = Counter()

    try:
        for f in folders:
            status, track_count, deleted_count, downgrade_or_equal, extra = process_folder(conn, f)
            stats[status] += 1
            stats["tracks_seen"] += track_count
            stats["library_items_deleted"] += deleted_count
            stats["incoming_not_better_tracks"] += downgrade_or_equal
            if status == "keep-for-import":
                kept.append(str(f))
            suffix = f" quarantine={extra}" if extra else ""
            log(
                f"[QUALITY] folder={f} status={status} tracks={track_count} "
                f"library_deleted={deleted_count} incoming_not_better={downgrade_or_equal}{suffix}"
            )
    finally:
        conn.close()

    p.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    log(
        "[SUMMARY] "
        f"input={len(folders)} kept={len(kept)} "
        f"skip_deleted={stats['skip-delete-incoming']} skip_kept={stats['skip-keep-incoming']} "
        f"quarantine_empty={stats['quarantine-empty']} quarantine_incomplete={stats['quarantine-incomplete']} "
        f"library_deleted={stats['library_items_deleted']} tracks_seen={stats['tracks_seen']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
