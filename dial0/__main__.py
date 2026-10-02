"""`python -m dial0 serve`: start the agent API (what the container's entrypoint runs)."""
import sys
from .server import serve

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    else:
        print("usage: python -m dial0 serve")
