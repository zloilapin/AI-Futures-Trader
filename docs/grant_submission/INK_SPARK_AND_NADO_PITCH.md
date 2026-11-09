# Ink Spark Grant & Nado Builder Pitch Package
**Project Name:** AI-Futures-Trader on Nado DEX (Ink L2)  
**Track:** Priority Nado DEX Ecosystem Integrations & Autonomous AI Agent Infrastructure  
**Target Chain:** Ink L2 (Chain ID: `57073` Mainnet, `763373` Sepolia Testnet)  
**Primary On-Chain Sub-Account:** [`0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa`](https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa)  
**Repository:** `https://github.com/your-org/AI-Futures-Trader`  
**License:** MIT (Milestones 1 & 3 Open-Source Tooling)  

---

## Executive Summary

**AI-Futures-Trader** is an open, non-custodial multi-agent algorithmic trading terminal and risk-engine built specifically for the **Nado DEX CLOB** on **Ink L2**. 

Unlike conventional "black-box" prop bots, AI-Futures-Trader is designed as ecosystem-grade public infrastructure for Ink:
1. **Non-Custodial Architecture via `LinkSigner`:** Users delegate algorithmic order placement without sharing primary private keys. Session keys have **zero withdrawal authority**, eliminating counterparty and hack risks.
2. **Microstructure-Aligned Execution:** Native maker `POST_ONLY` quoting to provide tight liquidity, dual-oracle protective triggers (`Oracle Price` for stop loss preventing manipulation wicks, `Last Price` for dynamic take profit), and automated `build_appendix` telemetry tagging.
3. **Decentralized Multi-Agent Consensus:** Specialized subagents (Bull, Bear, and Open Interest analysts) debate live market conditions before reaching strategic consensus, guarded by deterministic rules (Parity veto, Anti-Chase overextension filters, and Bayesian expectancy routing).
4. **Autonomous Capital Protection (Sentinel Agent):** 24/7 on-chain trailing stops with free-ride break-even corridors and dynamic parabolic profit locking.

> [!IMPORTANT]
> **Grant Funding Notice:** In accordance with Ink Foundation governance standards, 100% of requested funds are allocated to open-source software development, public intelligence infrastructure, and community security. **Zero funds are requested or used as trading capital.**

---

## 1. Application to Nado Core Team (Nado Builder Program)

**Official Submission Status:** ✅ **Submitted Successfully via Official Tally Form (`https://tally.so/r/0QO4oy`)**  
**Submission Confirmation:** `"Thanks, we’ve got your submission ✅ Your project is now on our radar."`  
**Confirmation Screenshot:** `logs/nado_grant_screenshots/04_submitted_confirmation.png`  
**Subject:** Request for Official `NADO_BUILDER_ID` & Ecosystem Partnership — AI-Futures-Trader on Ink L2  
**To:** Nado Core Team (@nado_team / Nado Discord `#builders` Track)  
**From:** Maxim Lapin (@lazerniyyyeth on TG, Discord: `lapinzloyyy`, Email: `zloilapinbs@gmail.com`)  
**Active Sub-Account:** `0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa`  

Dear Nado Team,

We have developed and deployed **AI-Futures-Trader**, an autonomous multi-agent algorithmic trading system and execution SDK tailored to Nado DEX's central limit order book on Ink Mainnet (Chain ID `57073`). 

We are writing to request an official **`NADO_BUILDER_ID`** to be integrated into our order appendix encoding pipeline across all production cycles.

### Technical Integration Overview

1. **Protocol-Compliant Appendix Tagging (`build_appendix`):**
   Our execution client natively integrates `nado_protocol.utils.order.build_appendix` with dynamic bitmask packing:
   - When configured with our allocated `NADO_BUILDER_ID` and fee schedule, every order (`DEFAULT`, `IOC`, and `POST_ONLY`) tags our builder ID on-chain.
   - Built-in fail-safe: if `NADO_BUILDER_ID == 0`, builder fields are omitted, guaranteeing the sequencer will not reject unassigned orders (Error code `2094`).
2. **CLOB Liquidity Provisioning & Maker Rebates:**
   - Over 60% of our entry orders utilize `POST_ONLY` (Maker) limit orders placed at or near the best bid/ask, reducing spread drag and enriching order book depth on Nado.
   - Auto-fallback engine: if a maker order crosses the book (Code `2008`), it safely falls back to IOC Taker with an algorithmic slippage budget.
