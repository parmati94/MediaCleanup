"""
Removal history: a permanent record of everything MediaCleanup deletes, captured
from Radarr/Sonarr *before* the delete (deleting an item also wipes its *arr
history), plus the logic to put an item back exactly as it was.

Each removal stores:
  - what it was (title/year/ids/path/size) and why it went (watch data, filters)
  - a re-add spec (profile, root folder, monitoring, seasons) to recreate it
  - the release(s) that were on disk: tracker page, info hash and the grab's
    download URL, so re-add can hand the *same* release back to Radarr/Sonarr,
    which then downloads + imports it through the normal pipeline.

download_url/guid carry tracker passkeys / the Jackett api key. They stay in this
local database (Radarr/Sonarr's own databases hold the same values) and are never
returned by the API or written to exports.
"""
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = os.environ.get("HISTORY_DB", "/app/data/history/history.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    source       TEXT NOT NULL,          -- 'app' | 'backfill'
    filters_json TEXT
);
CREATE TABLE IF NOT EXISTS removals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL REFERENCES batches(id),
    removed_at   TEXT NOT NULL,
    status       TEXT NOT NULL,          -- 'pending' (captured, delete in flight) | 'removed'
    media_type   TEXT NOT NULL,          -- 'movie' | 'show'
    arr_id       INTEGER,
    title        TEXT NOT NULL,
    year         INTEGER,
    tmdb_id      INTEGER,
    tvdb_id      INTEGER,
    imdb_id      TEXT,
    path         TEXT,
    size_bytes   INTEGER,
    quality      TEXT,
    last_played  INTEGER,
    play_count   INTEGER,
    readd_json   TEXT,                   -- spec to recreate the item in Radarr/Sonarr
    capture_note TEXT,                   -- set when capture was incomplete
    readd_status TEXT,                   -- NULL | running | grabbed | searching | partial | failed | restored
    readd_detail TEXT,
    readd_at     TEXT,
    new_arr_id   INTEGER,
    restored_at  TEXT                    -- when the re-added item had files in the library again
);
CREATE TABLE IF NOT EXISTS releases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    removal_id    INTEGER NOT NULL REFERENCES removals(id) ON DELETE CASCADE,
    title         TEXT,
    indexer       TEXT,
    indexer_id    INTEGER,
    info_url      TEXT,
    download_url  TEXT,                  -- secret-bearing; never leaves the backend
    guid          TEXT,                  -- secret-bearing; never leaves the backend
    info_hash     TEXT,
    size_bytes    INTEGER,
    protocol      TEXT,
    release_group TEXT,
    quality       TEXT,
    seasons       TEXT,                  -- JSON list of season numbers (shows)
    published_at  TEXT,
    grabbed_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_removals_batch ON removals(batch_id);
