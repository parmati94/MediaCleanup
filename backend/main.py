"""
Media Cleanup UI - FastAPI Backend
"""
import copy
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yaml

from backend.core.analyzer import LibraryAnalyzer
from backend.core.media_remover import MediaRemover
from backend.core.history import HistoryStore, start_readd, refresh_in_flight, IN_FLIGHT, poster_url
from backend.api.tautulli import TautulliAPI
from backend.api.radarr import RadarrAPI
from backend.api.sonarr import SonarrAPI
from backend.utils.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

# Config file path
CONFIG_PATH = Path("/app/data/config.yaml")

_history: Optional[HistoryStore] = None


def get_history() -> HistoryStore:
    """Removal-history store (SQLite under /app/data/history), opened on first use."""
    global _history
    if _history is None:
        _history = HistoryStore()
    return _history


def load_config() -> Dict[str, Any]:
    """Load configuration from YAML file."""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning(f"Config file not found at {CONFIG_PATH}, using defaults")
        return get_default_config()
    except yaml.YAMLError as e:
        logger.error(f"Error parsing config: {e}")
        return get_default_config()


def save_config(config: Dict[str, Any]) -> None:
    """Save configuration to YAML file."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)


def get_default_config() -> Dict[str, Any]:
    """Get default configuration."""
    return {
        'tautulli': {
            'url': 'http://localhost:8181',
            'api_key': ''
        },
        'media': {
            'process_movies': False,
            'process_tv_shows': True,
            'days_unwatched': 365,
            'min_days_since_added': 400,
            'max_play_count': 1,
            'filters': {
                'min_rating_to_keep': 0,
                'min_audience_rating_to_keep': 0,
                'protect_classics_before_year': 1995,
                'protect_recent_after_year': 0,
                'min_file_size_to_keep': 0,
                'protected_keywords': [],
                'protected_genres': []
            }
        },
        'radarr': {
            'enabled': False,
            'url': 'http://localhost:7878',
            'api_key': '',
            'delete_files': True,
            'add_to_exclusion': False
        },
        'sonarr': {
            'enabled': False,
            'url': 'http://localhost:8989',
            'api_key': '',
            'delete_files': True,
            'add_to_exclusion': False
        },
        'safety': {
            # Config-only (no UI toggle): when true, removals are logged, not performed.
            'dry_run': False,
            'max_items_per_run': 200,
            'require_confirmation': True
        }
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting Media Cleanup UI backend")
    if not CONFIG_PATH.exists():
        logger.info(f"No config file found — writing defaults to {CONFIG_PATH}")
        save_config(get_default_config())
    threading.Thread(target=_watch_warmer, name="watch-warmer", daemon=True).start()
    threading.Thread(target=_backfill_history_posters, name="poster-backfill", daemon=True).start()
    yield
    logger.info("Shutting down Media Cleanup UI backend")


# Create FastAPI app
app = FastAPI(
    title="Media Cleanup UI",
    description="Web interface for managing unwatched media removal",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic models
class ConfigUpdate(BaseModel):
    """Model for config updates."""
    config: Dict[str, Any]


class AnalysisRequest(BaseModel):
    """Model for analysis requests."""
    summary_only: bool = False
    test_thresholds: bool = False
    # When True, force Tautulli to rebuild its media info tables from Plex before
    # reading them (drops deleted items / updates sizes). Slower; used on demand.
    refresh: bool = False


class RemovalRequest(BaseModel):
    """Model for removal requests."""
    confirm: bool = False
    ids: Optional[List[str]] = None          # "movie:123" / "show:45" (preferred)
    rating_keys: Optional[List[str]] = None  # legacy Tautulli rating keys


class PreviewRequest(BaseModel):
    """Model for live filter-impact preview. `media` is the (possibly unsaved)
    media config block from the settings modal."""
    media: Dict[str, Any] = {}


# API Endpoints

@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "media-cleanup-ui"}


@app.get("/api/config")
async def get_config():
    """Get current configuration."""
    try:
        config = load_config()
        return {"success": True, "data": config}
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/config")
async def update_config(update: ConfigUpdate):
    """Update configuration."""
    try:
        save_config(update.config)
        return {"success": True, "message": "Configuration updated successfully"}
    except Exception as e:
        logger.error(f"Error updating config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/analyze")
async def analyze_library(request: AnalysisRequest):
    """Run library analysis and return results."""
    try:
        config = load_config()
        analyzer = LibraryAnalyzer(config)
        
        # Item universe from Radarr/Sonarr (authoritative catalog) joined with
        # Tautulli watch history - no dependence on Tautulli's cached media_info table.
        all_items, library_size = _build_catalog(config)

        # Calculate statistics
        result = {
            'total_items': len(all_items),
            'watch_status': _calculate_watch_status(all_items),
            'age_distribution': _calculate_age_distribution(all_items),
            'current_config_impact': _calculate_config_impact(all_items, analyzer, library_size=library_size),
            'top_lists': _get_top_lists(all_items)
        }
        
        if request.test_thresholds:
            result['threshold_tests'] = _test_thresholds(all_items, config, analyzer.tautulli)
        
        return {"success": True, "data": result}
        
    except Exception as e:
        logger.error(f"Error during analysis: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _make_clean_title(title: str, strip_year: bool = False) -> str:
    """Lowercase + strip non-alphanumeric, optionally removing a trailing year like '(2009)'."""
    import re
    if strip_year:
        title = re.sub(r'\s*\(\d{4}\)\s*$', '', title)
    return re.sub(r'[^a-z0-9]', '', title.lower())


def _sonarr_radarr_title_keys(raw_title: str, sonarr_clean: str = None) -> set:
    """Return all lookup keys for a Radarr/Sonarr title.

    We store three variants per entry so we can match regardless of whether
    Tautulli appended a disambiguation year or not, and regardless of whether
    Sonarr's own cleanTitle has stop words stripped (which we can't replicate):
      1. Sonarr/Radarr's own cleanTitle field (their authoritative key)
      2. Our simple transform with year kept   e.g. 'archer2009'
      3. Our simple transform with year stripped  e.g. 'archer'
    """
    keys = set()
    if sonarr_clean:
        keys.add(sonarr_clean)
    keys.add(_make_clean_title(raw_title, strip_year=False))
    keys.add(_make_clean_title(raw_title, strip_year=True))
    return keys


def _load_arr_sizes(config: Dict[str, Any]) -> tuple:
    """Build exact on-disk size maps from Radarr and Sonarr.

    Radarr/Sonarr report authoritative file sizes that never go stale, so we
    use these instead of Tautulli's media-info sizes (which blank out whenever
    the media-info table is refreshed - TV show aggregate sizes especially).

    Returns (movie_sizes, show_sizes). Each is a dict with:
      'by_title_year': {(clean_title_no_year, year_str): size_bytes}  # primary
      'by_key':        {clean_title_key: size_bytes}                  # fallback
    The 'by_key' keys use the same scheme as the stale cross-reference, so its
    key set doubles as the Radarr/Sonarr title lookup used there.
    """
    def build(items: List[Dict[str, Any]]) -> Dict[str, dict]:
        by_ty: dict = {}
        by_key: dict = {}
        total = 0
        for it in items:
            title = it.get('title', '') or ''
            # Radarr movies expose sizeOnDisk at top level; Sonarr series nest it
            # under statistics.
            size = it.get('sizeOnDisk')
            if size is None:
                size = (it.get('statistics') or {}).get('sizeOnDisk')
            if not size:
                continue
            total += size
            ct = _make_clean_title(title, strip_year=True)
            if ct:
                by_ty[(ct, str(it.get('year')))] = size
            for k in _sonarr_radarr_title_keys(title, it.get('cleanTitle')):
                if k:
                    by_key.setdefault(k, size)
        return {'by_title_year': by_ty, 'by_key': by_key, 'total': total}

    empty = {'by_title_year': {}, 'by_key': {}, 'total': 0}
    movie_sizes, show_sizes = dict(empty), dict(empty)

    if config.get('radarr', {}).get('enabled'):
        try:
            radarr = RadarrAPI(config['radarr']['url'], config['radarr']['api_key'])
            movie_sizes = build(radarr.get_movies())
            logger.info(f"Loaded {len(movie_sizes['by_title_year'])} Radarr movie sizes")
        except Exception as e:
            logger.warning(f"Could not load Radarr sizes: {e}")

    if config.get('sonarr', {}).get('enabled'):
        try:
            sonarr = SonarrAPI(config['sonarr']['url'], config['sonarr']['api_key'])
            show_sizes = build(sonarr.get_series())
            logger.info(f"Loaded {len(show_sizes['by_title_year'])} Sonarr series sizes")
        except Exception as e:
            logger.warning(f"Could not load Sonarr sizes: {e}")

    return movie_sizes, show_sizes


def _overlay_file_sizes(items: List[Dict[str, Any]], movie_sizes: dict, show_sizes: dict) -> int:
    """Replace each item's file_size with the exact Radarr/Sonarr on-disk size.

    Matches on (clean title, year) first, then falls back to any clean-title key.
    Items with no match keep whatever file_size they already had. Mutates items
    in place; returns the number of items matched.
    """
    matched = 0
    for item in items:
        maps = movie_sizes if item.get('library_type') == 'movie' else show_sizes
        title = item.get('title', '') or ''
        size = maps['by_title_year'].get((_make_clean_title(title, strip_year=True), str(item.get('year'))))
        if size is None:
            for k in _sonarr_radarr_title_keys(title):
                if k in maps['by_key']:
                    size = maps['by_key'][k]
                    break
        if size is not None:
            item['file_size'] = size
            matched += 1
    logger.info(f"Overlaid Radarr/Sonarr sizes onto {matched}/{len(items)} items")
    return matched


def _iso_to_epoch(value: Optional[str]) -> int:
    """Convert a Radarr/Sonarr ISO 'added' timestamp to a unix epoch int."""
    if not value:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
    except Exception:
        return 0


def _arr_rating(ratings: Optional[Dict[str, Any]], order) -> Optional[float]:
    """Pull a 0-10 rating value out of a Radarr ratings block by source priority."""
    ratings = ratings or {}
    for src in order:
        v = ratings.get(src)
        if isinstance(v, dict) and v.get('value'):
            return v.get('value')
    return None


def _add_play(movie_watch: dict, show_watch: dict, r: Dict[str, Any]) -> None:
    """Fold one Tautulli history row into the watch maps."""
    mt = r.get('media_type')
    try:
        date = int(r.get('date') or 0)
    except (ValueError, TypeError):
        date = 0
    if mt == 'movie':
        title = r.get('title') or r.get('full_title') or ''
        target = movie_watch
    elif mt == 'episode':
        title = r.get('grandparent_title') or ''
        target = show_watch
    else:
        return
    if not title:
        return
    for k in _sonarr_radarr_title_keys(title):
        if not k:
            continue
        entry = target.get(k)
        if entry is None:
            target[k] = {'last_played': date, 'play_count': 1}
        else:
            if date > (entry['last_played'] or 0):
                entry['last_played'] = date
            entry['play_count'] += 1


# Tautulli's play log only grows, and re-reading all of it costs several seconds
# (~2s per 10k-row page, mostly Tautulli's own overhead). Keep the folded maps in
# memory and on later loads fetch only recent rows, skipping ones already counted.
# A full rebuild every few hours picks up history deleted in Tautulli.
_WATCH_CACHE: Dict[str, Any] = {"sig": None, "built": 0.0, "movie": None, "show": None,
                                "row_ids": set(), "max_date": 0}
_WATCH_LOCK = threading.Lock()
_WATCH_FULL_REBUILD_SECONDS = 6 * 3600
# Sessions are logged with their *start* date when they end, so a play can land
# older than the newest row already seen - re-scan this far back on each refresh.
_WATCH_OVERLAP_SECONDS = 6 * 3600
_HISTORY_PAGE = 10000


def _history_page(tau, start: int, length: int) -> Dict[str, Any]:
    return tau._make_request('get_history', timeout=60, length=length, start=start,
                             order_column='date', order_dir='desc')


def _play_key(r: Dict[str, Any]) -> tuple:
    """Identity of one history row. get_history doesn't return its row id, so use
    fields that stay fixed for the life of a play: in-progress sessions are listed
    too, and their stop time keeps moving until they end."""
    return (r.get('started'), r.get('rating_key'), r.get('user_id'))


def _rows(resp) -> List[Dict[str, Any]]:
    return resp.get('data', []) if isinstance(resp, dict) else (resp or [])


def _load_watch_map(config: Dict[str, Any]) -> tuple:
    """Watch maps from Tautulli's history (the complete play log) rather than the
    lazily-cached media_info table.

    Returns (movie_watch, show_watch); each is {clean_title_key: {'last_played', 'play_count'}}.
    Movies key on the play's title; shows key on the show (grandparent) title. A title
    absent from the map has never been played - so an empty map must never be
    returned just because Tautulli was unreachable: that would make the whole library
    look unwatched. On failure the last good maps are used, or the error propagates.
    """
    tau = TautulliAPI(config['tautulli']['url'], config['tautulli']['api_key'])
    sig = (config['tautulli'].get('url'), config['tautulli'].get('api_key'))

    with _WATCH_LOCK:
        c = _WATCH_CACHE
        have = c['movie'] is not None and c['sig'] == sig
        if have and time.time() - c['built'] < _WATCH_FULL_REBUILD_SECONDS:
            try:
                return _refresh_watch_map(tau)
            except Exception as e:
                logger.warning(f"Incremental Tautulli refresh failed, rebuilding: {e}")
        try:
            _store_watch_map(sig, _read_watch_history(tau))
        except Exception as e:
            if not have:
                raise RuntimeError(f"Could not read Tautulli watch history: {e}") from e
            logger.warning(f"Tautulli history rebuild failed, keeping the last good copy: {e}")
        return c['movie'], c['show']


def _refresh_watch_map(tau) -> tuple:
    """Fold in plays newer than the cache (plus an overlap window). Caller holds the lock."""
    c = _WATCH_CACHE
    cutoff = c['max_date'] - _WATCH_OVERLAP_SECONDS
    start, added, page = 0, 0, 1000
    while True:
        rows = _rows(_history_page(tau, start, page))
        for r in rows:
            key = _play_key(r)
            if key in c['row_ids']:
                continue
            c['row_ids'].add(key)
            _add_play(c['movie'], c['show'], r)
            c['max_date'] = max(c['max_date'], int(r.get('date') or 0))
            added += 1
        oldest = int(rows[-1].get('date') or 0) if rows else 0
        if len(rows) < page or oldest < cutoff:
            break
        start += page
    if added:
        logger.info(f"Tautulli history: {added} new play(s) since last load")
    return c['movie'], c['show']


def _read_watch_history(tau) -> Dict[str, Any]:
    """Read and fold the whole play log (pages fetched in parallel). Touches no
    shared state, so it can run without the lock. Raises on any failure."""
    from concurrent.futures import ThreadPoolExecutor
    first = _history_page(tau, 0, _HISTORY_PAGE)
    if not isinstance(first, dict):
        raise RuntimeError("unexpected get_history response")
    total = first.get('recordsFiltered') or first.get('recordsTotal') or 0
    pages = [_rows(first)]
    starts = list(range(_HISTORY_PAGE, min(total, 500000), _HISTORY_PAGE))
    if starts:
        with ThreadPoolExecutor(max_workers=4) as pool:
            pages += [_rows(p) for p in pool.map(lambda st: _history_page(tau, st, _HISTORY_PAGE), starts)]

    movie_watch: dict = {}
    show_watch: dict = {}
    row_ids, max_date = set(), 0
    for rows in pages:
        for r in rows:
            key = _play_key(r)
            if key in row_ids:
                continue  # rows can shift between pages if a play lands mid-read
            row_ids.add(key)
            _add_play(movie_watch, show_watch, r)
            max_date = max(max_date, int(r.get('date') or 0))
    if total and not row_ids:
        raise RuntimeError("Tautulli reported plays but returned none")
    return {'movie': movie_watch, 'show': show_watch, 'row_ids': row_ids, 'max_date': max_date}


def _store_watch_map(sig, data: Dict[str, Any]) -> None:
    """Swap in a freshly read history. Caller holds the lock."""
    _WATCH_CACHE.update(sig=sig, built=time.time(), **data)
    logger.info(f"Loaded Tautulli history: {len(data['row_ids'])} plays, "
                f"{len(data['movie'])} movie keys, {len(data['show'])} show keys")


def _backfill_history_posters() -> None:
    """Fill in posters for history records captured before posters were stored
    (one metadata lookup each; runs once in the background and skips failures)."""
    try:
        store = get_history()
        missing = store.missing_posters()
        if not missing:
            return
        config = load_config()
        radarr = RadarrAPI(config['radarr']['url'], config['radarr']['api_key']) if config.get('radarr', {}).get('enabled') else None
        sonarr = SonarrAPI(config['sonarr']['url'], config['sonarr']['api_key']) if config.get('sonarr', {}).get('enabled') else None
        found = 0
        for rid, media_type, tmdb_id, tvdb_id in missing:
            try:
                if media_type == 'movie' and radarr and tmdb_id:
                    url = poster_url(radarr.lookup_movie_by_tmdb(tmdb_id).get('images'))
                elif media_type == 'show' and sonarr and tvdb_id:
                    url = poster_url((sonarr.lookup_series_by_tvdb(tvdb_id) or {}).get('images'))
                else:
                    continue
                if url:
                    store.set_poster(rid, url)
                    found += 1
            except Exception:
                continue
        logger.info(f"Removal history: filled in {found}/{len(missing)} posters")
    except Exception as e:
        logger.warning(f"Poster backfill failed: {e}")


_WATCH_REFRESH_SECONDS = 5 * 3600   # < _WATCH_FULL_REBUILD_SECONDS, so requests never pay for it


def _watch_warmer() -> None:
    """Background thread: read the history right after startup (requests arriving
    meanwhile wait on the lock instead of starting a second read), then re-read it
    every few hours *outside* the lock and swap it in, so a page load never waits
    for a full read."""
    try:
        _load_watch_map(load_config())
    except Exception as e:
        logger.warning(f"Startup Tautulli history warm-up failed (will load on demand): {e}")
    while True:
        time.sleep(_WATCH_REFRESH_SECONDS)
        try:
            config = load_config()
            tau = TautulliAPI(config['tautulli']['url'], config['tautulli']['api_key'])
            sig = (config['tautulli'].get('url'), config['tautulli'].get('api_key'))
            data = _read_watch_history(tau)
            with _WATCH_LOCK:
                _store_watch_map(sig, data)
        except Exception as e:
            logger.warning(f"Background Tautulli history refresh failed, keeping the last good copy: {e}")


def watch_for(lib: str, title: str) -> Dict[str, Any]:
    """Cached watch data for one title (for the removal-history record)."""
    target = _WATCH_CACHE['movie' if lib == 'movie' else 'show'] or {}
    for k in _sonarr_radarr_title_keys(title or ''):
        if k in target:
            return target[k]
    return {}


def _catalog_item(lib, arr_id, title, year, size, added_iso, genres, rating, audience, clean_title, watch_map, poster=None):
    """Assemble one catalog item (Radarr/Sonarr fields + joined Tautulli watch data)."""
    watch = None
    for k in _sonarr_radarr_title_keys(title or '', clean_title):
        if k in watch_map:
            watch = watch_map[k]
            break
    watch = watch or {}
    return {
        'library_type': lib,
        'library_name': 'Movies' if lib == 'movie' else 'TV Shows',
        'id': f"{lib}:{arr_id}",        # stable selection/removal handle
        'arr_id': arr_id,
        'title': title or '',
        'year': year,
        'file_size': size,
        'added_at': _iso_to_epoch(added_iso),
        'last_played': watch.get('last_played'),
        'play_count': watch.get('play_count', 0),
        'genres': genres or [],
        'rating': rating,
        'audience_rating': audience,
        'poster': poster,
    }


def _build_catalog(config: Dict[str, Any]) -> tuple:
    """Build the item universe straight from Radarr/Sonarr (the authoritative catalog),
    joined with Tautulli watch history. Returns (items, library_size).

    This replaces reading Tautulli's stale media_info table: the item list is always
    complete/current, sizes are exact, and each item carries its Radarr/Sonarr id so
    removal can target it precisely.
    """
    from concurrent.futures import ThreadPoolExecutor
    want_movies = config.get('radarr', {}).get('enabled') and config['media'].get('process_movies', True)
    want_shows = config.get('sonarr', {}).get('enabled') and config['media'].get('process_tv_shows', True)

    def fetch(fn):
        try:
            return fn(), None
        except Exception as e:
            return None, e

    # Tautulli, Radarr and Sonarr are independent - fetch them concurrently.
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_watch = pool.submit(_load_watch_map, config)
        f_movies = pool.submit(fetch, lambda: RadarrAPI(config['radarr']['url'], config['radarr']['api_key']).get_movies()) if want_movies else None
        f_series = pool.submit(fetch, lambda: SonarrAPI(config['sonarr']['url'], config['sonarr']['api_key']).get_series()) if want_shows else None
        movie_watch, show_watch = f_watch.result()
        movies, movies_err = f_movies.result() if f_movies else (None, None)
        series, series_err = f_series.result() if f_series else (None, None)

    items: List[Dict[str, Any]] = []
    library_size = 0

    if want_movies:
        try:
            if movies_err:
                raise movies_err
            for mv in movies:
                size = mv.get('sizeOnDisk') or 0
                if not size:
                    continue
                library_size += size
                items.append(_catalog_item(
                    'movie', mv.get('id'), mv.get('title'), mv.get('year'), size,
                    mv.get('added'), mv.get('genres'),
                    _arr_rating(mv.get('ratings'), ('imdb', 'tmdb', 'trakt')),
                    _arr_rating(mv.get('ratings'), ('tmdb', 'trakt', 'imdb')),
                    mv.get('cleanTitle'), movie_watch, poster_url(mv.get('images'))))
            logger.info(f"Radarr catalog: {sum(1 for i in items if i['library_type']=='movie')} movies with files")
        except Exception as e:
            logger.warning(f"Could not load Radarr catalog: {e}")

    if want_shows:
        try:
            if series_err:
                raise series_err
            for sr in series:
                size = (sr.get('statistics') or {}).get('sizeOnDisk') or 0
                if not size:
                    continue
                library_size += size
                rv = (sr.get('ratings') or {}).get('value')
                items.append(_catalog_item(
                    'show', sr.get('id'), sr.get('title'), sr.get('year'), size,
                    sr.get('added'), sr.get('genres'), rv, rv,
                    sr.get('cleanTitle'), show_watch, poster_url(sr.get('images'))))
            logger.info(f"Sonarr catalog: {sum(1 for i in items if i['library_type']=='show')} series with files")
        except Exception as e:
            logger.warning(f"Could not load Sonarr catalog: {e}")

    return items, library_size


# In-memory cache of the FULL catalog (both media types, ignoring process flags)
# so the live filter preview can re-filter in memory without rebuilding on every
# keystroke. Keyed on the connection signature; short TTL keeps it fresh.
_CATALOG_CACHE: Dict[str, Any] = {"sig": None, "ts": 0.0, "items": None, "library_size": 0}
_CATALOG_TTL_SECONDS = 120


def _connection_signature(config: Dict[str, Any]) -> tuple:
    """Identity of the data sources; the cached catalog is invalid if any change."""
    r = config.get('radarr', {}) or {}
    s = config.get('sonarr', {}) or {}
    t = config.get('tautulli', {}) or {}
    return (
        r.get('enabled'), r.get('url'), r.get('api_key'),
        s.get('enabled'), s.get('url'), s.get('api_key'),
        t.get('url'), t.get('api_key'),
    )


def _get_full_catalog(config: Dict[str, Any]) -> tuple:
    """Return (items, library_size) for the whole library (movies AND shows,
    regardless of the process_movies/process_tv_shows toggles), cached briefly.

    Media-type selection is applied by callers in memory, so toggling it in the
    preview never forces a rebuild.
    """
    sig = _connection_signature(config)
    now = time.time()
    cached = _CATALOG_CACHE
    if cached["items"] is not None and cached["sig"] == sig and (now - cached["ts"]) < _CATALOG_TTL_SECONDS:
        return cached["items"], cached["library_size"]

    full_cfg = copy.deepcopy(config)
    full_cfg.setdefault('media', {})
    full_cfg['media']['process_movies'] = True
    full_cfg['media']['process_tv_shows'] = True
    items, library_size = _build_catalog(full_cfg)

    _CATALOG_CACHE.update(sig=sig, ts=now, items=items, library_size=library_size)
    return items, library_size


def _genre_name(g) -> Optional[str]:
    """Normalize a genre entry (string from *arr, or a {'tag': ...} dict) to a name."""
    if isinstance(g, dict):
        return g.get('tag')
    return str(g) if g else None


@app.post("/api/preview")
async def preview_impact(request: PreviewRequest):
    """Live preview of how many items (and how much space) the given, possibly
    unsaved, filter settings would flag for removal. Re-filters the cached
    catalog in memory - no writes, no removal, cheap enough to call as-you-type."""
    from backend.core.filters import MediaFilter
    try:
        config = load_config()
        items, library_size = _get_full_catalog(config)

        # Merge the posted (unsaved) media block over the saved one, deep-merging
        # the nested filters dict so partial payloads don't wipe other filters.
        preview_config = copy.deepcopy(config)
        posted = request.media or {}
        merged_media = {**config.get('media', {}), **posted}
        if 'filters' in posted:
            merged_media['filters'] = {**config.get('media', {}).get('filters', {}), **(posted['filters'] or {})}
        preview_config['media'] = merged_media

        tautulli = TautulliAPI(config['tautulli']['url'], config['tautulli']['api_key'])
        media_filter = MediaFilter(preview_config, tautulli)

        process_movies = merged_media.get('process_movies', True)
        process_tv = merged_media.get('process_tv_shows', True)

        count = movies = shows = 0
        reclaimable = 0
        available_genres = set()

        for it in items:
            for g in it.get('genres') or []:
                name = _genre_name(g)
                if name:
                    available_genres.add(name)

            lt = it.get('library_type')
            if lt == 'movie' and not process_movies:
                continue
            if lt == 'show' and not process_tv:
                continue

            if media_filter.is_old_enough(
                it.get('last_played'), it.get('added_at'), it.get('play_count'), item_data=it
            ):
                count += 1
                reclaimable += int(it.get('file_size') or 0)
                if lt == 'movie':
                    movies += 1
                else:
                    shows += 1

        return {"success": True, "data": {
            "count": count,
            "movies": movies,
            "shows": shows,
            "reclaimable": reclaimable,
            "library_size": library_size,
            "available_genres": sorted(available_genres, key=str.lower),
        }}

    except Exception as e:
        logger.error(f"Error building preview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/candidates")
async def get_removal_candidates():
    """Get list of items that would be removed with current config."""
    try:
        config = load_config()
        analyzer = LibraryAnalyzer(config)

        # Build the catalog from Radarr/Sonarr (+ Tautulli watch history), then keep
        # only items old/unwatched enough to remove. Each carries its own *arr id.
        all_items, _ = _build_catalog(config)
        candidates = []
        for item in all_items:
            if analyzer.filter.is_old_enough(
                item.get('last_played'),
                item.get('added_at'),
                item.get('play_count'),
                item_data=item
            ):
                candidates.append({
                    'library_type': item['library_type'],
                    'library_name': item['library_name'],
                    'id': item['id'],
                    'arr_id': item['arr_id'],
                    'title': item['title'],
                    'year': item.get('year'),
                    'last_played': item.get('last_played'),
                    'added_at': item.get('added_at'),
                    'play_count': item.get('play_count', 0),
                    'file_size': item.get('file_size', 0),
                    'poster': item.get('poster'),
                })

        return {"success": True, "data": candidates}

    except Exception as e:
        logger.error(f"Error getting candidates: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# One removal at a time, run in a background thread so the UI can show progress
# (a few hundred items, each captured then deleted, can take a couple of minutes).
_REMOVAL_LOCK = threading.Lock()
_REMOVAL_JOB: Dict[str, Any] = {}


def _start_removal_job(remover: MediaRemover, ids: List[str]) -> Optional[str]:
    with _REMOVAL_LOCK:
        if _REMOVAL_JOB.get('running'):
            return None
        job_id = f"{int(time.time() * 1000):x}"
        _REMOVAL_JOB.clear()
        _REMOVAL_JOB.update(id=job_id, running=True, total=len(ids), done=0, current=None,
                            failed=[], success=None, dry_run=bool(remover.config.get('safety', {}).get('dry_run')),
                            started=time.time(), finished=None)

    def progress(handle: str, ok: Optional[bool]) -> None:
        with _REMOVAL_LOCK:
            if ok is None:
                _REMOVAL_JOB['current'] = handle
            else:
                _REMOVAL_JOB['done'] += 1
                if not ok:
                    _REMOVAL_JOB['failed'].append(handle)

    def run() -> None:
        success = False
        try:
            try:
                history = get_history()
            except Exception as e:
                logger.error(f"Removal history unavailable, continuing without it: {e}")
                history = None
            success = remover.remove_by_ids(ids, history=history, watch=watch_for, progress=progress)
        except Exception as e:
            logger.error(f"Removal job failed: {e}", exc_info=True)
        finally:
            _CATALOG_CACHE['items'] = None   # the preview must not show what was just removed
            with _REMOVAL_LOCK:
                _REMOVAL_JOB.update(running=False, current=None, success=success, finished=time.time())

    threading.Thread(target=run, name=f"removal-{job_id}", daemon=True).start()
    return job_id


@app.get("/api/remove/status")
def removal_status():
    """Progress of the current (or last) removal job."""
    with _REMOVAL_LOCK:
        return {"success": True, "data": dict(_REMOVAL_JOB, failed=list(_REMOVAL_JOB.get('failed', [])))}


@app.post("/api/remove")
async def remove_media(request: RemovalRequest):
    """Execute media removal (by Radarr/Sonarr id, with legacy rating_key support)."""
    try:
        config = load_config()

        if not request.confirm:
            raise HTTPException(status_code=400, detail="Confirmation required")

        # Prefer explicit *arr ids; fall back to legacy Tautulli rating_keys.
        targets = request.ids or request.rating_keys or []
        use_ids = bool(request.ids)

        # SAFETY: never allow an unintentional mass removal - require explicit items.
        if not targets:
            raise HTTPException(
                status_code=400,
                detail="No items specified for removal. Please select specific items to remove."
            )

        # SAFETY: enforce the maximum items per operation.
        max_allowed = config['safety'].get('max_items_per_run', 200)
        if len(targets) > max_allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot remove {len(targets)} items at once. Maximum allowed: {max_allowed}"
            )

        logger.info(f"Remove request for {len(targets)} items ({'ids' if use_ids else 'rating_keys'})")

        # Respect the configured dry_run; only skip the interactive CLI prompt.
        config['safety']['require_confirmation'] = False

        remover = MediaRemover(config)
        if use_ids:
            # Runs in the background; the UI follows it via /api/remove/status.
            job = _start_removal_job(remover, request.ids)
            if job is None:
                raise HTTPException(status_code=409, detail="A removal is already running")
            return {"success": True, "job": job}

        success = remover.remove_by_rating_keys(request.rating_keys)
        return {
            "success": success,
            "message": "Removal process completed" if success else "Removal process failed"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during removal: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/history")
def list_history():
    """Everything MediaCleanup has removed, grouped into batches (no secrets)."""
    try:
        store = get_history()
        removals = store.list_removals()
        if any(r.get('readd_status') in IN_FLIGHT for r in removals):
            config = load_config()
            radarr = sonarr = None
            if config.get('radarr', {}).get('enabled'):
                radarr = RadarrAPI(config['radarr']['url'], config['radarr']['api_key'])
            if config.get('sonarr', {}).get('enabled'):
                sonarr = SonarrAPI(config['sonarr']['url'], config['sonarr']['api_key'])
            refresh_in_flight(store, removals, radarr, sonarr)
        return {"success": True, "data": {"removals": removals, "batches": store.list_batches()}}
    except Exception as e:
        logger.error(f"Error reading removal history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/history/summary")
def history_summary():
    """Totals for the dashboard: what's currently removed (not restored)."""
    rows = [r for r in get_history().list_removals() if r.get('readd_status') != 'restored']
    return {"success": True, "data": {
        "items": len(rows),
        "bytes": sum(r.get('size_bytes') or 0 for r in rows),
        "last_removed_at": max((r['removed_at'] for r in rows), default=None),
    }}