3. **Dual-Trigger Risk Engine:**
   - Stop Loss orders leverage `TriggerType.ORACLE_PRICE` to prevent artificial liquidation wicks.
   - Take Profit and Trailing Stops use `TriggerType.LAST_PRICE` to capture real-time order book momentum.
4. **Production Proven:**
   - Verified live sub-account on Ink Mainnet: [`0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa`](https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa).
   - Over 1,300 market cycles evaluated with deterministic risk management, zero margin call incidents, and automated reconciliation.

We look forward to registering our `NADO_BUILDER_ID` and collaborating with the Nado team to expand algorithmic trading volume on Ink L2.

---

## 2. Ink Spark Program Grant Application

### Project Details
- **Project Title:** AI-Futures-Trader: Non-Custodial Multi-Agent Terminal & Python SDK for Nado DEX
- **Applicant:** Maxim Lapin (AI-Futures-Trader Lead)
- **Email:** `zloilapinbs@gmail.com`
- **Telegram:** `@lazerniyyyeth`
- **Twitter / X:** `https://x.com/LapkaMax64244`
- **Discord:** `lapinzloyyy`
- **Submission Status:** ✅ **Submitted Officially to Ink Foundation** (form `https://forms.inkonchain.com/spark-builder-program`)
- **Ecosystem Focus:** Ink L2 Native DeFi & Autonomous Agent Infrastructure
- **Requested Amount:** **$12,000 USDC** (Distributed across 3 milestone deliverables)
- **Primary Deliverable Timeline:** 12 Weeks (3 months)

### Problem Statement
1. **High Barrier to Algorithmic Trading on Ink L2:** Retail traders and quantitative developers lack open-source, production-ready Python execution frameworks for Nado DEX's off-chain CLOB / on-chain settlement architecture.
2. **Custodial Risk in Automated Trading:** Most existing trading bots require users to paste their primary private key into a server, exposing user collateral to theft or server compromise.
3. **Community Information Asymmetry:** Everyday Ink users lack access to real-time institutional-grade order flow analytics (CVD, OI spikes, Bollinger squeeze regimes) across Nado perp markets.

### Solution & Ecosystem Impact
AI-Futures-Trader solves these bottlenecks by delivering:
- A **non-custodial session architecture (`LinkSigner`)** that separates execution rights from withdrawal rights.
- An **open-source Python SDK** with pre-built risk controls (Sentinel trailing stops, dynamic ATR sizing, and anti-chase guards).
- A **public, free-to-use Telegram Intelligence & Signal Hub** powered by zero-token telemetry caches for community traders on Ink.

---

### Milestone Breakdown & Budget Allocation

```
+---------------------------------------------------------------------------------------------------+
| Total Grant Budget: $12,000 USDC (100% Software Engineering, Security & Community Infrastructure) |
+---------------------------------------------------------------------------------------------------+
  │
  ├── Milestone 1: Non-Custodial Multi-Agent Terminal & LinkSigner Onboarding ($4,500 USDC | Weeks 1-4)
  │   ├── EIP-712 LinkSigner Web/CLI integration (zero-withdrawal session keys)
  │   ├── Web Dashboard for subaccount monitoring (PnL, margin ratio, Sentinel SL/TP state)
  │   └── Security audit of delegation flow & automated reconciliation guards
  │
  ├── Milestone 2: Public Community Signal Hub & 6-Month Inference/RPC Reserve ($4,000 USDC | Weeks 5-8)
  │   ├── Public Telegram Bot with 0-Token Showcase Mode (/status, /regime, /stats)
  │   ├── Automated macro regime broadcasts & breakout alerts for community traders
  │   └── 6-Month dedicated high-throughput RPC node & inference gateway reserve
  │
  └── Milestone 3: Open-Source Python Execution & Risk SDK for Nado CLOB ($3,500 USDC | Weeks 9-12)
      ├── PyPI package release: `nado-agent-sdk`
      ├── Reference bot templates: Maker Grid, Trend-Following, and Anti-Bot Liquidator
      └── Comprehensive documentation, unit test suite (>=90% coverage), and Ink developer tutorials
```

#### Detailed Milestone Specifications

