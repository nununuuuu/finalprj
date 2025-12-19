import uvicorn
import webbrowser
import threading
import time
import sys
import os

def start_browser():
    """等待 2 秒讓 Server 啟動後，自動開啟預設瀏覽器"""
    time.sleep(2)
    url = "http://127.0.0.1:8000"
    print(f"正在開啟瀏覽器: {url}")
    webbrowser.open(url)

if __name__ == "__main__":
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    threading.Thread(target=start_browser, daemon=True).start()
    print("正在啟動 ..")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)