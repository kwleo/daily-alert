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


def _ticker_data(symbol):
    t = yf.Ticker(symbol)
    hist = t.history(period="2d")
    close = hist["Close"].iloc[-1]
    prev  = hist["Close"].iloc[-2]
    chg   = close - prev
    pct   = chg / prev * 100
    return close, chg, pct


def fetch_market_data():
    sp500_close,  sp500_chg,  sp500_pct  = _ticker_data("^GSPC")
    krw_close,    krw_chg,    krw_pct    = _ticker_data("KRW=X")
    kospi_close,  kospi_chg,  kospi_pct  = _ticker_data("^KS11")
    nasdaq_close, nasdaq_chg, nasdaq_pct = _ticker_data("^IXIC")
    gold_close,   gold_chg,   gold_pct   = _ticker_data("GC=F")
    uso_close,    uso_chg,    uso_pct    = _ticker_data("USO")
    btc_close,    btc_chg,    btc_pct    = _ticker_data("BTC-USD")
    eth_close,    eth_chg,    eth_pct    = _ticker_data("ETH-USD")
    xrp_close,    xrp_chg,    xrp_pct    = _ticker_data("XRP-USD")

    return {
        "sp500_close":  sp500_close,  "sp500_chg":  sp500_chg,  "sp500_pct":  sp500_pct,
        "krw_close":    krw_close,    "krw_chg":    krw_chg,    "krw_pct":    krw_pct,
        "kospi_close":  kospi_close,  "kospi_chg":  kospi_chg,  "kospi_pct":  kospi_pct,
        "nasdaq_close": nasdaq_close, "nasdaq_chg": nasdaq_chg, "nasdaq_pct": nasdaq_pct,
        "gold_close":   gold_close,   "gold_chg":   gold_chg,   "gold_pct":   gold_pct,
        "uso_close":    uso_close,    "uso_chg":    uso_chg,    "uso_pct":    uso_pct,
        "btc_close":    btc_close,    "btc_chg":    btc_chg,    "btc_pct":    btc_pct,
        "eth_close":    eth_close,    "eth_chg":    eth_chg,    "eth_pct":    eth_pct,
        "xrp_close":    xrp_close,    "xrp_chg":    xrp_chg,    "xrp_pct":    xrp_pct,
    }


def format_message(d):
    today = datetime.now().strftime("%Y-%m-%d")

    def arrow(val):
        return "▲" if val >= 0 else "▼"

    return f"""📊 일일 시장 브리핑 ({today})

📈 주요 지수
🇺🇸 S&P 500
  {d['sp500_close']:,.2f}pt  {arrow(d['sp500_chg'])} {abs(d['sp500_chg']):.2f} ({d['sp500_pct']:+.2f}%)
🇺🇸 NASDAQ
  {d['nasdaq_close']:,.2f}pt  {arrow(d['nasdaq_chg'])} {abs(d['nasdaq_chg']):.2f} ({d['nasdaq_pct']:+.2f}%)
🇰🇷 KOSPI
  {d['kospi_close']:,.2f}pt  {arrow(d['kospi_chg'])} {abs(d['kospi_chg']):.2f} ({d['kospi_pct']:+.2f}%)

💱 환율
원/달러
  {d['krw_close']:,.2f}원  {arrow(d['krw_chg'])} {abs(d['krw_chg']):.2f} ({d['krw_pct']:+.2f}%)

🏅 원자재
금 (Gold)
  ${d['gold_close']:,.2f}  {arrow(d['gold_chg'])} {abs(d['gold_chg']):.2f} ({d['gold_pct']:+.2f}%)
원유 (USO)
  ${d['uso_close']:,.2f}  {arrow(d['uso_chg'])} {abs(d['uso_chg']):.2f} ({d['uso_pct']:+.2f}%)

🪙 암호화폐
Bitcoin
  ${d['btc_close']:,.2f}  {arrow(d['btc_chg'])} {abs(d['btc_chg']):.2f} ({d['btc_pct']:+.2f}%)
Ethereum
  ${d['eth_close']:,.2f}  {arrow(d['eth_chg'])} {abs(d['eth_chg']):.2f} ({d['eth_pct']:+.2f}%)
XRP
  ${d['xrp_close']:,.4f}  {arrow(d['xrp_chg'])} {abs(d['xrp_chg']):.4f} ({d['xrp_pct']:+.2f}%)"""


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
