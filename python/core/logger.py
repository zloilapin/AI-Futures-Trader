import os
import json
import structlog
from datetime import datetime

class TradeLogger:
    """
    Handles hierarchical logging of agent reasoning and trade decisions.
    Uses structlog for structured JSON lines output.
    """
    def __init__(self, base_dir="logs"):
        self.base_dir = base_dir
        
        # Configure structlog for JSON output to files
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(ensure_ascii=False)
            ],
            wrapper_class=structlog.make_filtering_bound_logger(20), # INFO
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=False
        )

    def _get_hierarchical_path(self) -> str:
        """
        Ensures the day folder is created strictly inside the month-1 folder.
        """
        now = datetime.now()
        month_folder = f"{now.strftime('%m')}-1"    
        day_folder = now.strftime("%d_%m_%Y")       
        
        path = os.path.join(self.base_dir, month_folder, day_folder)
        os.makedirs(path, exist_ok=True)
        return path

    def log_decision(self, agent_name: str, message: str, **kwargs):
        """
        Appends agent reasoning and decisions to a daily JSONL file.
        """
        log_dir = self._get_hierarchical_path()
        file_path = os.path.join(log_dir, "decisions.jsonl")
        
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "agent": agent_name,
            "message": message,
            **kwargs
        }
        
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_data, ensure_ascii=False) + "\n")
            
        print(f"[{agent_name}] {message}")

    def info(self, message: str, **kwargs):
        """
        Standard info logging method used across all agents and services.
        Maps to log_decision under the hood with 'System' as the actor.
        """
        self.log_decision("System_Core", message, level="info", **kwargs)

    def error(self, message: str, **kwargs):
        """
        Standard error logging method.
        """
        self.log_decision("System_Error", message, level="error", **kwargs)

    def warning(self, message: str, **kwargs):
        """
        Standard warning logging method.
        """
        self.log_decision("System_Warning", message, level="warning", **kwargs)

if __name__ == "__main__":
    logger = TradeLogger()
    logger.info("System initialized for Nado DEX monitoring.")
