"""
Configuration module for Judge Helper backend.
Handles environment variables, logging, path constants, and global settings.
"""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).parent
PROJECT_DIR = BACKEND_DIR.parent
LOGS_DIR = BACKEND_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
LOG_FILE = LOGS_DIR / "app.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"),
    ],
    force=True,
)
log = logging.getLogger("judge-helper")

try:
    from dotenv import load_dotenv
    env_file = PROJECT_DIR / ".env"
    load_dotenv(env_file)
except ImportError:
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

ASSEMBLYAI_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
MODEL = os.environ.get("LLM_MODEL", "openai/gpt-4o-mini")
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DB_PATH = os.environ.get("DB_PATH") or str(BACKEND_DIR / "data" / "jobs.db")
JOB_TTL_DAYS = int(os.environ.get("JOB_TTL_DAYS", "30"))
DEFAULT_USER = os.environ.get("DEFAULT_USER", "admin")
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB

# Models tried in order; first success wins.
LLM_FALLBACK_CHAIN: list[str] = list(dict.fromkeys([
    MODEL,
    "openai/gpt-4o-mini",
    "google/gemini-2.5-flash",
    "anthropic/claude-3-haiku",
]))

SYSTEM_PROMPT_PATH = PROJECT_DIR / "prompts" / "system-protocol.md"
SYSTEM_PROMPT_EXAMPLE_PATH = PROJECT_DIR / "prompts" / "system-protocol.md.example"
STATIC_DIR = BACKEND_DIR / "static"

for name, val in [
    ("ASSEMBLYAI_API_KEY", ASSEMBLYAI_KEY),
    ("OPENROUTER_API_KEY", OPENROUTER_KEY),
]:
    if not val:
        log.warning(f"{name} not set — endpoint calls will fail")
if not BASE_URL:
    log.warning("BASE_URL not set — AssemblyAI webhooks disabled, /status will poll instead")

_cached_prompt = ""
_cached_prompt_mtime = 0.0

def get_system_prompt() -> str:
    global _cached_prompt, _cached_prompt_mtime
    
    path_to_check = SYSTEM_PROMPT_PATH if SYSTEM_PROMPT_PATH.exists() else SYSTEM_PROMPT_EXAMPLE_PATH
    if not path_to_check.exists():
        log.error(f"System prompt not found at {SYSTEM_PROMPT_PATH} or {SYSTEM_PROMPT_EXAMPLE_PATH}")
        return ""
        
    try:
        mtime = path_to_check.stat().st_mtime
        if mtime > _cached_prompt_mtime:
            _cached_prompt = path_to_check.read_text(encoding="utf-8")
            _cached_prompt_mtime = mtime
            log.info(f"Loaded/Reloaded system prompt: {len(_cached_prompt)} chars from {path_to_check}")
    except Exception as e:
        log.error(f"Error reading system prompt: {e}")
        
    return _cached_prompt

# Initial load on startup
get_system_prompt()


WORD_BOOST = [
    "ходатайство", "определение суда", "прения сторон", "последнее слово",
    "мера пресечения", "подсудимый", "потерпевший", "защитник",
    "государственный обвинитель", "приговор", "судебное заседание",
    "разъяснение прав", "примирение сторон", "процессуальные издержки",
    "Нижневартовск", "Нижневартовский городской суд",
    "Югра", "Ханты-Мансийский автономный округ",
    "УК РФ", "УПК РФ", "статья", "часть", "пункт",
    "явка с повинной", "вещественные доказательства", "апелляционная жалоба",
]
