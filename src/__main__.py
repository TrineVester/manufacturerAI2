"""Run the ManufacturerAI web server: python -m src"""

import sys
import uvicorn

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "serve":
        print("Starting ManufacturerAI server on http://localhost:8000")
        uvicorn.run("src.web.server:app", host="127.0.0.1", port=8000, reload=True)
