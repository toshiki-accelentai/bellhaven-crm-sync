"""Every business knob lives here so it can be changed live without touching logic."""
import os

from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("BH_BASE", "https://analyst-assessment-production.up.railway.app").rstrip("/")
API = f"{BASE}/api/v1"
TOKEN = os.environ.get("BH_TOKEN", "")
DB_PATH = os.environ.get("BH_DB", "data/bhsync.db")
# Read-only CRM browser page for an account (the browser URL embeds the token, so it is built, never committed).
CRM_URL = os.environ.get("BH_CRM_URL", f"{BASE}/crm/{TOKEN}/accounts/" + "{id}")

PARENT_NAME = "Bellhaven Senior Living (Parent Account)"
PARENT_ID = "0015QAPLGS3FVYEEEM"

# Website care offering -> CRM care_type.
CARE_MAP = {
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
    "Memory Support": "Memory Care",
    "Assisted Living": "Assisted Living",
    "Independent Living": "Independent Living",
}

# Survivor ranking for duplicate groups, applied in order (first difference wins).
SURVIVOR_RULES = ("has_revenue", "contacts", "under_parent", "name_similarity", "lowest_id")

# Scrape completeness gate: max drop vs. previous run before STALE proposals are withheld.
MAX_LOCATION_DROP = 2

NOTE_PREFIX = "[bhsync {date}]"
