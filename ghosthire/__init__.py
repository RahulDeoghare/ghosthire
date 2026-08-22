"""GhostHire — detects job listings advertised on boards that the hiring
company does not list on its own careers page."""

from pathlib import Path

__version__ = "0.1.0"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
FIXTURE_DIR = DATA_DIR / "fixtures"
HEAL_DIR = DATA_DIR / "heal"
SCRAPERS_DIR = ROOT / "scrapers"
WEB_DIR = ROOT / "web"
