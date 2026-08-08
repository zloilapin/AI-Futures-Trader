import json
import os
from datetime import datetime
from typing import Dict, Any, List

from core.logger import TradeLogger

class MemoryManager:
    """
    Acts as the historian for the AI-Futures-Trader system.
    Persists cycle data (verdicts, market state, risk decisions) and retrieves 
    historical context to help the CEO Agent adapt to changing market regimes.
    """
    def __init__(self, logger: TradeLogger, storage_path: str = "data/memory/"):
        self.logger = logger
        self.storage_path = storage_path
        self.name = "Memory_Manager"
        
        # Создаем папку для хранения истории, если её еще нет
        if not os.path.exists(self.storage_path):
            os.makedirs(self.storage_path)
            self.logger.info(f"[{self.name}] Создана директория для памяти: {self.storage_path}")

    def save_cycle(self, cycle_data: Dict[str, Any]) -> None:
        """
        Сохраняет результаты полного торгового цикла (отчеты аналитиков, решение CEO, риск-менеджмент) в JSON.
        """
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(self.storage_path, f"cycle_{timestamp}.json")
            
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(cycle_data, f, indent=4, ensure_ascii=False)
                
            self.logger.info(f"[{self.name}] Данные цикла успешно сохранены в {filename}")
        except Exception as e:
            self.logger.error(f"[{self.name}] Ошибка при сохранении данных цикла: {e}")

    def get_recent_context(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Извлекает данные последних N циклов. Эту функцию будет вызывать main.py, 
        чтобы передать историю сделок в CEO_Agent перед принятием нового решения.
        """
        context = []
        try:
            # Ищем все json файлы в папке
            files = [f for f in os.listdir(self.storage_path) if f.endswith('.json')]
            # Сортируем от самых новых к старым (т.к. в имени дата и время)
            files.sort(reverse=True)
            
            # Читаем только последние `limit` файлов
            for file in files[:limit]:
                filepath = os.path.join(self.storage_path, file)
                with open(filepath, "r", encoding="utf-8") as f:
                    context.append(json.load(f))
                    
            return context
        except Exception as e:
            self.logger.error(f"[{self.name}] Ошибка при чтении исторического контекста: {e}")
            return []
                    
