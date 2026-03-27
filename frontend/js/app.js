/**
 * Main entry point for Vite bundling
 * Imports Alpine.js and initializes the app
 */
import Alpine from 'alpinejs';
import '../css/style.css';

// Expose Alpine globally BEFORE any DOM parsing
window.Alpine = Alpine;

// Alpine.js app data
function appData() {
    return {
        currentTab: 'dashboard',
        analysisSection: 'space-consumers',
        loading: false,
        config: null,
        analysis: null,
        candidates: null,
        searchQuery: '',
        confirmRemoval: false,
        protectedKeywordsText: '',
        selectedCandidates: [],
        sortColumn: 'title',
        sortDirection: 'asc',
        toasts: [],

        async init() {
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
                    // Convert protected keywords array to text
                    if (this.config.media?.filters?.protected_keywords) {
                        this.protectedKeywordsText = this.config.media.filters.protected_keywords.join('\n');
                    }
                }
            } catch (error) {
                console.error('Error loading config:', error);
                this.showError('Failed to load configuration');
            } finally {
                this.loading = false;
            }
        },
        
        updateProtectedKeywords() {
            // Convert text to array, filtering out empty lines
            if (this.config.media?.filters) {
                this.config.media.filters.protected_keywords = this.protectedKeywordsText
                    .split('\n')
                    .map(k => k.trim())
                    .filter(k => k.length > 0);
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

        async runAnalysis(includeThresholds) {
            this.loading = true;
            try {
                const response = await fetch('/api/analyze', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        summary_only: false,
                        test_thresholds: includeThresholds
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
            }
        },

        async loadCandidates() {
            this.loading = true;
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
            }
        },
        
        toggleSelectAll() {
            const filtered = this.filteredCandidates();
            if (this.selectedCandidates.length === filtered.length) {
                this.selectedCandidates = [];
            } else {
                this.selectedCandidates = filtered.map(c => c.rating_key);
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
                        rating_keys: itemsToRemove
                    }),
                });
                const data = await response.json();
                if (data.success) {
                    const isDryRun = this.config?.safety?.dry_run;
                    if (isDryRun) {
                        this.showWarning(`DRY RUN: Preview completed. ${itemsToRemove.length} item(s) would be removed (no files were actually deleted)`);
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
