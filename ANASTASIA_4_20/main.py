"""Executable composition root. Importing it creates no resources."""
import asyncio
from dotenv import load_dotenv
from application import create_application

async def main():
    load_dotenv()
    app = create_application()
    try:
        await app.main()
    finally:
        app.close()

if __name__ == '__main__':
    asyncio.run(main())
