"""
Sonarr API client for TV show management.
"""
import requests
import logging
from typing import Dict, List, Any, Optional


class SonarrAPI:
    """API client for Sonarr TV show management."""
    
    def __init__(self, url: str, api_key: str):
        """Initialize Sonarr API client.
        
        Args:
            url: Sonarr server URL
            api_key: Sonarr API key
        """
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({'X-Api-Key': api_key})

    def _make_request(self, endpoint: str, method: str = 'GET', timeout: int = 30, **kwargs) -> Any:
        """Make a request to the Sonarr API.
        
        Args:
            endpoint: API endpoint
            method: HTTP method
            **kwargs: Additional request parameters
            
        Returns:
            API response data
            
        Raises:
            requests.RequestException: If API request fails
        """
        url = f"{self.url}/api/v3/{endpoint}"
        
        try:
            response = self.session.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            
            if response.content:
                return response.json()
            return None
        except requests.exceptions.RequestException as e:
            logging.error(f"Sonarr API request failed: {e}")
            raise

    def get_series(self) -> List[Dict[str, Any]]:
        """Get all TV series from Sonarr.
        
        Returns:
            List of TV series
        """
        return self._make_request('series')

    def get_series_by_id(self, series_id: int) -> Dict[str, Any]:
        """Get a specific TV series by ID.
        
        Args:
            series_id: Series ID
            
        Returns:
            Series details
        """
        return self._make_request(f'series/{series_id}')

    def delete_series(self, series_id: int, delete_files: bool = True, add_exclusion: bool = False) -> bool:
        """Delete a TV series from Sonarr.
        
        Args:
            series_id: Series ID to delete
            delete_files: Whether to delete files from disk
            add_exclusion: Whether to add to exclusion list
            
        Returns:
            True if successful
        """
        params = {
            'deleteFiles': delete_files,
            'addImportExclusion': add_exclusion
        }
        
        try:
            self._make_request(f'series/{series_id}', method='DELETE', params=params)
            return True
        except requests.exceptions.RequestException:
            return False

    def search_series_by_tvdb_id(self, tvdb_id: int) -> Optional[Dict[str, Any]]:
        """Search for a TV series by TVDB ID.
        
        Args:
            tvdb_id: TVDB ID to search for
            
        Returns:
            Series details if found, None otherwise
        """
        series_list = self.get_series()
        for series in series_list:
            if series.get('tvdbId') == tvdb_id:
                return series
        return None

    # --- Removal history / re-add support ---

    def get_series_history(self, series_id: int) -> List[Dict[str, Any]]:
        """All history events for one series, with episode details attached."""
        return self._make_request('history/series',
                                  params={'seriesId': series_id, 'includeEpisode': 'true'}) or []

    def find_series_by_tvdb(self, tvdb_id: int) -> Optional[Dict[str, Any]]:
        """The library series with this TVDB id, or None if it isn't in Sonarr."""
        found = self._make_request('series', params={'tvdbId': tvdb_id}) or []
        return found[0] if found else None

    def lookup_series_by_tvdb(self, tvdb_id: int) -> Optional[Dict[str, Any]]:
        """Metadata lookup used to build an add-series payload."""
        found = self._make_request('series/lookup', params={'term': f'tvdb:{tvdb_id}'}) or []
        return found[0] if found else None

    def add_series(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._make_request('series', method='POST', json=payload)

    def push_release(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Hand Sonarr a specific release; Sonarr decides on it and, if approved,
        sends it to its download client."""
        result = self._make_request('release/push', method='POST', json=payload, timeout=120)
        return result if isinstance(result, list) else [result] if result else []

    def search_releases(self, series_id: int, season_number: int) -> List[Dict[str, Any]]:
        """Interactive season search across Sonarr's indexers (slow)."""
        return self._make_request('release', params={'seriesId': series_id, 'seasonNumber': season_number},
                                  timeout=300) or []

    def grab_release(self, guid: str, indexer_id: int) -> Any:
        """Grab a result from a recent interactive search."""
        return self._make_request('release', method='POST',
                                  json={'guid': guid, 'indexerId': indexer_id}, timeout=120)

    def search_seasons(self, series_id: int, season_numbers: List[int]) -> None:
        """Queue Sonarr's normal automatic search, per season (or whole series)."""
        if not season_numbers:
            self._make_request('command', method='POST', json={'name': 'SeriesSearch', 'seriesId': series_id})
            return
        for n in season_numbers:
            self._make_request('command', method='POST',
                               json={'name': 'SeasonSearch', 'seriesId': series_id, 'seasonNumber': n})

    def importable_files(self, download_id: str, series_id: int) -> List[Dict[str, Any]]:
        """Files Sonarr could import from a download still in the download client.
        NB: for an unknown download id Sonarr lists the series folder instead, so
        callers must check each file's downloadId."""
        return self._make_request('manualimport', params={
            'downloadId': download_id, 'seriesId': series_id, 'filterExistingFiles': 'true'}) or []

    def import_files(self, files: List[Dict[str, Any]]) -> Any:
        return self._make_request('command', method='POST',
                                  json={'name': 'ManualImport', 'files': files, 'importMode': 'auto'})
