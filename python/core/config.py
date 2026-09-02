import os
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal

class Settings(BaseSettings):
    """Глобальные настройки проекта и API ключи, с валидацией типов."""
    
    # API Ключи
    GEMINI_API_KEY: str = Field(default="", validation_alias="GEMINI_API_KEY")
    GOOGLE_API_KEY: str = Field(default="", validation_alias="GOOGLE_API_KEY")
    GROQ_API_KEY: str = Field(default="", validation_alias="GROQ_API_KEY")
    OPENROUTER_API_KEY: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    KIE_API_KEY: str = Field(default="", validation_alias="KIE_API_KEY")
    KIE_MODEL: str = Field(default="DeepSeek-V3")
    
    # Web3 / Nado
    INK_PRIVATE_KEY: SecretStr = Field(default=SecretStr(""))
    NADO_NETWORK: Literal["TESTNET", "MAINNET"] = Field(default="TESTNET")
    
    LIVE_TRADING_ENABLED: bool = Field(default=True)
    NADO_LIVE_TRADING_ENABLED: bool = Field(default=True)
    
    # Телеграм
    TELEGRAM_BOT_TOKEN: str = Field(default="")
    TELEGRAM_CHAT_ID: str = Field(default="")
    PUBLIC_CHANNEL_ID: str = Field(default="")
    
    # Торговые настройки
    TRADING_PROFILE: Literal["AGGRESSIVE", "BALANCED", "CONSERVATIVE"] = Field(default="BALANCED")
    LEVERAGE: int = Field(default=10, ge=1, le=100)
    STARTING_BALANCE: float = Field(default=60.0)
    SCAN_INTERVAL_MINUTES: int = Field(default=5, ge=1)
    
    # Настройки времени сна
    TIMEZONE_OFFSET: int = Field(default=3)
    REST_START_TIME: str = Field(default="24:00")
    REST_END_TIME: str = Field(default="00:00")
    
    # Настройки логирования
    LOG_LEVEL: str = Field(default="INFO")
    
    # Лимиты риск-менеджера
    MAX_CONCURRENT_POSITIONS: int = Field(default=2, ge=1)
    
    # Расширенные лимиты Risk Manager
    MIN_SL_PCT: float = Field(default=0.025)
    MIN_TP_PCT: float = Field(default=0.075)
    SPREAD_PENALTY_THRESHOLD: float = Field(default=0.4)
    SPREAD_VETO_THRESHOLD: float = Field(default=1.0)

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
