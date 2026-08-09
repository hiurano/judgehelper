"""
Configuration module for Judge Helper backend.
Handles environment variables, logging, path constants, and global settings.
"""
import logging
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
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
)
log = logging.getLogger("judge-helper")

from dotenv import load_dotenv

# Load .env
env_file = PROJECT_DIR / ".env"
load_dotenv(env_file)

ASSEMBLYAI_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
MODEL = os.environ.get("LLM_MODEL", "deepseek/deepseek-v4-flash-0731")
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DB_PATH = os.environ.get("DB_PATH") or str(BACKEND_DIR / "data" / "jobs.db")
JOB_TTL_DAYS = int(os.environ.get("JOB_TTL_DAYS", "30"))

# Models tried in order; first success wins.
LLM_FALLBACK_CHAIN: list[str] = list(dict.fromkeys([
    MODEL,
    "deepseek/deepseek-v4-flash-0731",
    "google/gemini-2.5-flash",
    "openai/gpt-4.1-nano",
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

SYSTEM_PROMPT = ""
if SYSTEM_PROMPT_PATH.exists():
    SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    log.info(f"Loaded system prompt: {len(SYSTEM_PROMPT)} chars from {SYSTEM_PROMPT_PATH}")
elif SYSTEM_PROMPT_EXAMPLE_PATH.exists():
    SYSTEM_PROMPT = SYSTEM_PROMPT_EXAMPLE_PATH.read_text(encoding="utf-8")
    log.info(f"Loaded example system prompt: {len(SYSTEM_PROMPT)} chars from {SYSTEM_PROMPT_EXAMPLE_PATH}")
else:
    log.error(f"System prompt not found at {SYSTEM_PROMPT_PATH} or {SYSTEM_PROMPT_EXAMPLE_PATH}")


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
