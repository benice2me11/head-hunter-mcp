from pathlib import Path
import os

from dotenv import load_dotenv

if os.environ.get("HH_ENV_FILE"):
    load_dotenv(os.environ["HH_ENV_FILE"], override=False)

TOOL_TIMEOUT_SECONDS: float = 90.0
BASE_URL = "https://hh.ru"
STATE_FILE = Path(os.environ.get("HH_STATE_FILE", str(Path.home() / ".hh-mcp" / "profile" / "state.json"))).expanduser().resolve()
PROFILE_DIR = STATE_FILE.parent
DRAFT_DIR = PROFILE_DIR / "drafts"

AREA_CODES = {
    "москва": "1",
    "санкт-петербург": "2",
    "екатеринбург": "3",
    "новосибирск": "4",
    "нижний новгород": "66",
    "казань": "88",
    "россия": "113",
}

SCHEDULE_MAP = {
    "remote": "remote",
    "office": "fullDay",
    "hybrid": "flyInFlyOut",
    "flexible": "flexible",
    "shift": "shift",
}

EXPERIENCE_MAP = {
    "no_experience": "noExperience",
    "1-3": "between1And3",
    "3-6": "between3And6",
    "6+": "moreThan6",
}
