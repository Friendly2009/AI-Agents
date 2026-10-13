import asyncio
import json

import httpx


class SourceError(Exception):
    """Sanitized upstream error, safe to store or display."""


class HTTP:
    def __init__(self, client: httpx.AsyncClient, retries=2):
        self.client, self.retries = client, retries

    async def fetch(self, method, url, **kwargs) -> bytes:
        for attempt in range(self.retries + 1):
            try:
                async with self.client.stream(method, url, **kwargs) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < self.retries:
                            await asyncio.sleep(min(2**attempt, 4))
                            continue
                    if not 200 <= response.status_code < 300:
                        raise SourceError(f"Источник ответил HTTP {response.status_code}")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 2_000_000:
                            raise SourceError("Ответ источника превышает 2 МБ")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.RequestError as exc:
                if attempt == self.retries:
                    raise SourceError("Источник недоступен или истекло время ожидания") from exc
                await asyncio.sleep(min(2**attempt, 4))
        raise SourceError("Источник недоступен")

    async def json(self, method, url, **kwargs):
        try:
            return json.loads(await self.fetch(method, url, **kwargs))
        except (ValueError, UnicodeError) as exc:
            raise SourceError("Источник вернул некорректный JSON") from exc
