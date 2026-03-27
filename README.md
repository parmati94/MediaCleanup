# MediaCleanup

A modern web-based interface for intelligently managing and removing unwatched media from your Plex library via Radarr/Sonarr integration.

## Overview

MediaCleanup helps you reclaim disk space by identifying and removing unwatched or stale media from your Plex library. With powerful filtering options, detailed analytics, and multiple safety mechanisms, you can confidently manage your media collection.

## ✨ Key Features

### 📊 **Smart Dashboard**
- Quick overview of library statistics
- Current configuration impact analysis
- Potential space savings calculator
- Active filter summary with visual cards

### 🔍 **Advanced Analysis (Tabbed Insights)**
- **Top Space Consumers**: Largest items by file size with play counts
- **Removal Candidates**: Never-watched items sorted by size
- **Stale Content**: Items not watched in over a year
- **Age Distribution**: Visual breakdown of unwatched content by age

### ⚙️ **Flexible Configuration**
- Web-based settings editor
- Advanced filter options (collapsible)
- Protected keywords management
- Separate controls for movies and TV shows

### 👁️ **Interactive Preview**
- Sortable, searchable removal candidate list
- Checkbox selection for targeted removal
- File size and last watched date display
- Dry run mode indicator

### 🛡️ **Multiple Safety Layers**
- **Dry Run Mode**: Test without actually removing files
- **Selection Required**: Prevents accidental mass deletion
- **Batch Warnings**: Confirms large operations (>10 items)
- **Max Items Limit**: Backend enforcement of maximum items per run
- **Toast Notifications**: Clear feedback for all operations

### 🎨 **Modern UI/UX**
- Built with Alpine.js and Tailwind CSS
- Responsive design with dark mode support
- Smooth transitions and animations
- Toast notifications (no more ugly alerts!)
- Single-page application experience

## 🚀 Quick Start

### Prerequisites
- Docker and Docker Compose installed
- Running Tautulli instance with API access
- Radarr and/or Sonarr instances (for media removal)
- Tautulli file size calculation enabled (optional but recommended)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/parmati94/MediaCleanup.git
   cd MediaCleanup
   ```

2. **Build and run with Docker Compose:**
   ```bash
   docker-compose up -d
   ```

3. **Access the UI:**
   Open your browser to `http://localhost:5180`

### First-Time Setup

1. **Configure Services** (Configuration tab)
   - Enter your Tautulli URL and API key
   - Add Radarr URL and API key (for movies)
   - Add Sonarr URL and API key (for TV shows)
   - Save configuration

2. **Set Removal Criteria** (Configuration tab)
   - Days Unwatched: How long since last watched
   - Min Days Since Added: Minimum age in library
   - Require Zero Play Count: Only remove never-watched items
   - Enable Dry Run Mode for testing

3. **Analyze Your Library** (Analysis or Dashboard tab)
   - Click "Refresh Analysis" to scan your library
   - Review space consumers, removal candidates, and stale content

4. **Preview & Execute** (Preview tab)
   - Review the list of items marked for removal
   - Use checkboxes to select specific items
   - Verify Dry Run Mode badge if testing
   - Execute removal (with confirmation)

### Enable File Size Tracking (Recommended)
In Tautulli: Settings → General → Tick "Calculate Total File Sizes for Library Statistics"

## ⚙️ Configuration

All settings are managed through the web UI Configuration tab, but you can also edit `data/config.yaml` directly.

### Core Settings

| Setting | Description | Example |
|---------|-------------|---------|
| `days_unwatched` | Days since last watched | `365` |
| `min_days_since_added` | Minimum age in library | `30` |
| `require_zero_play_count` | Only remove never-watched | `true` |
| `process_movies` | Enable movie removal | `true` |
| `process_tv_shows` | Enable TV show removal | `true` |

### Advanced Filters

- **Protected Keywords**: Comma-separated list of keywords to protect from removal
- **Never Watched Only**: Ignore any items with play count > 0
- **Minimum Play Count**: Additional play count threshold

### Safety Settings

| Setting | Description | Default |
|---------|-------------|---------|
| `dry_run` | Test mode (no actual deletion) | `true` |
| `require_confirmation` | Require explicit confirmation | `true` |
| `max_items_per_run` | Limit items per execution | `200` |

## 🛡️ Safety Features

MediaCleanup includes multiple safety layers to prevent accidental deletion:

1. **Dry Run Mode**: Enabled by default - logs all actions without deleting files
2. **Explicit Selection Required**: Frontend prevents removal without checkbox selection
3. **Backend Validation**: Server validates selection and enforces limits
4. **Large Batch Warnings**: Toast notification for operations >10 items
5. **Max Items Enforcement**: Hard limit on items per run (default: 200)
6. **Toast Notifications**: Clear, visible feedback for all operations (success, error, warnings)
7. **Protected Keywords**: Automatically skip items matching protected terms

## 🔧 Customization

### Port Configuration
- Default port: `5180`
- Change in `docker-compose.yml` under `ports` section

### Data Persistence
Configuration and logs are stored in the `./data` directory:
- `data/config.yaml` - Configuration file
- Container volume mount ensures data persists across restarts

## 🐛 Troubleshooting

### File sizes showing as N/A
Enable file size calculation in Tautulli (Settings → General)

### Items not appearing in Preview
- Verify your filter settings (days unwatched, min days since added)
- Check that items match your criteria (zero play count if required)
- Ensure Tautulli API is accessible

### Docker build fails
- Clear Docker cache: `docker-compose build --no-cache`
- Ensure ports are not in use: `docker ps`

## 📝 License

MIT

## 🙏 Credits

Originally based on a Python script workflow, rebuilt as a modern web application with enhanced UI/UX and safety features.Development

To run in development mode:

```bash
# Backend
cd backend
python -m uvicorn main:app --reload --port 8000

# Frontend  
cd frontend
npm install
npm run dev
```

## Port

- Default port: `5180`
- Change in `docker-compose.yml` if needed

## License

MIT
