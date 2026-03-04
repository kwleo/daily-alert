"""
GitHub Actions용: 환경변수에서 토큰 읽어서 실행
"""
import json
import os
import requests
import yfinance as yf
from datetime import datetime

REST_API_KEY  = os.environ["KAKAO_REST_API_KEY"]
REFRESH_TOKEN = os.environ["KAKAO_REFRESH_TOKEN"]


def get_access_token():
    resp = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": REST_API_KEY,
            "refresh_token": REFRESH_TOKEN,
        },
    )
    return resp.json()["access_token"]


def fetch_market_data():
    sp500 = yf.Ticker("^GSPC")
    sp500_hist = sp500.history(period="2d")
    sp500_close = sp500_hist["Close"].iloc[-1]
    sp500_prev  = sp500_hist["Close"].iloc[-2]
    sp500_chg   = sp500_close - sp500_prev
    sp500_pct   = sp500_chg / sp500_prev * 100

    krw = yf.Ticker("KRW=X")
    krw_hist  = krw.history(period="2d")
    krw_close = krw_hist["Close"].iloc[-1]
    krw_prev  = krw_hist["Close"].iloc[-2]
    krw_chg   = krw_close - krw_prev
    krw_pct   = krw_chg / krw_prev * 100

    return {
        "sp500_close": sp500_close,
        "sp500_chg": sp500_chg,
        "sp500_pct": sp500_pct,
        "krw_close": krw_close,
        "krw_chg": krw_chg,
        "krw_pct": krw_pct,
    }


def format_message(d):
    today = datetime.now().strftime("%Y-%m-%d")

    def arrow(val):
        return "▲" if val >= 0 else "▼"

    return f"""📊 일일 시장 브리핑 ({today})

🇺🇸 S&P 500
  {d['sp500_close']:,.2f}pt  {arrow(d['sp500_chg'])} {abs(d['sp500_chg']):.2f} ({d['sp500_pct']:+.2f}%)

💱 원/달러 환율
  {d['krw_close']:,.2f}원  {arrow(d['krw_chg'])} {abs(d['krw_chg']):.2f} ({d['krw_pct']:+.2f}%)"""


def send_kakao(message, access_token):
    payload = {
        "object_type": "text",
        "text": message,
        "link": {"web_url": "", "mobile_web_url": ""},
    }
    resp = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(payload)},
    )
    print("전송 결과:", resp.json())


if __name__ == "__main__":
    token = get_access_token()
    data  = fetch_market_data()
    msg   = format_message(data)
    print(msg)
    send_kakao(msg, token)
