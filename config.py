import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    deepseek_api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))
    deepseek_base_url: str = field(default_factory=lambda: os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    deepseek_model: str = field(default_factory=lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-chat"))

    brave_api_key: str = field(default_factory=lambda: os.getenv("BRAVE_API_KEY", ""))

    # Google AI Mode discovery (Selenium-driven, non-headless by default --
    # headless triggers Google's reCAPTCHA bot-detection instantly, a real
    # visible browser with a persistent profile does not). Off by default
    # since it opens a visible browser window and is much slower than the
    # HTTP-based sources; enable explicitly when needed.
    google_ai_mode_enabled: bool = field(default_factory=lambda: _bool("GOOGLE_AI_MODE_ENABLED", False))
    google_ai_mode_headless: bool = field(default_factory=lambda: _bool("GOOGLE_AI_MODE_HEADLESS", False))
    # When true and google_ai_mode_enabled is also true, AI Mode becomes the
    # ONLY discovery source -- DDG/Wikipedia/Wikidata/directory-mining are
    # skipped entirely rather than used as backfill. Explicit user choice:
    # accepts slower runs and fewer companies per run in exchange for
    # avoiding DDG's anti-bot blocking issues altogether.
    google_ai_mode_only: bool = field(default_factory=lambda: _bool("GOOGLE_AI_MODE_ONLY", False))
    google_ai_mode_max_queries: int = field(default_factory=lambda: _int("GOOGLE_AI_MODE_MAX_QUERIES", 3))
    # How many real Chromium windows are allowed open at once for discovery
    # and verification. Each one is a full browser process -- on a low-spec
    # laptop (e.g. an older i3 with 8GB RAM, the actual hardware this tool
    # is distributed on) too many at once makes the whole machine unusable
    # while a run is in progress, not just slow. Defaults to 2 as a safe
    # baseline; raise it in .env on a faster machine for quicker runs.
    google_ai_mode_max_parallel_browsers: int = field(
        default_factory=lambda: _int("GOOGLE_AI_MODE_MAX_PARALLEL_BROWSERS", 2)
    )

    max_concurrent_crawls: int = field(default_factory=lambda: _int("MAX_CONCURRENT_CRAWLS", 10))
    max_concurrent_classifications: int = field(default_factory=lambda: _int("MAX_CONCURRENT_CLASSIFICATIONS", 8))
    http_timeout_seconds: int = field(default_factory=lambda: _int("HTTP_TIMEOUT_SECONDS", 15))
    playwright_timeout_seconds: int = field(default_factory=lambda: _int("PLAYWRIGHT_TIMEOUT_SECONDS", 25))
    min_verification_confidence: int = field(default_factory=lambda: _int("MIN_VERIFICATION_CONFIDENCE", 40))
    discovery_target_companies: int = field(default_factory=lambda: _int("DISCOVERY_TARGET_COMPANIES", 200))
    discovery_max_widen_rounds: int = field(default_factory=lambda: _int("DISCOVERY_MAX_WIDEN_ROUNDS", 4))

    admin_emails: tuple = field(
        default_factory=lambda: tuple(
            e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()
        )
    )

    cache_dir: Path = ROOT / "cache"
    outputs_dir: Path = ROOT / "outputs"
    logs_dir: Path = ROOT / "logs"

    def ensure_dirs(self) -> None:
        for d in (self.cache_dir, self.outputs_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
CONFIG.ensure_dirs()
