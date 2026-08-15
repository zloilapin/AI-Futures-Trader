import os
from dataclasses import dataclass

@dataclass
class Config:
    """Глобальные настройки проекта и API ключи."""
    # API Ключи
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", ""))
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    KIE_API_KEY: str = os.getenv("KIE_API_KEY", "")
    KIE_MODEL: str = os.getenv("KIE_MODEL", "DeepSeek-V3")
    
    # Биржи
    TRADING_ENGINE: str = os.getenv("TRADING_ENGINE", "PAPER").upper()
    KRAKEN_API_KEY: str = os.getenv("KRAKEN_API_KEY", "")
    KRAKEN_API_SECRET: str = os.getenv("KRAKEN_API_SECRET", "")
    NADO_DEX_URL: str = os.getenv("NADO_DEX_URL", "https://app.nado.xyz/perpetuals")
    LIVE_TRADING_ENABLED: bool = os.getenv("LIVE_TRADING_ENABLED", "False").lower() == "true"
    
    # Телеграм
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    PUBLIC_CHANNEL_ID: str = os.getenv("PUBLIC_CHANNEL_ID", "")
    
    # Торговые настройки
    TRADING_PROFILE: str = os.getenv("TRADING_PROFILE", "BALANCED").upper()
    LEVERAGE: int = int(os.getenv("LEVERAGE", "10"))
    STARTING_BALANCE: float = float(os.getenv("STARTING_BALANCE", "60.0"))
    SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
    
    # Настройки времени сна
    TIMEZONE_OFFSET: int = int(os.getenv("TIMEZONE_OFFSET", "3"))
    REST_START_TIME: str = os.getenv("REST_START_TIME", "19:00")
    REST_END_TIME: str = os.getenv("REST_END_TIME", "07:00")
    
    # Настройки логирования
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    # Лимиты риск-менеджера
    MAX_MARGIN_PCT: float = float(os.getenv("MAX_MARGIN_PCT", "0.5"))
    MAX_CONCURRENT_POSITIONS: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "2"))
    
    # Расширенные лимиты Risk Manager
    MIN_SL_PCT: float = 0.012
    MIN_TP_PCT: float = 0.036
    MIN_NOTIONAL: float = 15.0
    SPREAD_PENALTY_THRESHOLD: float = 0.4
    SPREAD_VETO_THRESHOLD: float = 1.0

# Глобальный экземпляр конфига для импорта в другие модули
config = Config()