CREATE INDEX IF NOT EXISTS ix_releases_removal ON releases(removal_id);
"""

PROTOCOLS = {'1': 'usenet', '2': 'torrent', 1: 'usenet', 2: 'torrent'}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _int(v) -> Optional[int]:
    try:
        return int(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


class HistoryStore:
    """Thin SQLite wrapper. One short-lived connection per operation, so it is
    safe to use from FastAPI's threadpool and the re-add worker threads."""

    def __init__(self, path: str = DB_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(removals)")}
            if 'restored_at' not in cols:
                c.execute("ALTER TABLE removals ADD COLUMN restored_at TEXT")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- writes ---

    def start_batch(self, source: str = 'app', filters: Optional[Dict[str, Any]] = None,
                    created_at: Optional[str] = None, batch_id: Optional[str] = None) -> str:
        batch_id = batch_id or uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO batches(id, created_at, source, filters_json) VALUES (?,?,?,?)",
                      (batch_id, created_at or now_iso(), source,
                       json.dumps(filters) if filters is not None else None))
        return batch_id

    def add_removal(self, batch_id: str, record: Dict[str, Any], status: str = 'pending',
                    removed_at: Optional[str] = None) -> int:
        cols = ('media_type', 'arr_id', 'title', 'year', 'tmdb_id', 'tvdb_id', 'imdb_id', 'path',
                'size_bytes', 'quality', 'last_played', 'play_count', 'capture_note')
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO removals(batch_id, removed_at, status, readd_json, {', '.join(cols)}) "
                f"VALUES (?,?,?,?,{','.join('?' * len(cols))})",
                (batch_id, removed_at or now_iso(), status, json.dumps(record.get('readd') or {}),
                 *[record.get(k) for k in cols]))
            removal_id = cur.lastrowid
            for rel in record.get('releases') or []:
                c.execute(
                    "INSERT INTO releases(removal_id, title, indexer, indexer_id, info_url, download_url, guid,"
                    " info_hash, size_bytes, protocol, release_group, quality, seasons, published_at, grabbed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (removal_id, rel.get('title'), rel.get('indexer'), rel.get('indexer_id'), rel.get('info_url'),
                     rel.get('download_url'), rel.get('guid'), rel.get('info_hash'), rel.get('size_bytes'),
                     rel.get('protocol'), rel.get('release_group'), rel.get('quality'),
                     json.dumps(rel.get('seasons')) if rel.get('seasons') is not None else None,
                     rel.get('published_at'), rel.get('grabbed_at')))
        return removal_id

    def mark_removed(self, removal_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE removals SET status='removed', removed_at=? WHERE id=?", (now_iso(), removal_id))

    def discard(self, removal_id: int) -> None:
        """Drop a pending record whose delete failed (the item is still in the library)."""
        with self._conn() as c:
            c.execute("DELETE FROM removals WHERE id=? AND status='pending'", (removal_id,))

    def mark_restored(self, removal_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE removals SET readd_status='restored', restored_at=? WHERE id=?", (now_iso(), removal_id))

    def set_readd(self, removal_id: int, status: str, detail: str, new_arr_id: Optional[int] = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE removals SET readd_status=?, readd_detail=?, readd_at=?,"
                      " new_arr_id=COALESCE(?, new_arr_id) WHERE id=?",
                      (status, detail, now_iso(), new_arr_id, removal_id))

    # --- reads ---

    def batch_exists(self, batch_id: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone() is not None

    @staticmethod
    def _public_release(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d['has_download_url'] = bool(d.pop('download_url', None))
        d.pop('guid', None)
        d['seasons'] = json.loads(d['seasons']) if d.get('seasons') else None
        if d.get('info_hash') and (d.get('protocol') or 'torrent') == 'torrent':
            from urllib.parse import quote
            d['magnet'] = f"magnet:?xt=urn:btih:{d['info_hash'].lower()}&dn={quote(d.get('title') or '')}"
        return d

    def list_removals(self) -> List[Dict[str, Any]]:
        """Every removed item with its (secret-free) releases, newest first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT r.*, b.created_at AS batch_created_at, b.source AS batch_source FROM removals r"
                " JOIN batches b ON b.id=r.batch_id WHERE r.status='removed'"
                " ORDER BY b.created_at DESC, r.title COLLATE NOCASE").fetchall()
            rels: Dict[int, List[Dict[str, Any]]] = {}
            for rel in c.execute("SELECT * FROM releases ORDER BY id"):
                rels.setdefault(rel['removal_id'], []).append(self._public_release(rel))
        out = []
        for r in rows:
            d = dict(r)
            spec = json.loads(d.pop('readd_json') or '{}')
            d['quality_profile_id'] = spec.get('qualityProfileId')
            d['root_folder'] = spec.get('rootFolderPath')
            d['releases'] = rels.get(d['id'], [])
            out.append(d)
        return out

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT b.id, b.created_at, b.source, b.filters_json, COUNT(r.id) AS items,"
                " COALESCE(SUM(r.size_bytes),0) AS total_bytes,"
                " SUM(r.media_type='movie') AS movies, SUM(r.media_type='show') AS shows"
                " FROM batches b JOIN removals r ON r.batch_id=b.id AND r.status='removed'"
                " GROUP BY b.id ORDER BY b.created_at DESC")]

    def get_removal(self, removal_id: int) -> Optional[Dict[str, Any]]:
        """Full record INCLUDING secret-bearing release fields - backend use only."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM removals WHERE id=?", (removal_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d['readd'] = json.loads(d.pop('readd_json') or '{}')
            d['releases'] = []
            for rel in c.execute("SELECT * FROM releases WHERE removal_id=? ORDER BY id", (removal_id,)):
                r = dict(rel)
                r['seasons'] = json.loads(r['seasons']) if r.get('seasons') else None
                d['releases'].append(r)
            return d


# ---------------------------------------------------------------------------
# Capture: turn Radarr/Sonarr history into "the release(s) currently on disk".
#
# Events are normalized to {type: 'grabbed'|'imported', download_id, date, data,
# source_title, quality, episode_id, season} so the same logic serves the live
# API and the backup-database backfill.
# ---------------------------------------------------------------------------

def _hash(download_id: Optional[str], reported: Optional[str]) -> Optional[str]:
    """The torrent's v1 info hash. Prefer the download client's id (always the real
    hash for torrents); some trackers report torrentInfoHash in a mangled form."""
    for v in (download_id, reported):
        if v and len(v) == 40 and all(ch in '0123456789abcdefABCDEF' for ch in v):
            return v.lower()
    return None


def _release_from_grab(grab: Dict[str, Any], seasons: Optional[List[int]]) -> Dict[str, Any]:
    data = grab.get('data') or {}
    proto = data.get('protocol')
    return {
        'title': grab.get('source_title'),
        'indexer': data.get('indexer'),
        'indexer_id': _int(data.get('indexerId')),
        'info_url': data.get('nzbInfoUrl') or None,
        'download_url': data.get('downloadUrl') or None,
        'guid': data.get('guid') or None,
        'info_hash': _hash(grab.get('download_id'), data.get('torrentInfoHash')),
        'size_bytes': _int(data.get('size')),
        'protocol': PROTOCOLS.get(proto, proto) if proto else None,
        'release_group': data.get('releaseGroup') or None,
        'quality': grab.get('quality'),
        'seasons': seasons,
        'published_at': data.get('publishedDate') or None,
        'grabbed_at': grab.get('date'),
    }


def current_releases(events: List[Dict[str, Any]], per_episode: bool) -> List[Dict[str, Any]]:
    """Releases that produced the files currently on disk.

    For each movie / episode, the latest import wins; its download id links back to
    the grab that carries the indexer, tracker page and download URL. An import with
    no matching grab (manual import) yields a link-less release so the on-disk
    release name is still recorded.
    """
    latest_import: Dict[Any, Dict[str, Any]] = {}
    for ev in sorted((e for e in events if e['type'] == 'imported'), key=lambda e: e['date'] or ''):
        latest_import[ev.get('episode_id') if per_episode else 'movie'] = ev

    by_download: Dict[str, Dict[str, Any]] = {}
    for ev in latest_import.values():
        did = (ev.get('download_id') or '').upper()
        if not did:
            continue
        slot = by_download.setdefault(did, {'import': ev, 'seasons': set()})
        if ev.get('season') is not None:
            slot['seasons'].add(ev['season'])

    grabs: Dict[str, Dict[str, Any]] = {}
    for ev in sorted((e for e in events if e['type'] == 'grabbed'), key=lambda e: e['date'] or ''):
        if ev.get('download_id'):
            grabs[ev['download_id'].upper()] = ev

    releases = []
    for did, slot in by_download.items():
        seasons = sorted(slot['seasons']) if per_episode else None
        grab = grabs.get(did)
        if grab:
            releases.append(_release_from_grab(grab, seasons))
        else:
            imp = slot['import']
            releases.append({'title': imp.get('source_title'), 'info_hash': _hash(did, None),
                             'quality': imp.get('quality'), 'seasons': seasons,
                             'release_group': (imp.get('data') or {}).get('releaseGroup')})
    releases.sort(key=lambda r: (r.get('seasons') or [0])[0])
    return releases


def _api_event(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind = {'grabbed': 'grabbed', 'downloadFolderImported': 'imported'}.get(ev.get('eventType'))
    if not kind:
        return None
    ep = ev.get('episode') or {}
    return {
        'type': kind,
        'download_id': ev.get('downloadId'),
        'date': ev.get('date'),
        'data': ev.get('data') or {},
        'source_title': ev.get('sourceTitle'),
        'quality': ((ev.get('quality') or {}).get('quality') or {}).get('name'),
        'episode_id': ev.get('episodeId'),
        'season': ep.get('seasonNumber'),
    }


def capture_movie(radarr, movie_id: int) -> Dict[str, Any]:
    """Snapshot a Radarr movie (must run BEFORE it is deleted)."""
    mv = radarr.get_movie(movie_id)
    mf = mv.get('movieFile') or {}
    record = {
        'media_type': 'movie', 'arr_id': movie_id,
        'title': mv.get('title'), 'year': mv.get('year'),
        'tmdb_id': mv.get('tmdbId'), 'imdb_id': mv.get('imdbId'),
        'path': mv.get('path'), 'size_bytes': mv.get('sizeOnDisk'),
        'quality': ((mf.get('quality') or {}).get('quality') or {}).get('name'),
        'readd': {k: mv.get(k) for k in ('tmdbId', 'imdbId', 'title', 'year', 'qualityProfileId',
                                         'rootFolderPath', 'path', 'monitored', 'minimumAvailability', 'tags')},
        'releases': [],
    }
    try:
        events = [e for e in map(_api_event, radarr.get_movie_history(movie_id)) if e]
        record['releases'] = current_releases(events, per_episode=False)
    except Exception as e:
        record['capture_note'] = f"history unavailable: {e}"
    if not record['releases'] and mf.get('sceneName'):
        record['releases'] = [{'title': mf.get('sceneName'), 'release_group': mf.get('releaseGroup'),
                               'quality': record['quality']}]
    return record


def capture_series(sonarr, series_id: int) -> Dict[str, Any]:
    """Snapshot a Sonarr series (must run BEFORE it is deleted)."""
    sr = sonarr.get_series_by_id(series_id)
    record = {
        'media_type': 'show', 'arr_id': series_id,
        'title': sr.get('title'), 'year': sr.get('year'),
        'tvdb_id': sr.get('tvdbId'), 'tmdb_id': sr.get('tmdbId') or None, 'imdb_id': sr.get('imdbId'),
        'path': sr.get('path'), 'size_bytes': (sr.get('statistics') or {}).get('sizeOnDisk'),
        'readd': {
            **{k: sr.get(k) for k in ('tvdbId', 'title', 'year', 'qualityProfileId', 'rootFolderPath', 'path',
                                      'monitored', 'seasonFolder', 'seriesType', 'monitorNewItems', 'tags')},
            'seasons': [{'seasonNumber': s.get('seasonNumber'), 'monitored': s.get('monitored')}
                        for s in sr.get('seasons') or []],
        },
        'releases': [],
    }
    try:
        seasons = {ep['id']: ep.get('seasonNumber') for ep in
                   sonarr._make_request('episode', params={'seriesId': series_id}) or []}
        events = [e for e in map(_api_event, sonarr.get_series_history(series_id)) if e]
        for e in events:
            if e['season'] is None:
                e['season'] = seasons.get(e['episode_id'])
        record['releases'] = current_releases(events, per_episode=True)
    except Exception as e:
        record['capture_note'] = f"history unavailable: {e}"
    return record


def minimal_record(media_type: str, arr_id: int, error: Exception) -> Dict[str, Any]:
    return {'media_type': media_type, 'arr_id': arr_id, 'title': f'{media_type} #{arr_id}',
            'capture_note': f'capture failed: {error}', 'readd': {}, 'releases': []}


IN_FLIGHT = ('grabbed', 'searching', 'partial')


def _queue_progress(records: List[Dict[str, Any]]) -> Optional[str]:
    """One-line summary of Radarr/Sonarr queue entries for an item."""
    if not records:
        return None
    states = {r.get('trackedDownloadState') for r in records}
    if states & {'importPending', 'importing'}:
        return 'Importing' if len(records) == 1 else f'Importing ({len(records)} downloads)'
    if 'importBlocked' in states or 'failedPending' in states:
        return 'Needs attention in ' + ('Radarr' if 'movieId' in records[0] else 'Sonarr')
    size = sum(r.get('size') or 0 for r in records)
    left = sum(r.get('sizeleft') or 0 for r in records)
    pct = int(100 * (size - left) / size) if size else 0
    return f"Downloading {pct}%" + (f" ({len(records)} downloads)" if len(records) > 1 else '')


def refresh_in_flight(store: HistoryStore, rows: List[Dict[str, Any]], radarr, sonarr) -> None:
    """For re-adds still waiting on a download, ask Radarr/Sonarr how it's going:
    mark the item restored once it has files and nothing left in the queue, else
    attach a progress line. Touches only in-flight items (usually none), and a
    restored item is never checked again."""
    for row in rows:
        if row.get('readd_status') not in IN_FLIGHT or not row.get('new_arr_id'):
            continue
        aid = row['new_arr_id']
        try:
            if row['media_type'] == 'movie' and radarr:
                item = radarr.get_movie(aid)
                queue = radarr._make_request('queue', params={'movieIds': aid, 'pageSize': 50})
                has_files = bool(item.get('hasFile'))
            elif row['media_type'] == 'show' and sonarr:
                item = sonarr.get_series_by_id(aid)
                queue = sonarr._make_request('queue', params={'seriesIds': aid, 'pageSize': 500})
                has_files = (item.get('statistics') or {}).get('episodeFileCount', 0) > 0
            else:
                continue
            records = (queue or {}).get('records') or []
            if has_files and not records:
                store.mark_restored(row['id'])
                row['readd_status'] = 'restored'
                row['restored_at'] = now_iso()
            else:
                row['readd_progress'] = _queue_progress(records) or (
                    'Searching for a release' if row['readd_status'] == 'searching' else 'Waiting for download')
        except Exception as e:
            resp = getattr(e, 'response', None)
            if resp is not None and resp.status_code == 404:
                row['readd_progress'] = 'No longer in ' + ('Radarr' if row['media_type'] == 'movie' else 'Sonarr')
            else:
                logging.warning(f"Could not check re-add progress for '{row.get('title')}': {e}")


# ---------------------------------------------------------------------------
# Re-add: recreate the item, then hand the original release(s) back.
#   1. push the stored download URL (exact release, no search)
#   2. otherwise interactive-search and grab the result with the same hash/title
#   3. otherwise fall back to the normal automatic search
# Radarr/Sonarr do the download, import and move - nothing here touches files.
# ---------------------------------------------------------------------------

def _decision_ok(decisions: List[Dict[str, Any]]) -> Tuple[bool, str]:
    for d in decisions:
        if d.get('approved') and not d.get('rejected'):
            return True, ''
    reasons = [r for d in decisions for r in (d.get('rejections') or [])]
    return False, '; '.join(str(r) for r in reasons) or 'rejected'


def _err(e: Exception) -> str:
    resp = getattr(e, 'response', None)
    if resp is not None:
        try:
            body = resp.json()
            if isinstance(body, list):
                return '; '.join(b.get('errorMessage', str(b)) for b in body)
            return body.get('message') or str(body)
        except Exception:
            return f"HTTP {resp.status_code}"
    return str(e)


def _norm_title(t: str) -> str:
    """Release names differ cosmetically between indexer proxies / trackers
    ('Movie.2018.1080p' vs 'Movie 2018 1080p'): compare on alphanumerics only."""
    import re
    return re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()


def _indexer_base(name: Optional[str]) -> str:
    """'Aither (Prowlarr)' and 'Aither' are the same tracker."""
    return _norm_title((name or '').replace('(Prowlarr)', ''))


def _match_result(results: List[Dict[str, Any]], rel: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pick the search result that is the original release.

    1. Same info hash - exact torrent (most indexers don't return hashes in search).
    2. Same release name (ignoring punctuation/case) AND, when both sizes are known,
       size within 1% - the same release; this guards against an identically named
       but different upload. Among matches, prefer the original tracker.
    No fuzzy matching: if nothing qualifies, the caller falls back to a normal search.
    """
    want_hash = (rel.get('info_hash') or '').lower()
    if want_hash:
        for r in results:
            if (r.get('infoHash') or '').lower() == want_hash:
                return r
    want_title = _norm_title(rel.get('title'))
    if not want_title:
        return None
    want_size = rel.get('size_bytes') or 0
    want_indexer = _indexer_base(rel.get('indexer'))

    def size_ok(r):
        size = r.get('size') or 0
        return not (want_size and size) or abs(size - want_size) <= 0.01 * want_size

    hits = [r for r in results if _norm_title(r.get('title')) == want_title and size_ok(r)]
    hits.sort(key=lambda r: (_indexer_base(r.get('indexer')) != want_indexer,
                             abs((r.get('size') or 0) - want_size)))
    return hits[0] if hits else None


def _push_payload(rel: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        'title': rel['title'],
        'downloadUrl': rel['download_url'],
        'protocol': rel.get('protocol') or 'torrent',
        'publishDate': rel.get('published_at') or rel.get('grabbed_at') or now_iso(),
        'size': rel.get('size_bytes') or 0,
        'infoUrl': rel.get('info_url'),
        'infoHash': rel.get('info_hash'),
        'indexer': rel.get('indexer'),
        **extra,
    }
    # Indexer ids from the original grab may belong to since-removed indexers
    # (Jackett era); send only the name and let Radarr/Sonarr resolve it.
    return payload


def _reachable(url: str) -> bool:
    """Is the download link's host still answering? (e.g. links from a since-removed
    Jackett instance.) Only a TCP connect - the URL itself is never fetched here."""
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        port = u.port or (443 if u.scheme == 'https' else 80)
        with socket.create_connection((u.hostname, port), timeout=3):
            return True
    except (OSError, ValueError):
        return False


def _import_existing(rel, importable, import_files, file_payload) -> Optional[str]:
    """If the original torrent is still in the download client (complete), import it
    directly - no download, and a re-grab would be refused as a duplicate anyway.
    Returns a description of what was imported, or None."""
    did = (rel.get('info_hash') or '').upper()
    if not did:
        return None
    files = [f for f in importable(did)
             if (f.get('downloadId') or '').upper() == did and not f.get('rejections')]
    if not files:
        return None
    import_files([dict(file_payload(f), downloadId=did) for f in files])
    return f"{len(files)} file(s)"


def _deliver(rel, push, search, grab, label, existing=None) -> Tuple[bool, str]:
    """Import from the download client if the original is still there, else push,
    else search-and-match. Returns (delivered, log line)."""
    notes = []
    if existing:
        try:
            done = existing(rel)
            if done:
                return True, f"{label}: original torrent is still in the download client - imported it ({done}, no download needed)"
        except Exception as e:
            notes.append(f"import from download client failed: {_err(e)}")
    if rel.get('download_url') and not _reachable(rel['download_url']):
        notes.append("stored download link no longer reachable")
    elif rel.get('download_url') and rel.get('title'):
        try:
            ok, why = _decision_ok(push(rel))
            if ok:
                return True, f"{label}: sent original release to the download client ({rel['title']})"
            notes.append(f"push rejected: {why}")
        except Exception as e:
            notes.append(f"push failed: {_err(e)}")
    if rel.get('title') or rel.get('info_hash'):
        hit = None
        try:
            hit = _match_result(search(), rel)
            if not hit:
                notes.append("not found in search results")
        except Exception as e:
            notes.append(f"search failed: {_err(e)}")
        if hit:
            try:
                grab(hit['guid'], hit['indexerId'])
                return True, f"{label}: found the same release on {hit.get('indexer')} and grabbed it ({hit.get('title')})"
            except Exception as e:
                notes.append(f"found it on {hit.get('indexer')} but the grab failed: {_err(e)}")
    return False, f"{label}: {', '.join(notes) or 'no stored release'}"


def readd_movie(radarr, rec: Dict[str, Any]) -> Tuple[str, List[str], Optional[int]]:
    spec = rec['readd']
    log = []
    movie = radarr.find_movie_by_tmdb(spec['tmdbId'])
    if movie:
        log.append(f"Already in Radarr (id {movie['id']})")
    else:
        payload = radarr.lookup_movie_by_tmdb(spec['tmdbId'])
        payload.pop('id', None)
        payload.update({
            'qualityProfileId': spec['qualityProfileId'],
            'rootFolderPath': spec['rootFolderPath'],
            'monitored': True,
            'minimumAvailability': spec.get('minimumAvailability') or 'released',
            'tags': spec.get('tags') or [],
            'addOptions': {'searchForMovie': False, 'monitor': 'movieOnly'},
        })
        movie = radarr.add_movie(payload)
        log.append(f"Added to Radarr (id {movie['id']})")

    mid = movie['id']
    extra = {'tmdbId': spec['tmdbId'], 'movieId': mid}
    cache = {}

    def search():
        if 'r' not in cache:
            cache['r'] = radarr.search_releases(mid)
        return cache['r']

    def existing(rel):
        return _import_existing(
            rel, lambda did: radarr.importable_files(did, mid), radarr.import_files,
            lambda f: {'path': f['path'], 'movieId': mid, 'quality': f.get('quality'),
                       'languages': f.get('languages') or [], 'releaseGroup': f.get('releaseGroup'),
                       'indexerFlags': f.get('indexerFlags') or 0})

    for rel in rec['releases']:
        ok, line = _deliver(rel, lambda r: radarr.push_release(_push_payload(r, extra)),
                            search, radarr.grab_release, 'Movie', existing)
        log.append(line)
        if ok:
            return 'grabbed', log, mid
    radarr.search_movie(mid)
    log.append("Started Radarr's automatic search for the best available release")
    return 'searching', log, mid


def _wait_for_episodes(sonarr, series_id: int, timeout: int = 90) -> None:
    """Sonarr fills in episodes asynchronously after an add; releases can't be
    matched until they exist."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sonarr._make_request('episode', params={'seriesId': series_id}):
            return
        time.sleep(3)


def readd_series(sonarr, rec: Dict[str, Any]) -> Tuple[str, List[str], Optional[int]]:
    spec = rec['readd']
    log = []
    series = sonarr.find_series_by_tvdb(spec['tvdbId'])
    if series:
        log.append(f"Already in Sonarr (id {series['id']})")
    else:
        payload = sonarr.lookup_series_by_tvdb(spec['tvdbId'])
        if not payload:
            raise RuntimeError(f"TVDB {spec['tvdbId']} not found by Sonarr lookup")
        payload.pop('id', None)
        monitored = {s['seasonNumber']: s['monitored'] for s in spec.get('seasons') or []}
        for s in payload.get('seasons') or []:
            s['monitored'] = monitored.get(s['seasonNumber'], s.get('monitored', True))
        payload.update({
            'qualityProfileId': spec['qualityProfileId'],
            'rootFolderPath': spec['rootFolderPath'],
            'seasonFolder': spec.get('seasonFolder', True),
            'seriesType': spec.get('seriesType') or 'standard',
            'monitorNewItems': spec.get('monitorNewItems') or 'all',
            'monitored': True,
            'tags': spec.get('tags') or [],
            'addOptions': {'searchForMissingEpisodes': False, 'searchForCutoffUnmetEpisodes': False},
        })
        series = sonarr.add_series(payload)
        log.append(f"Added to Sonarr (id {series['id']})")
        _wait_for_episodes(sonarr, series['id'])

    sid = series['id']
    extra = {'tvdbId': spec['tvdbId'], 'seriesId': sid}
    cache: Dict[int, List[Dict[str, Any]]] = {}

    def searcher(season):
        def run():
            if season not in cache:
                cache[season] = sonarr.search_releases(sid, season)
            return cache[season]
        return run

    def existing(rel):
        return _import_existing(
            rel, lambda did: sonarr.importable_files(did, sid), sonarr.import_files,
            lambda f: {'path': f['path'], 'seriesId': sid, 'seasonNumber': f.get('seasonNumber'),
                       'episodeIds': [e['id'] for e in f.get('episodes') or []], 'quality': f.get('quality'),
                       'languages': f.get('languages') or [], 'releaseGroup': f.get('releaseGroup'),
                       'releaseType': f.get('releaseType'), 'indexerFlags': f.get('indexerFlags') or 0})

    covered, missed = set(), set()
    for rel in rec['releases']:
        seasons = rel.get('seasons') or []
        label = 'Season ' + ', '.join(str(s) for s in seasons) if seasons else 'Release'
        first = seasons[0] if seasons else None
        ok, line = _deliver(rel, lambda r: sonarr.push_release(_push_payload(r, extra)),
                            searcher(first) if first is not None else (lambda: []),
                            sonarr.grab_release, label, existing)
        log.append(line)
        (covered if ok else missed).update(seasons)

    # Sonarr's season search skips episodes already queued, so searching a season
    # that was only partly delivered is safe and fills the gaps.
    to_search = sorted(missed)
    if not rec['releases']:
        sonarr.search_seasons(sid, [])
        log.append("No stored releases - started Sonarr's automatic search for the whole series")
        return 'searching', log, sid
    if to_search:
        sonarr.search_seasons(sid, to_search)
        log.append(f"Started Sonarr's automatic search for season(s) {', '.join(map(str, to_search))}")
        return ('searching' if not covered else 'partial'), log, sid
    return 'grabbed', log, sid


_readd_lock = threading.Lock()
_readd_running: set = set()


def start_readd(store: HistoryStore, removal_id: int, radarr, sonarr) -> bool:
    """Kick off a re-add in the background. Returns False if one is already running."""
    with _readd_lock:
        if removal_id in _readd_running:
            return False
        _readd_running.add(removal_id)
    store.set_readd(removal_id, 'running', 'Re-adding...')

    def work():
        try:
            rec = store.get_removal(removal_id)
            if not rec or not rec.get('readd'):
                raise RuntimeError('No re-add information was captured for this item')
            logging.info(f"Re-add of '{rec['title']}' ({rec['media_type']}) started: "
                         f"{len(rec['releases'])} original release(s) on record")
            if rec['media_type'] == 'movie':
                if not radarr:
                    raise RuntimeError('Radarr is not enabled')
                status, log, new_id = readd_movie(radarr, rec)
            else:
                if not sonarr:
                    raise RuntimeError('Sonarr is not enabled')
                status, log, new_id = readd_series(sonarr, rec)
            store.set_readd(removal_id, status, '\n'.join(log), new_id)
            for line in log:
                logging.info(f"Re-add of '{rec['title']}': {line}")
            logging.info(f"Re-add of '{rec['title']}' finished: {status}")
        except Exception as e:
            logging.error(f"Re-add {removal_id} failed: {e}", exc_info=True)
            store.set_readd(removal_id, 'failed', _err(e))
        finally:
            with _readd_lock:
                _readd_running.discard(removal_id)

    threading.Thread(target=work, daemon=True).start()
    return True
