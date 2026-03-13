import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

MERLIN_API_URL = "www.getmerlin.in"
MERLIN_PATH = "/arcane/api/v2/thread/unified"
FIREBASE_AUTH_HOST = "identitytoolkit.googleapis.com"
FIREBASE_AUTH_PATH = "/v1/accounts:signInWithPassword"
FIREBASE_REFRESH_HOST = "securetoken.googleapis.com"
FIREBASE_REFRESH_PATH = "/v1/token"
FIREBASE_API_KEY = os.getenv("MERLIN_FIREBASE_API_KEY", "AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM")
MERLIN_EMAIL = os.getenv("MERLIN_EMAIL")
MERLIN_PASSWORD = os.getenv("MERLIN_PASSWORD")
MERLIN_VERSION = os.getenv("MERLIN_VERSION", "iframe-merlin-7.5.19")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "sk-123")
LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_TO_FILE = os.getenv("LOG_TO_FILE", "true").lower() in {"1", "true", "yes", "on"}
PROJECT_DIR = Path(__file__).resolve().parent.parent
LOG_FILE_PATH = PROJECT_DIR / "logs" / "proxy.log"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 3
TOKEN_REFRESH_BUFFER_SECONDS = 60
TOOL_PROMPT_MAX_MESSAGES = max(int(os.getenv("TOOL_PROMPT_MAX_MESSAGES", "5")), 1)
TOOL_DESCRIPTION_MAX_CHARS = max(int(os.getenv("TOOL_DESCRIPTION_MAX_CHARS", "160")), 0)
TOOL_MESSAGE_MAX_CHARS = max(int(os.getenv("TOOL_MESSAGE_MAX_CHARS", "1200")), 0)
