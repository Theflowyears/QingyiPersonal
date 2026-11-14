"""
website/run_web.py - start the Flask console in-process.

app.py only defines the Flask object (production uses gunicorn), so running it
directly would import-and-exit. This wrapper reads WEB_HOST/WEB_PORT and blocks.
"""
import os
from app import app


def main():
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "5000"))
    print("[Web] serving on http://%s:%d" % (host, port), flush=True)
    # threaded=True so the dashboard polling + heartbeats do not serialize behind each other
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()