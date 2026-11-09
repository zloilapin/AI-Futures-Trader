import os
import json
from pathlib import Path
from typing import Literal, Dict, Any, Optional, Tuple
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """Глобальные настройки проекта и API ключи, с валидацией типов."""
    
    # API Ключи
    GEMINI_API_KEY: str = Field(default="", validation_alias="GEMINI_API_KEY")
    GOOGLE_API_KEY: str = Field(default="", validation_alias="GOOGLE_API_KEY")
    GROQ_API_KEY: str = Field(default="", validation_alias="GROQ_API_KEY")
    OPENROUTER_API_KEY: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    OPENROUTER_MODEL: str = Field(default="deepseek/deepseek-chat")
    OPENROUTER_MODEL_CHEAP: str = Field(default="google/gemini-2.5-flash-lite")
    OPENROUTER_MODEL_CEO: str = Field(default="deepseek/deepseek-chat")
    OPENROUTER_MODEL_ESCALATION: str = Field(default="meta-llama/llama-3.3-70b-instruct")
    ESCALATE_WAIT_FOR_PULLBACK: bool = Field(default=False)
    USE_LLM_UNIVERSE: bool = Field(default=False)
    ENABLE_MAKER_LIMIT_ENTRY: bool = Field(default=True)
    ENABLE_DYNAMIC_TP2_RUNNER: bool = Field(default=True)
    KIE_API_KEY: str = Field(default="", validation_alias="KIE_API_KEY")
    KIE_MODEL: str = Field(default="DeepSeek-V3")
    
    # Web3 / Nado
    INK_PRIVATE_KEY: SecretStr = Field(default=SecretStr(""))
    NADO_NETWORK: Literal["TESTNET", "MAINNET"] = Field(default="TESTNET")
    SHADOW_MODE: bool = Field(default=False, description="Simulate execution without sending on-chain transactions")
    ANTI_BOT_MODE: bool = Field(
        default=False,
        description="Predator / Anti-Bot Mode targeting bots at Bollinger/Donchian extremes"
    )
    AUTO_PREDATOR_SWITCH: bool = Field(
        default=True,
        description="Auto-switch ANTI_BOT_MODE based on volatility and regime"
    )
    ALLOW_FORBIDDEN_STRATEGIES: bool = Field(
        default=False,
        description="Allow forbidden regime/strategy combinations (e.g. Trend-Following in Transition, Breakouts in Chop) for research/shadow exploration"
    )
    FAST_DEBATE_BYPASS: bool = Field(
        default=False,
        description="Bypass LLM debate and use deterministic analyst thesis for ultra-fast execution"
    )
    MIN_NOTIONAL_FLOOR_USD: float = Field(
        default=35.0,
        description="Minimum order notional in USD before auto-bumping to exchange CLOB min_size ($104.00). Orders below this are vetoed as micro-dust."
    )
    
    LIVE_TRADING_ENABLED: bool = Field(default=False)
    NADO_LIVE_TRADING_ENABLED: bool = Field(default=False)
    TRADING_ENGINE: str = Field(default="NADO")
    NADO_BUILDER_ID: int = Field(
        default=0,
        ge=0,
        le=65535,
        description="Nado Builder Program ID (0 if not registered/unassigned)"
    )
    NADO_BUILDER_FEE_RATE: int = Field(
        default=0,
        ge=0,
        le=1023,
        description="Nado Builder Fee Rate in tenths of bps (0-1023)"
    )
    
    # Реферальная программа Nado DEX
    NADO_REFERRAL_CODE: str = Field(
        default="ptn3l1u",
        description="Nado DEX Referral Code for 10% fee rebate and affiliate rewards"
    )
    NADO_REFERRAL_URL: str = Field(
        default="https://app.nado.xyz?join=ptn3l1u",
        description="Nado DEX Referral Link with 10% trading fee discount"
    )
    
    # Мульти-аккаунты и Telegram Mini App (Copy-Trading)
    COPY_TRADING_ENABLED: bool = Field(
        default=False,
        description="Enable multi-trader copy trading for connected subscribers (default: False)"
    )
    TELEGRAM_MINIAPP_URL: str = Field(
        default="https://raw.githack.com/zloilapin/AI-Futures-Trader/main/web/tg_miniapp_link_signer.html",
        description="Public HTTPS URL for the Telegram Mini App (TMA) LinkSigner frontend"
    )
    
    # Телеграм
    TELEGRAM_BOT_TOKEN: str = Field(default="")
    TELEGRAM_CHAT_ID: str = Field(default="")
    PUBLIC_CHANNEL_ID: str = Field(default="")
    
    # Торговые настройки
    TRADING_PROFILE: Literal["AGGRESSIVE", "BALANCED", "CONSERVATIVE"] = Field(default="BALANCED")
    STARTING_BALANCE: float = Field(default=60.0)
    SCAN_INTERVAL_MINUTES: int = Field(default=15, ge=1)
    MAX_CYCLES: int = Field(default=0, ge=0)
    SENTINEL_INTERVAL_SECONDS: int = Field(default=30, ge=5)
    SENTINEL_COOLDOWN_SECONDS: int = Field(default=600, ge=10)
    FAST_RADAR_INTERVAL_SECONDS: int = Field(default=45, ge=10)
    
    # Настройки Sentinel (Risk Control)
    SENTINEL_BE_ATR: float = Field(default=1.5)
    SENTINEL_TRAIL_ACTIVATION_ATR: float = Field(default=2.5)
    SENTINEL_TRAIL_DISTANCE_ATR: float = Field(default=2.0)
    SENTINEL_MIN_IMPROVE_ATR: float = Field(default=0.25)
    SENTINEL_FAST_POLL_SEC: int = Field(default=5, ge=1)
    
    # Настройки времени сна
    TIMEZONE_OFFSET: int = Field(default=3)
    REST_START_TIME: str = Field(default="19:00")
    REST_END_TIME: str = Field(default="07:00")
    
    # Настройки логирования
    LOG_LEVEL: str = Field(default="INFO")
    
    # Лимиты риск-менеджера
    MAX_CONCURRENT_POSITIONS: int = Field(default=3, ge=1)
    MAX_ACCOUNT_POSITIONS: int = Field(default=4, ge=1)
    
    # Флаг активности Reflector Agent (пост-мортем анализ и инъекция прошлых правил)
    ENABLE_REFLECTOR_AGENT: bool = Field(default=False)
    
    # Режим сетки частичного Take-Profit ("2_TIER" = 50/50 с Б/У на TP1, "4_TIER" = 25/25/25/25, "SINGLE_TIER" = 100%)
    PARTIAL_TP_MODE: Literal["2_TIER", "4_TIER", "SINGLE_TIER"] = Field(default="2_TIER")
    
    # Расширенные лимиты Risk Manager
    MIN_SL_PCT: float = Field(default=0.01)    # 1.0% floor (was 2.5% — dominated ATR for stable coins)
    MIN_TP_PCT: float = Field(default=0.03)    # 3.0% floor (was 7.5%)
    SPREAD_PENALTY_THRESHOLD: float = Field(default=0.4)
    SPREAD_VETO_THRESHOLD: float = Field(default=1.0)
    
    # Блокировка сделок Когорты D (низкая уверенность + REDUCE_SIZE) из-за отрицательного матожидания E(R) < 0
    BLOCK_COHORT_D_TRADES: bool = Field(default=True)

    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8", 
        extra="ignore"
    )

    @property
    def NADO_DEX_URL(self) -> str:
        return "https://testnet.app.nado.xyz/perpetuals" if self.NADO_NETWORK == "TESTNET" else "https://app.nado.xyz/perpetuals"

    @property
    def GEMINI_KEY(self) -> str:
        return self.GEMINI_API_KEY or self.GOOGLE_API_KEY

