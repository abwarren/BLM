# BLM — Research Console


## blm/
├── BLM_CONSTITUTION.md   # Core architecture & philosophy
├── PLANNING.md            # Roadmap, ledger, TB plans
├── ROADMAP.md             # Short roadmap reference
├── .gitignore
├── README.md
├── database.py            # SQLite schema + queries
├── collector.py           # Playwright scraper + snapshot loop
├── app.py                 # Flask API
└── static/
    ├── index.html         # Research console
    ├── style.css          # Styling (dark theme)
    └── script.js          # Auto-refresh + chart

## One command to run

```bash
cd ~/projects/blm
python3 app.py
```

This starts the Playwright scraper (background thread) and Flask API (main thread).
Open http://localhost:5000 in a browser to see the research console.
