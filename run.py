"""
Starts the Flask web server.
All scraping is triggered manually from the web UI.
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def main():
    from web.app import app
    print("JobScrape starting — open http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
