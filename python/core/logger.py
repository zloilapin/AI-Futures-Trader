import os
from datetime import datetime

class TradeLogger:
    """
    Handles hierarchical logging of agent reasoning and trade decisions.
    Structure: logs/MM-1/DD_MM_YYYY/decision_reasons.md
    """
    def __init__(self, base_dir="logs"):
        self.base_dir = base_dir

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

    def log_decision(self, agent_name: str, message: str):
        """
        Appends agent reasoning and decisions to a daily markdown file.
        """
        log_dir = self._get_hierarchical_path()
        file_path = os.path.join(log_dir, "decision_reasons.md")
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"\n### [{timestamp}] {agent_name}\n* **Reasoning:** {message}\n"
        
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(log_entry)
            
        print(f"[Logger] Logged entry for {agent_name} in {file_path}")

    def info(self, message: str):
        """
        Standard info logging method used across all agents and services.
        Maps to log_decision under the hood with 'System' as the actor.
        """
        print(f"[INFO] {message}")
        self.log_decision("System_Core", message)

    def error(self, message: str):
        """
        Standard error logging method.
        """
        print(f"[ERROR] {message}")
        self.log_decision("System_Error", message)

    def warning(self, message: str):
        """
        Standard warning logging method.
        """
        print(f"[WARNING] {message}")
        self.log_decision("System_Warning", message)

if __name__ == "__main__":
    logger = TradeLogger()
    logger.info("System initialized for Nado DEX monitoring.")

