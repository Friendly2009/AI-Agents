from datetime import date, datetime

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


class AIResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=8000)
    risks: list[str] = Field(max_length=10)
    source_ids: list[str] = Field(max_length=30)
