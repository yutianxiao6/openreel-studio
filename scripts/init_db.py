"""Initialize the SQLite database by creating all tables."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api"))

from app.db.session import init_db


async def main():
    print("Initializing database...")
    await init_db()
    print("Database initialized successfully.")
    print("Tables created in data/app.db")


if __name__ == "__main__":
    asyncio.run(main())
