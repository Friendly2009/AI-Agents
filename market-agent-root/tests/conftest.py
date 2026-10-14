import json

import pytest
from market_agent.config import Settings


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "markets.json"
    path.write_text(
        json.dumps({"instruments": [{"symbol": "SBER", "keywords": ["Сбербанк"]}], "news_feeds": []})
    )
    return Settings(
        _env_file=None,
        api_token="x" * 40,
        database_path=tmp_path / "db.sqlite3",
        markets_file=path,
        schedule_enabled=False,
        http_retries=0,
    )
