import argparse
import asyncio
import json
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Weekly Market Agent")
    parser.add_argument("command", choices=["init", "serve", "analyze"], nargs="?", default="serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "init":
        path = Path(".env")
        if path.exists():
            parser.error(".env уже существует; файл не изменён")
        text = Path(".env.example").read_text(encoding="utf-8")
        with path.open("x", encoding="utf-8") as output:
            output.write(text.replace("CHANGE_ME_GENERATE_A_RANDOM_TOKEN", secrets.token_urlsafe(32)))
        path.chmod(0o600)
        print("Создан .env со случайным API_TOKEN. Настройте MODE и источники перед live-запуском.")
    elif args.command == "serve":
        import uvicorn

        uvicorn.run("market_agent.api:create_app", factory=True, host=args.host, port=args.port, workers=1)
    else:
        asyncio.run(analyze())


async def analyze():
    import httpx

    from .config import Settings
    from .network import HTTP
    from .service import Service
    from .storage import Store

    settings = Settings()
    store = Store(settings.database_path)
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=False) as client:
        service = Service(settings, settings.markets(), store, HTTP(client, settings.http_retries))
        run_id = store.claim()
        if not run_id:
            raise SystemExit("Анализ уже выполняется")
        await service.execute(run_id)
        result = store.get(run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] == "failed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
