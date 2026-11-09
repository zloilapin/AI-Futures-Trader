import json
from typing import Dict, Any, List

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class UniverseAgent(BaseAgent):
    """
    The macro-filter agent. Scans the broader market to select the most liquid 
    and promising trading pairs, filtering out low-volume or scam tokens.
    Operates deterministically by default (<0.001s, 0 tokens), with optional LLM override.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient = None):
        super().__init__("Universe_Agent", logger, llm_client)
        
        import os
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "universe_prompt.txt")
        if os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        else:
            self.system_instruction = ""

    def select_deterministic_universe(self, broad_market_data: Dict[str, Any], limit: int = 7) -> Dict[str, Any]:
        """
        Fast deterministic universe selection (< 0.001s, 0 tokens).
        Algorithm:
        1. Select Core Majors (BTC, ETH, SOL) if present in the active perps.
        2. Rank remaining altcoins by liquidity (volumeQuote * atr_pct if present, or volumeQuote).
        3. Fill up to limit (default 7).
        """
        trending_perps = broad_market_data.get("trending_perps") or []
        if not trending_perps:
            # Safe default fallback
            default_majors = ["BTC-USD", "ETH-USD", "SOL-USD"]
            return {
                "selected_pairs": default_majors,
                "reasoning": "Резервный детерминированный выбор: базовые поводыри рынка (BTC, ETH, SOL)."
            }

        # 1. Normalize available symbols
        majors_priority = ["BTC-USD", "ETH-USD", "SOL-USD"]
        selected_majors = []
        altcoins = []

        for p in trending_perps:
            sym = p.get("symbol", "")
            base = sym.replace('/', '-').split('-')[0].upper()
            canonical_sym = f"{base}-USD"
            
            vol = float(p.get("volumeQuote") or 0.0)
            atr_pct = float(p.get("atr_pct") or 1.0)
            score = vol * atr_pct
            
            p_entry = {
                "symbol": canonical_sym,
                "volumeQuote": vol,
                "atr_pct": atr_pct,
                "score": score
            }
            
            if canonical_sym in majors_priority:
                selected_majors.append(p_entry)
            else:
                altcoins.append(p_entry)

        # Sort majors by priority order
        selected_majors.sort(key=lambda x: majors_priority.index(x["symbol"]) if x["symbol"] in majors_priority else 999)
        major_symbols = [m["symbol"] for m in selected_majors]

        # Sort altcoins by score descending
        altcoins.sort(key=lambda x: x["score"], reverse=True)
        remaining_slots = max(0, limit - len(major_symbols))
        selected_alts = [a["symbol"] for a in altcoins[:remaining_slots]]

        final_selection = major_symbols + selected_alts
        reasoning = (
            f"Детерминированный отбор (0 токенов, <0.001с): "
            f"{len(major_symbols)} лидеров ({', '.join(major_symbols)}) + "
            f"{len(selected_alts)} ликвидных альткоинов ({', '.join(selected_alts)})."
        )

        return {
            "selected_pairs": final_selection,
            "reasoning": reasoning
        }

    async def analyze(self, broad_market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluates the broad market to select the active trading universe.
        Runs deterministically by default (0 tokens, <0.001s).
        """
        self.logger.info(f"[{self.name}] Сканирование широкого рынка для отбора активов...")
        
        from core.config import config
        use_llm = getattr(config, "USE_LLM_UNIVERSE", False) and bool(self.llm_client)

        if not use_llm:
            res = self.select_deterministic_universe(broad_market_data)
            self.logger.info(f"[{self.name}] {res['reasoning']}")
            return res

        # LLM fallback / override mode
        data_string = json.dumps(broad_market_data, indent=2)
        full_prompt = f"{self.system_instruction}\n\nBroad Market Data:\n{data_string}"
        return await self.generate_json(full_prompt, required_keys=["selected_pairs", "reasoning"])
