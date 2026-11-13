"""測試 LINE 推播是否設定成功。先設定好 LINE_CHANNEL_TOKEN 與 LINE_USER_ID 環境變數,再執行:python test_line.py"""
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("請先安裝 requests:pip install requests")

tok, uid = os.environ.get("LINE_CHANNEL_TOKEN"), os.environ.get("LINE_USER_ID")
if not tok or not uid:
    sys.exit("找不到環境變數 LINE_CHANNEL_TOKEN 或 LINE_USER_ID,請在同一個視窗先設定。")

r = requests.post(
    "https://api.line.me/v2/bot/message/push",
    headers={"Authorization": f"Bearer {tok}"},
    json={"to": uid, "messages": [{"type": "text", "text": "測試成功:股票提醒已連線"}]},
    timeout=10,
)
print(r.status_code, r.text)
if r.status_code == 200:
    print("成功!請查看手機 LINE 是否收到訊息。")
elif r.status_code == 401:
    print("401:Token 錯誤或已失效,請重新複製。")
elif r.status_code == 400:
    print("400:通常是 User ID 錯誤,或內容格式有問題。")
