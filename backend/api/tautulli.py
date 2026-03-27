"""
Tautulli API client for media library information.
"""
import requests
import logging
from typing import Dict, List, Any, Optional


class TautulliAPI:
    """API client for Tautulli media server statistics."""
    
    def __init__(self, url: str, api_key: str):
        """Initialize Tautulli API client.
        
        Args:
            url: Tautulli server URL
            api_key: Tautulli API key
        """
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()

    def _make_request(self, cmd: str, **params) -> Dict[str, Any]:
        """Make a request to the Tautulli API.
        
        Args:
            cmd: API command
            **params: Additional parameters
            
        Returns:
            API response data
            
        Raises:
            requests.RequestException: If API request fails
        """
        url = f"{self.url}/api/v2"
        params.update({
            'apikey': self.api_key,
            'cmd': cmd
        })
        
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if data.get('response', {}).get('result') != 'success':
                raise requests.RequestException(f"API returned error: {data}")
            
            return data.get('response', {}).get('data', {})
        except requests.exceptions.RequestException as e:
            logging.error(f"Tautulli API request failed: {e}")
            raise

    def get_library_media_info(self, section_id: int, length: int = 1000, start: int = 0) -> List[Dict[str, Any]]:
        """Get all media items from a library section.
        
        Args:
            section_id: Library section ID
            length: Maximum number of items to retrieve
            start: Starting offset for pagination
            
        Returns:
            List of media items
        """
        # Get the first batch
        response = self._make_request('get_library_media_info', 
                                    section_id=section_id, 
                                    length=length, 
                                    start=start)
        
        # If response is a dict with pagination info, handle it
        if isinstance(response, dict) and 'data' in response:
            records_total = response.get('recordsTotal', 0)
            records_filtered = response.get('recordsFiltered', 0)
            data_length = len(response.get('data', []))
            
            logging.debug(f"API pagination info - Total: {records_total}, Filtered: {records_filtered}, Returned: {data_length}")
            
            # Check if we got all the data or if pagination is needed
            if data_length < records_total and data_length == length:
                logging.warning(f"Possible pagination needed: received {data_length} items but {records_total} total exist")
            
            return response.get('data', [])
        else:
            # Direct list response
            return response if isinstance(response, list) else []
    
    def get_libraries(self) -> List[Dict[str, Any]]:
        """Get all library sections.
        
        Returns:
            List of library sections
        """
        return self._make_request('get_libraries')
    
    def get_item_watch_time_stats(self, rating_key: str) -> Dict[str, Any]:
        """Get watch statistics for a specific item.
        
        Args:
            rating_key: Item's rating key
            
        Returns:
            Watch time statistics
        """
        return self._make_request('get_item_watch_time_stats', rating_key=rating_key)
    
    def get_metadata(self, rating_key: str) -> Dict[str, Any]:
        """Get detailed metadata for a specific item.
        
        Args:
            rating_key: Item's rating key
            
        Returns:
            Detailed metadata
        """
        return self._make_request('get_metadata', rating_key=rating_key)
    
    def refresh_libraries(self) -> bool:
        """Refresh Tautulli library data.
        
        This triggers Tautulli to update its library information from Plex,
        which is useful after removing media to update the UI.
        
        Returns:
            True if successful
        """
        try:
            self._make_request('refresh_libraries_list')
            logging.info("Tautulli libraries refreshed")
            return True
        except Exception as e:
            logging.error(f"Failed to refresh Tautulli libraries: {e}")
            return False
