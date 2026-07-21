# desktop_app.py
import os, secrets, socket, threading
from pathlib import Path
from datetime import datetime
import webview

def _free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); addr, port = s.getsockname(); s.close()
    return port

def run_flask(app, port):
    # lock Flask to localhost, turn off reloader
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True)

class Bridge:
    def pick_folder(self, start_dir=None):
        try:
            initial = Path(start_dir).expanduser() if start_dir else Path.home()
        except Exception:
            initial = Path.home()
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, directory=str(initial))
        if result and len(result) > 0:
            return str(result[0])
        return ""

if __name__ == '__main__':
    os.environ.setdefault('APP_ENV', 'desktop')
    os.environ.setdefault('DESKTOP_MODE', '1')
    os.environ.setdefault('APP_DATA_DIR', str(Path.home() / '.envelope-budget'))
    os.environ.setdefault('SECRET_KEY', secrets.token_urlsafe(32))

    from app import create_app  # import after desktop defaults are set

    app = create_app()

    # harden server mode for desktop
    app.config['ENV'] = 'production'
    app.config['PREFERRED_URL_SCHEME'] = 'http'
    # ensure meta/user dirs exist (your init already does this)
    # seed default user is in your db.init_app()

    port = _free_port()
    t = threading.Thread(target=run_flask, args=(app, port), daemon=True)
    t.start()

    bridge = Bridge()
    webview.create_window(
        title=app.config["APP_DISPLAY_NAME"],
        url=f"http://127.0.0.1:{port}",
        js_api=bridge,
        width=1200, height=800, resizable=True, confirm_close=True
    )
    webview.start()
