"""
Library analyzer for discovering watch patterns and testing removal thresholds.
"""
import logging
from typing import Dict, List, Any, Tuple
from datetime import datetime, timedelta
from collections import defaultdict

from backend.api.tautulli import TautulliAPI
from backend.core.filters import MediaFilter


class LibraryAnalyzer:
    """Analyzes library content and provides insights for removal strategy."""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize LibraryAnalyzer with configuration.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        
        # Initialize API client
        self.tautulli = TautulliAPI(
            config['tautulli']['url'],
            config['tautulli']['api_key']
        )
        
        # Initialize filter for testing
        self.filter = MediaFilter(config, self.tautulli)

    def run_analysis(self, summary_only: bool = False, test_thresholds: bool = False) -> bool:
        """Run library analysis and display results.
        
        Args:
            summary_only: Show only summary statistics
            test_thresholds: Test different threshold values
            
        Returns:
            True if successful
        """
        print("\n" + "="*80)
        print("LIBRARY ANALYSIS MODE".center(80))
        print("="*80 + "\n")
        
        try:
            # Get all libraries
            libraries = self.tautulli.get_libraries()
            
            all_items = []
            library_stats = {}
            
            # Collect data from each library
            for library in libraries:
                library_name = library.get('section_name', 'Unknown')
                library_type = library.get('section_type', 'Unknown')
                library_id = library.get('section_id')
                
                # Skip based on configuration
                if library_type == 'movie' and not self.config['media'].get('process_movies', True):
                    continue
                
                if library_type == 'show' and not self.config['media'].get('process_tv_shows', True):
                    continue
                
                print(f"Analyzing library: {library_name} (Type: {library_type})...")
                
                # Get all media from this library
                media_response = self.tautulli.get_library_media_info(library_id, length=10000)
                media_items = media_response.get('data', []) if isinstance(media_response, dict) else media_response
                
                # Add library context to each item
                for item in media_items:
                    item['library_name'] = library_name
                    item['library_type'] = library_type
                
                all_items.extend(media_items)
                library_stats[library_name] = {
                    'type': library_type,
                    'total': len(media_items)
                }
            
            print(f"\nTotal items across all libraries: {len(all_items)}\n")
            
            # Run different analyses
            self._analyze_watch_status(all_items)
            
            if not summary_only:
                self._analyze_age_distribution(all_items)
                self._analyze_current_config_impact(all_items)
                self._show_top_lists(all_items)
            
            if test_thresholds:
                self._test_threshold_scenarios(all_items)
            
            return True
            
        except Exception as e:
            logging.error(f"Error during analysis: {e}")
            print(f"\n❌ Error during analysis: {e}")
            return False

    def _analyze_watch_status(self, items: List[Dict[str, Any]]) -> None:
        """Analyze watch status distribution.
        
        Args:
            items: List of media items
        """
        print("="*80)
        print("WATCH STATUS DISTRIBUTION")
        print("="*80 + "\n")
        
        # Categorize by library type
        movies = [i for i in items if i.get('library_type') == 'movie']
        shows = [i for i in items if i.get('library_type') == 'show']
        
        for media_type, media_items in [('Movies', movies), ('TV Shows', shows)]:
            if not media_items:
                continue
            
            print(f"\n{media_type}:")
            print("-" * 80)
            
            never_watched = sum(1 for i in media_items if not i.get('play_count'))
            lightly_watched = sum(1 for i in media_items if i.get('play_count') and 1 <= i.get('play_count') <= 5)
            moderately_watched = sum(1 for i in media_items if i.get('play_count') and 6 <= i.get('play_count') <= 20)
            heavily_watched = sum(1 for i in media_items if i.get('play_count') and i.get('play_count') > 20)
            
            total = len(media_items)
            
            print(f"  Never watched (play_count = 0):     {never_watched:5} ({never_watched/total*100:5.1f}%)")
            print(f"  Lightly watched (1-5 plays):        {lightly_watched:5} ({lightly_watched/total*100:5.1f}%)")
            print(f"  Moderately watched (6-20 plays):    {moderately_watched:5} ({moderately_watched/total*100:5.1f}%)")
            print(f"  Heavily watched (20+ plays):        {heavily_watched:5} ({heavily_watched/total*100:5.1f}%)")
            print(f"  {'─'*40}")
            print(f"  Total:                               {total:5}")

    def _analyze_age_distribution(self, items: List[Dict[str, Any]]) -> None:
        """Analyze age distribution of unwatched content.
        
        Args:
            items: List of media items
        """
        print("\n" + "="*80)
        print("AGE DISTRIBUTION OF UNWATCHED CONTENT")
        print("="*80 + "\n")
        
        now = datetime.now()
        unwatched_items = [i for i in items if not i.get('play_count')]
        
        if not unwatched_items:
            print("No unwatched items found.\n")
            return
        
        # Categorize by age buckets
        age_buckets = {
            '0-6 months': 0,
            '6-12 months': 0,
            '1-2 years': 0,
            '2-3 years': 0,
            '3+ years': 0
        }
        
        for item in unwatched_items:
            try:
                added_at = datetime.fromtimestamp(int(item.get('added_at', 0)))
                days_old = (now - added_at).days
                
                if days_old < 180:
                    age_buckets['0-6 months'] += 1
                elif days_old < 365:
                    age_buckets['6-12 months'] += 1
                elif days_old < 730:
                    age_buckets['1-2 years'] += 1
                elif days_old < 1095:
                    age_buckets['2-3 years'] += 1
                else:
                    age_buckets['3+ years'] += 1
            except (ValueError, TypeError):
                continue
        
        for bucket, count in age_buckets.items():
            percentage = count / len(unwatched_items) * 100 if unwatched_items else 0
            bar = '█' * int(percentage / 2)
            print(f"  {bucket:15} {count:5} ({percentage:5.1f}%) {bar}")

    def _analyze_current_config_impact(self, items: List[Dict[str, Any]]) -> None:
        """Analyze what current config would remove.
        
        Args:
            items: List of media items
        """
        print("\n" + "="*80)
        print("CURRENT CONFIG IMPACT")
        print("="*80 + "\n")
        
        print("Current Settings:")
        print(f"  days_unwatched:           {self.config['media']['days_unwatched']}")
        print(f"  min_days_since_added:     {self.config['media']['min_days_since_added']}")
        print(f"  require_zero_play_count:  {self.config['media'].get('require_zero_play_count', False)}")
        print()
        
        # Count what would be removed
        candidates = []
        filtered_by_age = 0
        filtered_by_protection = 0
        
        for item in items:
            if self.filter.is_old_enough(
                item.get('last_played'),
                item.get('added_at'),
                item.get('play_count'),
                item_data=item
            ):
                candidates.append(item)
            else:
                if not self.filter.meets_age_requirements(item.get('last_played'), item.get('added_at'), item.get('play_count')):
                    filtered_by_age += 1
                else:
                    filtered_by_protection += 1
        
        # Break down by library type
        movies_to_remove = sum(1 for i in candidates if i.get('library_type') == 'movie')
        shows_to_remove = sum(1 for i in candidates if i.get('library_type') == 'show')
        
        print(f"Results:")
        print(f"  Would be removed:           {len(candidates):5} items")
        print(f"    - Movies:                 {movies_to_remove:5}")
        print(f"    - TV Shows:               {shows_to_remove:5}")
        print(f"  Protected by age criteria:  {filtered_by_age:5} items")
        print(f"  Protected by filters:       {filtered_by_protection:5} items")
        print(f"  {'─'*40}")
        print(f"  Total library size:         {len(items):5} items")
        
        # Show protection breakdown
        if filtered_by_protection > 0:
            self._show_protection_breakdown(items)

    def _show_protection_breakdown(self, items: List[Dict[str, Any]]) -> None:
        """Show detailed breakdown of why items are protected.
        
        Args:
            items: List of media items
        """
        print("\n  Protection Reasons:")
        
        filters = self.config['media'].get('filters', {})
        protected_keywords = filters.get('protected_keywords', [])
        protect_before_year = filters.get('protect_classics_before_year', 0)
        protect_after_year = filters.get('protect_recent_after_year', 0)
        
        keyword_protected = 0
        classic_protected = 0
        recent_protected = 0
        
        for item in items:
            if not self.filter.meets_age_requirements(item.get('last_played'), item.get('added_at'), item.get('play_count')):
                continue
            
            if not self.filter.passes_protection_filters(item):
                title = item.get('title', '').lower()
                year = item.get('year')
                
                # Check keyword
                for keyword in protected_keywords:
                    if keyword.lower() in title:
                        keyword_protected += 1
                        break
                
                # Check year
                if year:
                    try:
                        year_int = int(year)
                        if protect_before_year > 0 and year_int <= protect_before_year:
                            classic_protected += 1
                        elif protect_after_year > 0 and year_int >= protect_after_year:
                            recent_protected += 1
                    except (ValueError, TypeError):
                        pass
        
        if keyword_protected:
            print(f"    - Protected keywords:     {keyword_protected:5}")
        if classic_protected:
            print(f"    - Classic (≤{protect_before_year}):       {classic_protected:5}")
        if recent_protected:
            print(f"    - Recent (≥{protect_after_year}):        {recent_protected:5}")

    def _show_top_lists(self, items: List[Dict[str, Any]]) -> None:
        """Show top lists of interesting items.
        
        Args:
            items: List of media items
        """
        print("\n" + "="*80)
        print("TOP LISTS")
        print("="*80 + "\n")
        
        now = datetime.now()
        unwatched_items = [i for i in items if not i.get('play_count')]
        
        if not unwatched_items:
            print("No unwatched items found.\n")
            return
        
        # Calculate age for each item
        items_with_age = []
        for item in unwatched_items:
            try:
                added_at = datetime.fromtimestamp(int(item.get('added_at', 0)))
                days_old = (now - added_at).days
                items_with_age.append((item, days_old))
            except (ValueError, TypeError):
                continue
        
        # Sort by age (oldest first)
        items_with_age.sort(key=lambda x: x[1], reverse=True)
        
        # Oldest unwatched
        print("Oldest Unwatched Content (Top 10):")
        print("-" * 80)
        for i, (item, days_old) in enumerate(items_with_age[:10], 1):
            title = item.get('title', 'Unknown')[:40]
            lib_type = item.get('library_type', 'unknown')
            year = item.get('year', 'N/A')
            years_old = days_old / 365
            print(f"  {i:2}. {title:40} ({year}) [{lib_type:5}] - {years_old:.1f} years old")
        
        # Recently added but never watched
        recent_unwatched = [x for x in items_with_age if x[1] < 180]  # Less than 6 months
        if recent_unwatched:
            print("\nRecently Added But Never Watched (Last 6 months, up to 10):")
            print("-" * 80)
            for i, (item, days_old) in enumerate(recent_unwatched[:10], 1):
                title = item.get('title', 'Unknown')[:40]
                lib_type = item.get('library_type', 'unknown')
                year = item.get('year', 'N/A')
                print(f"  {i:2}. {title:40} ({year}) [{lib_type:5}] - {days_old} days ago")
        
        # Items close to removal threshold
        threshold_days = self.config['media']['min_days_since_added']
        near_threshold = [x for x in items_with_age if threshold_days <= x[1] <= threshold_days + 30]
        if near_threshold:
            print(f"\nItems Close to Removal Threshold ({threshold_days} days, up to 10):")
            print("-" * 80)
            for i, (item, days_old) in enumerate(near_threshold[:10], 1):
                title = item.get('title', 'Unknown')[:40]
                lib_type = item.get('library_type', 'unknown')
                year = item.get('year', 'N/A')
                print(f"  {i:2}. {title:40} ({year}) [{lib_type:5}] - {days_old} days old")

    def _test_threshold_scenarios(self, items: List[Dict[str, Any]]) -> None:
        """Test different threshold scenarios.
        
        Args:
            items: List of media items
        """
        print("\n" + "="*80)
        print("THRESHOLD TESTING")
        print("="*80 + "\n")
        
        test_scenarios = [
            (180, 200),   # 6 months
            (365, 400),   # 1 year
            (730, 750),   # 2 years
            (1095, 1100), # 3 years
        ]
        
        print("Testing different threshold combinations:")
        print("(days_unwatched, min_days_since_added)")
        print("-" * 80)
        
        original_unwatched = self.config['media']['days_unwatched']
        original_added = self.config['media']['min_days_since_added']
        
        for days_unwatched, min_days_added in test_scenarios:
            # Temporarily modify config
            self.config['media']['days_unwatched'] = days_unwatched
            self.config['media']['min_days_since_added'] = min_days_added
            
            # Recreate filter with new config
            temp_filter = MediaFilter(self.config, self.tautulli)
            
            # Count what would be removed
            would_remove = 0
            for item in items:
                if temp_filter.is_old_enough(
                    item.get('last_played'),
                    item.get('added_at'),
                    item.get('play_count'),
                    item_data=item
                ):
                    would_remove += 1
            
            print(f"  ({days_unwatched:4}, {min_days_added:4}): {would_remove:5} items would be removed")
        
        # Restore original config
        self.config['media']['days_unwatched'] = original_unwatched
        self.config['media']['min_days_since_added'] = original_added
        
        print("\n💡 Tip: Adjust your config.yaml based on these results")
