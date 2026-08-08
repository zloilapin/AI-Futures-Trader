import os
from dataclasses import dataclass

@dataclass
class Config:
    """Глобальные настройки проекта и API ключи."""
    # API Ключи (загружаются из виртуального окружения или файла .env)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    
    # Настройки DEX
    NADO_DEX_URL: str = os.getenv("NADO_DEX_URL", "https://api.nado.exchange")
    
    # Настройки логирования
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    # Лимиты риск-менеджера
    MAX_POSITION_SIZE_USDT: float = 100.0
    MAX_LOSS_PER_TRADE_USDT: float = 5.0

# Глобальный экземпляр конфига для импорта в другие модули
config = Config()
