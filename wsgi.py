"""Production entry point. Runs as ONE process (the scrape runs in a thread inside it)."""
import os

from waitress import serve

from app import create_app

if __name__ == "__main__":
    serve(create_app(), host=os.environ.get("MP_HOST", "127.0.0.1"), port=int(os.environ.get("MP_PORT", "8000")),
          threads=8)
