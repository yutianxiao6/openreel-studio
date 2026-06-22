"""Auto-port launcher for OpenReel Studio API."""
import socket
import sys
from pathlib import Path

PORT_FILE = Path(__file__).parent / ".port"


def find_free_port(start: int = 8000, end: int = 8020) -> int:
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return start


def main():
    port = find_free_port()
    PORT_FILE.write_text(str(port), encoding="utf-8")
    print(f"\n  OpenReel Studio API → http://localhost:{port}\n")

    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload="--no-reload" not in sys.argv,
    )


if __name__ == "__main__":
    main()
