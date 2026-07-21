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
        media_cfg = self.config['media']

        # Check if it's been long enough since added
        try:
            added_date = datetime.fromtimestamp(int(added_at))
            days_since_added = (now - added_date).days
            if days_since_added < media_cfg['min_days_since_added']:
                return False
        except (ValueError, TypeError):
            return False

        # Play-count protection: keep anything watched at least `max_play_count`
        # times. 0 (or unset) disables it. The legacy `require_zero_play_count`
        # flag maps to a threshold of 1 (only ever remove never-watched items).
        max_play_count = media_cfg.get('max_play_count')
        if max_play_count is None:
            max_play_count = 1 if media_cfg.get('require_zero_play_count') else 0
        if max_play_count and max_play_count > 0 and (play_count or 0) >= max_play_count:
            return False

        days_unwatched = media_cfg['days_unwatched']

        # Staleness: measure from last watch, or from added date if never played.
        if last_played and last_played != '':
            try:
                last_played_date = datetime.fromtimestamp(int(last_played))
                days_since_played = (now - last_played_date).days
                return days_since_played >= days_unwatched
            except (ValueError, TypeError):
                pass

        return days_since_added >= days_unwatched

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

        # Prefer authoritative fields already on the item (from Radarr/Sonarr); only
        # fall back to Tautulli metadata (legacy path) when a needed field is missing.
        rating = item_data.get('rating')
        audience_rating = item_data.get('audience_rating')
        genres = item_data.get('genres')
        file_size = item_data.get('file_size')

        rating_key = item_data.get('rating_key')
        needs_metadata = (
            (filters.get('min_rating_to_keep', 0) > 0 and rating is None) or
            (filters.get('min_audience_rating_to_keep', 0) > 0 and audience_rating is None) or
            (filters.get('protected_genres', []) and not genres) or
            (filters.get('min_file_size_to_keep', 0) > 0 and not file_size)
        )
        if needs_metadata and rating_key:
            try:
                metadata = self.tautulli.get_metadata(rating_key) or {}
            except Exception as e:
                logging.warning(f"Could not get metadata for {title}: {e}")
                metadata = {}
            if rating is None:
                rating = metadata.get('rating')
            if audience_rating is None:
                audience_rating = metadata.get('audience_rating')
            if not genres:
                genres = metadata.get('genre')
            if not file_size:
                for media in metadata.get('media_info', []) or []:
                    for part in media.get('parts', []) or []:
                        try:
                            file_size = max(int(file_size or 0), int(part.get('size') or 0))
                        except (ValueError, TypeError):
                            pass

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

                protect_before = filters.get('protect_classics_before_year', 0)
                if protect_before > 0 and year_int <= protect_before:
                    logging.debug(f"Protecting classic: {item_data.get('title', 'Unknown')} ({year})")
                    return False

                protect_after = filters.get('protect_recent_after_year', 0)
                if protect_after > 0 and year_int >= protect_after:
                    logging.debug(f"Protecting recent release: {item_data.get('title', 'Unknown')} ({year})")
                    return False
            except (ValueError, TypeError):
                pass

        # Critic rating
        min_rating = filters.get('min_rating_to_keep', 0)
        if min_rating > 0 and rating:
            try:
                if float(rating) >= min_rating:
                    logging.debug(f"Protecting by critic rating ({rating}): {item_data.get('title', 'Unknown')}")
                    return False
            except (ValueError, TypeError):
                pass

        # Audience rating
        min_audience_rating = filters.get('min_audience_rating_to_keep', 0)
        if min_audience_rating > 0 and audience_rating:
            try:
                if float(audience_rating) >= min_audience_rating:
                    logging.debug(f"Protecting by audience rating ({audience_rating}): {item_data.get('title', 'Unknown')}")
                    return False
            except (ValueError, TypeError):
                pass

        # Protected genres
        protected_genres = [g.lower() for g in filters.get('protected_genres', [])]
        if protected_genres and genres:
            if isinstance(genres, str):
                genres = [genres]
            for genre in genres:
                genre_name = genre.get('tag', '').lower() if isinstance(genre, dict) else str(genre).lower()
                if genre_name in protected_genres:
                    logging.debug(f"Protecting by genre '{genre_name}': {item_data.get('title', 'Unknown')}")
                    return False

        # File size protection
        min_file_size = filters.get('min_file_size_to_keep', 0)
        if min_file_size > 0 and file_size:
            try:
                if int(file_size) >= min_file_size:
                    logging.debug(f"Protecting by file size: {item_data.get('title', 'Unknown')}")
                    return False
            except (ValueError, TypeError):
                pass

        # Not protected - can be removed
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
