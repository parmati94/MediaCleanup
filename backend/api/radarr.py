"""
Radarr API client for movie management.
"""
import requests
import logging
from typing import Dict, List, Any, Optional


class RadarrAPI:
    """API client for Radarr movie management."""
    
    def __init__(self, url: str, api_key: str):
        """Initialize Radarr API client.
        
        Args:
            url: Radarr server URL
            api_key: Radarr API key
        """
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({'X-Api-Key': api_key})

    def _make_request(self, endpoint: str, method: str = 'GET', **kwargs) -> Any:
        """Make a request to the Radarr API.
        
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
            logging.error(f"Radarr API request failed: {e}")
            raise

    def get_movies(self) -> List[Dict[str, Any]]:
        """Get all movies from Radarr.
        
        Returns:
            List of movies
        """
        return self._make_request('movie')

    def get_movie(self, movie_id: int) -> Dict[str, Any]:
        """Get a specific movie by ID.
        
        Args:
            movie_id: Movie ID
            
        Returns:
            Movie details
        """
        return self._make_request(f'movie/{movie_id}')

    def delete_movie(self, movie_id: int, delete_files: bool = True, add_exclusion: bool = False) -> bool:
        """Delete a movie from Radarr.
        
        Args:
            movie_id: Movie ID to delete
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
            self._make_request(f'movie/{movie_id}', method='DELETE', params=params)
            return True
        except requests.exceptions.RequestException:
            return False

    def search_movies_by_tmdb_id(self, tmdb_id: int) -> Optional[Dict[str, Any]]:
        """Search for a movie by TMDB ID.
        
        Args:
            tmdb_id: TMDB ID to search for
            
        Returns:
            Movie details if found, None otherwise
        """
        movies = self.get_movies()
        for movie in movies:
            if movie.get('tmdbId') == tmdb_id:
                return movie
        return None
