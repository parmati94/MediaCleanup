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

    def _make_request(self, endpoint: str, method: str = 'GET', **kwargs) -> Any:
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
            response = self.session.request(method, url, timeout=30, **kwargs)
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