@app.get("/api/history/export.csv")
def export_history():
    """CSV export of the removal history (tracker page + hash, never download URLs)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['removed_at', 'type', 'title', 'year', 'tmdb_id', 'tvdb_id', 'imdb_id', 'size_bytes',
                'quality', 'path', 'last_played', 'play_count', 'releases', 'tracker_pages', 'info_hashes',
                'readd_status'])
    for r in get_history().list_removals():
        rels = r['releases']
        w.writerow([r['removed_at'], r['media_type'], r['title'], r['year'], r['tmdb_id'], r['tvdb_id'],
                    r['imdb_id'], r['size_bytes'], r['quality'], r['path'],
                    datetime.fromtimestamp(r['last_played']).date().isoformat() if r['last_played'] else '',
                    r['play_count'],
                    ' | '.join(x.get('title') or '' for x in rels),
                    ' | '.join(x.get('info_url') or '' for x in rels),
                    ' | '.join(x.get('info_hash') or '' for x in rels),
                    r['readd_status'] or ''])
    return Response(buf.getvalue(), media_type='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="mediacleanup-removed.csv"'})


@app.get("/api/history/{removal_id}/status")
def readd_status(removal_id: int):
    rec = get_history().get_removal(removal_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Not found")
    return {"success": True, "data": {k: rec.get(k) for k in
                                      ('id', 'readd_status', 'readd_detail', 'readd_at', 'new_arr_id', 'restored_at')}}


@app.post("/api/history/{removal_id}/readd")
def readd_item(removal_id: int):
    """Put a removed item back: re-create it in Radarr/Sonarr and hand back the
    original release (falls back to a normal search). Runs in the background."""
    store = get_history()
    rec = store.get_removal(removal_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Not found")
    config = load_config()
    radarr = sonarr = None
    if config.get('radarr', {}).get('enabled'):
        radarr = RadarrAPI(config['radarr']['url'], config['radarr']['api_key'])
    if config.get('sonarr', {}).get('enabled'):
        sonarr = SonarrAPI(config['sonarr']['url'], config['sonarr']['api_key'])
    if not start_readd(store, removal_id, radarr, sonarr):
        raise HTTPException(status_code=409, detail="A re-add for this item is already running")
    _CATALOG_CACHE['items'] = None
    return {"success": True, "message": f"Re-adding {rec['title']}"}


@app.get("/api/libraries")
async def get_libraries():
    """Get library statistics."""
    try:
        config = load_config()
        tautulli = TautulliAPI(config['tautulli']['url'], config['tautulli']['api_key'])
        
        libraries = tautulli.get_libraries()
        library_stats = []
        
        for library in libraries:
            library_type = library.get('section_type', 'Unknown')
            library_id = library.get('section_id')
            library_name = library.get('section_name', 'Unknown')
            
            # Get item count
            media_response = tautulli.get_library_media_info(library_id, length=1)
            if isinstance(media_response, dict):
                count = media_response.get('recordsTotal', 0)
            else:
                count = len(media_response)
            
            library_stats.append({
                'id': library_id,
                'name': library_name,
                'type': library_type,
                'count': count
            })
        
        return {"success": True, "data": library_stats}
        
    except Exception as e:
        logger.error(f"Error getting libraries: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/test/tautulli")
async def test_tautulli():
    """Test Tautulli connection using saved config."""
    try:
        config = load_config()
        tautulli = TautulliAPI(config['tautulli']['url'], config['tautulli']['api_key'])
        libraries = tautulli.get_libraries()
        count = len(libraries)
        return {"success": True, "message": f"Connected \u2014 {count} librar{'ies' if count != 1 else 'y'} found"}
    except Exception as e:
        logger.warning(f"Tautulli connection test failed: {e}")
        return {"success": False, "message": str(e)}


@app.get("/api/test/radarr")
async def test_radarr():
    """Test Radarr connection using saved config."""
    try:
        config = load_config()
        radarr = RadarrAPI(config['radarr']['url'], config['radarr']['api_key'])
        status = radarr._make_request('system/status')
        version = status.get('version', 'unknown') if status else 'unknown'
        return {"success": True, "message": f"Connected \u2014 Radarr v{version}"}
    except Exception as e:
        logger.warning(f"Radarr connection test failed: {e}")
        return {"success": False, "message": str(e)}


@app.get("/api/test/sonarr")
async def test_sonarr():
    """Test Sonarr connection using saved config."""
    try:
        config = load_config()
        sonarr = SonarrAPI(config['sonarr']['url'], config['sonarr']['api_key'])
        status = sonarr._make_request('system/status')
        version = status.get('version', 'unknown') if status else 'unknown'
        return {"success": True, "message": f"Connected \u2014 Sonarr v{version}"}
    except Exception as e:
        logger.warning(f"Sonarr connection test failed: {e}")
        return {"success": False, "message": str(e)}


# Helper functions

def _calculate_watch_status(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate watch status distribution."""
    movies = [i for i in items if i.get('library_type') == 'movie']
    shows = [i for i in items if i.get('library_type') == 'show']
    
    def calc_stats(media_items):
        if not media_items:
            return None
        return {
            'never_watched': sum(1 for i in media_items if not i.get('play_count')),
            'lightly_watched': sum(1 for i in media_items if i.get('play_count') and 1 <= i.get('play_count') <= 5),
            'moderately_watched': sum(1 for i in media_items if i.get('play_count') and 6 <= i.get('play_count') <= 20),
            'heavily_watched': sum(1 for i in media_items if i.get('play_count') and i.get('play_count') > 20),
            'total': len(media_items)
        }
    
    return {
        'movies': calc_stats(movies),
        'shows': calc_stats(shows)
    }


