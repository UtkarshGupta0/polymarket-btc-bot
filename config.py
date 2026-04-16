"""Centralized configuration loaded from .env with defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


def _get_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is None or val == "":
        return default
    return float(val)


def _get_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None or val == "":
        return default
    return int(val)


@dataclass(frozen=True)
class Config:
    # Mode
    trading_mode: str  # "paper" or "live"

    # Polymarket creds
    polymarket_private_key: str
    polymarket_funder_address: str
    polymarket_signature_type: int

    # Trading params
    starting_capital: float
    max_bet_size: float
    min_bet_size: float
    min_confidence: float
    entry_window_start: int  # seconds before close (e.g. 45)
    entry_window_end: int    # seconds before close (e.g. 8)
    max_daily_drawdown: float
    max_consecutive_losses: int
    min_reserve: float
    kelly_fraction: float
    reprice_interval_sec: int
    kelly_enable_after: int

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # Anthropic
    anthropic_api_key: str

    # Endpoints
    binance_ws_url: str
    binance_rest_url: str
    gamma_api_url: str
    clob_api_url: str

    def validate(self) -> None:
        assert self.trading_mode in ("paper", "live"), \
            f"TRADING_MODE must be 'paper' or 'live', got {self.trading_mode!r}"
        assert self.max_bet_size >= self.min_bet_size > 0
        assert 0 < self.min_confidence < 1
        assert self.entry_window_start > self.entry_window_end >= 0
        assert self.max_daily_drawdown > 0
        assert self.max_consecutive_losses > 0
        assert 0 < self.kelly_fraction <= 1
        if self.trading_mode == "live":
            assert self.polymarket_private_key, \
                "POLYMARKET_PRIVATE_KEY required for live mode"
            assert self.polymarket_funder_address, \
                "POLYMARKET_FUNDER_ADDRESS required for live mode"

    def print_summary(self) -> None:
        print("=" * 60)
        print("POLYMARKET BTC 5-MIN BOT — CONFIG")
        print("=" * 60)
        print(f"Mode:                 {self.trading_mode.upper()}")
        print(f"Starting capital:     ${self.starting_capital:.2f}")
        print(f"Bet size:             ${self.min_bet_size:.2f} - ${self.max_bet_size:.2f}")
        print(f"Min confidence:       {self.min_confidence:.0%}")
        print(f"Entry window:         T-{self.entry_window_start}s to T-{self.entry_window_end}s")
        print(f"Max daily drawdown:   ${self.max_daily_drawdown:.2f}")
        print(f"Max loss streak:      {self.max_consecutive_losses}")
        print(f"Kelly fraction:       {self.kelly_fraction}")
        print(f"Min reserve:          ${self.min_reserve:.2f}")
        telegram_on = bool(self.telegram_bot_token and self.telegram_chat_id)
        print(f"Telegram alerts:      {'ON' if telegram_on else 'OFF'}")
        print(f"Anthropic API:        {'SET' if self.anthropic_api_key else 'UNSET'}")
        if self.trading_mode == "live":
            print(f"Funder address:       {self.polymarket_funder_address}")
            print(f"Signature type:       {self.polymarket_signature_type}")
        print("=" * 60)


def load_config() -> Config:
    cfg = Config(
        trading_mode=_get_str("TRADING_MODE", "paper").lower(),
        polymarket_private_key=_get_str("POLYMARKET_PRIVATE_KEY"),
        polymarket_funder_address=_get_str("POLYMARKET_FUNDER_ADDRESS"),
        polymarket_signature_type=_get_int("POLYMARKET_SIGNATURE_TYPE", 1),
        starting_capital=_get_float("STARTING_CAPITAL", 30.0),
        max_bet_size=_get_float("MAX_BET_SIZE", 5.0),
        min_bet_size=_get_float("MIN_BET_SIZE", 1.0),
        min_confidence=_get_float("MIN_CONFIDENCE", 0.60),
        entry_window_start=_get_int("ENTRY_WINDOW_START", 45),
        entry_window_end=_get_int("ENTRY_WINDOW_END", 8),
        max_daily_drawdown=_get_float("MAX_DAILY_DRAWDOWN", 5.0),
        max_consecutive_losses=_get_int("MAX_CONSECUTIVE_LOSSES", 5),
        min_reserve=_get_float("MIN_RESERVE", 5.0),
        kelly_fraction=_get_float("KELLY_FRACTION", 0.25),
        reprice_interval_sec=_get_int("REPRICE_INTERVAL_SEC", 5),
        kelly_enable_after=_get_int("KELLY_ENABLE_AFTER", 100),
        telegram_bot_token=_get_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get_str("TELEGRAM_CHAT_ID"),
        anthropic_api_key=_get_str("ANTHROPIC_API_KEY"),
        binance_ws_url=_get_str(
            "BINANCE_WS_URL", "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"),
        binance_rest_url=_get_str(
            "BINANCE_REST_URL", "https://api.binance.com"),
        gamma_api_url=_get_str(
            "GAMMA_API_URL", "https://gamma-api.polymarket.com"),
        clob_api_url=_get_str(
            "CLOB_API_URL", "https://clob.polymarket.com"),
    )
    cfg.validate()
    return cfg


CONFIG = load_config()


if __name__ == "__main__":
    CONFIG.print_summary()
