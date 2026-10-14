from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Candle(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    day: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def prices(self):
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close):
            raise ValueError("Invalid OHLC range")
        return self


class News(BaseModel):
    id: str
    title: str
    url: str
    published_at: datetime
    excerpt: str = ""
    feed_url: str = ""


class AIResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=8000)
    risks: list[str] = Field(max_length=10)
    source_ids: list[str] = Field(max_length=30)


class Holding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9_-]{1,24}$")
    shares: int = Field(ge=1, le=1_000_000_000, strict=True)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cash_rub: float = Field(ge=0, le=1_000_000_000)
    horizon_days: int = Field(ge=7, le=365, strict=True)
    risk: Literal["conservative", "balanced", "aggressive"]
    portfolio_known: bool = False
    holdings: list[Holding] = Field(default_factory=list, max_length=50)
    notes: str = Field(default="", max_length=1500)

    @model_validator(mode="after")
    def consistent(self):
        if len({h.symbol for h in self.holdings}) != len(self.holdings):
            raise ValueError("Один тикер можно указать только один раз")
        if self.holdings and not self.portfolio_known:
            raise ValueError("Подтвердите полноту портфеля или очистите позиции")
        if not self.cash_rub and not self.holdings:
            raise ValueError("Укажите свободные деньги или текущие акции")
        return self


class Signal(AIResult):
    symbol: str = Field(pattern=r"^[A-Z0-9_-]{1,24}$")
    action: Literal["buy", "sell", "wait"]
    sell_fraction: Literal["quarter", "half", "all"] = "all"


class Signals(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signals: list[Signal] = Field(min_length=1, max_length=50)