def _calculate_age_distribution(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """Calculate age distribution of unwatched content."""
    from datetime import datetime
    
    now = datetime.now()
    unwatched_items = [i for i in items if not i.get('play_count')]
    
    buckets = {
        '0-6_months': 0,
        '6-12_months': 0,
        '1-2_years': 0,
        '2-3_years': 0,
        '3+_years': 0
    }
    
    for item in unwatched_items:
        try:
            added_at = datetime.fromtimestamp(int(item.get('added_at', 0)))
            days_old = (now - added_at).days
            
            if days_old < 180:
                buckets['0-6_months'] += 1
            elif days_old < 365:
                buckets['6-12_months'] += 1
            elif days_old < 730:
                buckets['1-2_years'] += 1
            elif days_old < 1095:
                buckets['2-3_years'] += 1
            else:
                buckets['3+_years'] += 1
        except (ValueError, TypeError):
            continue
    
    return buckets


def _calculate_config_impact(items: List[Dict[str, Any]], analyzer: LibraryAnalyzer,
                             radarr_keys: set = None, sonarr_keys: set = None,
                             library_size: int = None) -> Dict[str, Any]:
    """Calculate impact of current configuration.

    When Radarr/Sonarr title-key sets are provided, items no longer present in
    Radarr/Sonarr (stale Tautulli entries) are excluded - mirroring the removal
    candidates endpoint so the dashboard numbers match the Removal page.
    """
    candidates = []
    filtered_by_age = 0
    filtered_by_protection = 0
    total_library_size = 0
    potential_savings = 0

    for item in items:
        library_type = item.get('library_type')
        title = item.get('title', '') or ''

        # Skip stale items not present in Radarr/Sonarr (same cross-reference as
        # /api/candidates). Only applies when the relevant key set is populated.
        if library_type == 'movie' and radarr_keys and not (_sonarr_radarr_title_keys(title) & radarr_keys):
            continue
        if library_type == 'show' and sonarr_keys and not (_sonarr_radarr_title_keys(title) & sonarr_keys):
            continue

        # Track total (real, non-stale) library size
        file_size = item.get('file_size')
        if file_size and file_size != 'N/A':
            try:
                total_library_size += int(file_size)
            except (ValueError, TypeError):
                pass

        if analyzer.filter.is_old_enough(
            item.get('last_played'),
            item.get('added_at'),
            item.get('play_count'),
            item_data=item
        ):
            candidates.append(item)
            # Track potential space savings
            if file_size and file_size != 'N/A':
                try:
                    potential_savings += int(file_size)
                except (ValueError, TypeError):
                    pass
        else:
            if not analyzer.filter.meets_age_requirements(
                item.get('last_played'),
                item.get('added_at'),
                item.get('play_count')
            ):
                filtered_by_age += 1
            else:
                filtered_by_protection += 1
    
    movies_to_remove = sum(1 for i in candidates if i.get('library_type') == 'movie')
    shows_to_remove = sum(1 for i in candidates if i.get('library_type') == 'show')

    # Largest actual removal candidate (the single biggest space win).
    def _size(i):
        try:
            return int(i.get('file_size') or 0)
        except (ValueError, TypeError):
            return 0

    largest_candidate = None
    if candidates:
        top = max(candidates, key=_size)
        largest_candidate = {
            'title': top.get('title', 'Unknown'),
            'year': top.get('year'),
            'library_type': top.get('library_type'),
            'file_size': _size(top),
        }

    return {
        'would_remove': len(candidates),
        'movies_to_remove': movies_to_remove,
        'shows_to_remove': shows_to_remove,
        'filtered_by_age': filtered_by_age,
        'filtered_by_protection': filtered_by_protection,
        'total': len(items),
        'total_library_size': library_size if library_size is not None else total_library_size,
        'potential_savings': potential_savings,
        'largest_candidate': largest_candidate
    }


def _get_top_lists(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Get top lists of interesting items."""
    from datetime import datetime
    
    now = datetime.now()
    unwatched_items = [i for i in items if not i.get('play_count')]
    
    # Top items by file size (largest space consumers)
    items_with_size = []
    for item in items:
        file_size = item.get('file_size')
        if file_size and file_size != 'N/A':
            try:
                size_bytes = int(file_size)
                play_count = item.get('play_count') or 0  # Handle None as 0
                items_with_size.append({
                    'title': item.get('title', 'Unknown'),
                    'year': item.get('year', 'N/A'),
                    'library_type': item.get('library_type', 'unknown'),
                    'library_name': item.get('library_name', 'Unknown'),
                    'file_size': size_bytes,
                    'play_count': play_count,
                    'last_played': item.get('last_played'),
                    'rating_key': item.get('rating_key'),
                    'poster': item.get('poster'),
                })
            except (ValueError, TypeError):
                continue
    
    # Sort by size
    items_with_size.sort(key=lambda x: x['file_size'], reverse=True)
    
    # Largest items overall
    largest_items = items_with_size[:20]
    
    # Largest unwatched (removal candidates by size)
    largest_unwatched = [x for x in items_with_size if x.get('play_count', 0) == 0][:20]
    
    # Stale content - watched but not in past year
    one_year_ago = int((now - timedelta(days=365)).timestamp())
    stale_items = []
    for item in items_with_size:
        last_played = item.get('last_played')
        if last_played and last_played < one_year_ago and item['play_count'] > 0:
            try:
                last_watched_date = datetime.fromtimestamp(last_played)
                days_since_watched = (now - last_watched_date).days
                stale_items.append({
                    **item,
                    'days_since_watched': days_since_watched
                })
            except (ValueError, TypeError):
                continue
    
    stale_items.sort(key=lambda x: x['days_since_watched'], reverse=True)
    
    # Calculate age for oldest unwatched
    items_with_age = []
    for item in unwatched_items:
        try:
            added_at = datetime.fromtimestamp(int(item.get('added_at', 0)))
            days_old = (now - added_at).days
            file_size = item.get('file_size')
            size_bytes = int(file_size) if file_size and file_size != 'N/A' else 0
            
            items_with_age.append({
                'title': item.get('title', 'Unknown'),
                'year': item.get('year', 'N/A'),
                'library_type': item.get('library_type', 'unknown'),
                'library_name': item.get('library_name', 'Unknown'),
                'days_old': days_old,
                'added_at': item.get('added_at'),
                'file_size': size_bytes,
                'rating_key': item.get('rating_key'),
                'poster': item.get('poster'),
            })
        except (ValueError, TypeError):
            continue
    
    items_with_age.sort(key=lambda x: x['days_old'], reverse=True)
    
    return {
        'largest_items': largest_items,
        'largest_unwatched': largest_unwatched,
        'stale_content': stale_items[:20],
        'oldest_unwatched': items_with_age[:20],
        'recently_added_unwatched': [x for x in items_with_age if x['days_old'] < 180][:10]
    }


def _test_thresholds(items: List[Dict[str, Any]], config: Dict[str, Any], tautulli: TautulliAPI) -> List[Dict[str, Any]]:
    """Test different threshold scenarios."""
    from backend.core.filters import MediaFilter
    
    test_scenarios = [
        (180, 200),
        (365, 400),
        (730, 750),
        (1095, 1100),
    ]
    
    results = []
    original_unwatched = config['media']['days_unwatched']
    original_added = config['media']['min_days_since_added']
    
    for days_unwatched, min_days_added in test_scenarios:
        config['media']['days_unwatched'] = days_unwatched
        config['media']['min_days_since_added'] = min_days_added
        
        temp_filter = MediaFilter(config, tautulli)
        
        would_remove = sum(
            1 for item in items
            if temp_filter.is_old_enough(
                item.get('last_played'),
                item.get('added_at'),
                item.get('play_count'),
                item_data=item
            )
        )
        
        results.append({
            'days_unwatched': days_unwatched,
            'min_days_since_added': min_days_added,
            'would_remove': would_remove
        })
    
    # Restore original config
    config['media']['days_unwatched'] = original_unwatched
    config['media']['min_days_since_added'] = original_added
    
    return results
