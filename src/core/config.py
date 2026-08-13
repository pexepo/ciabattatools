"""Configuration, read once from the environment at import.

Numbers in the cost section decide whether the product makes money, and they
are easy to get wrong in the same optimistic direction. Two in particular:

* ``MRKT_FEE`` -- the market advertises zero commission but has changed policy
  before. An understated fee inflates every ROI figure shown to the user.
* ``GAS_BUY`` / ``GAS_SELL`` -- at the 2-5 TON lot sizes this tool targets, gas
  costs several times the fee. Omitting it understates the break-even hurdle by
  roughly 4x, which is how a "profitable" snipe loses money.

The transport constants are not tuning knobs. They were measured against the
live market, and the credential at risk is a personal Telegram account.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from src.core.money import Nano

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader.

    Hand-rolled rather than pulling python-dotenv: this runs before any
    dependency is guaranteed installed, and the format we need is two lines of
    parsing. Existing environment variables always win, so a real deployment
    can ignore the file entirely.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(_str(name) or default)
    except ValueError:
        return default


def _dec(name: str, default: str) -> Decimal:
    """Decimal, never float: these values feed price arithmetic."""
    try:
        return Decimal(_str(name) or default)
    except Exception:  # noqa: BLE001 - Decimal raises several types
        return Decimal(default)


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# --- Identity ------------------------------------------------------------
BOT_TOKEN = _str("BOT_TOKEN")
BOT_USERNAME = _str("BOT_USERNAME")
# my.telegram.org credentials. Required to create user sessions and to mint
# MRKT initData; a bot token cannot do either.
TG_API_ID = _int("TG_API_ID")
TG_API_HASH = _str("TG_API_HASH")

OWNER_TG_ID = _int("OWNER_TG_ID")
SUPPORT_CONTACT = _str("SUPPORT_CONTACT", "@awhoreable")

# --- Safety --------------------------------------------------------------
# On by default and deliberately explicit to turn off: every trading path is
# reverse-engineered, so the first run of any new code must not spend money.
DRY_RUN = _bool("DRY_RUN", True)
# Absolute ceiling per single purchase, regardless of what a Ciabatta says.
MAX_SPEND_PER_BUY = Nano.from_ton(_str("MAX_SPEND_PER_BUY_TON") or "5")
# Rolling 24h ceiling across all Ciabattas of one user.
MAX_SPEND_PER_DAY = Nano.from_ton(_str("MAX_SPEND_PER_DAY_TON") or "50")

# --- Secrets -------------------------------------------------------------
# 32 bytes, base64 or hex. Sessions and API keys are useless without it, so
# losing it means every user re-authenticates; leaking it means they are owned.
SECRET_KEY = _str("CIABATTA_SECRET_KEY")

# --- Storage -------------------------------------------------------------
DATABASE_URL = _str(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'data' / 'ciabatta.db'}"
)
REDIS_URL = _str("REDIS_URL", "redis://localhost:6379/0")

# --- Web -----------------------------------------------------------------
# Public HTTPS origin of the mini app. Telegram refuses to open it over plain
# HTTP, so this must be a real certificate in production.
PUBLIC_URL = _str("PUBLIC_URL").rstrip("/")
HOST = _str("HOST", "0.0.0.0")
PORT = _int("PORT", 8080)
# How long an initData blob stays acceptable. Telegram signs auth_date; a long
# window lets a captured blob be replayed.
INITDATA_MAX_AGE_SEC = _int("INITDATA_MAX_AGE_SEC", 3600)

# --- MRKT transport ------------------------------------------------------
MRKT_API = "https://api.tgmrkt.io/api/v1"
MRKT_CDN = "https://cdn.tgmrkt.io/"
MRKT_APP_URL = "https://t.me/mrkt/app"
MRKT_BOT = "mrkt"
MRKT_APP_SHORT_NAME = "app"
# Hand-pasted token, for when a session is not available. Cannot be refreshed.
MRKT_STATIC_TOKEN = _str("MRKT_TOKEN")

# MRKT filters on TLS fingerprint: a plain HTTP client is rejected whatever
# headers it sends. This is the impersonation profile known to work.
IMPERSONATE = _str("MRKT_IMPERSONATE", "chrome124")
# Minimum spacing between MRKT requests, process-wide. Measured, not guessed.
MIN_REQUEST_INTERVAL = float(_dec("MRKT_MIN_INTERVAL", "0.55"))
RATE_LIMIT_COOLDOWN = float(_dec("MRKT_COOLDOWN", "12"))
REQUEST_TIMEOUT = float(_dec("MRKT_TIMEOUT", "20"))
MRKT_PAGE_SIZE = 20  # server-side hard cap on /gifts/saling

# --- Telegram gift links -------------------------------------------------
# Verified against a working implementation: the slug separator is a hyphen.
GIFT_PAGE_TPL = "https://t.me/nft/{slug}"
GIFT_SLUG_TPL = "{base_name}-{number}"

# --- Polling -------------------------------------------------------------
# The tracker has no push channel: layer 227 has no update for gifts that are
# not yours, so the mint counter is polled and only the gap is fetched.
TRACKER_POLL_INTERVAL = float(_dec("TRACKER_POLL_INTERVAL", "4"))
TRACKER_POLL_BACKOFF_MAX = float(_dec("TRACKER_POLL_BACKOFF_MAX", "120"))
MRKT_POLL_INTERVAL = float(_dec("MRKT_POLL_INTERVAL", "5"))
COLLECTIONS_INTERVAL = float(_dec("COLLECTIONS_INTERVAL", "300"))
FLOOR_CACHE_TTL = float(_dec("FLOOR_CACHE_TTL", "60"))
SEEN_TTL_SEC = 7 * 24 * 3600

# --- Costs ---------------------------------------------------------------
# Both default pessimistic and both are meant to be replaced by measured
# values. Understating either makes a losing trade look profitable.
MRKT_FEE = _dec("MRKT_FEE", "0.02")
GAS_BUY = Nano.from_ton(_str("GAS_BUY_TON") or "0.1")
GAS_SELL = Nano.from_ton(_str("GAS_SELL_TON") or "0.1")

# --- Anti-bot detection (auto-ordering) ---------------------------------
ANTIBOT_ENABLED = _bool("ANTIBOT_ENABLED", True)
# Three outbids inside this window means a competing bot is present.
ANTIBOT_WINDOW_SEC = float(_dec("ANTIBOT_WINDOW_SEC", "60"))
ANTIBOT_OUTBID_LIMIT = _int("ANTIBOT_OUTBID_LIMIT", 3)
# "pause" waits out ANTIBOT_PAUSE_SEC; "stop" halts the Ciabatta entirely.
ANTIBOT_ACTION = _str("ANTIBOT_ACTION", "pause")
ANTIBOT_PAUSE_SEC = float(_dec("ANTIBOT_PAUSE_SEC", "600"))

# --- Pricing sources ----------------------------------------------------
# Optional. An absent key must degrade to Telegram + MRKT floors, never crash.
GIFT_SATELLITE_API = _str("GIFT_SATELLITE_API", "")

LOG_LEVEL = _str("LOG_LEVEL", "INFO")


class ConfigError(RuntimeError):
    """Raised when the process cannot safely start."""


def require_runtime() -> None:
    """Fail fast on the values without which nothing works.

    Called from entrypoints, not at import: tests and tooling import this
    module without a populated environment.
    """
    missing = [
        name
        for name, value in (
            ("BOT_TOKEN", BOT_TOKEN),
            ("TG_API_ID", TG_API_ID),
            ("TG_API_HASH", TG_API_HASH),
            ("CIABATTA_SECRET_KEY", SECRET_KEY),
            ("PUBLIC_URL", PUBLIC_URL),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "missing required settings: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )
    if not PUBLIC_URL.startswith("https://"):
        raise ConfigError(
            "PUBLIC_URL must be https:// for Telegram to open the mini app, "
            f"got {PUBLIC_URL!r}"
        )
