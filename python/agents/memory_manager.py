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
    _seq = 0

    def __init__(self, logger: TradeLogger, storage_path: str = "data/memory/"):
        self.logger = logger
        # Normalize to always be relative to python/ directory
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if storage_path.startswith("python/") or storage_path.startswith("python\\"):
            storage_path = storage_path[7:]
        self.storage_path = os.path.join(base_dir, storage_path)
        self.name = "Memory_Manager"
        
        # Создаем папку для хранения истории, если её еще нет
        if not os.path.exists(self.storage_path):
            os.makedirs(self.storage_path)
            self.logger.info(f"[{self.name}] Создана директория для памяти: {self.storage_path}")

    def save_cycle(self, cycle_data: Dict[str, Any]) -> None:
        """
        Сохраняет результаты полного торгового цикла (отчеты аналитиков, решение CEO, риск-менеджмент) в JSON.
        """
        if not isinstance(cycle_data, dict):
            return

        try:
            from core.state_store import StateStore
            sym = str(cycle_data.get("symbol", "")).replace('/', '-').split('-')[0].upper()
            suffix = f"_{sym}" if sym else ""
            MemoryManager._seq += 1
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = os.path.join(self.storage_path, f"cycle_{timestamp}_{MemoryManager._seq:06d}{suffix}.json")
            
            if "timestamp" not in cycle_data:
                cycle_data["timestamp"] = datetime.now().isoformat()

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

    def get_recent_context(self, limit: int = 5, symbol: str = None, compact: bool = True) -> List[Dict[str, Any]]:
        """
        Извлекает данные последних N циклов. Эту функцию вызывает pipeline, 
        чтобы передать историю сделок в CEO_Agent перед принятием нового решения.
        Поддерживает приоритизацию по конкретному символу и компактную форму для экономии токенов.
        """
        context = []
        try:
            if not os.path.exists(self.storage_path):
                return []

            files = [f for f in os.listdir(self.storage_path) if f.startswith('cycle_') and f.endswith('.json')]
            files.sort(reverse=True) # Newest first
            
            from core.state_store import StateStore
            candidate_files = []

            if symbol:
                base_sym = str(symbol).replace('/', '-').split('-')[0].upper()
                matching_files = [f for f in files if f"_{base_sym}." in f or f"_{base_sym}_" in f]
                other_files = [f for f in files if f not in matching_files]
                candidate_files = matching_files[:limit]
                if len(candidate_files) < limit:
                    candidate_files.extend(other_files[:limit - len(candidate_files)])
            else:
                candidate_files = files[:limit]

            for file in candidate_files:
                filepath = os.path.join(self.storage_path, file)
                data = StateStore.load(filepath)
                if not data or not isinstance(data, dict):
                    continue

                if compact:
                    ceo_dec = data.get("ceo_decision")
                    compact_dec = {
                        "decision": ceo_dec.get("decision"),
                        "conviction": ceo_dec.get("conviction"),
                        "trade_action": ceo_dec.get("trade_action"),
                        "reasoning": str(ceo_dec.get("reasoning", ""))[:200]
                    } if isinstance(ceo_dec, dict) else ceo_dec

                    risk_dec = data.get("risk_assessment")
                    compact_risk = {
                        "approved": risk_dec.get("approved"),
                        "trade_action": risk_dec.get("trade_action"),
                        "rejection_reason": risk_dec.get("rejection_reason")
                    } if isinstance(risk_dec, dict) else risk_dec

                    compact_cycle = {
                        "symbol": data.get("symbol"),
                        "status": data.get("status"),
                        "timestamp": data.get("timestamp", file.replace("cycle_", "").replace(".json", "")[:15]),
                        "ceo_decision": compact_dec,
                        "risk_assessment": compact_risk,
                        "execution_result": data.get("execution_result")
                    }
                    context.append(compact_cycle)
                else:
                    context.append(data)
                    
            return context
        except Exception as e:
            self.logger.error(f"[{self.name}] Ошибка при чтении исторического контекста: {e}")
            return []
                    
