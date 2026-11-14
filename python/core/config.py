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
    OPENROUTER_MODEL_CHEAP: str = Field(default="deepseek/deepseek-chat")
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
    MIN_NOTIONAL_MAX_BUMP_RISK_PCT: float = Field(
        default=0.038,
        description="Maximum risk as fraction of deposit allowed when bumping small notional to exchange CLOB min_size ($104.00). Defaults to 3.8% (allowing standard SL on small deposits)."
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
        default=True,
        description="Enable multi-trader copy trading for connected subscribers (default: True)"
    )
    TELEGRAM_MINIAPP_URL: str = Field(
        default="https://zloilapin.github.io/AI-Futures-Trader/",
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
# ⚡ Bayesian Tuned Hyperparameters Loader with Hot-Reload and Invariant Schema Bounds
# ═════════════════════════════════════════════════════════════════════════════

SCHEMA_VERSION: int = 2

PARAMETER_INVARIANTS: Dict[str, Dict[str, Any]] = {
    "half_risk_activation_r": {
        "bounds": (0.50, 0.95),
        "default": 0.75,
        "type": float,
    },
    "be_activation_r": {
        "bounds": (1.10, 1.40),
        "default": 1.15,
        "type": float,
        "relational_check": lambda p: p.get("be_activation_r", 1.15) >= p.get("half_risk_activation_r", 0.75) + 0.35,
        "heal": lambda p: max(p.get("be_activation_r", 1.15), p.get("half_risk_activation_r", 0.75) + 0.35)
    },
    "trail_activation_r": {
        "bounds": (1.40, 2.50),
        "default": 1.50,
        "type": float,
        "relational_check": lambda p: p.get("trail_activation_r", 1.50) >= p.get("be_activation_r", 1.15) + 0.25,
        "heal": lambda p: max(p.get("trail_activation_r", 1.50), p.get("be_activation_r", 1.15) + 0.25)
    },
    "chop_range_threshold": {"bounds": (50.0, 72.0), "default": 59.0, "type": float},
    "anti_chop_bb_max": {"bounds": (68.0, 90.0), "default": 89.0, "type": float},
    "min_reduce_size_conv": {"bounds": (60.0, 75.0), "default": 71.0, "type": float},
    "trailing_atr_mult": {"bounds": (1.50, 3.00), "default": 2.00, "type": float},
    "min_kelly_mult": {"bounds": (0.50, 1.00), "default": 1.00, "type": float},
}

# Hard safety bounds (Safety Clamps) that Optuna / tuned values can never exceed
_SAFETY_BOUNDS: Dict[str, Tuple[float, float]] = {
    k: v["bounds"] for k, v in PARAMETER_INVARIANTS.items()
}

_TUNED_CACHE: Dict[str, Any] = {"mtime": 0.0, "params": {}}


def validate_and_clamp_params(
    params: Dict[str, Any],
    auto_heal_disk: bool = False,
    source_path: Optional[Path] = None
) -> Dict[str, Any]:
    """
    Centralized Parameter Invariant Validator (Module 04 Guard):
    Clamps parameters to hard [min_val, max_val] bounds and enforces relational invariants:
      1. be_activation_r in [1.10, 1.40] and be_activation_r >= half_risk_activation_r + 0.35
      2. trail_activation_r in [1.40, 2.50] and trail_activation_r >= be_activation_r + 0.25
    If auto_heal_disk is True and violations or schema version mismatches are found, heals disk file.
    """
    if not isinstance(params, dict):
        return {}

    clamped = dict(params)
    modified = False

    # 1. Individual Bounds Clamping
    for key, spec in PARAMETER_INVARIANTS.items():
        if key in clamped:
            try:
                val = float(clamped[key])
                lo, hi = spec["bounds"]
                bounded_val = max(lo, min(hi, val))
                if abs(bounded_val - val) > 1e-6:
                    print(f"⚠️ [ConfigGuard] Clamped parameter '{key}' from {val} to {bounded_val} within bounds [{lo}, {hi}].")
                    clamped[key] = bounded_val
                    modified = True
            except (ValueError, TypeError):
                clamped[key] = spec["default"]
                modified = True

    # 2. Relational Invariants (half_risk -> be -> trail)
    if "half_risk_activation_r" in clamped or "be_activation_r" in clamped or "trail_activation_r" in clamped:
        hr = float(clamped.get("half_risk_activation_r", 0.75))
        lo_hr, hi_hr = PARAMETER_INVARIANTS["half_risk_activation_r"]["bounds"]
        hr = max(lo_hr, min(hi_hr, hr))
        if abs(hr - float(clamped.get("half_risk_activation_r", 0.75))) > 1e-6:
            clamped["half_risk_activation_r"] = hr
            modified = True

        be = float(clamped.get("be_activation_r", 1.15))
        lo_be, hi_be = PARAMETER_INVARIANTS["be_activation_r"]["bounds"]
        be = max(lo_be, min(hi_be, be))
        min_be = hr + 0.35
        if be < min_be:
            be = min_be
            clamped["be_activation_r"] = be
            modified = True
            print(f"⚠️ [ConfigGuard] Healed relational invariant: be_activation_r set to {be:.2f} (>= half_risk {hr:.2f} + 0.35).")
        clamped["be_activation_r"] = be

        tr = float(clamped.get("trail_activation_r", 1.50))
        lo_tr, hi_tr = PARAMETER_INVARIANTS["trail_activation_r"]["bounds"]
        tr = max(lo_tr, min(hi_tr, tr))
        min_tr = be + 0.25
        if tr < min_tr:
            tr = min_tr
            clamped["trail_activation_r"] = tr
            modified = True
            print(f"⚠️ [ConfigGuard] Healed relational invariant: trail_activation_r set to {tr:.2f} (>= be {be:.2f} + 0.25).")
        clamped["trail_activation_r"] = tr

    # 3. Disk Auto-Heal
    if auto_heal_disk and source_path and source_path.exists():
        try:
            raw_text = source_path.read_text(encoding="utf-8")
            raw_json = json.loads(raw_text)
            schema_ver = raw_json.get("schema_version", 1)
            if modified or schema_ver < SCHEMA_VERSION:
                raw_json["schema_version"] = SCHEMA_VERSION
                if "best_params" in raw_json and isinstance(raw_json["best_params"], dict):
                    raw_json["best_params"].update(clamped)
                elif "params" in raw_json and isinstance(raw_json["params"], dict):
                    raw_json["params"].update(clamped)
                else:
                    raw_json["best_params"] = clamped
                source_path.write_text(json.dumps(raw_json, indent=4), encoding="utf-8")
                print(f"✅ [ConfigGuard] Auto-healed parameter schema to v{SCHEMA_VERSION} on disk: {source_path}")
        except Exception as e:
            print(f"⚠️ [ConfigGuard] Error auto-healing {source_path}: {e}")

    return clamped


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
    via mtime and strict bounding within PARAMETER_INVARIANTS.
    """
    resolved_path = _resolve_tuned_path()
    if resolved_path is not None and resolved_path.exists():
        try:
            mtime = resolved_path.stat().st_mtime
            if mtime > _TUNED_CACHE.get("mtime", 0.0):
                raw = json.loads(resolved_path.read_text(encoding="utf-8"))
                params = raw.get("best_params") or raw.get("params") or {}
                if isinstance(params, dict):
                    clamped = validate_and_clamp_params(params, auto_heal_disk=True, source_path=resolved_path)
                    _TUNED_CACHE["params"] = clamped
                    _TUNED_CACHE["mtime"] = mtime
        except Exception:
            pass

    val = float(_TUNED_CACHE.get("params", {}).get(key, default))
    if key in _SAFETY_BOUNDS:
        lo, hi = _SAFETY_BOUNDS[key]
        val = max(lo, min(hi, val))
    return val

