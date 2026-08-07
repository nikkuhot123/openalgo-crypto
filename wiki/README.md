# OpenAlgo LLM Wiki

Welcome to the OpenAlgo strategy and operations knowledge base. This wiki is maintained autonomously by LLM agents in collaboration with the engineering team. It serves as the compounding repository of truth for all research, backtests, live trading incidents, and VPS configurations.

Unlike raw code or scattered chats, this wiki compiles knowledge once and keeps it current.

## Directory Structure

```
wiki/
├── README.md                 # This file
├── schema.md                 # Operational guidelines & constraints for LLMs
├── index.md                  # Content index of all pages
├── log.md                    # Chronological, append-only log of updates
├── strategies/               # Active and retired trading strategies
│   ├── pov_wall_squeeze.md
│   ├── judas_swing.md
│   ├── prior_levels_ema.md
│   └── red_bar_x_candle.md   # Retired
├── research/                 # Backtest results, studies, and methodology
│   ├── renko_pro_backtest.md
│   └── strike_selection.md
└── vps_operations/           # VPS setup, automation, and incident reports
    └── log_rotation.md
```

## How to Use

1. **Obsidian Integration**: Open this `wiki/` directory in Obsidian to view connections, links, and the local graph.
2. **LLM Ingest**: When deploying a new code version, starting a research spike, or diagnosing an incident, prompt the LLM to read the relevant wiki page, update it, and append to the log.
