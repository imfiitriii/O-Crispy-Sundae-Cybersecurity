import os

from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
# Used when the main model is overloaded, rate-limited or too slow. Leave empty in .env to disable.
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()
# Seconds to wait for one Gemini model before moving on to the backup.
GEMINI_TIMEOUT_S = float(os.getenv("GEMINI_TIMEOUT_S", "12"))
FACTCHECK_API_KEY = os.getenv("FACTCHECK_API_KEY", "").strip()

DEEPFAKE_MODEL = os.getenv("DEEPFAKE_MODEL", "prithivMLmods/Deep-Fake-Detector-v2-Model").strip()
AI_IMAGE_MODEL = os.getenv("AI_IMAGE_MODEL", "Ateeqq/ai-vs-human-image-detector").strip()
LOAD_LOCAL_MODELS = os.getenv("LOAD_LOCAL_MODELS", "1").strip() not in ("0", "false", "no")

DB_PATH = os.getenv("DB_PATH", "scrollsafe.db")

# Bump when models or scoring change, so old verdicts can be re-checked.
MODEL_VERSION = "scrollsafe-0.2"

MAX_FRAMES = 6
FRAME_MAX_SIDE = 512
# Reports needed before a video is raised to at least "yellow".
REPORT_THRESHOLD = 3
# Max Hamming distance between perceptual hashes to treat two videos as the same.
PHASH_MAX_DISTANCE = 6

# Scoring thresholds (0-1). Tune these on your own test videos; see scoring.py.
RED_AT = float(os.getenv("RED_AT", "0.75"))
DEEPFAKE_YELLOW_AT = float(os.getenv("DEEPFAKE_YELLOW_AT", "0.6"))
AI_YELLOW_AT = float(os.getenv("AI_YELLOW_AT", "0.6"))
AI_WITH_RISK_RED_AT = float(os.getenv("AI_WITH_RISK_RED_AT", "0.6"))
# When Gemini isn't available, a local model alone must be this sure to give yellow.
LOCAL_ONLY_YELLOW_AT = float(os.getenv("LOCAL_ONLY_YELLOW_AT", "0.9"))
