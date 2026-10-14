import asyncio
import json

import httpx


class SourceError(Exception):
    """Sanitized upstream error, safe to store or display."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def error_message(status, body):
    # Recognize actionable conditions but never expose the raw upstream body/key.
    text = body[:16000].decode("utf-8", errors="ignore").lower()
    if "user location" in text or "location is not supported" in text:
        return "Gemini недоступен в регионе запроса. Проверьте официальную доступность сервиса."
    if "api_key_invalid" in text or "api key not valid" in text or status == 401:
        return "Ключ API не принят. Проверьте LLM_API_KEY и доступ к API."
    if status == 429:
        return "Исчерпана квота или превышена частота запросов (HTTP 429). Проверьте лимиты в AI Studio; повторите позже."
    if status == 403:
        return "Доступ запрещён (HTTP 403). Проверьте разрешения ключа, проект и доступность источника."
    if status == 404:
        return "Источник или модель не найдены (HTTP 404). Проверьте LLM_MODEL и адрес API."
    if status >= 500:
        return f"Временная ошибка сервиса (HTTP {status}). Повторите позже."
    if status == 400:
        return "API отклонил запрос (HTTP 400). Проверьте модель, ключ и поддержку JSON-ответов."
    return f"Источник ответил HTTP {status}"


class HTTP:
    def __init__(self, client: httpx.AsyncClient, retries=2):
        self.client, self.retries = client, retries

    async def fetch(self, method, url, **kwargs) -> bytes:
        # A failed generative POST is not automatically repeated: it may have
        # consumed quota already. GET retries are safe and bounded.
        retries = self.retries if method.upper() == "GET" else 0
        for attempt in range(retries + 1):
            try:
                async with self.client.stream(method, url, **kwargs) as response:
                    if (response.status_code == 429 or response.status_code >= 500) and attempt < retries:
                        try:
                            delay = min(30, max(1, float(response.headers.get("Retry-After", 2**attempt))))
                        except ValueError:
                            delay = 2**attempt
                        await asyncio.sleep(delay)
                        continue
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 2_000_000:
                            raise SourceError("Ответ источника превышает 2 МБ")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if not 200 <= response.status_code < 300:
                        raise SourceError(error_message(response.status_code, body), response.status_code)
                    return body
            except httpx.RequestError as exc:
                if attempt == retries:
                    raise SourceError("Источник недоступен или истекло время ожидания") from exc
                await asyncio.sleep(min(2**attempt, 4))
        raise SourceError("Источник недоступен")

    async def json(self, method, url, **kwargs):
        try:
            return json.loads(await self.fetch(method, url, **kwargs))
        except (ValueError, UnicodeError) as exc:
            raise SourceError("Источник вернул некорректный JSON") from exc
