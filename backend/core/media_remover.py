"""
Main media removal orchestration logic.
"""
import logging
from typing import Dict, List, Any, Optional

from backend.api.tautulli import TautulliAPI
from backend.api.radarr import RadarrAPI
from backend.api.sonarr import SonarrAPI
from backend.core.filters import MediaFilter


class MediaRemover:
    """Main class for orchestrating media removal operations."""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize MediaRemover with configuration.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        
        # Initialize API clients
        self.tautulli = TautulliAPI(
            config['tautulli']['url'],
            config['tautulli']['api_key']
        )
        
        self.radarr = None
        if config.get('radarr', {}).get('enabled', False):
            self.radarr = RadarrAPI(
                config['radarr']['url'],
                config['radarr']['api_key']
            )
        
        self.sonarr = None
        if config.get('sonarr', {}).get('enabled', False):
            self.sonarr = SonarrAPI(
                config['sonarr']['url'],
                config['sonarr']['api_key']
            )
        
        # Initialize filter
        self.filter = MediaFilter(config, self.tautulli)

    @staticmethod
    def _format_file_size(size_bytes: Optional[int]) -> str:
        """Format file size in human-readable format.
        
        Args:
            size_bytes: Size in bytes
            
        Returns:
            Formatted size string
        """
        if size_bytes is None:
            return "Unknown"
        
        # Try to convert to int if it's a string
        try:
            if isinstance(size_bytes, str):
                size_bytes = int(size_bytes)
            elif not isinstance(size_bytes, (int, float)):
                return "Unknown"
        except (ValueError, TypeError):
            return "Unknown"
        
        if size_bytes == 0:
            return "0 B"
        
        # Convert to appropriate unit
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024.0:
                if unit == 'B':
                    return f"{size_bytes} {unit}"
                else:
                    return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} PB"

    def run(self) -> bool:
        """Run the media removal process.
        
        Returns:
            True if successful
        """
        logging.info("Starting media removal process...")
        
        # Check for dry run mode
        if self.config.get('safety', {}).get('dry_run', False):
            logging.info("DRY RUN MODE: No items will actually be removed")
        
        try:
            # Get all libraries from Tautulli
            libraries = self.tautulli.get_libraries()
            removal_candidates = []
            
            # Process each library
            for library in libraries:
                library_name = library.get('section_name', 'Unknown')
                library_type = library.get('section_type', 'Unknown')
                library_id = library.get('section_id')
                
                logging.info(f"Processing library: {library_name} (Type: {library_type})")
                
                # Skip based on configuration
                if library_type == 'movie' and not self.config['media'].get('process_movies', True):
                    logging.info(f"Skipping movie library '{library_name}' - movie processing disabled in config")
                    continue
                
                if library_type == 'show' and not self.config['media'].get('process_tv_shows', True):
                    logging.info(f"Skipping TV show library '{library_name}' - TV show processing disabled in config")
                    continue
                
                # Get media from this library
                candidates = self._process_library(library_id, library_name, library_type)
                removal_candidates.extend(candidates)
            
            # Limit the number of items to process
            max_items = self.config['safety']['max_items_per_run']
            if len(removal_candidates) > max_items:
                logging.warning(f"Found {len(removal_candidates)} candidates, limiting to {max_items}")
                removal_candidates = removal_candidates[:max_items]
            
            logging.info(f"Found {len(removal_candidates)} items for potential removal")
            
            if not removal_candidates:
                logging.info("No items found for removal")
                return True
            
            # Show confirmation if required
            if self.config.get('safety', {}).get('require_confirmation', True):
                self._show_removal_preview(removal_candidates)
                
                if not self.config.get('safety', {}).get('dry_run', False):
                    response = input(f"\nProceed with removing {len(removal_candidates)} items? (y/N): ")
                    if response.lower() != 'y':
                        logging.info("Removal cancelled by user")
                        return True
            
            # Remove items
            return self._remove_items(removal_candidates)
            
        except Exception as e:
            logging.error(f"Error during media removal process: {e}")
            return False

    def remove_by_rating_keys(self, rating_keys: List[str]) -> bool:
        """Remove specific items by their rating keys.
        
        Args:
            rating_keys: List of rating keys to remove
            
        Returns:
            True if successful
        """
        logging.info(f"Starting selective removal of {len(rating_keys)} items...")
        
        # Check for dry run mode
        if self.config.get('safety', {}).get('dry_run', False):
            logging.info("DRY RUN MODE: No items will actually be removed")
        
        try:
            removal_candidates = []
            
            # Fetch full details for each rating key 
            for rating_key in rating_keys:
                try:
                    # Get metadata for this item from Tautulli
                    metadata = self.tautulli.get_metadata(rating_key)
                    if metadata:
                        library_type = metadata.get('media_type', 'unknown')
                        if library_type == 'episode':
                            library_type = 'show'
                        
                        removal_candidates.append({
                            'library_type': library_type,
                            'title': metadata.get('title', ''),
                            'year': metadata.get('year'),
                            'last_played': metadata.get('last_played'),
                            'added_at': metadata.get('added_at'),
                            'rating_key': rating_key,
                            'play_count': metadata.get('play_count', 0),
                            'file_size': metadata.get('file_size')
                        })
                    else:
                        logging.warning(f"Could not fetch metadata for rating_key: {rating_key}")
                except Exception as e:
                    logging.error(f"Error fetching metadata for rating_key {rating_key}: {e}")
                    continue
            
            if not removal_candidates:
                logging.warning("No valid items found to remove")
                return False
            
            logging.info(f"Found {len(removal_candidates)} items to remove")
            
            # Remove items
            return self._remove_items(removal_candidates)
            
        except Exception as e:
            logging.error(f"Error during selective removal process: {e}")
            return False

    def _process_library(self, library_id: int, library_name: str, library_type: str) -> List[Dict[str, Any]]:
        """Process a single library and return removal candidates.
        
        Args:
            library_id: Library ID
            library_name: Library name
            library_type: Library type (movie/show)
            
        Returns:
            List of removal candidates
        """
        candidates = []
        
        try:
            media_response = self.tautulli.get_library_media_info(library_id, length=5000)
            media_items = media_response.get('data', []) if isinstance(media_response, dict) else media_response
            
            logging.info(f"Retrieved {len(media_items)} items from library '{library_name}'")
            
            # Track filtering statistics
            filtered_by_age = 0
            filtered_by_protection = 0
            candidates_found = 0
            
            for item in media_items:
                if self.filter.is_old_enough(
                    item.get('last_played'), 
                    item.get('added_at'),
                    item.get('play_count'),
                    item_data=item
                ):
                    file_size = item.get('file_size') or item.get('size') or item.get('total_size')
                    
                    candidates.append({
                        'library_type': library_type,
                        'title': item.get('title', ''),
                        'year': item.get('year'),
                        'last_played': item.get('last_played'),
                        'added_at': item.get('added_at'),
                        'rating_key': item.get('rating_key'),
                        'play_count': item.get('play_count', 0),
                        'file_size': file_size
                    })
                    candidates_found += 1
                else:
                    # Check why it was filtered out
                    if not self.filter.meets_age_requirements(item.get('last_played'), item.get('added_at'), item.get('play_count')):
                        filtered_by_age += 1
                    else:
                        filtered_by_protection += 1
            
            logging.info(f"Library '{library_name}' summary: {len(media_items)} total, {filtered_by_age} filtered by age/watch criteria, {filtered_by_protection} protected by filters, {candidates_found} candidates for removal")
            
        except Exception as e:
            logging.error(f"Error processing library '{library_name}': {e}")
        
        return candidates

    def _show_removal_preview(self, removal_candidates: List[Dict[str, Any]]) -> None:
        """Show preview of items to be removed.
        
        Args:
            removal_candidates: List of items to be removed
        """
        # Check if we're in dry run mode for the header
        dry_run_text = " (DRY RUN)" if self.config.get('safety', {}).get('dry_run', False) else ""
        print(f"\nItems to be removed{dry_run_text}:")
        
        for item in removal_candidates:
            play_count = item.get('play_count', 'Unknown')
            if play_count is None:
                play_count = 'None'
            
            # Format file size
            file_size_str = self._format_file_size(item.get('file_size'))
            
            print(f"  - {item['title']} ({item['year']}) [{item['library_type']}] - Play count: {play_count} - Size: {file_size_str}")

    def _remove_items(self, removal_candidates: List[Dict[str, Any]]) -> bool:
        """Remove the specified items.
        
        Args:
            removal_candidates: List of items to remove
            
        Returns:
            True if all removals successful
        """
        success_count = 0
        
        for item in removal_candidates:
            title = item['title']
            year = item['year']
            library_type = item['library_type']
            play_count = item.get('play_count')
            
            # Log what we're processing
            play_status = "Never watched" if (play_count is None or play_count == 0) else f"Watched {play_count} times"
            logging.info(f"Processing {library_type}: {title} ({year}) - Play count: {play_count}")
            
            # Check for dry run mode
            if self.config.get('safety', {}).get('dry_run', False):
                logging.info(f"Would remove {library_type} '{title}' ({year}) - {play_status}")
                success_count += 1
                continue
            
            # Attempt removal
            if self._remove_single_item(item):
                success_count += 1
        
        logging.info(f"Removal process completed. {success_count}/{len(removal_candidates)} items processed successfully")
        return success_count == len(removal_candidates)

    def _remove_single_item(self, item: Dict[str, Any]) -> bool:
        """Remove a single item from the appropriate service.
        
        Args:
            item: Item to remove
            
        Returns:
            True if successful
        """
        title = item['title']
        year = item['year']
        library_type = item['library_type']
        
        success = False
        
        if library_type == 'movie' and self.radarr and self.config['radarr']['enabled']:
            movie = self._find_radarr_movie(title, year, item.get('rating_key'))
            if movie:
                logging.info(f"Found movie in Radarr: {title} ({year})")
                success = self.radarr.delete_movie(
                    movie['id'],
                    delete_files=self.config['radarr']['delete_files'],
                    add_exclusion=self.config['radarr']['add_to_exclusion']
                )
                if success:
                    logging.info(f"Successfully removed movie from Radarr: {title} ({year})")
            else:
                logging.warning(f"Movie not found in Radarr: {title} ({year})")
        
        elif library_type == 'show' and self.sonarr and self.config['sonarr']['enabled']:
            series = self._find_sonarr_series(title, item.get('rating_key'))
            if series:
                logging.info(f"Found series in Sonarr: {title}")
                success = self.sonarr.delete_series(
                    series['id'],
                    delete_files=self.config['sonarr']['delete_files'],
                    add_exclusion=self.config['sonarr']['add_to_exclusion']
                )
                if success:
                    logging.info(f"Successfully removed series from Sonarr: {title}")
            else:
                logging.warning(f"Series not found in Sonarr: {title}")
        
        else:
            if library_type == 'movie' and not self.config['radarr']['enabled']:
                logging.warning(f"Cannot remove movie '{title}' - Radarr is disabled")
            elif library_type == 'show' and not self.config['sonarr']['enabled']:
                logging.warning(f"Cannot remove TV show '{title}' - Sonarr is disabled")
            else:
                logging.warning(f"No handler available for {library_type}: {title}")
        
        return success

    def _find_radarr_movie(self, title: str, year: Optional[str], rating_key: Optional[str]) -> Optional[Dict[str, Any]]:
        """Find a movie in Radarr using multiple matching strategies.
        
        Args:
            title: Movie title
            year: Movie year
            rating_key: Tautulli rating key
            
        Returns:
            Movie details if found
        """
        if not self.radarr:
            return None
        
        # Strategy 1: Try TMDB ID matching first
        if rating_key:
            try:
                metadata = self.tautulli.get_metadata(rating_key)
                tmdb_id = None
                
                # Look for TMDB ID in various places
                guid = metadata.get('guid', '')
                if 'tmdb://' in guid:
                    tmdb_id = int(guid.split('tmdb://')[1].split('?')[0])
                elif metadata.get('guids'):
                    for guid_info in metadata.get('guids', []):
                        if isinstance(guid_info, dict) and guid_info.get('id', '').startswith('tmdb://'):
                            tmdb_id = int(guid_info['id'].split('tmdb://')[1])
                            break
                
                if tmdb_id:
                    movie = self.radarr.search_movies_by_tmdb_id(tmdb_id)
                    if movie:
                        logging.debug(f"Found movie by TMDB ID {tmdb_id}: {title}")
                        return movie
                    
            except Exception as e:
                logging.debug(f"TMDB ID matching failed for {title}: {e}")
        
        # Strategy 2: Fallback to title/year matching
        movies = self.radarr.get_movies()
        year_int = None
        if year:
            try:
                year_int = int(year)
            except (ValueError, TypeError):
                pass
        
        best_match = None
        best_score = 0.0
        
        for movie in movies:
            movie_title = movie.get('title', '').lower()
            movie_year = movie.get('year')
            
            # Calculate title similarity
            title_score = self.filter.similarity_score(title, movie_title)
            
            # Year must match exactly or be very close
            year_match = True
            if year_int and movie_year:
                year_diff = abs(year_int - movie_year)
                if year_diff > 1:  # Allow 1 year difference for release date variations
                    year_match = False
            
            # Require good title match and year match
            if year_match and title_score > 0.8:
                if title_score > best_score:
                    best_score = title_score
                    best_match = movie
        
        if best_match:
            logging.debug(f"Found movie by title/year matching: {title} -> {best_match.get('title')} (score: {best_score:.2f})")
        
        return best_match

    def _find_sonarr_series(self, title: str, rating_key: Optional[str]) -> Optional[Dict[str, Any]]:
        """Find a TV series in Sonarr using multiple matching strategies.
        
        Args:
            title: Series title
            rating_key: Tautulli rating key
            
        Returns:
            Series details if found
        """
        if not self.sonarr:
            return None
        
        # Strategy 1: Try TVDB ID matching first
        if rating_key:
            try:
                metadata = self.tautulli.get_metadata(rating_key)
                tvdb_id = None
                
                # Look for TVDB ID in various places
                guid = metadata.get('guid', '')
                if 'tvdb://' in guid:
                    tvdb_id = int(guid.split('tvdb://')[1].split('?')[0])
                elif metadata.get('guids'):
                    for guid_info in metadata.get('guids', []):
                        if isinstance(guid_info, dict) and guid_info.get('id', '').startswith('tvdb://'):
                            tvdb_id = int(guid_info['id'].split('tvdb://')[1])
                            break
                
                if tvdb_id:
                    series = self.sonarr.search_series_by_tvdb_id(tvdb_id)
                    if series:
                        logging.debug(f"Found series by TVDB ID {tvdb_id}: {title}")
                        return series
                    
            except Exception as e:
                logging.debug(f"TVDB ID matching failed for {title}: {e}")
        
        # Strategy 2: Fallback to title matching
        series_list = self.sonarr.get_series()
        
        best_match = None
        best_score = 0.0
        
        for series in series_list:
            series_title = series.get('title', '')
            
            # Calculate title similarity
            title_score = self.filter.similarity_score(title, series_title)
            
            if title_score > 0.8 and title_score > best_score:
                best_score = title_score
                best_match = series
        
        if best_match:
            logging.debug(f"Found series by title matching: {title} -> {best_match.get('title')} (score: {best_score:.2f})")
        
        return best_match
