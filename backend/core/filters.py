"""
Media filtering logic for protecting valuable content.
"""
import logging
import re
from datetime import datetime
from typing import Dict, Any, Optional
from difflib import SequenceMatcher

from backend.api.tautulli import TautulliAPI


class MediaFilter:
    """Handles filtering and protection of media content."""
    
    def __init__(self, config: Dict[str, Any], tautulli: TautulliAPI):
        """Initialize media filter.
        
        Args:
            config: Configuration dictionary
            tautulli: Tautulli API client
        """
        self.config = config
        self.tautulli = tautulli

    def meets_age_requirements(self, last_played: Optional[str], added_at: str, play_count: Optional[int] = None) -> bool:
        """Check if media meets basic age requirements (without protection filters).
        
        Args:
            last_played: Last played timestamp
            added_at: Added to library timestamp
            play_count: Number of times played
            
        Returns:
            True if meets age requirements
        """
        now = datetime.now()
        
        # Check if it's been long enough since added
        try:
            added_date = datetime.fromtimestamp(int(added_at))
            days_since_added = (now - added_date).days
            if days_since_added < self.config['media']['min_days_since_added']:
                return False
        except (ValueError, TypeError):
            return False
        
        # If configured to only remove never-watched items, check play count
        if self.config['media'].get('require_zero_play_count', False):
            if play_count is None or play_count == 0:
                # Never watched - only check if it's been long enough since added
                return days_since_added >= self.config['media']['days_unwatched']
            else:
                # Has been watched at least once - don't remove
                return False
        
        # Original logic: Check last played date
        if last_played and last_played != '':
            try:
                last_played_date = datetime.fromtimestamp(int(last_played))
                days_since_played = (now - last_played_date).days
                return days_since_played >= self.config['media']['days_unwatched']
            except (ValueError, TypeError):
                pass
        
        # If never played, check against added date
        return days_since_added >= self.config['media']['days_unwatched']

    def is_old_enough(self, last_played: Optional[str], added_at: str, play_count: Optional[int] = None, item_data: Optional[Dict[str, Any]] = None) -> bool:
        """Check if media is old enough to be considered for removal.
        
        Args:
            last_played: Last played timestamp
            added_at: Added to library timestamp
            play_count: Number of times played
            item_data: Additional item metadata
            
        Returns:
            True if item qualifies for removal
        """
        # First check basic age requirements
        if not self.meets_age_requirements(last_played, added_at, play_count):
            return False
        
        # Then check protection filters
        return self.passes_protection_filters(item_data or {})

    def passes_protection_filters(self, item_data: Dict[str, Any]) -> bool:
        """Check if item passes all protection filters (returns True if item should be removed).
        
        Args:
            item_data: Item metadata
            
        Returns:
            True if item should be removed (not protected)
        """
        filters = self.config['media'].get('filters', {})
        title = item_data.get('title', '').lower()
        year = item_data.get('year')
        
        # Get detailed metadata for this item if we need ratings/genres
        rating_key = item_data.get('rating_key')
        metadata = None
        
        # Check if we need metadata for any filters
        needs_metadata = (
            filters.get('min_rating_to_keep', 0) > 0 or
            filters.get('min_audience_rating_to_keep', 0) > 0 or
            filters.get('protected_genres', []) or
            filters.get('protected_resolutions', []) or
            filters.get('min_file_size_to_keep', 0) > 0
        )
        
        if needs_metadata and rating_key:
            try:
                metadata = self.tautulli.get_metadata(rating_key)
            except Exception as e:
                logging.warning(f"Could not get metadata for {title}: {e}")
                metadata = {}
        
        # Check protected keywords in title
        protected_keywords = filters.get('protected_keywords', [])
        for keyword in protected_keywords:
            if keyword.lower() in title:
                logging.debug(f"Protecting by keyword '{keyword}': {item_data.get('title', 'Unknown')}")
                return False
        
        # Check year-based protections
        if year:
            try:
                year_int = int(year)
                
                # Protect classics
                protect_before = filters.get('protect_classics_before_year', 0)
                if protect_before > 0 and year_int <= protect_before:
                    logging.debug(f"Protecting classic: {item_data.get('title', 'Unknown')} ({year}) - Before {protect_before}")
                    return False
                
                # Protect recent releases
                protect_after = filters.get('protect_recent_after_year', 0)
                if protect_after > 0 and year_int >= protect_after:
                    logging.debug(f"Protecting recent release: {item_data.get('title', 'Unknown')} ({year}) - After {protect_after}")
                    return False
                    
            except (ValueError, TypeError):
                pass
        
        # Check ratings if we have metadata
        if metadata:
            # Check critic rating
            min_rating = filters.get('min_rating_to_keep', 0)
            if min_rating > 0:
                rating = metadata.get('rating')
                if rating and float(rating) >= min_rating:
                    logging.debug(f"Protecting by critic rating ({rating}): {item_data.get('title', 'Unknown')}")
                    return False
            
            # Check audience rating
            min_audience_rating = filters.get('min_audience_rating_to_keep', 0)
            if min_audience_rating > 0:
                audience_rating = metadata.get('audience_rating')
                if audience_rating and float(audience_rating) >= min_audience_rating:
                    logging.debug(f"Protecting by audience rating ({audience_rating}): {item_data.get('title', 'Unknown')}")
                    return False
            
            # Check protected genres
            protected_genres = [g.lower() for g in filters.get('protected_genres', [])]
            if protected_genres:
                item_genres = metadata.get('genre', [])
                if isinstance(item_genres, str):
                    item_genres = [item_genres]
                
                for genre in item_genres:
                    if isinstance(genre, dict):
                        genre_name = genre.get('tag', '').lower()
                    else:
                        genre_name = str(genre).lower()
                    
                    if genre_name in protected_genres:
                        logging.debug(f"Protecting by genre '{genre_name}': {item_data.get('title', 'Unknown')}")
                        return False
            
            # Check file size protection
            min_file_size = filters.get('min_file_size_to_keep', 0)
            if min_file_size > 0:
                media_info = metadata.get('media_info', [])
                if media_info:
                    for media in media_info:
                        parts = media.get('parts', [])
                        for part in parts:
                            file_size = part.get('size')
                            if file_size and int(file_size) >= min_file_size:
                                size_gb = int(file_size) / (1024**3)
                                logging.debug(f"Protecting by file size ({size_gb:.1f}GB): {item_data.get('title', 'Unknown')}")
                                return False
        
        # If we get here, the item is not protected and can be removed
        return True

    @staticmethod
    def normalize_title(title: str) -> str:
        """Normalize a title for better matching.
        
        Args:
            title: Title to normalize
            
        Returns:
            Normalized title
        """
        import re
        
        # Remove year in parentheses
        title = re.sub(r'\s*\(\d{4}\)\s*', '', title)
        
        # Remove common subtitle variations and extra info
        title = re.sub(r'\s*:\s*(Episode\s+[IVX]+|Part\s+\d+)\s*-?\s*', ': ', title)
        
        # Handle special character variations
        title = re.sub(r'[:\-–—]', ' ', title)  # Replace colons and dashes with space
        title = re.sub(r'[^\w\s&]', '', title)  # Remove special chars except ampersand
        
        # Handle common variations
        title = re.sub(r'\s*\([^)]*\)\s*', '', title)  # Remove any remaining parentheses content
        title = re.sub(r'\s+', ' ', title)  # Normalize whitespace
        
        # Handle common title differences
        replacements = {
            "philosopher's": "sorcerer's",  # Harry Potter regional difference
            "sorcerer's": "philosopher's",
            "&": "and",  # Handle ampersand variations
        }
        
        title_lower = title.lower().strip()
        for old, new in replacements.items():
            title_lower = title_lower.replace(old, new)
        
        return title_lower

    @staticmethod
    def similarity_score(a: str, b: str) -> float:
        """Calculate similarity score between two strings.
        
        Args:
            a: First string
            b: Second string
            
        Returns:
            Similarity score between 0 and 1
        """
        # Use normalized titles for better matching
        a_norm = MediaFilter.normalize_title(a)
        b_norm = MediaFilter.normalize_title(b)
        
        # Calculate primary similarity
        primary_score = SequenceMatcher(None, a_norm, b_norm).ratio()
        
        # If primary score is low, try additional matching strategies
        if primary_score < 0.8:
            # Try removing articles (the, a, an)
            a_no_articles = re.sub(r'\b(the|a|an)\s+', '', a_norm)
            b_no_articles = re.sub(r'\b(the|a|an)\s+', '', b_norm)
            article_score = SequenceMatcher(None, a_no_articles, b_no_articles).ratio()
            
            # Try word-based matching (better for reordered words)
            a_words = set(a_norm.split())
            b_words = set(b_norm.split())
            if a_words and b_words:
                word_score = len(a_words & b_words) / len(a_words | b_words)
            else:
                word_score = 0.0
            
            # Return the best score
            return max(primary_score, article_score, word_score)
        
        return primary_score
