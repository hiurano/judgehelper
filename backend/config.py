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
LOGS_DIR = Path(os.environ.get("LOGS_DIR") or BACKEND_DIR / "logs")
LOGS_DIR.mkdir(parents=True, exist_ok=True)
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
log = logging.getLogger("judgehelper")

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


def parse_origins(raw: str) -> list[str]:
    """Split a comma-separated origin list, ignoring padding and empty items."""
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


ALLOWED_ORIGINS = parse_origins(os.environ.get("ALLOWED_ORIGINS", ""))
MODEL = os.environ.get("LLM_MODEL", "openai/gpt-4o-mini")
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DB_PATH = os.environ.get("DB_PATH") or str(BACKEND_DIR / "data" / "jobs.db")
JOB_TTL_DAYS = int(os.environ.get("JOB_TTL_DAYS", "30"))
# Owner recorded for a job written without one. Never an authorization
# fallback: handlers refuse rather than assume an account (see current_user).
DEFAULT_USER = os.environ.get("DEFAULT_USER", "admin")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
# Rendering is a paragraph at a time in pure Python: measured at ~2.6 s for
# 500k characters and ~9.6 s for two million. A hearing's protocol runs to tens
# of thousands, so a larger figure does not buy a longer protocol — it only
# lengthens how long one request can hold a worker thread.
#
# This is a ceiling rather than a default because deployments already carry
# the old two-million figure in their env file, written there by an earlier
# version of .env.example. Refusing to start on it, the way this module treats
# a bad SECRET_KEY, would take a running site down on its next deploy; ignoring
# it would leave the limit that was raised in review still in force. So the
# configured value is honoured up to what the renderer can sustain, and the
# difference is logged rather than applied quietly.
MAX_RENDER_TEXT_CHARS_CEILING = 500_000


def resolve_render_limit(configured: int, ceiling: int = MAX_RENDER_TEXT_CHARS_CEILING) -> int:
    """Honour the configured limit, up to what the renderer can sustain."""
    if configured > ceiling:
        log.warning(
            "MAX_RENDER_TEXT_CHARS is set to %s; using %s, the most the document "
            "renderer can produce without holding a worker thread for seconds. "
            "Lower it in the env file to silence this.",
            configured, ceiling,
        )
        return ceiling
    return configured


_configured_render_chars = int(
    os.environ.get("MAX_RENDER_TEXT_CHARS", str(MAX_RENDER_TEXT_CHARS_CEILING))
)
MAX_RENDER_TEXT_CHARS = resolve_render_limit(_configured_render_chars)
MAX_ACTIVE_JOBS_PER_USER = int(os.environ.get("MAX_ACTIVE_JOBS_PER_USER", "3"))
# A job holds one of the user's active slots until it reaches `done` or `error`,
# and nothing else ever moves it off `processing`: AssemblyAI can drop a
# transcript, and a worker can die between two writes. Three jobs stuck that way
# leave the account unable to upload at all, so give up on one eventually.
# Generous on purpose — transcription and drafting together take minutes.
JOB_MAX_LIFETIME_HOURS = int(os.environ.get("JOB_MAX_LIFETIME_HOURS", "6"))
for _name, _value in (
    ("JOB_TTL_DAYS", JOB_TTL_DAYS),
    ("MAX_UPLOAD_BYTES", MAX_UPLOAD_BYTES),
    ("MAX_RENDER_TEXT_CHARS", _configured_render_chars),
    ("MAX_ACTIVE_JOBS_PER_USER", MAX_ACTIVE_JOBS_PER_USER),
    ("JOB_MAX_LIFETIME_HOURS", JOB_MAX_LIFETIME_HOURS),
):
    if _value <= 0:
        raise ValueError(f"{_name} must be greater than zero")

# Models tried in order; first success wins.
# Cross-provider fallbacks can change the processor of sensitive court data.
# They are therefore opt-in and must be explicitly listed by the operator.
_fallback_models = [
    item.strip()
    for item in os.environ.get("LLM_FALLBACK_MODELS", "").split(",")
    if item.strip()
]
LLM_FALLBACK_CHAIN: list[str] = list(dict.fromkeys([MODEL, *_fallback_models]))

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
    "УК РФ", "УПК РФ", "статья", "часть", "пункт",
    "явка с повинной", "вещественные доказательства", "апелляционная жалоба",
]
# Court-specific terms (city, court name, region) stay out of the repo:
# comma-separated in WORD_BOOST_EXTRA.
WORD_BOOST += [
    item.strip()
    for item in os.environ.get("WORD_BOOST_EXTRA", "").split(",")
    if item.strip()
]