# Глобальный экземпляр конфига для импорта в другие модули
try:
    config = Settings()
except Exception as e:
    print(f"❌ [Config] Ошибка конфигурации среды: {e}")
    # Fallback for when we're just parsing AST or initializing docs without .env
    config = Settings(_env_file=None)


# ═════════════════════════════════════════════════════════════════════════════
# ⚡ Bayesian Tuned Hyperparameters Loader with Hot-Reload and Safety Clamps
# ═════════════════════════════════════════════════════════════════════════════

_TUNED_CACHE: Dict[str, Any] = {"mtime": 0.0, "params": {}}

# Hard safety bounds (Safety Clamps) that Optuna / tuned values can never exceed
_SAFETY_BOUNDS: Dict[str, Tuple[float, float]] = {
    "chop_range_threshold": (50.0, 72.0),
    "anti_chop_bb_max": (68.0, 90.0),
    "min_reduce_size_conv": (60.0, 75.0),
    "be_activation_r": (0.60, 1.35),
    "trailing_atr_mult": (1.50, 3.00),
    "min_kelly_mult": (0.50, 1.00),
}


def _resolve_tuned_path() -> Optional[Path]:
    """Resolves the active path to tuned_hyperparams.json across test overrides and roots."""
    try:
        from core.state_store import StateStore
        if StateStore._base_dir_override:
            normalized = StateStore._normalize_path("data/tuned_hyperparams.json")
            p = Path(normalized)
            return p if p.exists() else None
        normalized = StateStore._normalize_path("data/tuned_hyperparams.json")
        p = Path(normalized)
        if p.exists():
            return p
    except Exception:
        pass

    for candidate in [Path("python/data/tuned_hyperparams.json"), Path("data/tuned_hyperparams.json")]:
        if candidate.exists():
            return candidate
    return None


def reset_tuned_param_cache() -> None:
    """Resets in-memory cached hyperparameters (for test isolation and forced reload)."""
    global _TUNED_CACHE
    _TUNED_CACHE = {"mtime": 0.0, "params": {}}


def get_tuned_param(key: str, default: float) -> float:
    """
    Reads calibrated hyperparameter from tuned_hyperparams.json with hot reloading
    via mtime and strict bounding within _SAFETY_BOUNDS.
    """
    resolved_path = _resolve_tuned_path()
    if resolved_path is not None and resolved_path.exists():
        try:
            mtime = resolved_path.stat().st_mtime
            if mtime > _TUNED_CACHE.get("mtime", 0.0):
                raw = json.loads(resolved_path.read_text(encoding="utf-8"))
                params = raw.get("best_params") or raw.get("params") or {}
                if isinstance(params, dict):
                    _TUNED_CACHE["params"] = params
                    _TUNED_CACHE["mtime"] = mtime
        except Exception:
            pass

    val = float(_TUNED_CACHE.get("params", {}).get(key, default))
    if key in _SAFETY_BOUNDS:
        lo, hi = _SAFETY_BOUNDS[key]
        val = max(lo, min(hi, val))
    return val

