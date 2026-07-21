import os


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


host = os.environ.get("HOST", "127.0.0.1")
port = _int_env("PORT", 8080)

if host != "127.0.0.1":
    raise RuntimeError("Refusing to bind Gunicorn to anything other than 127.0.0.1.")

bind = f"{host}:{port}"
workers = _int_env("WEB_CONCURRENCY", 1)
threads = _int_env("GUNICORN_THREADS", 2)
timeout = _int_env("GUNICORN_TIMEOUT", 120)
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT", 30)

accesslog = "-"
errorlog = "-"
capture_output = True
preload_app = False
