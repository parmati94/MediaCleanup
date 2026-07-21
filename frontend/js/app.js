/**
 * Main entry point for Vite bundling
 * Imports Alpine.js and initializes the app
 */
import Alpine from 'alpinejs';
import '@fontsource/ibm-plex-sans/400.css';
import '@fontsource/ibm-plex-sans/500.css';
import '@fontsource/ibm-plex-sans/600.css';
import '@fontsource/ibm-plex-sans/700.css';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import '@fontsource/ibm-plex-mono/600.css';
import '../css/style.css';

// Expose Alpine globally BEFORE any DOM parsing
window.Alpine = Alpine;

// Alpine.js app data
function appData() {
    return {
        currentTab: 'dashboard',
        analysisSection: 'space-consumers',
        loading: false,
        refreshing: false,
        settingsOpen: false,
        settingsTab: 'connections',
        filtersOpen: false,
        config: null,
        analysis: null,
        candidates: null,
        searchQuery: '',
        confirmRemoval: false,
        selectedCandidates: [],
        loadingCandidates: false,
        previewData: null,
        previewLoading: false,
        sortColumn: 'title',
        sortDirection: 'asc',
        toasts: [],
        connectionStatus: {
            tautulli: null,
            radarr: null,
            sonarr: null,
        },

        // --- Removal-filter derived controls (friendly UI <-> raw config) ---
        get mediaType() {
            const m = this.config?.media?.process_movies;
            const t = this.config?.media?.process_tv_shows;
            if (m && t) return 'both';
            if (m) return 'movies';
            if (t) return 'tv';
            return 'none';
        },
        setMediaType(type) {
            if (!this.config?.media) return;
            this.config.media.process_movies = (type === 'both' || type === 'movies');
            this.config.media.process_tv_shows = (type === 'both' || type === 'tv');
            this.schedulePreview();
        },
        get minSizeGB() {
            const b = this.config?.media?.filters?.min_file_size_to_keep || 0;
            return b ? Math.round((b / (1024 ** 3)) * 100) / 100 : 0;
        },
        set minSizeGB(v) {
            if (this.config?.media?.filters) {
                this.config.media.filters.min_file_size_to_keep = Math.round((v || 0) * (1024 ** 3));
            }
        },

        async init() {
            // Lazy-load the removal candidates the first time the tab is opened,
            // so the list is populated on arrival instead of sitting empty until
            // "Refresh List" is clicked manually.
            this.$watch('currentTab', (tab) => {
                if (tab === 'removal' && this.candidates === null && !this.loadingCandidates) {
                    this.loadCandidates();
                }
            });
            await this.loadConfig();
            await this.runAnalysis(false);
        },

        async loadConfig() {
            this.loading = true;
            try {
                const response = await fetch('/api/config');
                const data = await response.json();
                if (data.success) {
                    this.config = data.data;
                    this.normalizeConfig();
                }
            } catch (error) {
                console.error('Error loading config:', error);
                this.showError('Failed to load configuration');
            } finally {
                this.loading = false;
            }
        },
        
        // Ensure the config has the shape the settings UI binds to, migrating the
        // legacy require_zero_play_count flag into the max_play_count threshold.
        normalizeConfig() {
            const media = this.config?.media;
            if (!media) return;
            if (media.max_play_count === undefined || media.max_play_count === null) {
                media.max_play_count = media.require_zero_play_count ? 1 : 0;
            }
            media.filters = media.filters || {};
            if (!Array.isArray(media.filters.protected_keywords)) media.filters.protected_keywords = [];
            if (!Array.isArray(media.filters.protected_genres)) media.filters.protected_genres = [];
        },

        // --- Pill list helpers (protected keywords / genres) ---
        addPill(listName, value) {
            const v = (value || '').trim();
            if (!v) return false;
            const list = this.config.media.filters[listName];
            if (!list.some(x => x.toLowerCase() === v.toLowerCase())) {
                list.push(v);
                this.schedulePreview();
            }
            return true;
        },
        removePill(listName, idx) {
            this.config.media.filters[listName].splice(idx, 1);
            this.schedulePreview();
        },
        // Library genres not already protected, for the click-to-add suggestions.
        genreSuggestions() {
            const all = this.previewData?.available_genres || [];
            const chosen = (this.config?.media?.filters?.protected_genres || []).map(g => g.toLowerCase());
            return all.filter(g => !chosen.includes(g.toLowerCase()));
        },

        // Effective minimum library age for a NEVER-watched title: both the
        // added-date gate and the unwatched gate measure from the added date, so
        // the larger of the two wins.
        neverWatchedFloor() {
            const a = Number(this.config?.media?.min_days_since_added) || 0;
            const u = Number(this.config?.media?.days_unwatched) || 0;
            return Math.max(a, u);
        },

        // --- Live filter-impact preview ---
        schedulePreview() {
            if (!this.filtersOpen) return;
            clearTimeout(this._previewTimer);
            this._previewTimer = setTimeout(() => this.refreshPreview(), 450);
        },
        async refreshPreview() {
            if (!this.config?.media) return;
            this.previewLoading = true;
            try {
                const response = await fetch('/api/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ media: this.config.media }),
                });
                const data = await response.json();
                if (data.success) this.previewData = data.data;
            } catch (error) {
                console.error('Error building preview:', error);
            } finally {
                this.previewLoading = false;
            }
        },

        async saveConfig() {
            this.loading = true;
            try {
                const response = await fetch('/api/config', {
                    method: 'PUT',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ config: this.config }),
                });
                const data = await response.json();
                if (data.success) {
                    this.showSuccess('Configuration saved successfully');
                    // Refresh analysis with new config
                    await this.runAnalysis(false);
                }
            } catch (error) {
                console.error('Error saving config:', error);
                this.showError('Failed to save configuration');
            } finally {
                this.loading = false;
            }
        },

        async runAnalysis(includeThresholds, refresh = false) {
            this.loading = true;
            // refresh=true forces Tautulli to rebuild media info from Plex first
            // (drops deleted items). It's noticeably slower, so surface that state.
            this.refreshing = refresh;
            try {
                const response = await fetch('/api/analyze', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        summary_only: false,
                        test_thresholds: includeThresholds,
                        refresh: refresh
                    }),
                });
                const data = await response.json();
                if (data.success) {
                    this.analysis = data.data;
                }
            } catch (error) {
                console.error('Error running analysis:', error);
                this.showError('Failed to run analysis');
            } finally {
                this.loading = false;
                this.refreshing = false;
            }
        },

        async saveSettings() {
            // The threshold field supersedes the legacy flag; drop it to avoid two
            // competing keys lingering in the saved config.
            if (this.config?.media) delete this.config.media.require_zero_play_count;
            await this.saveConfig();               // persists + re-runs analysis (dashboard)
            if (this.candidates) await this.loadCandidates();  // keep the removal list in sync
            this.settingsOpen = false;
        },

        // Open the Removal Filters drawer and prime the live preview.
        openFilters() {
            this.filtersOpen = true;
            this.refreshPreview();
        },

        // Just persist config.yaml — no dependent refreshes. Returns success bool.
        async persistConfig() {
            try {
                const response = await fetch('/api/config', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config: this.config }),
                });
                const data = await response.json();
                return !!data.success;
            } catch (error) {
                console.error('Error saving config:', error);
                return false;
            }
        },

        async saveFilters() {
            if (this.config?.media) delete this.config.media.require_zero_play_count;
            const ok = await this.persistConfig();
            if (!ok) {
                this.showError('Failed to save filters');
                return;
            }
            // Close the drawer as soon as the config is persisted, then refresh the
            // dependent views in the background (the removal list shows its own
            // "updating" state) so the user isn't stuck staring at the filter panel.
            this.filtersOpen = false;
            this.showSuccess('Filters saved');
            this.runAnalysis(false);
            if (this.candidates || this.currentTab === 'removal') this.loadCandidates();
        },

        async applyFilters() {
            this.loading = true;
            try {
                const response = await fetch('/api/config', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config: this.config }),
                });
                const data = await response.json();
                if (data.success) {
                    this.showSuccess('Filters applied');
                    await this.loadCandidates();
                } else {
                    this.showError('Failed to apply filters');
                }
            } catch (error) {
                console.error('Error applying filters:', error);
                this.showError('Failed to apply filters');
            } finally {
                this.loading = false;
            }
        },

        async testConnection(service) {
            try {
                const response = await fetch(`/api/test/${service}`);
                const data = await response.json();
                this.connectionStatus[service] = data;
            } catch (error) {
                this.connectionStatus[service] = { success: false, message: 'Request failed' };
            }
        },

        async testAllConnections() {
            await Promise.all([
                this.testConnection('tautulli'),
                this.testConnection('radarr'),
                this.testConnection('sonarr'),
            ]);
        },

        async toggleDryRun() {
            this.config.safety.dry_run = !this.config.safety.dry_run;
            try {
                await fetch('/api/config', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config: this.config }),
                });
                if (this.config.safety.dry_run) {
                    this.showSuccess('Dry Run enabled — no files will be deleted');
                } else {
                    this.showWarning('⚠️ Dry Run disabled — executions will permanently delete files!', 8000);
                }
            } catch (error) {
                // Revert on failure
                this.config.safety.dry_run = !this.config.safety.dry_run;
                this.showError('Failed to save dry run setting');
            }
        },

        async loadCandidates() {
            this.loading = true;
            this.loadingCandidates = true;
            try {
                const response = await fetch('/api/candidates');
                const data = await response.json();
                if (data.success) {
                    this.candidates = data.data;
                    this.selectedCandidates = [];
                }
            } catch (error) {
                console.error('Error loading candidates:', error);
                this.showError('Failed to load removal candidates');
            } finally {
                this.loading = false;
                this.loadingCandidates = false;
            }
        },
        
        toggleSelectAll() {
            const filtered = this.filteredCandidates();
            if (this.selectedCandidates.length === filtered.length) {
                this.selectedCandidates = [];
            } else {
                this.selectedCandidates = filtered.map(c => c.id);
            }
        },
        
        isSelected(ratingKey) {
            return this.selectedCandidates.includes(ratingKey);
        },
        
        toggleSelection(ratingKey) {
            const index = this.selectedCandidates.indexOf(ratingKey);
            if (index === -1) {
                this.selectedCandidates.push(ratingKey);
            } else {
                this.selectedCandidates.splice(index, 1);
            }
        },
        
        sortBy(column) {
            if (this.sortColumn === column) {
                this.sortDirection = this.sortDirection === 'asc' ? 'desc' : 'asc';
            } else {
                this.sortColumn = column;
                this.sortDirection = 'asc';
            }
        },
        
        formatFileSize(bytes) {
            if (!bytes || bytes === 0) return 'N/A';
            
            const units = ['B', 'KB', 'MB', 'GB', 'TB'];
            let size = bytes;
            let unitIndex = 0;
            
            while (size >= 1024 && unitIndex < units.length - 1) {
                size /= 1024;
                unitIndex++;
            }
            
            return `${size.toFixed(2)} ${units[unitIndex]}`;
        },
        
        formatDate(timestamp) {
            if (!timestamp) return 'Never';
            
            // Tautulli timestamps are Unix epoch seconds
            const date = new Date(timestamp * 1000);
            const now = new Date();
            const diffMs = now - date;
            const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
            
            // Show relative time for recent watches
            if (diffDays === 0) return 'Today';
            if (diffDays === 1) return 'Yesterday';
            if (diffDays < 7) return `${diffDays} days ago`;
            if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`;
            if (diffDays < 365) return `${Math.floor(diffDays / 30)} months ago`;
            
            // Show actual date for older watches
            return date.toLocaleDateString();
        },
        
        filteredCandidates() {
            if (!this.candidates) return [];
            
            let filtered = this.candidates;
            
            // Apply search filter
            if (this.searchQuery) {
                const query = this.searchQuery.toLowerCase();
                filtered = filtered.filter(item => 
                    item.title.toLowerCase().includes(query) ||
                    (item.year && item.year.toString().includes(query))
                );
            }
            
            // Apply sorting
            const sorted = [...filtered].sort((a, b) => {
                let aVal = a[this.sortColumn];
                let bVal = b[this.sortColumn];
                
                // Handle nulls
                if (aVal === null || aVal === undefined) return 1;
                if (bVal === null || bVal === undefined) return -1;
                
                // String comparison
                if (typeof aVal === 'string') {
                    aVal = aVal.toLowerCase();
                    bVal = bVal.toLowerCase();
                }
                
                if (aVal < bVal) return this.sortDirection === 'asc' ? -1 : 1;
                if (aVal > bVal) return this.sortDirection === 'asc' ? 1 : -1;
                return 0;
            });
            
            return sorted;
        },
        
        async executeRemoval() {
            // SAFETY: Never fall back to removing all items - require explicit selection
            if (this.selectedCandidates.length === 0) {
                this.showError('Please select at least one item to remove. Use checkboxes to select items.');
                return;
            }
            
            const itemsToRemove = this.selectedCandidates;
            
            // Additional confirmation for large batch removals
            if (itemsToRemove.length > 10) {
                this.showWarning(`Large batch operation: You are about to remove ${itemsToRemove.length} items. Please verify your selection carefully.`, 10000);
            }

            this.loading = true;
            try {
                const response = await fetch('/api/remove', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        confirm: true,
                        ids: itemsToRemove
                    }),
                });
                const data = await response.json();
                if (data.success) {
                    const isDryRun = this.config?.safety?.dry_run;
                    if (isDryRun) {
                        this.showWarning(`DRY RUN: ${itemsToRemove.length} item(s) would have been removed (no files were actually deleted)`);
                    } else {
                        this.showSuccess(`Successfully removed ${itemsToRemove.length} item(s)`);
                    }
                    this.confirmRemoval = false;
                    this.selectedCandidates = [];
                    // Refresh data
                    await this.runAnalysis(false);
                    await this.loadCandidates();
                } else {
                    this.showError('Removal process failed');
                }
            } catch (error) {
                console.error('Error executing removal:', error);
                this.showError('Failed to execute removal');
            } finally {
                this.loading = false;
            }
        },

        getTotalUnwatched() {
            if (!this.analysis?.age_distribution) return 1;
            return Object.values(this.analysis.age_distribution).reduce((a, b) => a + b, 0) || 1;
        },

        // Share of a watch bucket ('shows'|'movies') for the dashboard mix bar.
        watchPct(kind, bucket) {
            const w = this.analysis?.watch_status?.[kind];
            if (!w) return 0;
            const tot = (w.never_watched || 0) + (w.lightly_watched || 0) + (w.moderately_watched || 0) + (w.heavily_watched || 0);
            return tot ? ((w[bucket] || 0) / tot * 100) : 0;
        },

        // Reclaimable space as a % of the managed library (for the capacity meter).
        reclaimPct() {
            const i = this.analysis?.current_config_impact;
            if (!i || !i.total_library_size) return 0;
            return Math.min(100, (i.potential_savings || 0) / i.total_library_size * 100);
        },

        showSuccess(message) {
            this.addToast(message, 'success');
        },

        showError(message) {
            this.addToast(message, 'error');
        },
        
        showWarning(message, duration) {
            this.addToast(message, 'warning', duration);
        },
        
        addToast(message, type = 'info', customDuration = null) {
            const id = Date.now();
            this.toasts.push({ id, message, type });
            
            // Auto-remove after specified duration or defaults (8s for warnings, 5s for others)
            const duration = customDuration || (type === 'warning' ? 8000 : 5000);
            setTimeout(() => {
                this.removeToast(id);
            }, duration);
        },
        
        removeToast(id) {
            const index = this.toasts.findIndex(t => t.id === id);
            if (index !== -1) {
                this.toasts.splice(index, 1);
            }
        }
    };
}

// Register the component with Alpine
Alpine.data('appData', appData);

// Start Alpine (will automatically process x-data elements)
Alpine.start();

console.log('✅ Media Cleanup UI loaded with Alpine.js');