#### **Milestone 1: Non-Custodial Multi-Agent Terminal & LinkSigner ($4,500 USDC)**
- **Objective:** Eliminate private key vulnerability for Ink users wanting to automate trading on Nado DEX.
- **Deliverables:**
  1. Complete Web/CLI onboarding tool (`nado_link_signer_helper.py`) generating one-click EIP-712 `LinkSigner` signatures for MetaMask, Rabby, and Coinbase Wallet.
  2. Cryptographic assurance layer: strictly blocks `withdraw_collateral` and `transfer_quote` from session keys.
  3. Interactive web dashboard displaying live positions, unrealized PnL, open Sentinel trigger orders, and liquidation distance.
  4. Acceptance criteria: A user can link their subaccount, execute algorithmic trades, and verify that session keys cannot initiate withdrawals under any scenario.

#### **Milestone 2: Public AI Market Intelligence & Community Signal Hub ($4,000 USDC)**
- **Objective:** Provide the Ink community with free, institutional-grade market regime detection and trading alerts without token paywalls.
- **Deliverables:**
  1. Telegram Showcase Bot (`@AIFuturesTrader_bot`) operating in zero-token read-only mode for the public, serving instant `/status`, `/regime`, and `/stats` from memory cache.
  2. Public Telegram broadcast channel streaming real-time Nado market anomalies: OI divergence, liquidation clusters, volatility compression alerts.
  3. 6-Month infrastructure hosting budget: dedicated low-latency Ink RPC endpoint and inference engine reserve to ensure 99.9% uptime.
  4. Acceptance criteria: 1,000+ public users onboarded to the signal hub with continuous 24/7 monitoring and latency < 500ms.

#### **Milestone 3: Open-Source Python Execution & Risk SDK for Nado CLOB ($3,500 USDC)**
- **Objective:** Foster a vibrant developer ecosystem of algorithmic traders and market makers on Ink L2.
- **Deliverables:**
  1. Open-source Python package on PyPI (`nado-agent-sdk` / `ai-futures-trader-sdk`) with full typing and async architecture.
  2. Modular risk primitives: Sentinel Agent (parabolic trailing stop, free-ride breakeven corridor), Anti-Chase overextension filters, and dynamic Kelly/ATR sizing.
  3. Pre-built template bots: Trend-Following Momentum, Maker Post-Only Range Quoter, and Anti-Liquidation Hedger.
  4. Developer documentation, quick-start guide, and CI/CD test suite with >90% test coverage.
  5. Acceptance criteria: Published PyPI package, verified GitHub repository with MIT license, and reproducible end-to-end tutorial for Ink developers.

---

## 3. Catalog Card for `inkonchain.com/apps`

```markdown
### Name:
AI-Futures-Trader

### Short Description (Tagline - 138 chars):
Autonomous multi-agent algorithmic trading terminal and open-source risk engine on Nado DEX with non-custodial LinkSigner delegation.

### Categories:
- DeFi
- AI / Autonomous Agents
- Perpetuals / CLOB
- Infrastructure & Developer Tooling

### Network:
Ink L2 (Mainnet Chain ID: 57073)

### Contract / Sub-Account Explorer:
https://explorer.inkonchain.com/address/0x12d1293CBD98eEA18a2e5906dF5ED3BEf07fB1aa

### Key Features:
• Multi-Agent Consensus: Bull, Bear, and OI specialist models debate market structure prior to entry.
• Non-Custodial Security: Employs Nado LinkSigner architecture—session keys can place orders but have ZERO withdrawal capability.
• Autonomous Capital Guard: Sentinel Agent provides 24/7 on-chain trailing stops, breakeven corridors, and anti-chase filters.
• Open-Source Python SDK: Modular templates for developers building algorithmic market makers and copy-trading bots on Ink.

### Links:
- GitHub: https://github.com/your-org/AI-Futures-Trader
- Documentation: https://docs.aifuturestrader.xyz
- Telegram Showcase: https://t.me/AIFuturesTrader_bot
- Nado DEX: https://app.nado.xyz/perpetuals
```

---

## 4. Verification and Security Disclosures

- **Non-Custodial Invariant:** The codebase contains zero methods capable of withdrawing user collateral to external addresses. All collateral stays on Nado Clearinghouse contracts.
- **Graceful Builder ID Fallback:** All orders sent via `NadoTradingService.build_order_appendix()` safely verify whether `NADO_BUILDER_ID > 0`. If unregistered, no builder metadata is attached, preventing exchange sequencer rejection (Error code `2094`).
- **Telemetry Transparency:** All trade snapshots, regime classifications, and risk rejections are stored in append-only JSONL files (`decision_snapshots.jsonl`), enabling full offline verification and auditability.

