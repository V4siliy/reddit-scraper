# Reddit Scraper

A universal, config-driven Reddit scraper that collects and analyzes posts based on YAML profiles. Each profile defines its own subreddits, search queries, date range, and extraction categories — no code changes needed to research a new topic.

## Setup

```bash
uv sync
```

Requires a Reddit API application. Create one at [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) (script type, any redirect URI).

## Usage

**Interactive profile selection:**
```bash
uv run scrape.py --client-id YOUR_ID --client-secret YOUR_SECRET
```

**Direct profile:**
```bash
uv run scrape.py --client-id YOUR_ID --client-secret YOUR_SECRET --profile profiles/ai_side_projects.yaml
uv run scrape.py --client-id YOUR_ID --client-secret YOUR_SECRET --profile profiles/ai_tools_landscape.yaml
```

**Override profile settings on the fly:**
```bash
# Custom date range
uv run scrape.py --client-id ID --client-secret SECRET \
  --profile profiles/ai_side_projects.yaml \
  --date-from 2026-03-01 --date-to 2026-03-31

# Disable date filtering (all time within the Reddit time window)
uv run scrape.py --client-id ID --client-secret SECRET \
  --profile profiles/ai_side_projects.yaml \
  --no-date-filter --time-filter month

# Specific subreddits only
uv run scrape.py --client-id ID --client-secret SECRET \
  --profile profiles/ai_side_projects.yaml \
  --subreddits SideProject indiehackers
```

### All options

| Flag | Default | Description |
|------|---------|-------------|
| `--profile` | interactive | Path to a YAML profile file |
| `--limit` | 30 | Max posts per query per subreddit |
| `--workers` | 5 | Parallel subreddit workers |
| `--time-filter` | from profile | `week` / `month` / `year` / `all` |
| `--date-from` | from profile | Keep posts on/after this date (YYYY-MM-DD) |
| `--date-to` | from profile | Keep posts on/before this date (YYYY-MM-DD) |
| `--no-date-filter` | — | Disable date filtering entirely |
| `--subreddits` | from profile | Override subreddit list |
| `--output` | results_YYYYMMDD_HHmmss_xxxxxx.json | Analysis output path |
| `--raw-output` | raw_posts_YYYYMMDD_HHmmss_xxxxxx.json | Raw posts output path |

## Outputs

Each run produces a uniquely named pair of files (timestamp + 6-char hex, e.g. `results_20260405_143022_a1b2c3.json`) so previous runs are never overwritten. Use `--output` / `--raw-output` to set explicit names.

- **`results_<run_id>.json`** — ranked tables for each extraction category, top 30 posts, and notable quotes
- **`raw_posts_<run_id>.json`** — full post data with all extracted fields per post

## Profiles

Profiles live in `profiles/` and are auto-discovered at startup.

### Included profiles

| Profile | Focus | Subreddits | Extractions |
|---------|-------|------------|-------------|
| `ai_side_projects.yaml` | AI-assisted side project launches and first-user stories | 14 | AI tools, tech stack, acquisition channels, problem domains |
| `ai_tools_landscape.yaml` | Developer discussions, comparisons, and reviews of AI coding tools | 10 | AI tools, use cases, sentiment, underlying models |

### Creating a custom profile

Add a new `.yaml` file to `profiles/` and it will appear automatically in the selection menu.

```yaml
name: "My Research Topic"
description: "What this profile tracks"
time_filter: "week"          # week / month / year / all
date_from: null              # null = no filter, or "YYYY-MM-DD"
date_to: null
min_score: 1                 # minimum post score to include

subreddits:
  - SubredditName

search_queries:
  - "query string one"
  - "query string two"

post_types:                  # optional: classify posts with binary tags
  launch:
    - '\bjust launched\b'
    - '\bjust shipped\b'

extractions:                 # define as many categories as needed
  my_category:
    display: "Display Name"
    terms:
      term_key:
        name: "Term Display Name"
        patterns:
          - '\bregex pattern\b'

notable_quotes:
  min_score: 3
  trigger_extractions:       # which categories make a comment "notable"
    - my_category
```

All regex patterns use Python `re` syntax and are matched case-insensitively. Use single-quoted YAML strings to avoid backslash escaping issues.

## Architecture

Single-file scraper (`scrape.py`). `main.py` is an unused scaffold.

**Data flow:** YAML profile → compiled `Config` → Reddit API (PRAW) → parallel subreddit scraping → post-type tagging + structured extraction → ranked analysis → JSON outputs

**Key functions:**

| Function | Description |
|----------|-------------|
| `load_profile(path)` | Parses YAML and compiles all regex patterns into a `Config` |
| `select_profile(dir)` | Interactive numbered menu for profile selection |
| `extract_category(text, cat)` | Generic extraction against any `ExtractionCategory` |
| `classify_post_types(title, body, config)` | Tags posts using config-defined patterns |
| `scrape_subreddit(...)` | Searches one subreddit, filters by date, extracts comments |
| `analyze_results(posts, config)` | Counts and ranks all extraction categories |
| `display_analysis(analysis, config)` | Rich terminal output with tables and notable quotes |

Subreddits are scraped in parallel using `ThreadPoolExecutor` (default: 5 workers). Deduplication by post ID happens after all futures complete.
