"""Settings read from environment variables. No secret has a default and none is ever returned by the API."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
    _root_env = Path(__file__).resolve().parents[2] / ".env"
    if _root_env.exists():
        load_dotenv(_root_env)
except Exception:
    pass


def _csv(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in value.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/workbench.db"
    openrouter_api_key: str = ""
    primary_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
    fallback_models: tuple[str, ...] = ("nvidia/nemotron-3.5-lightning:free", "nvidia/nemotron-3-super-120b-a12b:free")
    llm_timeout: float = 60.0
    llm_max_retries: int = 1
    agent_max_rounds: int = 20
    app_url: str = "http://localhost:8000"
    demo_users: tuple[str, ...] = ("alice", "bob")
    cors_origins: tuple[str, ...] = ()
    log_level: str = "INFO"
    frontend_dir: str = ""
    enable_test_runner: bool = True
    llm_enable_fallbacks: bool = True
    agent_policy_mode: str = "propose_defaults"  # or "strict": keep unresolved policies that block approval

    @staticmethod
    def from_env(overrides: dict | None = None) -> "Settings":
        e = os.environ
        s = Settings(
            database_url=e.get("DATABASE_URL", "sqlite:///./data/workbench.db"),
            openrouter_api_key=e.get("OPENROUTER_API_KEY", "").strip(),
            primary_model=e.get("OPENROUTER_PRIMARY_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free").strip(),
            fallback_models=_csv(e.get("OPENROUTER_FALLBACK_MODELS", "nvidia/nemotron-3.5-lightning:free,nvidia/nemotron-3-super-120b-a12b:free")),
            llm_timeout=float(e.get("OPENROUTER_TIMEOUT", "60")),
            llm_max_retries=int(e.get("OPENROUTER_MAX_RETRIES", "1")),
            agent_max_rounds=int(e.get("AGENT_MAX_ROUNDS", "20")),
            app_url=e.get("APP_URL", "http://localhost:8000"),
            demo_users=_csv(e.get("DEMO_USERS", "alice,bob")),
            cors_origins=_csv(e.get("CORS_ORIGINS", "")),
            log_level=e.get("LOG_LEVEL", "INFO").upper(),
            frontend_dir=e.get("FRONTEND_DIR", ""),
            llm_enable_fallbacks=(
                e.get("OPENROUTER_ENABLE_FALLBACKS", "1").strip().lower()
                in ("1", "true", "yes", "on")
            ),
            enable_test_runner=e.get("ENABLE_TEST_RUNNER", "1") == "1",
            agent_policy_mode=e.get("AGENT_POLICY_MODE", "propose_defaults").strip().lower(),
        )
        return replace(s, **overrides) if overrides else s

    @property
    def llm_configured(self) -> bool:
        return bool(self.openrouter_api_key)
