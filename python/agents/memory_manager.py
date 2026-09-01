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
            from core.state_store import StateStore
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(self.storage_path, f"cycle_{timestamp}.json")
            
            StateStore.save(filename, cycle_data)
                
            self.logger.info(f"[{self.name}] Данные цикла успешно сохранены в {filename}")
            
            # File Rotation: keep only the latest 100 cycle logs
            files = [f for f in os.listdir(self.storage_path) if f.startswith('cycle_') and f.endswith('.json')]
            if len(files) > 100:
                files.sort() # Oldest first because of timestamp naming
                for old_file in files[:-100]:
                    try:
                        os.remove(os.path.join(self.storage_path, old_file))
                    except Exception:
                        pass
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
            from core.state_store import StateStore
            for file in files[:limit]:
                filepath = os.path.join(self.storage_path, file)
                data = StateStore.load(filepath)
                if data:
                    context.append(data)
                    
            return context
        except Exception as e:
            self.logger.error(f"[{self.name}] Ошибка при чтении исторического контекста: {e}")
            return []
                    
