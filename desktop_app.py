"""
PDF.AI Desktop — десктопное приложение-обёртка.
==============================================
Запускает FastAPI сервер на 127.0.0.1 и открывает его в окне pywebview.
Не требует активного интернет-соединения пользователя.

Использование:
  python desktop_app.py          — запуск в режиме разработки
  PDFAI.exe                      — запуск из собранного .exe
"""
import os
import sys
import time
import socket
import threading
import traceback
import webbrowser

# ── Пути ──────────────────────────────────────────────────────────
# PyInstaller помещает бандл в _MEIPASS; при обычном запуске
# используем директорию скрипта.
EXE_DIR = os.path.dirname(sys.executable)
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))

os.chdir(BUNDLE_DIR)

# ── Логирование ────────────────────────────────────────────────────
_LOG_PATH = os.path.join(EXE_DIR, "pdfai.log")


def _log(msg: str):
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S") + " " + str(msg) + "\n")
    except Exception:
        pass


sys.stdout = open(_LOG_PATH, "a", encoding="utf-8")
sys.stderr = sys.stdout

_log("=== PDFAI launch ===")
_log("EXE_DIR=" + EXE_DIR)
_log("BUNDLE_DIR=" + BUNDLE_DIR)

# ── БД и статика — указываем явные пути ───────────────────────────
os.environ["PDFAI_DB_PATH"] = os.path.join(EXE_DIR, "journal.db")
os.environ["PDFAI_STATIC_DIR"] = os.path.join(BUNDLE_DIR, "static")


# ── Сеть ──────────────────────────────────────────────────────────

def find_free_port(start: int = 8081, end: int = 8200) -> int:
    """Ищет свободный порт в диапазоне."""
    for port in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError("Не удалось найти свободный порт")


def wait_for_server(port: int, timeout: float = 30.0):
    """Ждёт пока сервер не начнёт принимать соединения."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(("127.0.0.1", port))
            s.close()
            return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)


def start_server(port: int):
    """Запускает uvicorn в текущем потоке."""
    _log("start_server port=" + str(port))
    try:
        import uvicorn
        import main
        fastapi_app = main.app
        _log("main.app imported")
        uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="warning")
    except Exception:
        _log("start_server FAIL:\n" + traceback.format_exc())


# ── Главная функция ────────────────────────────────────────────────

def main():
    port = find_free_port()
    url = f"http://127.0.0.1:{port}/app"

    print(f"[App] Старт на порту {port}...")
    _log("[App] DB: " + os.environ.get("PDFAI_DB_PATH", ""))

    # Запускаем сервер в фоновом потоке
    server_thread = threading.Thread(target=start_server, args=(port,), daemon=True)
    server_thread.start()

    # Ждём готовности сервера (max 30 сек)
    wait_for_server(port, timeout=30.0)

    if not server_thread.is_alive():
        _log("[App] Ошибка: сервер не запустился!")
        sys.exit(1)

    _log("[App] server ready")
    print("[App] Server ready")

    # ── Пробуем открыть pywebview ──────────────────────────────────
    try:
        _log("importing webview")
        import webview
        _log("webview ok")

        class JsApi:
            """API доступный из JS через window.pywebview.api.*"""

            def __init__(self):
                self._window = None

            def set_window(self, w):
                self._window = w

            def save_pdf(self, suggested_name: str, data_b64: str) -> dict:
                """Открывает диалог 'Сохранить как', сохраняет PDF. data_b64 — base64 содержимое."""
                import base64
                try:
                    safe_name = suggested_name if suggested_name.lower().endswith(".pdf") else suggested_name + ".pdf"
                    safe_name = "".join(
                        c for c in safe_name.strip() if c not in '<>:"/\\|?*'
                    ) or "document.pdf"

                    result = self._window.create_file_dialog(
                        webview.SAVE_DIALOG,
                        save_filename=safe_name,
                    )
                    if not result:
                        return {"ok": False}

                    if isinstance(result, (list, tuple)):
                        path = result[0] if result else None
                    else:
                        path = result

                    if not path:
                        return {"ok": False}
                    with open(path, "wb") as f:
                        f.write(base64.b64decode(data_b64))
                    _log("saved PDF -> " + path)
                    return {"ok": True, "path": path}
                except Exception as ex:
                    _log("save_pdf FAIL: " + traceback.format_exc())
                    return {"ok": False, "error": str(ex)}

        api = JsApi()
        window = webview.create_window(
            "PDF.AI",
            url,
            js_api=api,
            width=1280,
            height=800,
            min_size=(900, 600),
        )
        api.set_window(window)
        webview.start()

    except Exception as e:
        _log("webview FAIL: " + traceback.format_exc())
        # Фолбэк — открываем в браузере
        _log("[App] Открываем в браузере: " + url)
        webbrowser.open(url)
        try:
            print(f"[App] Нажмите Ctrl+C для остановки.")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    print("\n[App] Остановка...")


if __name__ == "__main__":
    main()
