"""Compatibility entrypoint.

Bothost should use main.py.  Keeping app.py working avoids broken deployments
where the platform or an old configuration still points to app.py.
"""
from main import app, run


if __name__ == "__main__":
    run()
