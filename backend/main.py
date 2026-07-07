"""
Media Cleanup UI - FastAPI Backend
"""
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yaml

from backend.core.analyzer import LibraryAnalyzer
from backend.core.media_remover import MediaRemover
from backend.api.tautulli import TautulliAPI
from backend.api.radarr import RadarrAPI
from backend.api.sonarr import SonarrAPI
from backend.utils.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

# Config file path
CONFIG_PATH = Path("/app/data/config.yaml")


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
            'require_zero_play_count': True,
            'filters': {
                'min_rating_to_keep': 0,
                'min_audience_rating_to_keep': 0,
                'protect_classics_before_year': 1995,
                'protect_recent_after_year': 0,
                'min_file_size_to_keep': 0,
                'protected_keywords': []
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
            'dry_run': True,
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
    rating_keys: Optional[List[str]] = None


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
        
        # Collect analysis data
        libraries = analyzer.tautulli.get_libraries()
        all_items = []
        
        for library in libraries:
            library_type = library.get('section_type', 'Unknown')
            library_id = library.get('section_id')
            
            # Skip based on configuration
            if library_type == 'movie' and not config['media'].get('process_movies', True):
                continue
            if library_type == 'show' and not config['media'].get('process_tv_shows', True):
                continue
            
            media_response = analyzer.tautulli.get_library_media_info(library_id, length=10000, refresh=request.refresh)
            media_items = media_response.get('data', []) if isinstance(media_response, dict) else media_response

            for item in media_items:
                item['library_type'] = library_type
                item['library_name'] = library.get('section_name', 'Unknown')

            all_items.extend(media_items)
        
        # Overlay authoritative on-disk sizes from Radarr/Sonarr (Tautulli's
        # media-info sizes blank out on refresh; *arr sizes never go stale).
        movie_sizes, show_sizes = _load_arr_sizes(config)
        _overlay_file_sizes(all_items, movie_sizes, show_sizes)
        # Same cross-reference keys the candidates endpoint uses, so the
        # dashboard impact numbers match the Removal page.
        radarr_keys = set(movie_sizes['by_key'].keys())
        sonarr_keys = set(show_sizes['by_key'].keys())
        # True managed-library size straight from Radarr/Sonarr (authoritative on-disk
        # totals), respecting which library types are enabled. Tautulli's cached
        # media-info table can lag behind new media, so we don't total from it.
        library_size = 0
        if config['media'].get('process_movies', True):
            library_size += movie_sizes.get('total', 0)
        if config['media'].get('process_tv_shows', True):
            library_size += show_sizes.get('total', 0)

        # Calculate statistics
        result = {
            'total_items': len(all_items),
            'watch_status': _calculate_watch_status(all_items),
            'age_distribution': _calculate_age_distribution(all_items),
            'current_config_impact': _calculate_config_impact(all_items, analyzer, radarr_keys, sonarr_keys, library_size),
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


@app.get("/api/candidates")
async def get_removal_candidates():
    """Get list of items that would be removed with current config."""
    try:
        config = load_config()
        analyzer = LibraryAnalyzer(config)

        # Load exact Radarr/Sonarr on-disk sizes once. The size-map key set
        # doubles as the clean-title lookup used to cross-reference Tautulli
        # candidates so stale items (removed from Plex/Radarr/Sonarr but still
        # in Tautulli history) are excluded before the user ever sees them.
        movie_sizes, show_sizes = _load_arr_sizes(config)
        radarr_clean_titles: set = set(movie_sizes['by_key'].keys())
        sonarr_clean_titles: set = set(show_sizes['by_key'].keys())

        libraries = analyzer.tautulli.get_libraries()
        candidates = []
        stale_count = 0

        for library in libraries:
            library_type = library.get('section_type', 'Unknown')
            library_id = library.get('section_id')
            library_name = library.get('section_name', 'Unknown')

            if library_type == 'movie' and not config['media'].get('process_movies', True):
                continue
            if library_type == 'show' and not config['media'].get('process_tv_shows', True):
                continue

            media_response = analyzer.tautulli.get_library_media_info(library_id, length=10000)
            media_items = media_response.get('data', []) if isinstance(media_response, dict) else media_response

            for item in media_items:
                if not analyzer.filter.is_old_enough(
                    item.get('last_played'),
                    item.get('added_at'),
                    item.get('play_count'),
                    item_data=item
                ):
                    continue

                # Cross-reference: skip items no longer present in Radarr/Sonarr.
                # Try both with-year and without-year variants of the Tautulli title — Tautulli
                # sometimes appends a disambiguation year that Sonarr may or may not include.
                title = item.get('title', '')
                tautulli_keys = _sonarr_radarr_title_keys(title)  # no authoritative cleanTitle here
                if library_type == 'movie' and radarr_clean_titles and not tautulli_keys & radarr_clean_titles:
                    logger.debug(f"Cross-ref filtered movie: {title!r} -> keys={tautulli_keys!r}")
                    stale_count += 1
                    continue
                if library_type == 'show' and sonarr_clean_titles and not tautulli_keys & sonarr_clean_titles:
                    logger.debug(f"Cross-ref filtered show: {title!r} -> keys={tautulli_keys!r}")
                    stale_count += 1
                    continue

                candidates.append({
                    'library_type': library_type,
                    'library_name': library_name,
                    'title': title,
                    'year': item.get('year'),
                    'last_played': item.get('last_played'),
                    'added_at': item.get('added_at'),
                    'play_count': item.get('play_count', 0),
                    'rating_key': item.get('rating_key'),
                    'file_size': item.get('file_size', 0),
                    'total_duration': item.get('total_duration', 0)
                })

        # Replace Tautulli's (possibly-blank) sizes with exact Radarr/Sonarr sizes.
        _overlay_file_sizes(candidates, movie_sizes, show_sizes)

        if stale_count:
            logger.info(f"Cross-reference filtered {stale_count} stale Tautulli item(s) not found in Radarr/Sonarr")

        return {"success": True, "data": candidates}

    except Exception as e:
        logger.error(f"Error getting candidates: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/remove")
async def remove_media(request: RemovalRequest):
    """Execute media removal."""
    try:
        config = load_config()
        
        if not request.confirm:
            raise HTTPException(status_code=400, detail="Confirmation required")
        
        # SAFETY: Require explicit rating_keys - never allow unintentional mass removal
        if not request.rating_keys or len(request.rating_keys) == 0:
            raise HTTPException(
                status_code=400, 
                detail="No items specified for removal. Please select specific items to remove."
            )
        
        # SAFETY: Enforce maximum items per operation
        max_allowed = config['safety'].get('max_items_per_run', 200)
        if len(request.rating_keys) > max_allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot remove {len(request.rating_keys)} items at once. Maximum allowed: {max_allowed}"
            )
        
        logger.info(f"Remove request for {len(request.rating_keys)} specific items")
        
        # Don't override dry_run - respect the config file setting
        # Only disable confirmation prompts since user already confirmed via UI
        config['safety']['require_confirmation'] = False
        
        remover = MediaRemover(config)
        success = remover.remove_by_rating_keys(request.rating_keys)
        
        # Trigger Tautulli to refresh library data from Plex
        # This picks up removed shows without recalculating file sizes
        # (file size calculation is a separate opt-in background job)
        if success:
            try:
                remover.tautulli.refresh_libraries()
                logger.info("Tautulli libraries refreshed to pick up removed media")
            except Exception as e:
                logger.warning(f"Failed to refresh Tautulli libraries: {e}")
        
        return {
            "success": success,
            "message": "Removal process completed" if success else "Removal process failed"
        }
        
    except Exception as e:
        logger.error(f"Error during removal: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
                    'rating_key': item.get('rating_key')
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
                'rating_key': item.get('rating_key')
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
