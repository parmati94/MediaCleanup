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
        analysisAt: null,
        candidatesAt: null,
        previewData: null,
        previewLoading: false,
        history: null,
        historyBatches: [],
        historyLoading: false,
        historyQuery: '',
        historyType: 'all',
        historyStatus: 'removed',
        historyExpanded: {},
        dialog: { open: false, title: '', message: '', details: [], confirmLabel: 'Confirm', cancelLabel: 'Cancel', tone: 'primary' },
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

        // --- Local persistence: survive a plain page reload without re-fetching
        // everything. State is restored instantly and only refreshed on demand. ---
        _persist(key, value) {
            try { localStorage.setItem('mc_' + key, JSON.stringify(value)); } catch (e) { /* quota/private mode */ }
        },
        _restore(key, fallback) {
            try {
                const v = localStorage.getItem('mc_' + key);
                return v === null ? fallback : JSON.parse(v);
            } catch (e) { return fallback; }
        },
        // Human-friendly "x ago" for the freshness hints.
        timeAgo(ms) {
            if (!ms) return '';
            const s = Math.floor((Date.now() - ms) / 1000);
            if (s < 45) return 'just now';
            const m = Math.floor(s / 60);
            if (m < 60) return `${Math.max(1, m)}m ago`;
            const h = Math.floor(m / 60);
            if (h < 24) return `${h}h ago`;
            return `${Math.floor(h / 24)}d ago`;
        },

        async init() {
            // Restore persisted state from the last session (instant, no re-fetch).
            this.currentTab = this._restore('currentTab', 'dashboard');
            this.connectionStatus = this._restore('connectionStatus', this.connectionStatus);
            this.analysis = this._restore('analysis', null);
            this.analysisAt = this._restore('analysisAt', null);
            this.candidates = this._restore('candidates', null);
            this.candidatesAt = this._restore('candidatesAt', null);

            // Keep the persisted copies in sync as state changes.
            this.$watch('currentTab', (v) => this._persist('currentTab', v));
            this.$watch('analysis', (v) => this._persist('analysis', v));
            this.$watch('candidates', (v) => this._persist('candidates', v));

            // Lazy-load removal candidates the first time the tab is opened (unless
            // a cached list was just restored).
            this.$watch('currentTab', (tab) => {
                if (tab === 'removal' && this.candidates === null && !this.loadingCandidates) {
                    this.loadCandidates();
                }
                if (tab === 'removed' && !this.historyLoading) this.loadHistory();
            });

            await this.loadConfig();

            // Only auto-run the analysis if we have nothing cached; otherwise the
            // restored data stands until the user hits Refresh.
            if (!this.analysis) {
                await this.runAnalysis(false);
            }
            if (this.currentTab === 'removal' && this.candidates === null && !this.loadingCandidates) {
                this.loadCandidates();
            }
            if (this.currentTab === 'removed') this.loadHistory();
        },

        // --- Removal history ("Removed" tab) ---
        async loadHistory() {
            this.historyLoading = true;
            try {
                const response = await fetch('/api/history');
                const data = await response.json();
                if (data.success) {
                    this.history = data.data.removals;
                    this.historyBatches = data.data.batches;
                    // Resume polling for anything still re-adding (e.g. after a reload).
                    this.history.filter(r => r.readd_status === 'running').forEach(r => this.pollReadd(r));
                    this.scheduleHistoryRefresh();
                }
            } catch (error) {
                console.error('Error loading history:', error);
                this.showError('Failed to load removal history');
            } finally {
                this.historyLoading = false;
            }
        },
        // While re-adds are downloading, refresh every 30s so progress / "Restored"
        // shows up on its own. Stops once nothing is in flight or the tab is left.
        scheduleHistoryRefresh() {
            clearTimeout(this._historyTimer);
            const inFlight = (this.history || []).filter(r => ['grabbed', 'searching', 'partial'].includes(r.readd_status));
            if (!inFlight.length) return;
            // Imports from the download client finish in seconds - check back sooner.
            const soon = inFlight.some(r => /imported it/.test(r.readd_detail || ''));
            this._historyTimer = setTimeout(() => {
                if (this.currentTab === 'removed' && !document.hidden) this.loadHistory();
                else this.scheduleHistoryRefresh();
            }, soon ? 5000 : 30000);
        },
        // 'removed' (still gone, incl. failed re-adds) | 'readding' (in flight) | 'restored'
        itemStatus(r) {
            if (r.readd_status === 'restored') return 'restored';
            if (['running', 'grabbed', 'searching', 'partial'].includes(r.readd_status)) return 'readding';
            return 'removed';
        },
        statusCount(status) {
            return (this.history || []).filter(r =>
                (status === 'all' || this.itemStatus(r) === status) &&
                (this.historyType === 'all' || r.media_type === this.historyType)).length;
        },
        filteredHistory() {
            if (!this.history) return [];
            const q = this.historyQuery.trim().toLowerCase();
            return this.history.filter(r =>
                (this.historyStatus === 'all' || this.itemStatus(r) === this.historyStatus) &&
                (this.historyType === 'all' || r.media_type === this.historyType) &&
                (!q || r.title.toLowerCase().includes(q) || String(r.year || '').includes(q) ||
                    r.releases.some(x => (x.title || '').toLowerCase().includes(q) || (x.indexer || '').toLowerCase().includes(q))));
        },
        // Filtered items grouped by removal batch, newest batch first.
        historyGroups() {
            const byBatch = {};
            for (const r of this.filteredHistory()) (byBatch[r.batch_id] = byBatch[r.batch_id] || []).push(r);
            return this.historyBatches
                .filter(b => byBatch[b.id])
                .map(b => ({ ...b, rows: byBatch[b.id], shown_bytes: byBatch[b.id].reduce((t, r) => t + (r.size_bytes || 0), 0) }));
        },
        // Totals for the selected status + type (not the search box).
        historyTotals() {
            const rows = (this.history || []).filter(r =>
                (this.historyStatus === 'all' || this.itemStatus(r) === this.historyStatus) &&
                (this.historyType === 'all' || r.media_type === this.historyType));
            const labels = {
                removed: ['Items removed', 'Space freed'], readding: ['Re-adding', 'Size'],
                restored: ['Restored', 'Size'], all: ['Items', 'Total size'],
            }[this.historyStatus];
            return {
                items: rows.length, bytes: rows.reduce((t, r) => t + (r.size_bytes || 0), 0),
                batches: new Set(rows.map(r => r.batch_id)).size, itemsLabel: labels[0], bytesLabel: labels[1],
            };
        },
        toggleHistoryRow(id) {
            this.historyExpanded[id] = !this.historyExpanded[id];
        },
        formatDateTime(iso) {
            if (!iso) return '';
            return new Date(iso).toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
        },
        seasonLabel(seasons) {
            if (!seasons || !seasons.length) return '';
            return seasons.length === 1 ? `S${String(seasons[0]).padStart(2, '0')}` : `S${seasons.join(', S')}`;
        },
        // Releases for a show: collapse per-episode grabs into one line per season.
        releaseGroups(item) {
            if (item.media_type !== 'show') return item.releases.map(r => ({ label: '', releases: [r] }));
            const groups = {};
            for (const r of item.releases) {
                const key = this.seasonLabel(r.seasons) || 'Other';
                (groups[key] = groups[key] || []).push(r);
            }
            return Object.entries(groups).map(([label, releases]) => ({ label, releases }));
        },
        readdLabel(item) {
            if (item.readd_status === 'restored') return 'Restored ' + new Date(item.restored_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
            if (item.readd_status === 'running') return 'Re-adding…';
            return item.readd_progress || {
                grabbed: 'Waiting for download', partial: 'Partly re-added', searching: 'Searching for a release',
            }[item.readd_status] || '';
        },
        // Themed confirmation dialog. Resolves true/false.
        askConfirm({ title, message = '', details = [], confirmLabel = 'Confirm', cancelLabel = 'Cancel', tone = 'primary' }) {
            this.dialog = { open: true, title, message, details, confirmLabel, cancelLabel, tone };
            this.$nextTick(() => this.$refs.dialogConfirm?.focus());
            return new Promise(resolve => { this._dialogResolve = resolve; });
        },
        closeDialog(result) {
            this.dialog.open = false;
            const resolve = this._dialogResolve;
            this._dialogResolve = null;
            if (resolve) resolve(result);
        },
        async readdItem(item) {
            const what = item.media_type === 'movie' ? 'Radarr' : 'Sonarr';
            const rels = item.releases;
            const indexers = [...new Set(rels.map(r => r.indexer).filter(Boolean))];
            const details = [['Size', this.formatFileSize(item.size_bytes)]];
            if (rels.length === 1) details.push(['Release', rels[0].title, true]);
            else if (rels.length > 1) details.push(['Releases', `${rels.length} (${this.seasonLabel([...new Set(rels.flatMap(r => r.seasons || []))].sort((a, b) => a - b)) || 'various'})`]);
            if (indexers.length) details.push(['From', indexers.join(', ')]);
            const ok = await this.askConfirm({
                title: `Re-add ${item.title}${item.year ? ` (${item.year})` : ''}?`,
                message: rels.length
                    ? `Adds it back to ${what} with its original profile and folder, then downloads the same release again. If that release can't be found, ${what} searches for the best available one.`
                    : `Adds it back to ${what} with its original profile and folder. No original release is on record, so ${what} searches for the best available one.`,
                details,
                confirmLabel: 'Re-add & download',
            });
            if (!ok) return;
            try {
                const response = await fetch(`/api/history/${item.id}/readd`, { method: 'POST' });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Request failed');
                item.readd_status = 'running';
                item.readd_detail = 'Re-adding...';
                this.historyExpanded[item.id] = true;
                this.showSuccess(`Re-adding ${item.title} - follow it under Re-adding`);
                this.pollReadd(item);
            } catch (error) {
                this.showError(`Re-add failed: ${error.message}`);
            }
        },
        async pollReadd(item) {
            for (;;) {
                await new Promise(r => setTimeout(r, 3000));
                try {
                    const response = await fetch(`/api/history/${item.id}/status`);
                    const data = await response.json();
                    Object.assign(item, data.data);
                } catch (error) { continue; }
                if (item.readd_status !== 'running') break;
            }
            // Re-check right away: an import from the download client is already done,
            // and a push shows real queue progress instead of a placeholder.
            if (item.readd_status !== 'failed') await this.loadHistory();
            if (item.readd_status === 'failed') this.showError(`Re-add of ${item.title} failed`);
            else if (/imported it/.test(item.readd_detail || '')) this.showSuccess(`${item.title}: imported from the download client`);
            else if (item.readd_status === 'grabbed') this.showSuccess(`${item.title}: original release sent to the download client`);
            else this.showWarning(`${item.title}: re-added, ${item.readd_status === 'partial' ? 'some seasons' : 'release'} being searched for`);
        },
        async copyText(text, what) {
            // navigator.clipboard needs a secure context (https/localhost); over plain
            // http on the LAN fall back to a hidden textarea + execCommand.
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(text);
                } else {
                    const ta = document.createElement('textarea');
                    ta.value = text;
                    ta.setAttribute('readonly', '');
                    ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
                    document.body.appendChild(ta);
                    ta.select();
                    const ok = document.execCommand('copy');
                    document.body.removeChild(ta);
                    if (!ok) throw new Error('copy failed');
                }
                this.showSuccess(`${what} copied`);
            } catch (e) {
                this.showError(`Couldn't copy ${what.toLowerCase()}`);
            }
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
                    this.analysisAt = Date.now();
                    this._persist('analysisAt', this.analysisAt);
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
            this._persist('connectionStatus', this.connectionStatus);
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
                    this.candidatesAt = Date.now();
                    this._persist('candidatesAt', this.candidatesAt);
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
                    if (this.history) this.loadHistory();
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
