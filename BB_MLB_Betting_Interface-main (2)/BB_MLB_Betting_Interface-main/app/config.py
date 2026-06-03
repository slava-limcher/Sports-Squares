from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # balldontlie
    bdl_api_key: str = ""
    bdl_webhook_secret: str = ""
    bdl_base_url: str = "https://api.balldontlie.io/mlb/v1"

    # Kalshi
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"

    # Polling intervals (seconds)
    poll_game_state: int = 10
    poll_lineup_context: int = 10
    poll_stats: int = 30
    poll_odds: int = 30
    poll_props: int = 60

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    cors_origins: List[str] = ["http://localhost:3000", "http://localhost:5173", "*"]
    use_webhook_state: bool = False

    # Debugging paths
    pinch_log_path: str = "pinch_events.jsonl"
    dropped_log_path: str = "dropped_events.jsonl"
    webhook_log_path: str = "webhook_log.jsonl"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
