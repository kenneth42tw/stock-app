"""
台股分析儀表板:技術面 + 三大法人 + 融資融券 + 估值 + 新聞 + 回測 + 投資建議

事先只需安裝 streamlit(pip install streamlit),其餘套件第一次執行時會自動安裝。
執行:  streamlit run app.py
(選用) 環境變數 ANTHROPIC_API_KEY 啟用 AI 解讀;FINMIND_TOKEN 提高資料額度
本工具僅供資訊整理,不構成投資建議。
"""
import importlib.util
import os
import re
import smtplib
import time
from email.message import EmailMessage
import subprocess
import sys

# 第一次執行時,自動安裝缺少的套件
for _mod, _pkg in {"numpy": "numpy", "pandas": "pandas", "requests": "requests",
                   "plotly": "plotly", "yfinance": "yfinance", "anthropic": "anthropic"}.items():
    if not getattr(sys, "frozen", False) and importlib.util.find_spec(_mod) is None:
        print(f"[自動安裝] {_pkg} ...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", _pkg])
        except Exception as _e:
            print(f"[警告] 安裝 {_pkg} 失敗:{_e}")


def _load_config():
    """讀取設定檔(KEY=VALUE),存放 API Key、LINE、Telegram 等設定,免去手動設定環境變數。
    路徑:APPDATA 資料夾下 StockAnalyzer 內的 config.env,或程式資料夾內的 config.env。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else "."
    for path in (os.path.join(here, "config.env"), os.path.join(base, "StockAnalyzer", "config.env"), "config.env"):
        if os.path.exists(path):
            for line in open(path, encoding="utf-8-sig", errors="ignore"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if v.strip():
                        os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_config()

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots

st.set_page_config(page_title="台股分析儀表板", page_icon="📈", layout="wide", initial_sidebar_state="auto")
FINMIND = "https://api.finmindtrade.com/api/v4/data"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# ------------------------------------------------------------ 外觀(淺色/深色主題皆適用)
import html as _html
import plotly.io as pio

pio.templates["pro"] = go.layout.Template(layout=dict(
    font=dict(family="Inter, 'Noto Sans TC', 'PingFang TC', 'Microsoft JhengHei', sans-serif", size=12),
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    hoverlabel=dict(font_size=12, bordercolor="rgba(0,0,0,0)"),
    xaxis=dict(gridcolor="rgba(128,128,128,.14)", zeroline=False, linecolor="rgba(128,128,128,.3)"),
    yaxis=dict(gridcolor="rgba(128,128,128,.14)", zeroline=False, linecolor="rgba(128,128,128,.3)")))
pio.templates.default = "plotly+pro"

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Sans+TC:wght@400;500;700&display=swap');
html, body, .stApp, [data-testid="stSidebar"] {
  font-family: 'Inter','Noto Sans TC','PingFang TC','Microsoft JhengHei',sans-serif;
}
.block-container { padding-top: 2rem; padding-bottom: 4rem; max-width: 1320px; }
footer { visibility: hidden; }
h1, h2, h3 { letter-spacing: .2px; font-weight: 700; }
h3 { padding-top: .6rem; }

/* 頂部橫幅 */
.hero { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap;
  padding:20px 26px; margin-bottom:20px; border-radius:18px; color:#fff;
  background: radial-gradient(1200px 300px at 0% 0%, rgba(44,123,229,.45), transparent 60%),
              linear-gradient(120deg,#0b1623 0%,#14263a 55%,#1b3a57 100%);
  box-shadow:0 12px 32px rgba(11,22,35,.28); }
.hero .t { font-size:1.55rem; font-weight:700; letter-spacing:.5px; }
.hero .s { margin-top:3px; font-size:.84rem; opacity:.68; }
.badge { display:inline-flex; align-items:center; gap:7px; padding:5px 14px; border-radius:999px;
  font-size:.8rem; font-weight:600; }
.badge i { width:8px; height:8px; border-radius:50%; display:inline-block; }
.badge.on  { background:rgba(74,222,128,.14); color:#4ade80; border:1px solid rgba(74,222,128,.4); }
.badge.on i { background:#4ade80; box-shadow:0 0 0 0 rgba(74,222,128,.7); animation:pulse 1.8s infinite; }
.badge.off { background:rgba(148,163,184,.16); color:#cbd5e1; border:1px solid rgba(203,213,225,.3); }
.badge.off i { background:#94a3b8; }
@keyframes pulse { 70% { box-shadow:0 0 0 8px rgba(74,222,128,0); } 100% { box-shadow:0 0 0 0 rgba(74,222,128,0); } }

/* 報價卡 */
.qcard { display:flex; align-items:center; justify-content:space-between; gap:24px; flex-wrap:wrap;
  padding:20px 24px; margin:4px 0 14px; border-radius:18px;
  background:rgba(128,128,128,.07); border:1px solid rgba(128,128,128,.2); }
.qcard .nm { font-size:1.45rem; font-weight:700; }
.qcard .cd { font-weight:500; opacity:.55; margin-left:8px; font-size:1.05rem; }
.chip { display:inline-block; padding:2px 10px; border-radius:999px; font-size:.76rem; font-weight:600;
  background:rgba(44,123,229,.13); color:#2c7be5; margin-top:8px; }
.qcard .meta { font-size:.78rem; opacity:.6; margin-top:8px; }
.qcard .px { font-size:2.5rem; font-weight:700; line-height:1; font-variant-numeric:tabular-nums; }
.qcard .chg { font-size:1.02rem; font-weight:600; margin-top:6px; font-variant-numeric:tabular-nums; }
.up { color:#e0383e; } .dn { color:#1f9d55; } .flat { color:inherit; opacity:.7; }
.stats { display:grid; grid-template-columns:repeat(4,minmax(70px,1fr)); gap:10px; }
.stats div { padding:8px 14px; border-radius:12px; background:rgba(128,128,128,.09); text-align:center; }
.stats small { display:block; opacity:.6; font-size:.72rem; }
.stats b { font-size:1rem; font-variant-numeric:tabular-nums; }

/* 指標卡 */
[data-testid="stMetric"] { background:rgba(128,128,128,.07); border:1px solid rgba(128,128,128,.18);
  border-radius:14px; padding:14px 18px; transition:transform .15s, box-shadow .15s; }
[data-testid="stMetric"]:hover { transform:translateY(-2px); box-shadow:0 8px 20px rgba(0,0,0,.08); }
[data-testid="stMetricValue"] { font-weight:700; font-variant-numeric:tabular-nums; }
[data-testid="stMetricLabel"] { opacity:.75; }

/* 分頁 */
.stTabs [data-baseweb="tab-list"] { gap:4px; border-bottom:1px solid rgba(128,128,128,.22); }
.stTabs [data-baseweb="tab"] { height:44px; padding:0 18px; font-weight:600; border-radius:10px 10px 0 0; }
.stTabs [data-baseweb="tab"]:hover { background:rgba(44,123,229,.08); }
.stTabs [aria-selected="true"] { color:#2c7be5; }
.stTabs [data-baseweb="tab-highlight"] { background:#2c7be5; height:3px; border-radius:3px; }

/* 提醒訊息、展開區、表格 */
[data-testid="stAlert"] { border-radius:12px; border:1px solid rgba(128,128,128,.14); }
[data-testid="stExpander"] { border-radius:14px; border:1px solid rgba(128,128,128,.2); overflow:hidden; }
[data-testid="stExpander"] summary { font-weight:600; }
[data-testid="stDataFrame"] { border:1px solid rgba(128,128,128,.2); border-radius:12px; overflow:hidden; }

/* 按鈕與輸入元件 */
.stButton > button { border-radius:10px; font-weight:600; border:1px solid rgba(128,128,128,.3);
  transition:all .15s ease; }
.stButton > button:hover { transform:translateY(-1px); border-color:#2c7be5; color:#2c7be5;
  box-shadow:0 6px 16px rgba(44,123,229,.18); }
[data-baseweb="input"], [data-baseweb="select"] > div, [data-baseweb="textarea"] { border-radius:10px !important; }
[data-testid="stProgress"] > div > div > div { background:#2c7be5; }

/* 側邊欄 */
[data-testid="stSidebar"] { border-right:1px solid rgba(128,128,128,.18); }
[data-testid="stSidebar"] h3 { font-size:1rem; margin-top:.4rem; opacity:.85; }
[data-testid="stSidebar"] [role="radiogroup"] { gap:6px; }

/* ===== App 質感與手機版優化 ===== */
html { scroll-behavior:smooth; -webkit-tap-highlight-color:transparent; }
[data-testid="stDecoration"], #MainMenu, [data-testid="stToolbarActions"], [data-testid="stMainMenu"],
[data-testid="stAppDeployButton"] { visibility:hidden; }
[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapsedControl"],
[data-testid="stSidebarCollapseButton"] { visibility:visible !important; opacity:1 !important; }
[data-testid="stHeader"] { background:transparent; }
.stApp { padding-bottom: env(safe-area-inset-bottom, 0px); }
.stTabs [data-baseweb="tab-list"] { overflow-x:auto; scrollbar-width:none; flex-wrap:nowrap; }
.stTabs [data-baseweb="tab-list"]::-webkit-scrollbar { display:none; }
.stTabs [data-baseweb="tab"] { white-space:nowrap; }
.stButton > button, [data-baseweb="input"] input { min-height:44px; }
@media (max-width: 640px) {
  .block-container { padding:1rem .8rem 4rem; }
  .hero { padding:14px 16px; border-radius:14px; margin-bottom:14px; }
  .hero .t { font-size:1.2rem; }
  .hero .s { font-size:.74rem; }
  .qcard { padding:14px 16px; border-radius:14px; gap:14px; }
  .qcard .nm { font-size:1.15rem; }
  .qcard .px { font-size:2rem; }
  .stats { grid-template-columns:repeat(2,1fr); width:100%; }
  [data-testid="stMetric"] { padding:10px 12px; }
  [data-testid="stMetricValue"] { font-size:1.25rem; }
  .stTabs [data-baseweb="tab"] { padding:0 12px; height:40px; font-size:.9rem; }
  h1 { font-size:1.5rem; } h2 { font-size:1.3rem; } h3 { font-size:1.1rem; }
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def hero():
    on = market_open()
    badge = ('<span class="badge on"><i></i>盤中交易中</span>' if on
             else '<span class="badge off"><i></i>休市 / 已收盤</span>')
    now = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    st.markdown(f'<div class="hero"><div><div class="t">📈 台股分析儀表板</div>'
                f'<div class="s">技術面 · 三大法人 · 融資融券 · 估值 · 新聞 · 回測 · 投資建議 ｜ 台北時間 {now}</div></div>'
                f'{badge}</div>', unsafe_allow_html=True)


def quote_card(title, code, industry, px, pc, live):
    chg = px - pc if pc else 0.0
    pct = chg / pc * 100 if pc else 0.0
    cls, arrow = ("up", "▲") if chg > 0 else ("dn", "▼") if chg < 0 else ("flat", "—")
    e = _html.escape
    chip = f'<span class="chip">{e(str(industry))}</span>' if industry else ""
    meta, stats = "", ""
    if live:
        meta = (f'<div class="meta">⏱ {e(str(live["source"]))}｜{e(str(live["time"]))}｜'
                f'{"盤中即時價" if market_open() else "最新成交價"}</div>')
        stats = ('<div class="stats">'
                 f'<div><small>開</small><b>{live["open"]:.2f}</b></div>'
                 f'<div><small>高</small><b class="up">{live["high"]:.2f}</b></div>'
                 f'<div><small>低</small><b class="dn">{live["low"]:.2f}</b></div>'
                 f'<div><small>量(張)</small><b>{live["volume"] // 1000:,}</b></div></div>')
    return (f'<div class="qcard"><div><div><span class="nm">{e(str(title))}</span>'
            f'<span class="cd">{e(str(code))}</span></div>{chip}{meta}</div>'
            f'<div><div class="px {cls}">{px:.2f}</div>'
            f'<div class="chg {cls}">{arrow} {abs(chg):.2f} ({pct:+.2f}%)</div></div>{stats}</div>')


# ------------------------------------------------------------ 資料
def clean_ohlc(h, interval="1d"):
    """清理 Yahoo 回傳的 K 線,讓均線以「正確的 K 棒數」計算:
    - 移除收盤價為空/<=0 的列
    - 日/週/月線:日期統一(週→該週週一、月→當月1日),合併重複的 K 棒
      (Yahoo 常在週K/月K 多回傳一筆「今天」的重複資料)
    - 日線:移除成交量為 0 的填充列(非交易日)
    """
    if h is None or len(h) == 0:
        return pd.DataFrame()
    h = h[[c for c in ("Open", "High", "Low", "Close", "Volume") if c in h.columns]].copy()
    h = h.dropna(subset=["Close"])
    h = h[h["Close"] > 0]
    if h.empty:
        return pd.DataFrame()
    for c in ("Open", "High", "Low"):
        h[c] = h[c].where(h[c] > 0, h["Close"])
    h["Volume"] = h["Volume"].fillna(0)
    idx = pd.DatetimeIndex(h.index)
    if interval in ("1d", "1wk", "1mo"):
        idx = idx.normalize()
        if interval == "1wk":
            idx = idx - pd.to_timedelta(idx.weekday, unit="D")
        elif interval == "1mo":
            idx = idx.to_period("M").to_timestamp()
    h.index = idx
    if interval == "1d":
        h = h[h["Volume"] > 0]
    if h.index.has_duplicates:
        h = h.sort_index().groupby(level=0).agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "max"})
    return h.sort_index()


@st.cache_data(ttl=600)
def get_prices(code):
    return get_long_daily(code)


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


@st.cache_data(ttl=86400)
def get_stock_list():
    try:
        p = {"dataset": "TaiwanStockInfo"}
        if os.environ.get("FINMIND_TOKEN"):
            p["token"] = os.environ["FINMIND_TOKEN"]
        df = pd.DataFrame(requests.get(FINMIND, params=p, timeout=30).json().get("data", []))
        if not df.empty and {"stock_id", "stock_name"} <= set(df.columns):
            if "industry_category" not in df:
                df["industry_category"] = ""
            df = df[["stock_id", "stock_name", "industry_category"]].astype(str)
            return df.drop_duplicates("stock_id").sort_values("stock_id").reset_index(drop=True)
    except Exception:
        pass
    try:
        d = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", timeout=30).json()
        return pd.DataFrame({"stock_id": [x["公司代號"] for x in d], "stock_name": [x["公司簡稱"] for x in d],
                             "industry_category": [x.get("產業別", "") for x in d]})
    except Exception:
        return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category"])


def market_open():
    t = datetime.utcnow() + timedelta(hours=8)
    return t.weekday() < 5 and (9, 0) <= (t.hour, t.minute) <= (13, 30)


@st.cache_data(ttl=5)
def get_live_quote(code):
    try:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
        sess.get("https://mis.twse.com.tw/stock/index.jsp", timeout=8)
        for ex in ("tse", "otc"):
            r = sess.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                         params={"ex_ch": f"{ex}_{code}.tw", "json": 1, "delay": 0}, timeout=8).json()
            arr = r.get("msgArray", [])
            if not arr or not arr[0].get("n"):
                continue
            q = arr[0]
            price = _f(q.get("z"))
            if price is None:
                b, a = _f(str(q.get("b", "")).split("_")[0]), _f(str(q.get("a", "")).split("_")[0])
                price = (b + a) / 2 if b and a else _f(q.get("y"))
            if price is None:
                continue
            return dict(price=price, open=_f(q.get("o")) or price, high=_f(q.get("h")) or price,
                        low=_f(q.get("l")) or price, volume=int((_f(q.get("v")) or 0) * 1000),
                        name=q.get("n", ""), date=q.get("d"), time=f"{q.get('d')} {q.get('t')}", source="證交所即時",
                        prev=_f(q.get("y")), book={k: q.get(k, "") for k in "bagf"})
    except Exception:
        pass
    for suf in (".TW", ".TWO"):
        try:
            h = yf.Ticker(code + suf).history(period="1d", interval="1m")
            if len(h):
                return dict(price=float(h.Close.iloc[-1]), open=float(h.Open.iloc[0]), high=float(h.High.max()),
                            low=float(h.Low.min()), volume=int(h.Volume.sum()), name="",
                            date=str(h.index[-1].date()), time=str(h.index[-1])[:16], source="yfinance(可能延遲)")
        except Exception:
            pass
    return None


def merge_live(raw, live):
    if not live or raw.empty:
        return raw
    # 盤前/尚未成交時,即時資料的「現價」只是昨收,不可當成今天的 K 棒併入
    if not live.get("volume") or live["volume"] <= 0:
        return raw
    raw = raw.copy()
    try:
        day = pd.Timestamp(pd.to_datetime(live["date"]).date())
    except Exception:
        return raw
    if day < raw.index[-1]:
        return raw
    if day in raw.index:
        raw.loc[day, "Close"] = live["price"]
        raw.loc[day, "High"] = max(raw.loc[day, "High"], live["high"])
        raw.loc[day, "Low"] = min(raw.loc[day, "Low"], live["low"])
        raw.loc[day, "Volume"] = max(raw.loc[day, "Volume"], live["volume"])
    else:
        row = {c: 0.0 for c in raw.columns}
        row.update(Open=live["open"], High=live["high"], Low=live["low"], Close=live["price"], Volume=live["volume"])
        raw.loc[day] = pd.Series(row)
        raw = raw.sort_index()
    return raw


@st.cache_data(ttl=3600)
def get_dividends(code):
    for suf in (".TW", ".TWO"):
        try:
            s = yf.Ticker(code + suf).dividends
            if len(s):
                s.index = s.index.tz_localize(None)
                return s
        except Exception:
            pass
    return pd.Series(dtype=float)


@st.cache_data(ttl=3600)
def get_div_table(code):
    try:
        fm = finmind("TaiwanStockDividend", code, 365 * 7)
        if not fm.empty and "CashExDividendTradingDate" in fm:
            def col(n):
                return pd.to_numeric(fm[n], errors="coerce").fillna(0) if n in fm else pd.Series(0.0, index=fm.index)
            pay = pd.to_datetime(fm["CashDividendPaymentDate"], errors="coerce") if "CashDividendPaymentDate" in fm else pd.NaT
            t = pd.DataFrame({"除息日": pd.to_datetime(fm["CashExDividendTradingDate"], errors="coerce"),
                              "發放日": pay,
                              "現金股利": col("CashEarningsDistribution") + col("CashStatutorySurplus")})
            t = t[(t["現金股利"] > 0) & t["除息日"].notna()]
            if len(t):
                return t.sort_values("除息日").reset_index(drop=True)
    except Exception:
        pass
    d = get_dividends(code)
    if len(d):
        return pd.DataFrame({"除息日": d.index, "發放日": pd.NaT, "現金股利": d.values}).reset_index(drop=True)
    return pd.DataFrame(columns=["除息日", "發放日", "現金股利"])


def div_summary(t):
    now = pd.Timestamp.now()
    t = t.assign(年度=t["除息日"].dt.year)
    ttm = t[t["除息日"] >= now - pd.Timedelta(days=365)]["現金股利"].sum()
    full = t[t["年度"] < now.year].groupby("年度")["現金股利"].sum().tail(3)
    yrs = full.index
    avg3 = full.mean() if len(full) else None
    cnt = t[t["年度"].isin(yrs)].groupby("年度").size().mean() if len(full) else None
    mc = t[t["年度"].isin(yrs)]["除息日"].dt.month.value_counts()
    months = sorted(mc[mc >= max(1, len(full) / 2)].index.tolist()) if len(full) else []
    nxt = None
    if months:
        later = [m for m in months if m > now.month]
        nxt = later[0] if later else months[0]
    return dict(ttm=ttm, avg3=avg3, cnt=cnt, months=months, next_m=nxt,
                annual=t.groupby("年度")["現金股利"].sum())


@st.cache_data(ttl=3600)
def finmind(dataset, code, days):
    p = {"dataset": dataset, "data_id": code,
         "start_date": (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")}
    if os.environ.get("FINMIND_TOKEN"):
        p["token"] = os.environ["FINMIND_TOKEN"]
    try:
        d = requests.get(FINMIND, params=p, timeout=25).json().get("data", [])
        df = pd.DataFrame(d)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception:
        return pd.DataFrame()


def _news_from_rss(url, direct=False):
    """解析 RSS → 新聞清單(台北時間、由新到舊)。direct=True 時從 Bing 中轉網址取出原始出處連結。"""
    from email.utils import parsedate_to_datetime
    from datetime import timezone
    from urllib.parse import urlparse, parse_qs
    tw = timezone(timedelta(hours=8))
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    out, seen = [], set()
    for i in ET.fromstring(r.content).iter("item"):
        link = (i.findtext("link", "") or "").strip()
        if direct:
            real = parse_qs(urlparse(link).query).get("url", [""])[0]
            if real.startswith("http"):
                link = real
        title = (i.findtext("title", "") or "").strip()
        if not link or not title or link in seen:
            continue
        seen.add(link)
        raw_d = i.findtext("pubDate", "") or ""
        try:
            dt = parsedate_to_datetime(raw_d).astimezone(tw)
        except Exception:
            dt = None
        out.append({"title": title, "link": link, "ts": dt.timestamp() if dt else 0,
                    "date": dt.strftime("%Y-%m-%d %H:%M") if dt else raw_d[:16]})
    out.sort(key=lambda x: x["ts"], reverse=True)  # 最新在最上面
    return out[:12]


@st.cache_data(ttl=1800)
def get_news(code, name):
    q = quote(f"{code} {name} 股".strip())
    # 1) 先用 Bing News(可取得出處原始網址,App 內也能直接開);2) 失敗再退回 Google News
    for url, direct in ((f"https://www.bing.com/news/search?q={q}&format=rss&setlang=zh-TW&cc=TW", True),
                        (f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", False)):
        try:
            res = _news_from_rss(url, direct)
            if res:
                return res
        except Exception:
            continue
    return []


def indicators(df):
    df = df.copy()
    c, h, l = df.Close, df.High, df.Low
    for n in (5, 20, 60):
        df[f"MA{n}"] = c.rolling(n).mean()
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    ls = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + g / ls)
    df["DIF"] = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    df["MACD"] = df.DIF.ewm(span=9, adjust=False).mean()
    df["OSC"] = df.DIF - df.MACD
    rsv = (c - l.rolling(9).min()) / (h.rolling(9).max() - l.rolling(9).min()) * 100
    df["K"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    df["D"] = df.K.ewm(alpha=1 / 3, adjust=False).mean()
    m, s = c.rolling(20).mean(), c.rolling(20).std()
    df["BB_UP"], df["BB_LOW"] = m + 2 * s, m - 2 * s
    df["VR"] = df.Volume / df.Volume.rolling(20).mean()
    return df


def tech_components(df):
    c = df.Close
    up = (df.DIF.shift() <= df.MACD.shift()) & (df.DIF > df.MACD)
    dn = (df.DIF.shift() >= df.MACD.shift()) & (df.DIF < df.MACD)
    kup = (df.K.shift() <= df.D.shift()) & (df.K > df.D)
    kdn = (df.K.shift() >= df.D.shift()) & (df.K < df.D)
    comp = {
        "趨勢(均線排列)": np.select([(c > df.MA20) & (df.MA20 > df.MA60), (c < df.MA20) & (df.MA20 < df.MA60), c > df.MA60], [2, -2, 1], -1),
        "MACD": np.select([up, dn, (df.OSC > 0) & (df.OSC > df.OSC.shift()), (df.OSC < 0) & (df.OSC < df.OSC.shift())], [2, -2, 1, -1], 0),
        "RSI": np.select([df.RSI > 80, df.RSI > 70, df.RSI < 20, df.RSI < 30], [-2, -1, 2, 1], 0),
        "KD": np.select([kup & (df.K < 50), kdn & (df.K > 50)], [1, -1], 0),
        "布林通道": np.select([c > df.BB_UP, c < df.BB_LOW], [-1, 1], 0),
        "量價": np.select([(df.VR > 1.5) & (c > c.shift()), (df.VR > 1.5) & (c < c.shift())], [1, -1], 0),
    }
    return pd.DataFrame(comp, index=df.index)


def prep_inst(raw):
    if raw.empty:
        return pd.DataFrame()
    raw = raw.copy()
    raw["net"] = (raw.buy - raw.sell) / 1000
    mp = {"Foreign_Investor": "外資", "Foreign_Dealer_Self": "外資", "Investment_Trust": "投信",
          "Dealer_self": "自營商", "Dealer_Hedging": "自營商", "Dealer": "自營商"}
    raw["who"] = raw["name"].map(mp)
    return raw.dropna(subset=["who"]).pivot_table(index="date", columns="who", values="net", aggfunc="sum").fillna(0)


def score_inst(inst):
    if inst.empty:
        return 0, ["無三大法人資料"]
    s, notes = 0, []
    r5 = inst.tail(5).sum()
    for who in ("外資", "投信"):
        if who in r5:
            v = r5[who]
            pts = 1 if v > 0 else -1 if v < 0 else 0
            s += pts
            notes.append(f"{pts:+d}  {who}近5日合計 {v:,.0f} 張")
    return s, notes


def score_margin(px, m):
    if m.empty or "MarginPurchaseTodayBalance" not in m:
        return 0, ["無融資融券資料"], {}
    m = m.sort_values("date").reset_index(drop=True)
    bal, sh = m.MarginPurchaseTodayBalance, m.ShortSaleTodayBalance
    n = min(10, len(m) - 1)
    mc = (bal.iloc[-1] - bal.iloc[-1 - n]) / max(bal.iloc[-1 - n], 1) * 100
    pc = (px.Close.iloc[-1] / px.Close.iloc[-1 - n] - 1) * 100
    lim = m.MarginPurchaseLimit.iloc[-1]
    usage = bal.iloc[-1] / lim * 100 if lim else np.nan
    ratio = sh.iloc[-1] / max(bal.iloc[-1], 1) * 100
    s, notes = 0, []
    if pc > 3 and mc > 8: s -= 1; notes.append(f"-1  近{n}日股價{pc:+.1f}%、融資{mc:+.1f}%:散戶追價,籌碼偏浮")
    elif pc > 3 and mc < 0: s += 2; notes.append(f"+2  近{n}日股價{pc:+.1f}%、融資{mc:+.1f}%:價漲資減,籌碼沉澱")
    elif pc < -3 and mc < -5: s += 1; notes.append(f"+1  近{n}日股價{pc:+.1f}%、融資{mc:+.1f}%:融資退場")
    elif pc < -3 and mc > 3: s -= 2; notes.append(f"-2  近{n}日股價{pc:+.1f}%、融資{mc:+.1f}%:價跌資增,斷頭賣壓風險")
    if usage == usage and usage > 60: s -= 1; notes.append(f"-1  融資使用率 {usage:.0f}%,偏高")
    if ratio > 30: s += 1; notes.append(f"+1  券資比 {ratio:.0f}%,有軋空題材")
    if not notes: notes.append("融資融券無明顯訊號")
    return s, notes, {"融資使用率%": round(usage, 1), "券資比%": round(ratio, 1), f"近{n}日融資變動%": round(mc, 1)}


def score_val(per, divs=None, price=None):
    if per.empty:
        if divs is not None and len(divs) and price:
            ttm = divs[divs.index >= pd.Timestamp.now() - pd.Timedelta(days=365)].sum()
            y = ttm / price * 100
            s = 1 if y >= 4 else 0
            note = f"{s:+d}  無本益比資料(ETF 或無獲利個股),以近12個月配息 {ttm:.2f} 元推算殖利率約 {y:.2f}%"
            return s, [note], {"殖利率%": round(y, 2)}
        return 0, ["無估值資料(ETF 沒有本益比)"], {}
    cur = per.iloc[-1]
    s, notes = 0, []
    pos = per[per.PER > 0].PER
    pct = (pos <= cur.PER).mean() * 100 if cur.PER > 0 and len(pos) > 30 else np.nan
    if pct == pct:
        if pct < 25: s += 1; notes.append(f"+1  本益比 {cur.PER:.1f},位於近3年 {pct:.0f}% 分位(相對便宜)")
        elif pct > 80: s -= 1; notes.append(f"-1  本益比 {cur.PER:.1f},位於近3年 {pct:.0f}% 分位(相對昂貴)")
        else: notes.append(f"0   本益比 {cur.PER:.1f},位於近3年 {pct:.0f}% 分位(中性)")
    elif cur.PER <= 0:
        s -= 1; notes.append("-1  目前無本益比(可能虧損)")
    if cur.dividend_yield >= 4: s += 1; notes.append(f"+1  殖利率 {cur.dividend_yield:.2f}%,具防守性")
    return s, notes, {"本益比": cur.PER, "股價淨值比": cur.PBR, "殖利率%": cur.dividend_yield, "PER分位%": None if pct != pct else round(pct)}


def backtest(df, score, buy_th, sell_th, fee=0.001425, tax=0.003):
    cur, pos = 0, []
    for s in score.values:
        cur = 1 if s >= buy_th else 0 if s <= sell_th else cur
        pos.append(cur)
    pos = pd.Series(pos, index=df.index).shift(1).fillna(0)
    ret = df.Close.pct_change().fillna(0)
    ch = pos.diff().fillna(pos.iloc[0])
    strat = pos * ret - ch.abs() * fee - (ch < 0) * tax
    eq, bh = (1 + strat).cumprod(), (1 + ret).cumprod()
    trades, ent = [], None
    for dt, v in ch.items():
        if v > 0: ent = df.Close[dt]
        elif v < 0 and ent: trades.append(df.Close[dt] / ent - 1 - 2 * fee - tax); ent = None
    yrs = max(len(df) / 245, 0.1)
    mdd = lambda e: ((e / e.cummax()) - 1).min()
    return eq, bh, {
        "策略報酬%": (eq.iloc[-1] - 1) * 100, "買進持有報酬%": (bh.iloc[-1] - 1) * 100,
        "策略年化%": (eq.iloc[-1] ** (1 / yrs) - 1) * 100, "策略最大回落%": mdd(eq) * 100,
        "買進持有最大回落%": mdd(bh) * 100, "交易次數": len(trades),
        "勝率%": (np.mean([t > 0 for t in trades]) * 100 if trades else 0)}


def tech_chart(df, n=200, levels=None):
    d = df.tail(n)
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, row_heights=[.5, .15, .17, .18], vertical_spacing=.02)
    fig.add_trace(go.Candlestick(x=d.index, open=d.Open, high=d.High, low=d.Low, close=d.Close, name="K線",
                                 increasing_line_color="red", decreasing_line_color="green"), 1, 1)
    for col, color in (("MA5", "orange"), ("MA20", "blue"), ("MA60", "purple")):
        fig.add_trace(go.Scatter(x=d.index, y=d[col], name=col, line=dict(color=color, width=1)), 1, 1)
    for col in ("BB_UP", "BB_LOW"):
        fig.add_trace(go.Scatter(x=d.index, y=d[col], name=col, line=dict(color="gray", width=1, dash="dot")), 1, 1)
    fig.add_trace(go.Bar(x=d.index, y=d.Volume, name="成交量", marker_color=np.where(d.Close >= d.Open, "red", "green")), 2, 1)
    fig.add_trace(go.Bar(x=d.index, y=d.OSC, name="OSC", marker_color=np.where(d.OSC >= 0, "red", "green")), 3, 1)
    fig.add_trace(go.Scatter(x=d.index, y=d.DIF, name="DIF"), 3, 1)
    fig.add_trace(go.Scatter(x=d.index, y=d.MACD, name="MACD"), 3, 1)
    fig.add_trace(go.Scatter(x=d.index, y=d.K, name="K"), 4, 1)
    fig.add_trace(go.Scatter(x=d.index, y=d.D, name="D"), 4, 1)
    fig.add_trace(go.Scatter(x=d.index, y=d.RSI, name="RSI", line=dict(dash="dot")), 4, 1)
    for key, label, color in (("big", "大舉買入", "darkgreen"), ("small", "小額加碼", "green"),
                              ("trim", "分批減碼", "orange"), ("stop", "停損", "red")):
        if levels:
            fig.add_hline(y=levels[key], row=1, col=1, line_dash="dash", line_color=color,
                          annotation_text=f"{label} {levels[key]:.2f}", annotation_position="right")
    fig.update_layout(height=800, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=10, b=10),
                      legend=dict(orientation="h", y=1.03))
    return fig


UP, DN = "#e0383e", "#1f9d55"
MA_COLORS = {5: "#5aa9e6", 10: "#7b4fd6", 20: "#e8803a", 60: "#e6b422", 120: "#8b3a2e", 240: "#888888"}
TF = {"5分": ("5m", "60d"), "15分": ("15m", "60d"), "30分": ("30m", "60d"), "60分": ("60m", "730d"),
      "日": ("1d", "max"), "週": ("1wk", "max"), "月": ("1mo", "max")}


def _to_taipei(h):
    idx = h.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("Asia/Taipei").tz_localize(None)
    h = h.copy()
    h.index = idx
    return h[["Open", "High", "Low", "Close", "Volume"]]


@st.cache_data(ttl=30)
def get_ohlc(code, tf):
    itv, per = TF[tf]
    for suf in (".TW", ".TWO"):
        try:
            h = yf.Ticker(code + suf).history(period=per, interval=itv, auto_adjust=False)
        except Exception:
            continue
        if not h.empty:
            h = clean_ohlc(_to_taipei(h), itv)
            if not h.empty:
                return h
    return pd.DataFrame()


@st.cache_data(ttl=10)
def get_intraday(code):
    for suf in (".TW", ".TWO"):
        try:
            h = yf.Ticker(code + suf).history(period="5d", interval="1m", auto_adjust=False)
        except Exception:
            continue
        if not h.empty:
            h = _to_taipei(h)
            return h[h.index.normalize() == h.index[-1].normalize()]
    return pd.DataFrame()


def apply_live(h, live, tf):
    if not live or h.empty or tf.endswith("分") or not live.get("volume"):
        return h
    if tf == "日":
        return merge_live(h, live)
    try:
        day = pd.Timestamp(pd.to_datetime(live["date"]).date())
    except Exception:
        return h
    h = h.copy()
    i = h.index[-1]
    start = day - pd.Timedelta(days=day.weekday()) if tf == "週" else day.replace(day=1)
    if start < i:
        return h
    if start > i:  # Yahoo 尚未產生本週/本月的 K 棒 → 補一根
        h.loc[start] = pd.Series(dict(Open=live["open"], High=live["high"], Low=live["low"],
                                      Close=live["price"], Volume=live["volume"]))
        return h.sort_index()
    h.loc[i, "Close"] = live["price"]
    h.loc[i, "High"] = max(h.loc[i, "High"], live["high"], live["price"])
    h.loc[i, "Low"] = min(h.loc[i, "Low"], live["low"], live["price"])
    return h


def kline_header(h, mas, sel, tf, title):
    r = h.iloc[-1]
    pc = h.Close.iloc[-2] if len(h) > 1 else r.Close
    col = lambda v: UP if v > pc else DN if v < pc else "#888"
    date = h.index[-1].strftime("%Y/%m/%d %H:%M" if tf.endswith("分") else "%Y/%m/%d")
    line = (f"<b style='font-size:1.1rem'>{title}</b><br>{date} "
            f"開 <b style='color:{col(r.Open)}'>{r.Open:.2f}</b> 高 <b style='color:{col(r.High)}'>{r.High:.2f}</b> "
            f"低 <b style='color:{col(r.Low)}'>{r.Low:.2f}</b> 收 <b style='color:{col(r.Close)}'>{r.Close:.2f}</b> "
            f"量 <b>{int(r.Volume // 1000):,}</b> 漲跌 <b style='color:{col(r.Close)}'>{r.Close - pc:+.2f}</b>")
    parts = []
    for n in sel:
        sr = mas[n].dropna()
        if not len(sr):
            continue
        v, arrow = sr.iloc[-1], ""
        if len(sr) > 1:
            arrow = f" <span style='color:{UP if v > sr.iloc[-2] else DN}'>{'▲' if v > sr.iloc[-2] else '▼'}</span>"
        parts.append(f"<span style='color:{MA_COLORS[n]}'>■ MA{n}</span> {v:.2f}{arrow}")
    return line + "<br>" + " ".join(parts)


def kline_chart(h, mas, sel, tf, nshow):
    fmt = "%m/%d %H:%M" if tf.endswith("分") else "%Y/%m/%d"
    d = h.tail(nshow)
    x = list(d.index.strftime(fmt))
    prev = h.Close.shift().reindex(d.index).values
    mv = {n: mas[n].reindex(d.index).values for n in sel}
    hover = []
    for i in range(len(d)):
        r = d.iloc[i]
        chg = 0.0 if pd.isna(prev[i]) else r.Close - prev[i]
        pct = 0.0 if pd.isna(prev[i]) or not prev[i] else chg / prev[i] * 100
        ma_txt = " ".join(f"MA{n} {mv[n][i]:.2f}" for n in sel if not pd.isna(mv[n][i]))
        hover.append(f"<b>{x[i]}</b><br>開 {r.Open:.2f} 高 {r.High:.2f}<br>低 {r.Low:.2f} 收 {r.Close:.2f}"
                     f"<br>量 {int(r.Volume // 1000):,} 張<br>漲跌 {chg:+.2f} ({pct:+.2f}%)"
                     + (f"<br>{ma_txt}" if ma_txt else ""))
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[.75, .25], vertical_spacing=.02)
    fig.add_trace(go.Candlestick(x=x, open=d.Open, high=d.High, low=d.Low, close=d.Close, name="K線",
                                 increasing=dict(line=dict(color=UP), fillcolor=UP),
                                 decreasing=dict(line=dict(color=DN), fillcolor=DN),
                                 hovertext=hover, hoverinfo="text", hoverlabel=dict(align="left")), 1, 1)
    for n in sel:
        fig.add_trace(go.Scatter(x=x, y=mv[n], name=f"MA{n}", mode="lines",
                                 line=dict(color=MA_COLORS[n], width=1.3), hoverinfo="skip"), 1, 1)
    fig.add_trace(go.Bar(x=x, y=d.Volume / 1000, name="量(張)", hoverinfo="skip",
                         marker_color=np.where(d.Close >= d.Open, UP, DN)), 2, 1)
    fig.update_xaxes(type="category", nticks=10, showspikes=True, spikemode="across", spikesnap="cursor",
                     spikethickness=1, rangeslider_visible=False)
    fig.update_layout(height=650, hovermode="x", dragmode="pan", margin=dict(l=10, r=10, t=10, b=10),
                      legend=dict(orientation="h", y=1.04))
    return fig


@st.cache_data(ttl=600)
def get_long_daily(code):
    for suf in (".TW", ".TWO"):
        try:
            h = yf.Ticker(code + suf).history(period="max", interval="1d", auto_adjust=False)
        except Exception:
            continue
        if not h.empty:
            h = clean_ohlc(_to_taipei(h), "1d")
            if not h.empty:
                return h
    return pd.DataFrame()


RSI_ZONES = ["<30 超賣", "30~50", "50~70", ">70 超買"]
HLABEL = {5: "5日", 10: "10日", 20: "20日", 60: "60日(約3個月)", 120: "120日(半年)",
          240: "240日(1年)", 480: "480日(2年)", 720: "720日(3年)"}


def situation_stats(hd, horizons=(5, 10, 20, 60, 120, 240, 480, 720), cost=0.00585, z=1.96):
    d = indicators(hd).dropna(subset=["MA60", "K", "RSI", "BB_UP"])
    if len(d) < 250:
        return None
    sc = tech_components(d).sum(axis=1)
    rz = pd.cut(d.RSI, [0, 30, 50, 70, 100.1], labels=False)
    above = d.Close > d.MA60
    near = (sc - sc.iloc[-1]).abs() <= 1
    strict = near & (above == above.iloc[-1]) & (rz == rz.iloc[-1])
    rows = []
    for h in horizons:
        fwd = d.Close.shift(-h) / d.Close - 1 - cost
        ok = fwd.notna()
        use, label = (strict, "分數相近+同季線位置+同RSI區間") if (strict & ok).sum() >= 30 else (near, "僅技術分數相近(樣本不足,放寬條件)")
        sel = fwd[ok & use]
        if not len(sel):
            continue
        n_eff = max(len(sel) / h, 1.0)
        w = (sel > 0).mean()
        den = 1 + z * z / n_eff
        mid = (w + z * z / (2 * n_eff)) / den
        half = z * np.sqrt(w * (1 - w) / n_eff + z * z / (4 * n_eff ** 2)) / den
        rows.append({"持有天數": h, "期間": HLABEL.get(h, f"{h}日"), "樣本數": len(sel), "獨立樣本約": n_eff, "勝率": w * 100, "勝率下限": max(mid - half, 0) * 100,
                     "勝率上限": min(mid + half, 1) * 100, "平均報酬": sel.mean() * 100, "中位數": sel.median() * 100,
                     "最差": sel.min() * 100, "最佳": sel.max() * 100, "全期基準勝率": (fwd[ok] > 0).mean() * 100,
                     "年化中位數": (max(1 + sel.median(), 1e-6) ** (245 / h) - 1) * 100 if h >= 120 else np.nan,
                     "條件": label})
    if not rows:
        return None
    return dict(table=pd.DataFrame(rows), score=int(sc.iloc[-1]), above=bool(above.iloc[-1]),
                zone=RSI_ZONES[int(rz.iloc[-1])], days=len(d), years=len(d) / 245)


def pick_row(SS):
    T = SS["table"]
    r = T[T["持有天數"] == 20]
    return (r if len(r) else T).iloc[0]


def streak(series):
    v = series.dropna().values
    if len(v) == 0 or v[-1] == 0:
        return 0, 0, 0.0
    sg = 1 if v[-1] > 0 else -1
    n, tot = 0, 0.0
    for x in v[::-1]:
        if x * sg > 0:
            n += 1
            tot += x
        else:
            break
    return sg, n, tot


def push_telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data={"chat_id": chat, "text": text}, timeout=8)
        except Exception:
            pass


@st.cache_resource
def _line_state():
    return {"day": "", "n": 0, "blocked": ""}


def push_line(text):
    tok = os.environ.get("LINE_CHANNEL_TOKEN")
    if not tok:
        return "LINE:未設定"
    stt = _line_state()
    today = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")
    if stt["blocked"] == today[:7]:
        return "LINE:本月免費則數已用完,已暫停(下個月自動恢復)"
    if stt["day"] != today:
        stt["day"], stt["n"] = today, 0
    if stt["n"] >= int(os.environ.get("LINE_MAX_PER_DAY", "5") or 5):
        return "LINE:已達每日推播上限"
    hdr, base = {"Authorization": f"Bearer {tok}"}, "https://api.line.me/v2/bot/message"
    try:
        if os.environ.get("LINE_BROADCAST") == "1":
            url, body = f"{base}/broadcast", {"messages": [{"type": "text", "text": (text + "\n※僅供參考,非投資建議")[:4900]}]}
        elif os.environ.get("LINE_USER_IDS"):
            ids = [x.strip() for x in os.environ["LINE_USER_IDS"].split(",") if x.strip()]
            url, body = f"{base}/multicast", {"to": ids[:500], "messages": [{"type": "text", "text": (text + "\n※僅供參考,非投資建議")[:4900]}]}
        elif os.environ.get("LINE_USER_ID"):
            url, body = f"{base}/push", {"to": os.environ["LINE_USER_ID"], "messages": [{"type": "text", "text": text[:4900]}]}
        else:
            return "LINE:缺 LINE_USER_ID"
        r = requests.post(url, headers=hdr, json=body, timeout=8)
        stt["n"] += 1
        if r.status_code == 429:
            stt["blocked"] = today[:7]
            return "LINE:本月免費則數已用完(429)"
        return "LINE:成功" if r.status_code == 200 else f"LINE:失敗 {r.status_code}"
    except Exception as e:
        return f"LINE:連線失敗 {e}"


@st.cache_resource
def _mail_state():
    return {"day": "", "n": 0}


def _send_email(subject, body):
    user, pw, to = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASSWORD"), os.environ.get("EMAIL_TO")
    if not (user and pw and to):
        return "Email:未設定"
    host = os.environ.get("EMAIL_SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("EMAIL_SMTP_PORT") or 465)
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, os.environ.get("EMAIL_FROM") or user
    msg["To"] = ", ".join(x.strip() for x in to.split(",") if x.strip())
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as srv:
                srv.login(user, pw)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as srv:
                srv.starttls()
                srv.login(user, pw)
                srv.send_message(msg)
        return "Email:成功"
    except Exception as e:
        return f"Email:失敗 {e}"


def push_email(items):
    if not items or not os.environ.get("EMAIL_TO"):
        return
    stt = _mail_state()
    today = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")
    if stt["day"] != today:
        stt["day"], stt["n"] = today, 0
    if stt["n"] >= int(os.environ.get("EMAIL_MAX_PER_DAY") or 30):
        return
    stt["n"] += 1
    first = items[0][0].replace("\n", " ")[:60]
    _send_email(f"[股票提醒] {first}" + (f"(共 {len(items)} 則)" if len(items) > 1 else ""),
                "\n\n".join(t for t, _ in items) + "\n\n※ 本通知僅供參考,不構成投資建議。")


def test_push():
    text, out = "【測試】股票提醒推播已連線", []
    tok, uid = os.environ.get("LINE_CHANNEL_TOKEN"), os.environ.get("LINE_USER_ID")
    if tok and uid:
        try:
            r = requests.post("https://api.line.me/v2/bot/message/push", headers={"Authorization": f"Bearer {tok}"},
                              json={"to": uid, "messages": [{"type": "text", "text": text}]}, timeout=8)
            if r.status_code == 200:
                out.append("LINE:成功")
            elif r.status_code == 429:
                out.append("LINE:本月免費則數已用完")
            else:
                out.append(f"LINE:失敗 {r.status_code} {r.text[:100]}")
        except Exception as e:
            out.append(f"LINE:連線失敗 {e}")
    else:
        out.append("LINE:未設定")
    tt, ch = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tt and ch:
        try:
            r = requests.post(f"https://api.telegram.org/bot{tt}/sendMessage", data={"chat_id": ch, "text": text}, timeout=8)
            out.append("Telegram:成功" if r.status_code == 200 else f"Telegram:失敗 {r.status_code} {r.text[:100]}")
        except Exception as e:
            out.append(f"Telegram:連線失敗 {e}")
    else:
        out.append("Telegram:未設定")
    out.append(_send_email("[股票提醒] 測試", text))
    return out


def push_batch(items):
    if not items:
        return
    if not st.session_state.get("notify_ok", True):
        return
    allt = "\n\n".join(t for t, _ in items)[:3900]
    strong = [t for t, sg in items if sg]
    push_telegram(allt)
    push_email(items)
    if os.environ.get("LINE_PUSH_ALL") == "1":
        push_line(allt)
    elif strong:
        push_line("\n\n".join(strong)[:3900])


def build_alerts(df, inst, margin, live, idy, prevc, th):
    A, info = [], {}

    def add(key, sc, title, detail, force=False):
        A.append(dict(key=key, s=sc, title=title, detail=detail, force=force))

    avgv = df.Volume.iloc[-21:-1].mean() / 1000 if len(df) > 21 else 0
    info["20日均量(張)"] = round(avgv)

    if inst is not None and not inst.empty and avgv:
        found = {}
        for who, wt in (("外資", 2), ("投信", 2), ("自營商", 1)):
            if who not in inst:
                continue
            sg, n, tot = streak(inst[who])
            info[f"{who}連續"] = f"{'買超' if sg > 0 else '賣超' if sg < 0 else '—'} {n} 天"
            if n >= th["days"]:
                ratio = abs(tot / n) / avgv * 100
                if ratio >= th["ratio"]:
                    found[who] = sg
                    word = "買超" if sg > 0 else "賣超"
                    add(f"inst-{who}-{sg}-{n}", sg * wt, f"{'📈' if sg > 0 else '📉'} {who}連續 {n} 天{word}",
                        f"合計 {tot:,.0f} 張,日均 {tot / n:,.0f} 張(約 20 日均量的 {ratio:.1f}%)", force=(who == "自營商"))
        if found.get("外資") and found.get("外資") == found.get("投信"):
            sg = found["外資"]
            add(f"inst-both-{sg}", sg, f"{'📈' if sg > 0 else '📉'} 外資與投信同步{'買超' if sg > 0 else '賣超'}",
                "兩大法人方向一致,訊號較強")

    if margin is not None and not margin.empty and "MarginPurchaseTodayBalance" in margin:
        m = margin.sort_values("date").reset_index(drop=True)
        bal, sh = m.MarginPurchaseTodayBalance, m.ShortSaleTodayBalance
        usage = bal / m.MarginPurchaseLimit.replace(0, np.nan) * 100
        if usage.notna().any():
            u = usage.iloc[-1]
            info["融資使用率%"] = round(float(u), 1)
            hi = 0
            for x in usage.values[::-1]:
                if x >= th["use"]:
                    hi += 1
                else:
                    break
            if hi:
                add(f"muse-{hi}", -1, "⚠️ 融資使用率偏高",
                    f"目前 {u:.1f}%,已連續 {hi} 天 ≥ {th['use']}%,融資籌碼擁擠,下跌時有斷頭賣壓風險")
        sg, n, tot = streak(bal.diff())
        info["融資連續"] = f"{'增加' if sg > 0 else '減少' if sg < 0 else '—'} {n} 天"
        if n >= th["mdays"] and len(df) > n:
            chg = (df.Close.iloc[-1] / df.Close.iloc[-1 - n] - 1) * 100
            if sg > 0:
                add(f"mup-{n}", -2 if chg < 0 else -1, f"⚠️ 融資連續 {n} 天增加",
                    f"增加 {tot:,.0f} 張,期間股價 {chg:+.1f}%," + ("價跌資增,斷頭風險升高" if chg < 0 else "散戶追價,籌碼偏浮"))
            elif sg < 0:
                add(f"mdn-{n}", 1, f"📈 融資連續 {n} 天減少",
                    f"減少 {abs(tot):,.0f} 張,期間股價 {chg:+.1f}%,浮額退場、籌碼趨於沉澱")
        ratio = sh.iloc[-1] / max(bal.iloc[-1], 1) * 100
        info["券資比%"] = round(float(ratio), 1)
        sg2, n2, tot2 = streak(sh.diff())
        if sg2 > 0 and n2 >= th["mdays"]:
            add(f"sup-{n2}", 0, f"⚠️ 融券連續 {n2} 天增加",
                f"增加 {tot2:,.0f} 張,券資比 {ratio:.0f}%" + (",偏高:有軋空題材,但也代表空方力道強" if ratio > 30 else ""))

    vol = live["volume"] / 1000 if live else df.Volume.iloc[-1] / 1000
    cur = live["price"] if live else float(df.Close.iloc[-1])
    if avgv and vol:
        frac = 1.0
        if market_open():
            t = datetime.utcnow() + timedelta(hours=8)
            frac = min(max(((t.hour * 60 + t.minute) - 540) / 270, 0.1), 1.0)
        pace = vol / (avgv * frac)
        info["量比(盤中推估)"] = round(pace, 2)
        pct = (cur / prevc - 1) * 100
        if pace >= th["mult"]:
            if pct >= 1: sc, tip = 1, "爆量上漲,買盤積極"
            elif pct <= -1: sc, tip = -2, "爆量下跌,賣壓沉重"
            else: sc, tip = 0, "爆量但價格變動不大,多空分歧"
            add(f"vol-{int(pace)}", sc, f"🔥 即時爆量:量比 {pace:.1f} 倍",
                f"累計 {vol:,.0f} 張(20 日均量 {avgv:,.0f} 張),股價 {pct:+.2f}%,{tip}")
    if market_open() and idy is not None and len(idy) >= 40:
        v5 = idy.Volume.tail(5).sum()
        base = idy.Volume.iloc[:-5].tail(60).mean() * 5
        if base > 0 and v5 / base >= 4:
            chg5 = (idy.Close.iloc[-1] / idy.Close.iloc[-6] - 1) * 100
            sc = 1 if chg5 >= 0.3 else -1 if chg5 <= -0.3 else 0
            add(f"burst-{idy.index[-1]:%H%M}", sc, f"🔥 近 5 分鐘爆量:約平常的 {v5 / base:.1f} 倍",
                f"近 5 分鐘股價 {chg5:+.2f}%(分時資料可能有延遲)")
    return A, info


def combine_advice(alerts, lv, c, total):
    net = sum(a["s"] for a in alerts)
    low, high, broke = c <= lv["small"], c >= lv["trim"], c <= lv["stop"]
    if net >= 2:
        if high: msg = "🟠 多項訊號偏多,但股價已在高檔區:不建議追高,持有者可續抱,新進場等拉回。"
        elif low: msg = "🟢 多項訊號偏多,且股價在低檔區:可考慮分批買進(先小量,再依走勢加碼)。"
        else: msg = "🟢 多項訊號偏多,股價在中間區:可小量布局,拉回月線附近再加碼。"
        if total <= -4: msg += " 但整體評分偏空,請降低部位。"
    elif net <= -2:
        if any(a.get("id") == "S7L" for a in alerts): msg = "🔻 長線翻空(季線失守):應果斷停損/停利離場,禁止低接或攤平。"
        elif broke: msg = "🔻 訊號偏空且已跌破停損參考價:建議減碼或停損出場。"
        elif low: msg = "🟡 訊號偏空,但股價已跌到低檔區:不要一次接刀,等止跌訊號,最多小量試單。"
        elif high: msg = "🔴 訊號偏空且股價在高檔區:持有者建議分批減碼或獲利了結。"
        else: msg = "🔴 訊號偏空:持有者可考慮減碼並設好停損;未持有者先觀望。"
    else:
        msg = "⚪ 多空訊號互相抵銷,維持原本的操作建議(見「總覽與建議」)。"
    # 短線提醒合計(net)與中線綜合評分(total)方向相反 → 明確提示
    if (net >= 2 and total < 0) or (net <= -2 and total > 0):
        msg += "\n\n⚠️ 短線訊號與中線評分方向不一致(短線反彈/中長線趨勢分歧)"
    return net, msg


COST_RT = 0.00585
def make_rules(sl=7.0, tp=15.0):
    return [("持有20日(不設停損)", "time"), (f"固定停損 -{sl:g}%", "fix"), (f"固定停利 +{tp:g}%", "tp"),
            (f"固定停損 -{sl:g}% + 停利 +{tp:g}%", "fixtp"), ("ATR停損(進場價 − 2×ATR)", "atr"),
            ("移動停損(跌破近20日區間下緣)", "trail"), ("跌破20日線即出場", "sig")]

def sstreak(x):
    out, cnt = np.zeros(len(x)), 0
    for i, v in enumerate(np.asarray(x, dtype=float)):
        if v > 0:
            cnt = cnt + 1 if cnt > 0 else 1
        elif v < 0:
            cnt = cnt - 1 if cnt < 0 else -1
        else:
            cnt = 0
        out[i] = cnt
    return pd.Series(out, index=x.index)


def simulate(entry, sell, A, rule, lo, hi, maxhold=20, sl=0.07, tp=0.15):
    O, H, L, C, ATR = A
    trades, i = [], lo
    while i < hi - 2:
        if not entry[i] or np.isnan(ATR[i]) or not O[i + 1] > 0:
            i += 1
            continue
        e, ep = i + 1, O[i + 1]
        stop = {"fix": ep * (1 - sl), "fixtp": ep * (1 - sl), "atr": ep - 2 * ATR[i], "trail": ep - 2.5 * ATR[i]}.get(rule)
        tpv = ep * (1 + tp) if rule in ("fixtp", "tp") else None
        peak, mae, xp = ep, 0.0, None
        last = min(e + maxhold - 1, hi - 1)
        jx = last
        for j in range(e, last + 1):
            mae = min(mae, L[j] / ep - 1)
            if rule == "trail" and j > e:
                stop = max(stop, L[max(0, j - 20):j].min())
            if stop is not None and L[j] <= stop:
                xp, jx = min(stop, O[j]), j
                break
            if tpv is not None and H[j] >= tpv:
                xp, jx = max(tpv, O[j]), j
                break
            if rule == "sig" and sell[j] and j + 1 < hi:
                xp, jx = O[j + 1], j + 1
                break
        if xp is None:
            xp = C[last]
        trades.append((xp / ep - 1 - COST_RT, mae, jx - e + 1))
        i = max(jx, i + 1)
    return trades


def trade_stats(tr):
    if not tr:
        return None
    r = np.array([t[0] for t in tr])
    eq = np.cumprod(1 + r)
    return {"交易次數": len(r), "勝率%": (r > 0).mean() * 100, "平均報酬%": r.mean() * 100,
            "中位報酬%": float(np.median(r)) * 100, "最差單筆%": r.min() * 100,
            "平均最大不利%": np.mean([t[1] for t in tr]) * 100, "平均持有天": np.mean([t[2] for t in tr]),
            "累計報酬%": (eq[-1] - 1) * 100, "最大回撤%": (eq / np.maximum.accumulate(eq) - 1).min() * 100}


def build_signals(d, inst):
    x = indicators(d)
    c, o, h, l = x.Close, x.Open, x.High, x.Low
    v = (x.Volume / 1000).replace(0, np.nan)
    ret = c.pct_change()
    ma5, ma20, ma60 = x.MA5, x.MA20, x.MA60
    has_inst = inst is not None and len(inst) > 0
    inst = inst if has_inst else pd.DataFrame(index=d.index)
    S = {k: (inst[k].reindex(d.index).fillna(0.0) if k in inst else pd.Series(0.0, index=d.index))
         for k in ("外資", "投信", "自營商")}
    S["三大法人"] = S["外資"] + S["投信"] + S["自營商"]
    ST = {k: sstreak(s) for k, s in S.items()}
    vr5, vr20 = v / v.shift(1).rolling(5).mean(), v / v.shift(1).rolling(20).mean()
    cross = lambda a, b: (a > b) & (a.shift(1) <= b.shift(1))
    shrink = lambda s: (s < 0) & (s.shift(1) < 0) & (s.shift(2) < 0) & (s > s.shift(1)) & (s.shift(1) > s.shift(2))
    newlow = l <= l.rolling(10).min()
    since = (~newlow).astype(int).groupby(newlow.cumsum()).cumsum()
    hh5, hh20 = h.shift(1).rolling(5).max(), h.shift(1).rolling(20).max()
    ll10, ll20 = l.shift(1).rolling(10).min(), l.shift(1).rolling(20).min()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    cum5, cum10 = S["三大法人"].rolling(5).sum(), S["三大法人"].rolling(10).sum()
    g = lambda s: s if has_inst else s & False
    sg = {}
    sg["B1"] = g(((ST["外資"] <= -3) | (ST["三大法人"] <= -3)) & (vr5 <= 0.8) & (since >= 3))
    sg["B2"] = g((shrink(S["外資"]) | shrink(S["三大法人"])) & (cross(c, ma5).astype(float).rolling(3).max() > 0) & (c > ma5))
    stop_sell = ((ST["外資"].shift(1) <= -3) & (S["外資"] >= 0)) | ((ST["三大法人"].shift(1) <= -3) & (S["三大法人"] >= 0))
    sg["B3"] = g(stop_sell & (vr5 >= 1.0) & (v > v.shift(1)))
    sg["B4"] = ((c > hh5) | (c > hh20)) & (vr5 >= 1.5)
    tech5 = (x.RSI > 50) & (x.RSI > x.RSI.shift(1)) & (x.OSC > 0) & (x.OSC > x.OSC.shift(1)) & (c > ma20) & (c > ma5)
    sg["B5"] = tech5 & (cum5 > 0) if has_inst else tech5
    drop = c / c.rolling(20).max() - 1
    sg["B6a"] = (drop <= -0.10) & (c > c.shift(3) * 1.03) & (c < ma20) & (ma60 < ma60.shift(5))
    rev = (c > ma20) & (ma20 > ma20.shift(3)) & (l.rolling(5).min() > l.shift(5).rolling(10).min())
    sg["B6b"] = rev & (cum10 > 0) if has_inst else rev
    sg["S1"] = (c < ll10) | (c < ll20)
    sg["S2a"], sg["S2b"] = cross(ma5, c), cross(ma20, c)
    sg["S3"] = g(((S["外資"].rolling(10).sum().shift(3) > 0) & (ST["外資"] <= -3)) | ((cum10.shift(3) > 0) & (ST["三大法人"] <= -3)))
    sg["S4"] = (c >= h.rolling(60).max() * 0.95) & (vr20 >= 1.5) & (ret <= 0.005)
    sg["S5"] = (c < ll20) & (vr5 >= 1.5)
    ma10 = c.rolling(10).mean()
    gap_up, gap_dn = l > h.shift(1), h < l.shift(1)
    base_up = (c > ma5) & (c > ma10) & (c > ma20)
    align = (ma5 > ma10) & (ma10 > ma20)
    gcross = (cross(ma5, ma10) | cross(ma10, ma20)).astype(float).rolling(3).max() > 0
    lo60 = l.rolling(60).min()
    lowzone = (c < ma60) | ((c - lo60) / (h.rolling(60).max() - lo60).replace(0, np.nan) <= 0.4)
    sg["B7p"] = base_up
    sg["B7"] = base_up & (gap_up | (vr5 >= 1.5)) & (align | (gcross & lowzone))
    base_dn = (c < ma5) & (c < ma10) & (c < ma20) & (c < ma60)
    body = o - c
    lblack = (c < o) & (body >= 0.6 * (h - l)) & ((body / o >= 0.025) | (body >= 1.5 * atr))
    below60 = c < ma60
    days60 = below60.astype(int).groupby((~below60).cumsum()).cumsum()
    evt = (lblack | gap_dn).astype(float).rolling(5).max() > 0
    sg["S7p"] = base_dn
    sg["S7"] = base_dn & (lblack | gap_dn)
    sg["S7L"] = base_dn & (days60 >= 3) & evt
    sg = {k: s.fillna(False).astype(bool) for k, s in sg.items()}
    return dict(sig=sg, x=x, S=S, ST=ST, ma10=ma10, gap_up=gap_up, gap_dn=gap_dn, align=align, gcross=gcross, lowzone=lowzone,
                lblack=lblack, days60=days60, v=v, vr5=vr5, vr20=vr20, since=since, atr=atr, ret=ret, cum5=cum5,
                hh5=hh5, hh20=hh20, ll10=ll10, ll20=ll20, has_inst=has_inst)


def signal_alerts(F):
    sg, x, S, ST = F["sig"], F["x"], F["S"], F["ST"]
    on = lambda n: bool(sg[n].iloc[-1])
    f = lambda s: float(s.iloc[-1])
    date, c = x.index[-1].strftime("%m%d"), f(x.Close)
    vr5, vr20, A = f(F["vr5"]), f(F["vr20"]), []
    fs, ts = f(ST["外資"]), f(ST["三大法人"])

    def add(key, sc, title, detail, brief):
        A.append(dict(id=key, key=f"{key}-{date}", s=sc, title=title, detail=detail, force=True, kind="sig", brief=brief))

    if on("B1"):
        add("B1", 1, "📈 買進訊號①:連續賣超+成交量萎縮+股價不再破底",
            f"外資連賣 {max(-fs, 0):.0f} 天、三大法人連賣 {max(-ts, 0):.0f} 天,今日量為前5日均量的 {vr5:.2f} 倍(量縮),"
            f"連續 {int(f(F['since']))} 日未創10日新低 → 賣壓可能趨緩,但還不是買進確認,宜等止穩訊號",
            f"買進訊號①連續賣超+量縮+不破底(量比 {vr5:.1f})")
    if on("B2"):
        add("B2", 1, "📈 買進訊號②:法人賣超縮小+股價站回5日均線",
            f"法人賣超逐日縮小,收盤 {c:.2f} 站回5日線 {f(x.MA5):.2f}", f"買進訊號②法人賣超縮小+站回5日線 {f(x.MA5):.2f}")
    if on("B3"):
        add("B3", 2, "📈 買進訊號③:法人停止賣超或轉買+成交量回升",
            f"外資今日 {f(S['外資']):+,.0f} 張、三大法人 {f(S['三大法人']):+,.0f} 張,前期連賣已停止,量比 {vr5:.2f} 倍",
            f"買進訊號③法人停止賣超或轉買+量回升(量比 {vr5:.1f})")
    if on("B4"):
        k, hh = (20, f(F["hh20"])) if c > f(F["hh20"]) else (5, f(F["hh5"]))
        add("B4", 2, "📈 買進訊號④:股價放量突破近期高點",
            f"收盤 {c:.2f} 突破前{k}日高 {hh:.2f},量為前5日均量的 {vr5:.2f} 倍(≥1.5 倍),留意隔日是否續強、避免假突破",
            f"買進訊號④放量突破前{k}日高 {hh:.2f}(量比 {vr5:.1f})")
    if on("B5"):
        add("B5", 2, "📈 買進訊號⑤:RSI、MACD、均線與法人同步轉強",
            f"RSI {f(x.RSI):.0f} 且上升、MACD柱狀體 {f(x.OSC):+.2f} 擴大、站上5/20日線、三大法人5日累計 {f(F['cum5']):+,.0f} 張",
            f"買進訊號⑤RSI {f(x.RSI):.0f}/MACD/均線/法人同步轉強")
    if on("B6b"):
        add("B6", 2, "📈 買進訊號⑥:屬「趨勢反轉型」(較可靠)",
            "站上月線且月線上彎、近期底部墊高、法人買盤支持;與單純跌深反彈不同,仍建議分批並設停損", "買進訊號⑥趨勢反轉型(較可靠)")
    elif on("B6a"):
        add("B6", 0, "⚠️ 買進訊號⑥:屬「跌深反彈型」(較弱)",
            "自20日高點回落逾10%後反彈,但仍在月線下、季線下彎,容易遇壓回落;只適合短線、小量,不宜當成趨勢反轉", "買進訊號⑥跌深反彈型(較弱,短線小量)")
    if on("B7"):
        ma5_, ma10_, ma20_ = f(x.MA5), f(F["ma10"]), f(x.MA20)
        t1 = [t for ok, t in ((bool(F["gap_up"].iloc[-1]), f"多方跳空缺口(今日最低 {f(x.Low):.2f} > 昨日最高 {float(x.High.iloc[-2]):.2f})"),
                              (vr5 >= 1.5, f"成交量放大(量比 {vr5:.1f} 倍)")) if ok]
        t2 = [t for ok, t in ((bool(F["align"].iloc[-1]), "均線多頭排列(5>10>20日線)"),
                              (bool(F["gcross"].iloc[-1]) and bool(F["lowzone"].iloc[-1]), "低檔黃金交叉")) if ok]
        add("B7", 2, "📈 買進訊號⑦:收盤同時站上5/10/20日線,短線翻多",
            f"收盤 {c:.2f} 大於5日線 {ma5_:.2f}、10日線 {ma10_:.2f}、20日線 {ma20_:.2f};伴隨{'、'.join(t1)};{'、'.join(t2)}"
            f" → 短線翻多,可尋找買點或續抱;進場仍宜設停損",
            f"買進訊號⑦站上5/10/20日線+{'/'.join(('跳空' if 'gap' in k else '放量') for k, ok in (('gap', bool(F['gap_up'].iloc[-1])), ('vol', vr5 >= 1.5)) if ok)}"
            f"+{'/'.join(('多頭排列' if 'al' in k else '低檔黃金交叉') for k, ok in (('al', bool(F['align'].iloc[-1])), ('gc', bool(F['gcross'].iloc[-1]) and bool(F['lowzone'].iloc[-1]))) if ok)}(短線翻多)")
    if on("S7L"):
        n60, ma60_ = int(f(F["days60"])), f(x.MA60)
        add("S7L", -3, f"📉 賣出訊號⑦:長線翻空(季線失守且連續 {n60} 天無法站回)",
            f"收盤 {c:.2f} 同時跌破5/10/20/60日線,季線 {ma60_:.2f} 已連續 {n60} 天無法站回,且近5日出現長黑K或向下跳空"
            f" → 長線翻空,應果斷停損/停利離場,禁止低接或攤平",
            f"賣出訊號⑦長線翻空:季線 {ma60_:.2f} 失守 {n60} 天,果斷離場、禁止低接或攤平")
    elif on("S7"):
        why = "、".join(t for ok, t in ((bool(F["lblack"].iloc[-1]), "長黑K棒"), (bool(F["gap_dn"].iloc[-1]), "向下跳空缺口")) if ok)
        add("S7", -3, "📉 賣出訊號⑦:收盤同時跌破5/10/20/60日線,且出現" + why,
            f"收盤 {c:.2f} 跌破5日 {f(x.MA5):.2f}、10日 {f(F['ma10']):.2f}、20日 {f(x.MA20):.2f}、60日線 {f(x.MA60):.2f},伴隨{why};"
            f"若季線連續數天無法站回,將確認長線翻空,屆時應果斷離場、禁止低接或攤平",
            f"賣出訊號⑦跌破5/10/20/60日線+{why}")
    if on("S1"):
        ll20, ll10 = f(F["ll20"]), f(F["ll10"])
        if c < ll20:
            add("S1", -2, "📉 賣出訊號①:股價跌破近期重要低點", f"收盤 {c:.2f} 跌破前20日低 {ll20:.2f}", f"賣出訊號①跌破前20日低 {ll20:.2f}")
        else:
            add("S1", -1, "📉 賣出訊號①:股價跌破近期支撐", f"收盤 {c:.2f} 跌破前10日低 {ll10:.2f}", f"賣出訊號①跌破前10日低 {ll10:.2f}")
    if on("S2b"):
        add("S2b", -2, "📉 賣出訊號②:股價跌破20日均線", f"收盤 {c:.2f} 跌破20日線 {f(x.MA20):.2f}", f"賣出訊號②跌破20日線 {f(x.MA20):.2f}")
    if on("S2a"):
        add("S2a", -1, "📉 賣出訊號②:股價跌破5日均線", f"收盤 {c:.2f} 跌破5日線 {f(x.MA5):.2f}", f"賣出訊號②跌破5日線 {f(x.MA5):.2f}")
    if on("S3"):
        add("S3", -2, "📉 賣出訊號③:法人由買超轉為連續賣超",
            f"外資連賣 {max(-fs, 0):.0f} 天、三大法人連賣 {max(-ts, 0):.0f} 天(此前10日為淨買超)", "賣出訊號③法人由買轉連賣")
    if on("S4"):
        add("S4", -2, "📉 賣出訊號④:高檔成交量放大但股價無法上漲",
            f"股價在近60日高點5%內,量為前20日均量的 {vr20:.2f} 倍,今日漲幅僅 {f(F['ret']) * 100:+.2f}%,疑似高檔出貨",
            f"賣出訊號④高檔量增不漲(量比 {vr20:.1f}、漲幅 {f(F['ret']) * 100:+.2f}%)")
    if on("S5"):
        add("S5", -3, "📉 賣出訊號⑤:跌破支撐且成交量放大",
            f"收盤 {c:.2f} 跌破前20日低 {f(F['ll20']):.2f},量為前5日均量的 {vr5:.2f} 倍(放量破底,風險較高)",
            f"賣出訊號⑤破底放量(量比 {vr5:.1f})")
    return A


def signal_rows(F):
    sg, x = F["sig"], F["x"]
    on = lambda n: "✅ 觸發" if bool(sg[n].iloc[-1]) else "—"
    if bool(sg["B6b"].iloc[-1]):
        b6 = "✅ 趨勢反轉型(較可靠)"
    elif bool(sg["B6a"].iloc[-1]):
        b6 = "⚠️ 跌深反彈型(較弱)"
    else:
        b6 = "—"
    s1 = "✅ 觸發" if bool(sg["S1"].iloc[-1]) else "—"
    s2 = "、".join(t for n, t in (("S2a", "5日線"), ("S2b", "20日線")) if bool(sg[n].iloc[-1])) or "—"
    rows = [("買進訊號①", "連續賣超+成交量萎縮+股價不再破底", on("B1")),
            ("買進訊號②", "法人賣超縮小+股價站回5日均線", on("B2")),
            ("買進訊號③", "法人停止賣超或轉買+成交量回升", on("B3")),
            ("買進訊號④", "股價放量突破近期高點(量≥前5日均×1.5)", on("B4")),
            ("買進訊號⑤", "RSI、MACD、均線與法人同步轉強", on("B5")),
            ("買進訊號⑥", "跌深反彈 vs 趨勢反轉", b6),
            ("買進訊號⑦", "收盤同時站上5/10/20日線+(跳空或放量)+(多頭排列或低檔黃金交叉)",
             "✅ 觸發" if bool(sg["B7"].iloc[-1]) else "部分符合(已站上5/10/20日線,尚缺跳空/放量或排列/交叉)" if bool(sg["B7p"].iloc[-1]) else "—"),
            ("賣出訊號①", "股價跌破近期支撐或重要低點(前10/20日低)", s1),
            ("賣出訊號②", "股價跌破5日、20日均線", s2),
            ("賣出訊號③", "法人由買超轉為連續賣超", on("S3")),
            ("賣出訊號④", "高檔成交量放大但股價無法上漲", on("S4")),
            ("賣出訊號⑤", "跌破支撐且成交量放大", on("S5")),
            ("賣出訊號⑥", "固定停損/停利/ATR停損/移動停損", "見下方價位與歷史效果"),
            ("賣出訊號⑦", "收盤同時跌破5/10/20/60日線+(長黑K或向下跳空),季線連續失守=長線翻空",
             "✅ 長線翻空" if bool(sg["S7L"].iloc[-1]) else "✅ 觸發" if bool(sg["S7"].iloc[-1]) else "部分符合(已跌破5/10/20/60日線,尚無長黑/跳空)" if bool(sg["S7p"].iloc[-1]) else "—")]
    return pd.DataFrame(rows, columns=["訊號", "內容", "目前狀態"])


def exit_levels(c, atr, hi20, lo20, cost=0.0, sl=7.0, tp=15.0):
    base = cost if cost and cost > 0 else c
    return [("固定停損", f"固定停損 -{sl:g}%", base * (1 - sl / 100)),
            ("固定停利", f"固定停利 +{tp:g}%", base * (1 + tp / 100)),
            ("ATR停損", "ATR停損(成本 − 2×ATR)", base - 2 * atr),
            ("移動停損", f"移動停損:近20日價格區間 {lo20:.2f} ~ {hi20:.2f}(跌破下緣才警示)", lo20)]


def exit_alerts(c, atr, hi20, lo20, cost, sl, tp, date):
    if not cost or cost <= 0:
        return []
    lv = {k: p for k, _, p in exit_levels(c, atr, hi20, lo20, cost, sl, tp)}
    pl, A = (c / cost - 1) * 100, []

    def add(key, sc, title, detail, brief):
        A.append(dict(id=key, key=f"{key}-{date}", s=sc, title=title, detail=detail, force=True, kind="sig", brief=brief))

    if c <= lv["固定停損"]:
        add("X1", -3, "📉 賣出訊號⑥:已觸及固定停損", f"現價 {c:.2f} ≤ 成本 {cost:.2f} 的 -{sl:g}%({lv['固定停損']:.2f}),目前損益 {pl:+.1f}%",
            f"賣出訊號⑥觸及固定停損 {lv['固定停損']:.2f}(損益 {pl:+.1f}%)")
    elif c <= lv["ATR停損"]:
        add("X2", -2, "📉 賣出訊號⑥:已觸及ATR停損", f"現價 {c:.2f} ≤ 成本 − 2×ATR({lv['ATR停損']:.2f}),目前損益 {pl:+.1f}%",
            f"賣出訊號⑥觸及ATR停損 {lv['ATR停損']:.2f}(損益 {pl:+.1f}%)")
    if hi20 > cost and c < lo20:
        add("X3", -2, "📉 賣出訊號⑥:已跌破近20日價格區間下緣(移動停損,保護獲利)",
            f"近20日區間 {lo20:.2f} ~ {hi20:.2f},現價 {c:.2f} 已超出下緣;成本 {cost:.2f},目前損益 {pl:+.1f}%",
            f"賣出訊號⑥跌破近20日區間下緣 {lo20:.2f}(移動停損)")
    if c >= lv["固定停利"]:
        add("X4", -1, "📉 賣出訊號⑥:已達固定停利", f"現價 {c:.2f} ≥ 成本 +{tp:g}%({lv['固定停利']:.2f}),可考慮分批獲利了結",
            f"賣出訊號⑥達固定停利 {lv['固定停利']:.2f}(損益 {pl:+.1f}%)")
    return A


@st.cache_data(ttl=3600, show_spinner=False)
def exit_effects(code, sl=7.0, tp=15.0):
    hd = get_long_daily(code)
    if len(hd) < 400:
        return None
    x = indicators(hd)
    c, h, l = x.Close, x.High, x.Low
    vr5 = (x.Volume / x.Volume.shift(1).rolling(5).mean())
    entry = (((c > h.shift(1).rolling(20).max()) & (vr5 >= 1.5)) | ((c > x.MA20) & (c.shift(1) <= x.MA20.shift(1)))).fillna(False).values
    sell = ((x.MA20 > c) & (x.MA20.shift(1) <= c.shift(1))).fillna(False).values
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    A = (x.Open.values.astype(float), h.values.astype(float), l.values.astype(float), c.values.astype(float),
         tr.rolling(14).mean().values.astype(float))
    N = len(x)
    rows = []
    for label, rule in make_rules(sl, tp):
        s_ = trade_stats(simulate(entry, sell, A, rule, 120, N, sl=sl / 100, tp=tp / 100))
        if s_:
            rows.append({"出場規則": label, **s_})
    if not rows:
        return None
    return pd.DataFrame(rows), float(A[3][-1] / A[3][120] - 1) * 100, int(entry[120:].sum()), str(x.index[120].date())


def parse_stock_inputs(sel, txt, stock_list, limit=20):
    out, seen = [], set()

    def add(code, nm):
        if code and code not in seen:
            seen.add(code)
            out.append((code, nm))

    for o in sel:
        c, _, n = o.partition(" ")
        add(c, n)
    for tok in re.split(r"[,，、;；\s]+", txt or ""):
        tok = tok.strip()
        if not tok:
            continue
        if re.fullmatch(r"[0-9]{4,6}[A-Za-z]?", tok):
            c, nm = tok.upper(), ""
            if not stock_list.empty:
                r = stock_list[stock_list.stock_id == c]
                nm = r.stock_name.iloc[0] if len(r) else ""
            add(c, nm)
        elif not stock_list.empty:
            ex = stock_list[stock_list.stock_name == tok]
            mt = ex if len(ex) else stock_list[stock_list.stock_name.str.contains(tok, case=False, regex=False)]
            if len(mt):
                add(mt.stock_id.iloc[0], mt.stock_name.iloc[0])
    return out[:limit]


def _parse_mis(q):
    price = _f(q.get("z"))
    if price is None:
        b, a = _f(str(q.get("b", "")).split("_")[0]), _f(str(q.get("a", "")).split("_")[0])
        price = (b + a) / 2 if b and a else _f(q.get("y"))
    if price is None:
        return None
    return dict(price=price, open=_f(q.get("o")) or price, high=_f(q.get("h")) or price, low=_f(q.get("l")) or price,
                volume=int((_f(q.get("v")) or 0) * 1000), name=q.get("n", ""), date=q.get("d"),
                time=f"{q.get('d')} {q.get('t')}", source="證交所即時", prev=_f(q.get("y")),
                book={k: q.get(k, "") for k in "bagf"})


@st.cache_data(ttl=5)
def get_live_quotes(codes):
    out = {}
    try:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
        sess.get("https://mis.twse.com.tw/stock/index.jsp", timeout=8)
        ex = "|".join(f"{m}_{c}.tw" for c in codes for m in ("tse", "otc"))
        r = sess.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                     params={"ex_ch": ex, "json": 1, "delay": 0}, timeout=10).json()
        for q in r.get("msgArray", []):
            if q.get("n") and q.get("c") and q["c"] not in out:
                lv = _parse_mis(q)
                if lv:
                    out[q["c"]] = lv
    except Exception:
        pass
    for c in codes:
        if c not in out:
            lv = get_live_quote(c)
            if lv:
                out[c] = lv
    return out


def analyze_stock(code, name, live, th):
    raw = get_prices(code)
    if raw.empty:
        return None
    df = indicators(merge_live(raw, live)).dropna(subset=["MA60", "K", "RSI", "BB_UP"])
    if len(df) < 70:
        return None
    inst = prep_inst(finmind("TaiwanStockInstitutionalInvestorsBuySell", code, 120))
    margin = finmind("TaiwanStockMarginPurchaseShortSale", code, 150)
    per = finmind("TaiwanStockPER", code, 1100)
    last, prev = df.iloc[-1], df.iloc[-2]
    cur = live["price"] if live else float(last.Close)
    prevc = (live or {}).get("prev") or float(prev.Close)
    parts = (int(tech_components(df).iloc[-1].sum()), score_inst(inst)[0], score_margin(df, margin)[0],
             score_val(per, get_dividends(code), float(last.Close))[0])
    total = sum(parts)
    alerts, _ = build_alerts(df, inst, margin, live, None, prevc, th)
    alerts += signal_alerts(build_signals(df, inst))
    lv = price_levels(df, total)
    net, msg = combine_advice(alerts, lv, cur, total)
    return dict(code=code, name=name or (live or {}).get("name") or code, price=cur, chg=(cur / prevc - 1) * 100,
                total=total, parts=parts, rsi=float(last.RSI), lv=lv, alerts=alerts, net=net, msg=msg,
                action=action_now(lv, total, cur))


def sig_line(alerts):
    sg = [a for a in alerts if a.get("kind") == "sig"]
    if not sg:
        return "", 0
    sg = sorted(sg, key=lambda x: (0 if x["s"] > 0 else 1 if x["s"] == 0 else 2, x["id"]))
    d = 1 if all(a["s"] > 0 for a in sg) else -1 if all(a["s"] < 0 for a in sg) else 0
    return "觀察重點:" + "｜".join(a["brief"] for a in sg), d


def new_alert_items(code, name, alerts, net, msg, cur):
    seen = st.session_state.setdefault("seen_alerts", set())
    act = st.session_state.get(f"sig_active_{code}", set())
    lastn = st.session_state.setdefault(f"sig_last_{code}", {})
    now, out, nm = time.time(), [], name or code
    for a in alerts:
        if a.get("kind") == "sig":
            new = a["id"] not in act and now - lastn.get(a["id"], 0) >= 1800
            if new:
                lastn[a["id"]] = now
        else:
            k = f"{code}:{a['key']}"
            new = k not in seen
            seen.add(k)
        if new:
            st.toast(f"{nm}:{a['title']}|{a['detail']}", icon="🔔")
            out.append((f"【{nm} {code}】{a['title']}\n{a['detail']}", abs(a["s"]) >= 2 or a.get("force", False)))
    if alerts:
        ak = f"{code}:advice:{msg}"
        if ak not in seen:
            seen.add(ak)
            out.append((f"【{nm} {code}】當下建議(提醒訊號合計 {net:+d})\n{msg}\n現價 {cur:.2f}", True))
    st.session_state[f"sig_active_{code}"] = {a["id"] for a in alerts if a.get("kind") == "sig"}
    return out


def _goto_single(code):
    st.session_state["mode"] = "單一股票"
    st.session_state["query"] = code


def _back_to_multi():
    st.session_state["mode"] = "多股同時分析"


def render_multi(stocks, th):
    st.subheader("📊 多股同時分析")
    if not stocks:
        st.info("請在左側選擇或輸入要分析的股票(最多 20 檔)。")
        return
    live_map = get_live_quotes(tuple(c for c, _ in stocks))
    prog = st.progress(0.0, text="分析中...")
    res, errs = [], []
    for i, (c, n) in enumerate(stocks):
        try:
            r = analyze_stock(c, n, live_map.get(c), th)
            if r:
                res.append(r)
            else:
                errs.append(f"{c} {n}:抓不到資料或歷史資料太短,已略過")
        except Exception as e:
            errs.append(f"{c} {n}:分析失敗({e})")
        prog.progress((i + 1) / len(stocks), text=f"分析 {c} {n}({i + 1}/{len(stocks)})")
    prog.empty()
    for e in errs:
        st.warning(e)
    if not res:
        return
    _bull = sum(1 for r in res if r["net"] >= 2)
    _bear = sum(1 for r in res if r["net"] <= -2)
    _avg = sum(r["chg"] for r in res) / len(res)
    _top = max(res, key=lambda r: r["chg"])
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("分析檔數", f"{len(res)} 檔")
    k2.metric("偏多 / 偏空", f"{_bull} / {_bear}", help="提醒合計 ≥ +2 為偏多,≤ -2 為偏空")
    k3.metric("平均漲跌", f"{_avg:+.2f}%")
    k4.metric("今日最強", f"{_top['name'] or _top['code']}", f"{_top['chg']:+.2f}%")
    _sort_opts = ["依選擇順序", "提醒合計(偏多在前)", "提醒合計(偏空在前)", "綜合評分(高→低)", "漲跌幅(高→低)", "漲跌幅(低→高)"]
    if st.session_state.get("multi_sort") not in _sort_opts:
        st.session_state["multi_sort"] = st.session_state.get("_saved_multi_sort", _sort_opts[0])
    order = st.selectbox("排序", _sort_opts, key="multi_sort")
    st.session_state["_saved_multi_sort"] = order
    keyf = {"提醒合計(偏多在前)": lambda r: -r["net"], "提醒合計(偏空在前)": lambda r: r["net"],
            "綜合評分(高→低)": lambda r: -r["total"], "漲跌幅(高→低)": lambda r: -r["chg"], "漲跌幅(低→高)": lambda r: r["chg"]}.get(order)
    if keyf:
        res = sorted(res, key=keyf)
    rows = []
    for r in res:
        line, _d = sig_line(r["alerts"])
        rows.append({"代碼": r["code"], "名稱": r["name"], "現價": r["price"], "漲跌%": r["chg"], "綜合評分": r["total"],
                     "提醒合計": r["net"], "技術/法人/融資券/估值": "/".join(f"{x:+d}" for x in r["parts"]), "RSI": round(r["rsi"]),
                     "價位區間": r["lv"]["zone"].split("(")[0], "當下建議": r["msg"], "觀察重點": line.replace("觀察重點:", "")})
    tbl = pd.DataFrame(rows)
    try:
        colr = lambda v: "color:#e0383e" if v > 0 else "color:#1f9d55" if v < 0 else ""
        sty = tbl.style.format({"現價": "{:.2f}", "漲跌%": "{:+.2f}"})
        sty = getattr(sty, "map", getattr(sty, "applymap", None))(colr, subset=["漲跌%", "綜合評分", "提醒合計"]) if hasattr(tbl.style, "map") or hasattr(tbl.style, "applymap") else sty
        st.dataframe(sty, hide_index=True, use_container_width=True)
    except Exception:
        st.dataframe(tbl, hide_index=True, use_container_width=True)
    st.caption("綜合評分 = 技術 + 法人 + 融資券 + 估值;提醒合計 = 所有提醒方向強度加總(正偏多、負偏空)。多股模式不含 5 分鐘爆量與持有成本停損提醒。")
    items = []
    for r in res:
        icon = "🟢" if r["net"] >= 2 else "🔴" if r["net"] <= -2 else "⚪"
        with st.expander(f"{icon} {r['name']}({r['code']}) {r['price']:.2f} {r['chg']:+.2f}% 綜合 {r['total']:+d} 提醒 {r['net']:+d}"):
            line, d = sig_line(r["alerts"])
            if line:
                (st.success if d > 0 else st.error if d < 0 else st.info)("**" + line.replace(":", ":** ", 1))
            for a in sorted([a for a in r["alerts"] if a.get("kind") != "sig"], key=lambda x: -abs(x["s"])):
                (st.success if a["s"] > 0 else st.error if a["s"] < 0 else st.warning)(f"**{a['title']}** {a['detail']}")
            if not r["alerts"]:
                st.caption("目前沒有觸發提醒條件。")
            st.info(f"💡 **當下建議**:{r['msg']}")
            st.markdown("**價位面建議:** " + r["action"])
            st.markdown(level_text(r["lv"], r["price"]))
            st.button("🔍 切換到單一股票詳細分析", key=f"go_{r['code']}", on_click=_goto_single, args=(r["code"],))
        items += new_alert_items(r["code"], r["name"], r["alerts"], r["net"], r["msg"], r["price"])
    push_batch(items)


# ------------------------------------------------------------ 全市場「目前情境」歷史勝率掃描
SCAN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ".", "scan_cache")


@st.cache_data(ttl=86400, show_spinner=False)
def get_universe(include_etf=False):
    """上市櫃股票清單 + Yahoo 代號。FinMind 有市場別(twse/tpex)就精準對應 .TW/.TWO,否則先猜 .TW、失敗再試 .TWO。"""
    df, guess = pd.DataFrame(), False
    try:
        p = {"dataset": "TaiwanStockInfo"}
        if os.environ.get("FINMIND_TOKEN"):
            p["token"] = os.environ["FINMIND_TOKEN"]
        df = pd.DataFrame(requests.get(FINMIND, params=p, timeout=30).json().get("data", []))
    except Exception:
        pass
    if df.empty or "type" not in df:
        df, guess = get_stock_list().copy(), True
        df["type"] = "twse"
    df = df[df["type"].isin(["twse", "tpex"])].copy()
    if "industry_category" not in df:
        df["industry_category"] = ""
    df["stock_id"] = df["stock_id"].astype(str)
    pat = r"[0-9]{4,6}[A-Za-z]?" if include_etf else r"[1-9][0-9]{3}"      # 預設只掃 4 碼一般股票
    df = df[df["stock_id"].str.fullmatch(pat)].drop_duplicates("stock_id")
    df["ticker"] = df["stock_id"] + np.where(df["type"] == "tpex", ".TWO", ".TW")
    return df[["stock_id", "stock_name", "industry_category", "ticker"]].reset_index(drop=True), guess


def _live_batch(codes, chunk=40):
    """批次抓證交所即時報價(盤中=即時價;收盤後=當日收盤價),不逐檔 fallback,避免拖慢掃描"""
    out = {}
    try:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
        sess.get("https://mis.twse.com.tw/stock/index.jsp", timeout=8)
        for i in range(0, len(codes), chunk):
            part = codes[i:i + chunk]
            ex = "|".join(f"{m}_{c}.tw" for c in part for m in ("tse", "otc"))
            try:
                r = sess.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                             params={"ex_ch": ex, "json": 1, "delay": 0}, timeout=10).json()
            except Exception:
                continue
            for q in r.get("msgArray", []):
                if q.get("n") and q.get("c") and q["c"] not in out:
                    lv = _parse_mis(q)
                    if lv:
                        out[q["c"]] = lv
    except Exception:
        pass
    return out


def _download_batch(tickers, period):
    """一次下載一批股票的日 K,回傳 {ticker: 清理後 DataFrame}"""
    try:
        raw = yf.download(list(tickers), period=period, interval="1d", auto_adjust=False,
                          group_by="ticker", threads=True, progress=False)
    except Exception:
        return {}
    out = {}
    for t in tickers:
        try:
            h = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            h = clean_ohlc(_to_taipei(h.dropna(how="all")), "1d")
        except Exception:
            continue
        if not h.empty:
            out[t] = h
    return out


def scan_market(uni, guess, horizon, period, min_amt, progress, batch=40, use_live=True):
    """逐批下載 → 對每檔跑 situation_stats(沿用單股「歷史相似情境」同一套邏輯)→ 回傳結果表"""
    rows, rec = [], uni.to_dict("records")
    for b in range(0, len(rec), batch):
        part = rec[b:b + batch]
        data = _download_batch([r["ticker"] for r in part], period)
        if guess:  # 沒有市場別資訊:抓不到的改試上櫃 .TWO
            miss = [r for r in part if r["ticker"] not in data]
            alt = _download_batch([r["stock_id"] + ".TWO" for r in miss], period) if miss else {}
            for r in miss:
                if r["stock_id"] + ".TWO" in alt:
                    data[r["ticker"]] = alt[r["stock_id"] + ".TWO"]
        live = _live_batch([r["stock_id"] for r in part]) if use_live else {}
        for r in part:
            hd = data.get(r["ticker"])
            if hd is None or len(hd) < 300:
                continue
            amt = float((hd.Close.tail(20) * hd.Volume.tail(20)).mean())      # 近20日平均成交值(元),用併入即時價之前的資料
            if amt < min_amt * 1e6:
                continue
            if use_live:
                hd = merge_live(hd, live.get(r["stock_id"]))                 # 與單股分頁相同:併入即時報價
            try:
                SS = situation_stats(hd, horizons=(horizon,))
            except Exception:
                continue
            if not SS:
                continue
            t = SS["table"].iloc[0]
            rows.append({"代碼": r["stock_id"], "名稱": r["stock_name"], "產業": r["industry_category"],
                         "現價": float(hd.Close.iloc[-1]), "近20日均成交值(百萬)": amt / 1e6,
                         "技術分數": SS["score"], "季線": "之上" if SS["above"] else "之下", "RSI區間": SS["zone"],
                         "樣本數": int(t["樣本數"]), "獨立樣本": float(t["獨立樣本約"]),
                         "勝率%": float(t["勝率"]), "勝率下限%": float(t["勝率下限"]),
                         "全期基準勝率%": float(t["全期基準勝率"]),
                         "勝率優勢%": float(t["勝率"] - t["全期基準勝率"]),
                         "平均報酬%": float(t["平均報酬"]), "中位數%": float(t["中位數"]), "最差%": float(t["最差"]),
                         "條件": t["條件"], "歷史年數": round(SS["years"], 1), "資料截止日": hd.index[-1].strftime("%Y-%m-%d")})
        progress.progress(min((b + batch) / len(rec), 1.0), text=f"掃描中… {min(b + batch, len(rec))}/{len(rec)} 檔,已有 {len(rows)} 檔通過")
    return pd.DataFrame(rows)


def render_scan():
    st.subheader("🔎 全市場情境勝率掃描")
    st.caption("對每一檔股票,找出「與它現在相似的歷史情境」(技術分數相近 + 同季線位置 + 同 RSI 區間),"
               "統計之後持有 N 天的勝率,再把歷史勝率高的挑出來。判斷邏輯與單股『歷史相似情境』完全相同。")
    c1, c2, c3, c4 = st.columns(4)
    horizon = c1.selectbox("持有天數", [5, 10, 20, 60], index=2)
    period = c2.selectbox("歷史資料長度", ["5y", "10y", "max"], index=2, help="單股分頁使用全部歷史(max);要與單股/回測分頁的勝率一致,請選 max。越長樣本越多但下載越慢")
    min_amt = c3.number_input("最低近20日均成交值(百萬元)", 0.0, 5000.0, 30.0, 10.0, help="過濾流動性太差、不好進出的股票")
    inc_etf = c4.checkbox("包含 ETF / 特別股", value=False)
    use_live = st.checkbox("併入即時報價(與單股分頁一致,建議勾選)", value=True,
                           help="單股分頁會把即時價併入最新 K 棒再算情境。不勾選則只用 Yahoo 日K(可能落後 1~2 天),結果會和單股分頁不同。")
    uni, guess = get_universe(inc_etf)
    if uni.empty:
        st.error("股票清單載入失敗,請稍後再試。")
        return
    os.makedirs(SCAN_DIR, exist_ok=True)
    fn = os.path.join(SCAN_DIR, f"scan_v3_{datetime.now():%Y%m%d}_L{int(use_live)}_{'live' if market_open() else 'close'}_{horizon}_{period}_{int(min_amt)}_{int(inc_etf)}.pkl")
    st.caption(f"掃描範圍約 {len(uni)} 檔。第一次掃描需下載全部歷史股價,約數分鐘到十幾分鐘;同參數當天掃過會直接讀快取。")
    cA, cB = st.columns([1, 1])
    run = cA.button("▶ 開始掃描(使用今日快取)", type="primary")
    force = cB.button("🔄 強制重新掃描")
    if run or force:
        if os.path.exists(fn) and not force:
            st.session_state["scan_res"] = pd.read_pickle(fn)
        else:
            prog = st.progress(0.0, text="準備下載…")
            res = scan_market(uni, guess, horizon, period, min_amt, prog, use_live=use_live)
            prog.empty()
            if not res.empty:
                res.to_pickle(fn)
            st.session_state["scan_res"] = res
        st.session_state["scan_meta"] = dict(h=horizon, t=datetime.now().strftime("%Y-%m-%d %H:%M"))
    res = st.session_state.get("scan_res")
    if res is None:
        st.info("按下「開始掃描」。")
        return
    if res.empty:
        st.warning("沒有任何股票通過條件(可能是網路/Yahoo 限流),請稍後再試或放寬條件。")
        return
    meta = st.session_state.get("scan_meta", {})
    st.markdown(f"**結果:持有 {meta.get('h')} 個交易日**(掃描時間 {meta.get('t')},共 {len(res)} 檔有足夠歷史資料)")
    f1, f2, f3, f4 = st.columns(4)
    min_eff = f1.slider("最少獨立樣本", 3, 60, 10, help="獨立樣本 ≈ 樣本數 ÷ 持有天數(連續幾天的樣本高度重疊,不能當成獨立事件)")
    min_edge = f2.slider("勝率優勢 ≥ (相對自身全期基準,%)", -10, 40, 5)
    min_score = f3.slider("目前技術分數 ≥", -8, 8, -8)
    inds = sorted(x for x in res["產業"].dropna().unique() if x)
    pick_ind = f4.multiselect("產業(不選=全部)", inds)
    only_pos = st.checkbox("只看平均報酬 > 0 且中位數 > 0", value=True)
    v = res[(res["獨立樣本"] >= min_eff) & (res["勝率優勢%"] >= min_edge) & (res["技術分數"] >= min_score)]
    if pick_ind:
        v = v[v["產業"].isin(pick_ind)]
    if only_pos:
        v = v[(v["平均報酬%"] > 0) & (v["中位數%"] > 0)]
    sort_by = st.selectbox("排序依據", ["勝率下限%(最保守,建議)", "勝率%", "勝率優勢%", "平均報酬%"], index=0)
    v = v.sort_values(sort_by.split("(")[0], ascending=False).reset_index(drop=True)
    st.write(f"符合條件 **{len(v)}** 檔")
    if v.empty:
        st.info("沒有符合的股票,試著降低「最少獨立樣本」或「勝率優勢」。")
        return
    st.dataframe(v.round(1), hide_index=True, use_container_width=True, height=520,
                 column_config={"勝率%": st.column_config.ProgressColumn("勝率%", min_value=0, max_value=100, format="%.0f"),
                                "勝率下限%": st.column_config.ProgressColumn("勝率下限%", min_value=0, max_value=100, format="%.0f"),
                                "現價": st.column_config.NumberColumn(format="%.2f")})
    st.download_button("⬇ 下載結果 CSV", v.to_csv(index=False).encode("utf-8-sig"), "scan_result.csv", "text/csv")
    opts = [f"{r.代碼} {r.名稱}" for r in v.head(200).itertuples()]
    sel = st.selectbox("查看個股詳細分析", opts)
    st.button("🔍 切換到單一股票詳細分析", on_click=_goto_single, args=(sel.split()[0],))
    st.caption("⚠ 解讀注意:①『勝率下限』是考慮樣本重疊後的保守估計,比單純勝率可靠;②掃描上千檔,必然會有些股票靠運氣勝率很高,"
               "所以要同時看『勝率優勢』與『獨立樣本』;③只用現存股票,存在存活者偏差;④未計滑價與跳空;"
               "⑤過去勝率不保證未來,僅供資訊整理,不構成投資建議。")


def _cluster_levels(items, c, side):
    """把相近(1.5% 內)的關鍵價位合併成一個區,價位取最靠近現價者,依據文字列出所有成員與實際數值。
    side='sup' 取現價下方並由近到遠排序;side='res' 取現價上方並由近到遠排序。"""
    if side == "sup":
        pool = sorted([(n, float(v)) for n, v in items if pd.notna(v) and float(v) < c * 0.995], key=lambda x: -x[1])
    else:
        pool = sorted([(n, float(v)) for n, v in items if pd.notna(v) and float(v) > c * 1.005], key=lambda x: x[1])
    out = []
    for n, v in pool:
        if out and abs(v / out[-1]["price"] - 1) <= 0.015:
            out[-1]["names"].append((n, v))
        else:
            out.append(dict(price=v, names=[(n, v)]))
    for g in out:
        g["label"] = "、".join(f"{n} {v:.2f}" for n, v in g["names"])
    return out


def price_levels(df, total):
    """價位全部取自「實際存在的關鍵價位」(月線、季線、布林、近20/60日高低點),
    依它們在現價的上方或下方自動歸類為壓力或支撐,不再用隱藏係數打折,
    因此表格的「依據」欄與數字一定對得起來。"""
    last = df.iloc[-1]
    c = float(last.Close)
    low20, high20 = float(df.Low.tail(20).min()), float(df.High.tail(20).max())
    low60, high60 = float(df.Low.tail(60).min()), float(df.High.tail(60).max())
    keys = [("月線", last.MA20), ("季線", last.MA60), ("布林下軌", last.BB_LOW), ("布林上軌", last.BB_UP),
            ("近20日低點", low20), ("近20日高點", high20), ("近60日低點", low60), ("近60日高點", high60)]
    sup = _cluster_levels(keys, c, "sup")      # 現價下方:候選買點
    res = _cluster_levels(keys, c, "res")      # 現價上方:候選賣點
    bear = total <= -2
    note = ""

    # ---- 買進:偏空時第一道支撐容易被跌破,改看下一層;偏多/中性看最近一層 ----
    i = 1 if bear else 0
    if len(sup) > i:
        small, small_b = sup[i]["price"], f"現價下方支撐:{sup[i]['label']}"
    else:
        base = sup[-1]["price"] if sup else c
        small, small_b = base * 0.95, ("下方已無更低的關鍵價位,以" + (f"{sup[-1]['label']}再下探約 5%" if sup else "現價下探約 5%") + "估算")
    if bear and len(sup) > 0:
        note = f"綜合評分 {total:+d} 偏空,最近支撐({sup[0]['label']})容易被跌破,買點改看下一層支撐。"
    deeper = [g for g in sup if g["price"] <= small * 0.97]
    if deeper:
        big, big_b = deeper[0]["price"], f"更深一層支撐:{deeper[0]['label']}"
    else:
        big, big_b = small * 0.95, f"小額加碼價再下探約 5%(下方已無更低的關鍵價位)"

    # ---- 賣出:偏空時反彈到最近壓力就先減碼;其餘看上方高檔結構(布林上軌/近高點)----
    if bear:
        pool = res
    else:
        pool = _cluster_levels([k for k in keys if k[0] in ("布林上軌", "近20日高點", "近60日高點")], c, "res")
    if not bear:
        pool = [g for g in pool if g["price"] >= c * 1.02] or pool
    if pool:
        trim, trim_b = pool[0]["price"], (("反彈遇到壓力:" if bear else "上方壓力:") + pool[0]["label"])
    else:
        trim, trim_b = c * 1.05, "已在所有關鍵價位之上,以現價上方約 5% 估算"
    far = [g for g in res if g["price"] >= trim * 1.03]
    if far:
        take, take_b = far[0]["price"], f"再上一層壓力:{far[0]['label']}"
    else:
        take, take_b = max(high60 * 1.05, trim * 1.03), f"突破近60日高點 {high60:.2f} 約 5%"

    # ---- 停損:放在最深買點下方 3%,避免「買點在停損之下」的矛盾 ----
    if low20 * 0.97 < big * 0.97:
        stop, stop_b = low20 * 0.97, f"跌破近20日低點 {low20:.2f} 再留 3% 緩衝"
    else:
        stop, stop_b = big * 0.97, f"跌破大舉買入區 {big:.2f} 再留 3% 緩衝"

    if c <= stop: zone = "⛔ 已跌破停損參考價,趨勢轉弱,建議檢討是否出場"
    elif c <= big: zone = "🟢 大舉買入區(請先確認不是基本面轉壞造成的下跌,並仍建議分批)"
    elif c <= small: zone = "🟡 小額加碼區"
    elif c >= take: zone = "🔴 高檔區,可考慮大幅獲利了結"
    elif c >= trim: zone = "🟠 分批減碼區"
    else: zone = "⚪ 中間區域,持有或觀望為主"
    return dict(big=big, small=small, trim=trim, take=take, stop=stop, zone=zone, total=total, close=c,
                big_b=big_b, small_b=small_b, trim_b=trim_b, take_b=take_b, stop_b=stop_b, note=note)


def level_text(lv, c):
    def pct(p):
        return f"{(p / c - 1) * 100:+.1f}%"
    return (f"### 價位參考(目前收盤 {c:.2f})\n\n**目前位置:{lv['zone']}**\n\n"
            "| 動作 | 參考價位 | 距現價 | 依據 |\n|---|---|---|---|\n"
            f"| 大幅獲利了結 | ≥ {lv['take']:.2f} | {pct(lv['take'])} | {lv['take_b']} |\n"
            f"| 分批減碼 | ≥ {lv['trim']:.2f} | {pct(lv['trim'])} | {lv['trim_b']} |\n"
            f"| 小額加碼 | ≤ {lv['small']:.2f} | {pct(lv['small'])} | {lv['small_b']} |\n"
            f"| 大舉買入 | ≤ {lv['big']:.2f} | {pct(lv['big'])} | {lv['big_b']} |\n"
            f"| 停損賣出 | ≤ {lv['stop']:.2f} | {pct(lv['stop'])} | {lv['stop_b']} |\n\n"
            + (f"{lv['note']}" if lv.get("note") else "")
            + "以上是技術面推算的參考價位,每個價位都對應圖上實際存在的均線、布林或高低點;"
            "遇到財報、重大利空等基本面或消息面劇變時不適用;建議分批執行,不要一次投入全部資金。")


def action_now(lv, total, c):
    if c <= lv["stop"]:
        return "🔻 **建議:停損或大幅減碼**——股價已跌破關鍵支撐,先保本,等止穩後再評估。"
    if c <= lv["big"]:
        return ("🟢 **建議:大舉買入(分2~3次進場)**——已回檔到深度支撐區,綜合評分不差。" if total >= 0
                else "🟢 **建議:分批買入,首批先小量**——價位已低,但綜合評分偏空,要防續跌。")
    if c <= lv["small"]:
        if total >= 2: return "🟡 **建議:小額加碼**——回到支撐區,技術與籌碼偏多。"
        if total > -2: return "🟡 **建議:小量試單**——價位合理但訊號中性,站穩後再加碼。"
        return "⚪ **建議:暫不加碼**——雖在加碼區,但評分偏空,等止跌訊號。"
    if c >= lv["take"]:
        return "🔴 **建議:大幅獲利了結**——已漲到近期高檔之上,僅留少量部位。"
    if c >= lv["trim"]:
        return "🟠 **建議:分批減碼**——接近壓力區,先收回部分獲利。"
    if total >= 4: return "⚪ **建議:持有,不追高**——趨勢偏多,拉回月線附近再考慮加碼。"
    if total <= -4: return "⚪ **建議:持有者減碼觀望**——評分偏空,未持有者先不進場。"
    return "⚪ **建議:持有或觀望**——處於中間區域,沒有明確優勢。"


def rule_advice(total, last, df):
    lv = price_levels(df, total)
    return ("### 即時操作建議\n\n" + action_now(lv, total, last.Close) + "\n\n" + level_text(lv, last.Close)
            + "\n\n---\n\n" + _base_advice(total, last, df))


def _base_advice(total, last, df):
    c = float(last.Close)
    keys = [("月線", last.MA20), ("季線", last.MA60), ("布林下軌", last.BB_LOW), ("布林上軌", last.BB_UP),
            ("近20日低點", df.Low.tail(20).min()), ("近20日高點", df.High.tail(20).max()),
            ("近60日低點", df.Low.tail(60).min()), ("近60日高點", df.High.tail(60).max())]
    sup = _cluster_levels(keys, c, "sup")[:3]
    res = _cluster_levels(keys, c, "res")[:3]
    if total >= 6: tone = "**偏多**:多項面向同向支持,可考慮分批布局,不宜一次重壓。"
    elif total >= 2: tone = "**中性偏多**:訊號略偏正面,適合小量試單或等回測支撐再加碼。"
    elif total > -2: tone = "**中性**:多空訊號互相抵銷,建議觀望,等待方向明確。"
    elif total > -6: tone = "**中性偏空**:風險大於機會,持有者宜控制部位,未持有者不急於進場。"
    else: tone = "**偏空**:多項面向偏弱,不建議逢低承接,待止跌訊號出現再評估。"
    sup_t = "、".join(g["label"] for g in sup) or "現價已在所有關鍵價位之下,下方無明確支撐"
    res_t = "、".join(g["label"] for g in res) or "現價已在所有關鍵價位之上,上方無明確壓力"
    return (f"{tone}\n\n- 現價下方支撐(由近到遠):{sup_t}"
            f"\n- 現價上方壓力(由近到遠):{res_t}"
            f"\n- 停損思路:跌破最深買點下方支撐時重新檢討,單筆虧損以可承受範圍為限"
            f"\n- 操作思路:分批進出,不要把訊號當作保證")


def ai_advice(ctx, key):
    try:
        import anthropic
        r = anthropic.Anthropic(api_key=key).messages.create(
            model=MODEL, max_tokens=1500,
            messages=[{"role": "user", "content": "你是台股研究助理。根據下列資料用繁體中文寫分析,不要給絕對買賣指令:"
                       "1) 新聞面解讀(利多/利空/中性) 2) 多空論點各2~3點 3) 主要風險 4) 綜合傾向與操作思路,並註明非投資建議。\n\n" + ctx}])
        return "".join(b.text for b in r.content if b.type == "text")
    except Exception as e:
        return f"AI 解讀失敗:{e}"


hero()
with st.sidebar:
    mode = st.radio("模式", ["單一股票", "多股同時分析", "全市場勝率掃描"], horizontal=True, key="mode")
    stock_list = get_stock_list()
    code, name, industry, multi_list = "", "", "", []
    if mode == "單一股票":
        st.session_state.setdefault("query", "2330")
        query = st.text_input("股票代碼或中文名稱", key="query",
                              help="可輸入代碼(2330、0050)或名稱(台積電、元大台灣50),名稱支援部分關鍵字").strip()
        if re.fullmatch(r"[0-9]{4,6}[A-Za-z]?", query):
            code = query.upper()
        elif query and not stock_list.empty:
            mt = stock_list[stock_list.stock_id.str.startswith(query.upper())
                            | stock_list.stock_name.str.contains(query, case=False, regex=False)]
            if mt.empty:
                st.error(f"找不到與「{query}」相關的股票")
            elif len(mt) == 1:
                code = mt.stock_id.iloc[0]
            else:
                opts = [f"{r.stock_id} {r.stock_name}" for r in mt.head(50).itertuples()]
                code = st.selectbox(f"找到 {len(mt)} 檔,請選擇", opts).split()[0]
        elif query:
            st.error("股票清單載入失敗,請改輸入股票代碼。")
        if code and not stock_list.empty:
            row = stock_list[stock_list.stock_id == code]
            if len(row):
                name, industry = row.stock_name.iloc[0], row.industry_category.iloc[0]
    elif mode == "多股同時分析":
        _opts = [f"{r.stock_id} {r.stock_name}" for r in stock_list.itertuples()]
        if "multi_sel" not in st.session_state:
            _saved = st.session_state.get("_saved_multi_sel",
                                          [o for o in _opts if o.split()[0] in ("2330", "0050")])
            st.session_state["multi_sel"] = [o for o in _saved if o in set(_opts)]
        if "multi_txt" not in st.session_state:
            st.session_state["multi_txt"] = st.session_state.get("_saved_multi_txt", "")
        _sel = st.multiselect("選擇多檔股票(可輸入代碼或名稱搜尋)", _opts, key="multi_sel")
        _txt = st.text_input("或直接輸入多個代碼/名稱(逗號或空白分隔)", key="multi_txt",
                             help="例如:2330, 2317 鴻海 0056")
        if _opts:
            st.session_state["_saved_multi_sel"] = list(_sel)
        st.session_state["_saved_multi_txt"] = _txt
        multi_list = parse_stock_inputs(_sel, _txt, stock_list)
        st.caption(f"目前共 {len(multi_list)} 檔(最多 20 檔)")
    st.subheader("觸發提醒設定")
    buy_th = st.slider("技術分數 ≥ 時買進", 0, 8, 3,  help="技術分數由法人+融資券+股票估值計算,分數正值為多頭")
    
 
    
    sell_th = st.slider("技術分數 ≤ 時賣出", -8, 3, -1)
    
    # 回測期間選項增加 "max" 支援
    bt_years_option = st.selectbox("回測期間(年)", [1, 2, 3, "max"], index=3,help="利用程式參照目前的觸發設定買賣,回測過去的勝率,不代表未來")

    api_key = st.text_input("Anthropic API Key(付費選填)", "", type="password")
    with st.expander("提醒參數設定(預設最佳化)"):
        th_days = st.slider("法人連續買/賣超天數", 2, 10, 3)
        th_ratio = st.slider("法人日均買賣超占 20 日均量(%)", 1, 30, 5)
        th_use = st.slider("融資使用率過高(%)", 30, 90, 60)
        th_mdays = st.slider("融資/融券連續增減天數", 3, 10, 5)
        th_mult = st.slider("即時爆量(量比倍數)", 1.5, 5.0, 2.0, 0.5)
    hold_cost = st.number_input("持有成本價(選填,0=未持有;用於停損停利提醒)", min_value=0.0, value=0.0, step=0.5)
    sl_pct = st.number_input("固定停損(%,可手動調整)", min_value=0.5, max_value=50.0, value=7.0, step=0.5)
    tp_pct = st.number_input("固定停利(%,可手動調整)", min_value=0.5, max_value=300.0, value=15.0, step=0.5)
    _nk = st.text_input("通知密碼(只有你自己需要,朋友留空)", value=st.query_params.get("k", ""), type="password")
    st.session_state["notify_ok"] = (not os.environ.get("NOTIFY_KEY")) or _nk == os.environ.get("NOTIFY_KEY")
    if os.environ.get("NOTIFY_KEY") and not st.session_state["notify_ok"]:
        st.caption("🔕 未輸入通知密碼:僅顯示畫面提醒,不會寄信/推播")
    if st.button("📲 發送測試推播"):
        if not st.session_state["notify_ok"]:
            st.warning("請先輸入通知密碼")
        else:
            st.toast("這是測試提醒", icon="🔔")
            for _line in test_push():
                st.write(_line)
    auto = st.checkbox("自動更新即時報價", value=False)
    secs = st.slider("更新間隔(秒)", 5, 60, 10)
    st.caption("KK版權所有,僅供資訊整理,不構成投資建議")

if mode == "全市場勝率掃描":
    render_scan()
    st.stop()

if mode == "多股同時分析":
    render_multi(multi_list, dict(days=th_days, ratio=th_ratio, use=th_use, mdays=th_mdays, mult=th_mult))
    if auto:
        time.sleep(secs)
        st.rerun()
    st.stop()

if "_saved_multi_sel" in st.session_state or st.session_state.get("_saved_multi_txt"):
    st.button("⬅️ 返回多股同時分析", on_click=_back_to_multi)
if not code:
    st.stop()
with st.spinner("抓取資料中..."):
    raw = get_prices(code)
    if raw.empty:
        st.error(f"找不到 {code} 的股價資料")
        st.stop()
        
    # 動態計算該股票的最大可回測年限並處理 "max" 選項
    start_date = pd.to_datetime(raw.index[0])
    today = pd.Timestamp.today()
    max_years = max(1, int((today - start_date).days / 365.25))

    if bt_years_option == "max":
        bt_years = max_years
    else:
        bt_years = bt_years_option

    live = get_live_quote(code)
    df = indicators(merge_live(raw, live)).dropna(subset=["MA60", "K", "RSI", "BB_UP"])
    inst = prep_inst(finmind("TaiwanStockInstitutionalInvestorsBuySell", code, 120))
    margin = finmind("TaiwanStockMarginPurchaseShortSale", code, 150)
    per = finmind("TaiwanStockPER", code, 1100)
    news = get_news(code, name or (live or {}).get("name", ""))

comps = tech_components(df)
t_score = int(comps.iloc[-1].sum())
i_score, i_notes = score_inst(inst)
m_score, m_notes, m_stats = score_margin(df, margin)
v_score, v_notes, v_stats = score_val(per, get_dividends(code), float(df.Close.iloc[-1]))
total = t_score + i_score + m_score + v_score
_hd = get_long_daily(code)
SS = situation_stats(merge_live(_hd, live)) if not _hd.empty else None
last, prev = df.iloc[-1], df.iloc[-2]

_qpx = live["price"] if live else float(df.Close.iloc[-1])
_qpc = (live or {}).get("prev") or float(df.Close.iloc[-2])
st.markdown(quote_card(name or (live or {}).get("name") or code, code, industry, _qpx, _qpc, live),
            unsafe_allow_html=True)
if live:
    if market_open():
        st.caption("盤中成交量尚未完整,量價訊號與量比僅供參考;技術指標已用即時價重算。")
else:
    st.warning("抓不到即時報價,以下使用最近一筆收盤資料。")

_lv = price_levels(df, total)
_prevc = (live or {}).get("prev") or float(df.Close.iloc[-2])
_cur = live["price"] if live else float(df.Close.iloc[-1])
alerts, ainfo = build_alerts(df, inst, margin, live, get_intraday(code), _prevc,
                             dict(days=th_days, ratio=th_ratio, use=th_use, mdays=th_mdays, mult=th_mult))
_SIG = build_signals(df, inst)
_atr = float(_SIG["atr"].iloc[-1])
_hi20, _lo20 = float(df.High.iloc[-21:-1].max()), float(df.Low.iloc[-21:-1].min())
alerts += signal_alerts(_SIG) + exit_alerts(_cur, _atr, _hi20, _lo20, hold_cost, sl_pct, tp_pct, df.index[-1].strftime("%m%d"))
st.subheader("🔔 即時提醒")
if alerts:
    _sigs = [a for a in alerts if a.get("kind") == "sig"]
    for a in sorted([a for a in alerts if a.get("kind") != "sig"], key=lambda x: -abs(x["s"])):
        box = st.success if a["s"] > 0 else st.error if a["s"] < 0 else st.warning
        box(f"**{a['title']}** {a['detail']}")
    if _sigs:
        _ord = sorted(_sigs, key=lambda x: (0 if x["s"] > 0 else 1 if x["s"] == 0 else 2, x["id"]))
        _box = st.success if all(a["s"] > 0 for a in _sigs) else st.error if all(a["s"] < 0 for a in _sigs) else st.info
        _box("**觀察重點:** " + "｜".join(a["brief"] for a in _ord))
    _net, _msg = combine_advice(alerts, _lv, _cur, total)
    st.info(f"💡 **當下建議**(提醒訊號合計 {_net:+d}):{_msg}")
    _seen = st.session_state.setdefault("seen_alerts", set())
    _act = st.session_state.get(f"sig_active_{code}", set())
    _lastn = st.session_state.setdefault(f"sig_last_{code}", {})
    _now, _out = time.time(), []
    for a in alerts:
        if a.get("kind") == "sig":
            new = a["id"] not in _act and _now - _lastn.get(a["id"], 0) >= 1800
            if new:
                _lastn[a["id"]] = _now
        else:
            k = f"{code}:{a['key']}"
            new = k not in _seen
            _seen.add(k)
        if new:
            st.toast(f"{a['title']}|{a['detail']}", icon="🔔")
            _out.append((f"【{name or code} {code}】{a['title']}\n{a['detail']}", abs(a["s"]) >= 2 or a.get("force", False)))
    _ak = f"{code}:advice:{_msg}"
    if _ak not in _seen:
        _seen.add(_ak)
        st.toast(f"當下建議:{_msg}", icon="💡")
        _out.append((f"【{name or code} {code}】當下建議(提醒訊號合計 {_net:+d})\n{_msg}\n現價 {_cur:.2f}", True))
    push_batch(_out)
else:
    st.caption("目前沒有觸發提醒條件(可在左側「提醒門檻」調整敏感度)。")
st.session_state[f"sig_active_{code}"] = {a["id"] for a in alerts if a.get("kind") == "sig"}
with st.expander("提醒判斷明細"):
    st.json(ainfo)
with st.expander("買賣訊號判斷明細(買進 7 項、賣出 7 項)"):
    st.dataframe(signal_rows(_SIG), hide_index=True, use_container_width=True)
    if not _SIG["has_inst"]:
        st.caption("查不到三大法人資料,依賴法人的訊號(買進①②③、賣出③)視為未觸發。")
    st.markdown("**買進⑥ 跌深反彈 vs 趨勢反轉**")
    st.markdown("- **跌深反彈(較弱)**:自20日高點回落逾10%、近3日反彈≥3%,但股價仍在月線下、季線仍下彎。多半是超賣後的技術性反彈,遇到月線、前高壓力容易回落,適合短線小量。\n"
                "- **趨勢反轉(較可靠)**:股價站上月線、月線轉為上彎、近期底部墊高(近5日最低高於更早的低點),且法人10日淨買超。代表下跌趨勢可能真正結束,可分批布局並設停損。")
with st.expander("停損 / 停利規則:參考價位與歷史效果(賣出訊號⑥)"):
    _basetxt = f"持有成本 {hold_cost:.2f}" if hold_cost > 0 else f"現價 {_cur:.2f}(尚未輸入持有成本,以現價假設進場)"
    st.write(f"參考價位依據:{_basetxt};ATR(14)={_atr:.2f}")
    st.dataframe(pd.DataFrame([(lb, p) for _, lb, p in exit_levels(_cur, _atr, _hi20, _lo20, hold_cost, sl_pct, tp_pct)],
                              columns=["規則", "參考價位"]).round(2), hide_index=True)
    st.caption("固定停損/停利的百分比可在左側手動調整。移動停損以近20日(不含今日)最高價與最低價為區間,只有現價跌破區間下緣、"
               "且區間最高價高於你的成本時才警示。")
    _ee = exit_effects(code, sl_pct, tp_pct)
    if _ee is None:
        st.caption("歷史資料不足,無法比較各種停損停利規則的效果。")
    else:
        _eff, _bh, _nent, _d0 = _ee
        st.markdown(f"**歷史效果比較**(自 {_d0} 起;進場範例:放量突破前20日高 或 站回20日線;訊號隔日開盤買進,最長持有20日,已扣交易成本;同期買進持有 {_bh:+.0f}%)")
        st.dataframe(_eff.round(1), hide_index=True, use_container_width=True)
        _okr = _eff[_eff["交易次數"] >= 5]
        if len(_okr):
            _okr = _okr.assign(分數=_okr["累計報酬%"] / _okr["最大回撤%"].abs().clip(lower=1))
            _bst = _okr.sort_values("分數", ascending=False).iloc[0]
            st.info(f"這檔股票過去,在這個進場範例下,報酬相對回撤最好的是「{_bst['出場規則']}」"
                    f"(累計 {_bst['累計報酬%']:+.0f}%、最大回撤 {_bst['最大回撤%']:.0f}%、{int(_bst['交易次數'])} 筆)。")
        st.caption("只代表這檔股票自己的歷史與這個進場範例,樣本不多時請保守看待;未計滑價與跳空風險,過去結果不保證未來。")

tab1, tabLive, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
    ["總覽與建議", "即時走勢", "K線圖", "籌碼面", "估值", "回測", "新聞", "配息試算"])

with tab1:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("收盤價", f"{last.Close:.2f}", f"{(last.Close / prev.Close - 1) * 100:+.2f}%")
    c2.metric("綜合評分", f"{total:+d}", help="技術+法人+融資券+估值,正值偏多")
    c3.metric("RSI(14)", f"{last.RSI:.0f}")
    pe, yl = v_stats.get("本益比"), v_stats.get("殖利率%")
    pe_txt = "—" if pe is None or pe != pe else f"{pe:.1f}"
    yl_txt = "—" if yl is None or yl != yl else f"{yl:.2f}%"
    c4.metric("本益比 / 殖利率", f"{pe_txt} / {yl_txt}")
    st.subheader("投資建議")
    st.markdown(rule_advice(total, last, df))
    if SS is not None:
        _r = pick_row(SS)
        st.info(f"📊 歷史相似情境:持有 {int(_r.持有天數)} 個交易日的勝率約 **{_r.勝率:.0f}%**"
                f"(全期平均 {_r.全期基準勝率:.0f}%,樣本 {int(_r.樣本數)} 筆),詳見「回測」分頁。")
    a, b = st.columns(2)
    with a:
        st.markdown(f"**技術面 {t_score:+d}**")
        for k, v in comps.iloc[-1].items():
            if v: st.write(f"{int(v):+d}  {k}")
        st.markdown(f"**三大法人 {i_score:+d}**")
        for x in i_notes: st.write(x)
    with b:
        st.markdown(f"**融資融券 {m_score:+d}**")
        for x in m_notes: st.write(x)
        st.markdown(f"**估值 {v_score:+d}**")
        for x in v_notes: st.write(x)
    st.subheader("AI 綜合解讀(含新聞)")
    if api_key:
        if st.button("產生 AI 解讀"):
            ctx = (f"代碼{code} 收盤{last.Close:.2f} 綜合評分{total}\n技術:{comps.iloc[-1].to_dict()}\n法人:{i_notes}\n"
                   f"融資券:{m_stats} {m_notes}\n估值:{v_stats} {v_notes}\n新聞:\n" + "\n".join("- " + n["title"] for n in news))
            with st.spinner("分析中..."):
                st.markdown(ai_advice(ctx, api_key))
    else:
        st.info("輸入 Anthropic API Key 後可產生新聞利多利空解讀與多空論點。")

with tabLive:
    prevc = (live or {}).get("prev") or float(df.Close.iloc[-2])
    idy = get_intraday(code)
    ticks = st.session_state.setdefault(f"ticks_{code}", [])
    if live:
        try:
            tt = pd.to_datetime(live["time"])
        except Exception:
            tt = pd.Timestamp.now()
        if not ticks or ticks[-1][0] != tt:
            ticks.append((tt, live["price"]))
        del ticks[:-3000]
    day = idy.index[-1].normalize() if not idy.empty else (ticks[-1][0].normalize() if ticks else pd.Timestamp.now().normalize())
    ser = idy.Close if not idy.empty else pd.Series(dtype=float)
    _stale = False
    try:
        _ld = pd.Timestamp(pd.to_datetime(live["date"]).date()) if live and live.get("date") else None
    except Exception:
        _ld = None
    if _ld is not None and (idy.empty or _ld > day):
        _stale = not idy.empty
        day, ser, idy = _ld, pd.Series(dtype=float), idy.iloc[0:0]
    if market_open() and not auto:
        st.info("⏸ 目前是盤中,但左側「自動更新即時報價」沒有開啟,走勢不會自動更新(只在你操作頁面時才更新一次)。")
    if _stale:
        st.caption("Yahoo 分時資料尚未提供今日走勢,目前圖上為程式逐次累積的即時成交價(從開啟本頁起算)。")
    tk = pd.Series({t: pr for t, pr in ticks if t.normalize() == day}, dtype=float)
    if len(tk) and len(ser):
        tk = tk[tk.index > ser.index[-1]]
    pts = pd.concat([ser, tk]).sort_index() if (len(ser) or len(tk)) else ser
    if pts.empty:
        st.warning("目前沒有分時資料(可能尚未開盤或資料來源暫時無法取得)。")
    else:
        cur = live["price"] if live else float(pts.iloc[-1])
        o = live["open"] if live else float(idy.Open.iloc[0])
        hi = live["high"] if live else float(idy.High.max())
        lo = live["low"] if live else float(idy.Low.min())
        vol = live["volume"] // 1000 if live else int(idy.Volume.sum() // 1000)
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("成交價", f"{cur:.2f}", f"{cur - prevc:+.2f} ({(cur / prevc - 1) * 100:+.2f}%)", delta_color="inverse")
        m2.metric("昨收", f"{prevc:.2f}")
        m3.metric("開 / 高 / 低", f"{o:.2f} / {hi:.2f} / {lo:.2f}")
        m4.metric("累計量(張)", f"{vol:,}")
        m5.metric("資料時間", pts.index[-1].strftime("%H:%M:%S") if len(tk) else pts.index[-1].strftime("%H:%M"))
        cl, cr = st.columns([3, 1])
        with cl:
            colr = UP if cur >= prevc else DN
            fill = "rgba(224,56,62,0.12)" if cur >= prevc else "rgba(31,157,85,0.12)"
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[.72, .28], vertical_spacing=.03)
            fig.add_trace(go.Scatter(x=pts.index, y=[prevc] * len(pts), mode="lines", name="昨收", hoverinfo="skip",
                                     line=dict(color="gray", width=1, dash="dot")), 1, 1)
            fig.add_trace(go.Scatter(x=pts.index, y=pts.values, mode="lines", name="成交價", fill="tonexty",
                                     fillcolor=fill, line=dict(color=colr, width=1.8),
                                     hovertemplate="%{x|%H:%M} %{y:.2f}<extra></extra>"), 1, 1)
            fig.add_trace(go.Scatter(x=[pts.index[-1]], y=[pts.iloc[-1]], mode="markers+text", showlegend=False,
                                     marker=dict(color=colr, size=8), text=[f"{pts.iloc[-1]:.2f}"],
                                     textposition="top left", hoverinfo="skip"), 1, 1)
            if not idy.empty:
                fig.add_trace(go.Bar(x=idy.index, y=idy.Volume / 1000, name="量(張)", hoverinfo="skip",
                                     marker_color=np.where(idy.Close >= idy.Open, UP, DN)), 2, 1)
            mgn = max(abs(pts.max() - prevc), abs(pts.min() - prevc), prevc * 0.003) * 1.15
            fig.update_yaxes(range=[prevc - mgn, prevc + mgn], row=1, col=1)
            fig.update_xaxes(range=[day + pd.Timedelta(hours=9), day + pd.Timedelta(hours=13, minutes=30)],
                             showspikes=True, spikemode="across", spikethickness=1)
            fig.update_layout(height=520, hovermode="x", showlegend=False, margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)
        with cr:
            bk = (live or {}).get("book")
            if bk:
                sp = lambda k: [v for v in str(bk.get(k, "")).split("_") if v]
                b, a, g, f = sp("b"), sp("a"), sp("g"), sp("f")
                n = min(5, max(len(b), len(a)))
                pad = lambda L: (L + ["—"] * n)[:n]
                st.markdown("**最佳五檔**")
                st.dataframe(pd.DataFrame({"買量": pad(g), "買價": pad(b), "賣價": pad(a), "賣量": pad(f)}),
                             hide_index=True)
            else:
                st.caption("目前資料來源沒有五檔報價。")
        st.caption("分時資料來自 yfinance(可能延遲),程式會把即時成交價逐次接在最後面;"
                   "請勾選左側「自動更新即時報價」讓走勢持續更新。")

with tab2:
    disp_name = name or (live or {}).get("name") or code
    k1, k2, k3 = st.columns([3, 3, 2])
    tf = k1.radio("週期", list(TF), index=4, horizontal=True)
    sel = k2.multiselect("均線", [5, 10, 20, 60, 120, 240], default=[5, 10, 20, 60, 120, 240],
                         format_func=lambda n: f"MA{n}")
    dn = {"日": 160, "週": 120, "月": 100}.get(tf, 120)
    nshow = k3.slider("顯示K棒數", 30, 500, dn, key=f"nshow_{tf}")
    kd = get_ohlc(code, tf)
    if kd.empty:
        st.warning("抓不到這個週期的資料(分K 只提供近期資料)。")
    else:
        kd = apply_live(kd, live, tf)
        mas = {n: kd.Close.rolling(n).mean() for n in (5, 10, 20, 60, 120, 240)}
        st.markdown(kline_header(kd, mas, sel, tf, f"{disp_name} ({code})"), unsafe_allow_html=True)
        _short = [f"MA{n}" for n in sel if len(kd) < n]
        st.caption(f"均線以「收盤價」簡單移動平均計算,共 {len(kd)} 根{tf}K,最新一根:{kd.index[-1]:%Y-%m-%d}"
                   + (f";{'、'.join(_short)} 歷史資料不足,不顯示" if _short else ""))
        st.plotly_chart(kline_chart(kd, mas, sel, tf, nshow), use_container_width=True,
                        config={"scrollZoom": True})
        st.caption("滑鼠移到K棒上,會顯示該根的開高低收、成交量、漲跌與各均線數值;可拖曳平移、滾輪縮放。"
                   "上方文字為最新一根的數值,▲▼ 表示均線較前一根上升或下降。")
    with st.expander("進階指標(MACD / KD / RSI / 布林通道 / 買賣價位線)"):
        st.plotly_chart(tech_chart(df, levels=price_levels(df, total)), use_container_width=True)

with tab3:
    st.subheader("三大法人買賣超(張)")
    if inst.empty:
        st.warning("無法人資料")
    else:
        d = inst.tail(60)
        fig = go.Figure([go.Bar(x=d.index, y=d[c], name=c) for c in d.columns])
        fig.update_layout(barmode="group", height=350, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(inst.tail(10).round(0).sort_index(ascending=False))
    st.subheader("融資融券餘額")
    if margin.empty:
        st.warning("無融資融券資料")
    else:
        m = margin.sort_values("date")
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Scatter(x=m.date, y=m.MarginPurchaseTodayBalance, name="融資餘額"), secondary_y=False)
        fig.add_trace(go.Scatter(x=m.date, y=m.ShortSaleTodayBalance, name="融券餘額"), secondary_y=True)
        fig.update_layout(height=350, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)
        st.json(m_stats)

with tab4:
    if per.empty:
        st.warning("查不到本益比資料。ETF(如 0050)沒有 EPS,因此沒有本益比;新上市或虧損的個股也可能沒有。")
        if v_stats:
            st.json(v_stats)
            st.caption("殖利率為依近12個月配息與最新收盤價推算,可能與官方公布數字略有差異。")
    else:
        st.json({k: v for k, v in v_stats.items()})
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        p = per[per.PER > 0]
        fig.add_trace(go.Scatter(x=p.date, y=p.PER, name="本益比"), secondary_y=False)
        fig.add_hline(y=p.PER.median(), line_dash="dot", annotation_text="近3年中位數")
        fig.add_trace(go.Scatter(x=per.date, y=per.dividend_yield, name="殖利率%"), secondary_y=True)
        fig.update_layout(height=400, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("本益比分位為與自身近3年比較,不代表絕對便宜或昂貴;跨產業請另行比較。")

with tab5:
    st.subheader("目前情境的歷史勝率")
    if SS is None:
        st.warning("歷史資料不足(至少需要約一年多的日線),無法統計。")
    else:
        T, R = SS["table"], pick_row(SS)
        st.write(f"目前狀態:技術分數 **{SS['score']:+d}**、股價在季線**{'之上' if SS['above'] else '之下'}**、"
                 f"RSI 區間 **{SS['zone']}**。以過去約 {SS['years']:.0f} 年({SS['days']} 個交易日)中與此相似的日子為樣本,"
                 "假設當天收盤買進、持有 N 個交易日後賣出,已扣除手續費與證交稅。")
        w1, w2, w3, w4 = st.columns(4)
        w1.metric(f"持有{int(R.持有天數)}日勝率", f"{R.勝率:.0f}%", f"{R.勝率 - R.全期基準勝率:+.0f}% 對比全期平均")
        w2.metric("95%信賴區間", f"{R.勝率下限:.0f}% ~ {R.勝率上限:.0f}%")
        w3.metric("平均 / 中位數報酬", f"{R.平均報酬:+.1f}% / {R.中位數:+.1f}%")
        w4.metric("樣本數", f"{int(R.樣本數)} 筆")
        if R.勝率下限 > 50 and R.中位數 > 0:
            st.success("歷史上與目前相似的情境,多數時候是賺錢的,且勝率明顯高於 50%,統計上相對可靠。")
        elif R.勝率 >= R.全期基準勝率 + 5 and R.中位數 > 0:
            st.info("歷史上略優於這檔股票的平均表現,但樣本有限、信賴區間偏寬,可信度中等。")
        elif R.勝率 < 45:
            st.warning("歷史上與目前相似的情境多半是賠的,偏不利。")
        else:
            st.info("與全期平均沒有明顯差異,表示目前的技術訊號在這檔股票上沒有顯著優勢。")
        fig = go.Figure([go.Bar(x=list(T.期間), y=T.勝率, name="目前相似情境勝率",
                                marker_color="#e0383e"),
                         go.Bar(x=list(T.期間), y=T.全期基準勝率, name="全期平均勝率",
                                marker_color="#999999")])
        fig.add_hline(y=50, line_dash="dot")
        fig.update_layout(barmode="group", height=300, margin=dict(t=10), yaxis_title="勝率 %")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(T.round(1), hide_index=True)
        st.subheader("長期持有勝率(買進後長期不動)")
        LT = T[T["持有天數"] >= 120]
        if LT.empty:
            st.info("歷史資料不足以統計半年以上的持有結果(上市時間太短)。")
        else:
            for col_, r_ in zip(st.columns(len(LT)), LT.itertuples()):
                col_.metric(f"持有{r_.期間}", f"{r_.勝率:.0f}%", f"{r_.勝率 - r_.全期基準勝率:+.0f}% 對比全期平均")
                col_.caption(f"中位數報酬 {r_.中位數:+.1f}%(年化約 {r_.年化中位數:+.1f}%)  \n"
                             f"平均 {r_.平均報酬:+.1f}%  \n最差 {r_.最差:+.1f}%  \n獨立樣本約 {r_.獨立樣本約:.1f} 組")
            if (LT["獨立樣本約"] < 3).any():
                st.warning("部分長期期間的獨立樣本不到 3 組(例如 3 年期在 10 年資料中只有約 3 個不重疊區間),"
                           "勝率只能當作粗略參考。")
            st.caption("長期結果只計價差,不含配息,對高配息標的(如 ETF)會低估實際報酬;"
                       "「全期基準勝率」代表任何一天買進並持有同樣期間的勝率,可用來檢查目前進場時機有沒有比隨機進場更好。")
        st.caption("樣本條件:" + R.條件 + "。相鄰日期的結果高度重疊,實際獨立樣本比「樣本數」少很多,所以另外提供信賴區間。"
                   "這是歷史統計,不是對未來的預測;未含基本面與突發消息,過去勝率高不代表下次會賺。")
    st.markdown("---")
    st.subheader("技術訊號策略回測")
    st.caption(f"回測期間: {'全部歷史 (' + str(bt_years) + '年)' if bt_years_option == 'max' else str(bt_years) + '年'}。回測只使用「技術面分數」(法人、融資券與估值缺乏足夠長的歷史),次日收盤價成交,已計入手續費 0.1425%×2 與證交稅 0.3%。過去績效不代表未來。")
    bd = df.tail(int(bt_years * 245))
    eq, bh, stat = backtest(bd, comps.loc[bd.index].sum(axis=1), buy_th, sell_th)
    cols = st.columns(4)
    for i, (k, v) in enumerate(stat.items()):
        cols[i % 4].metric(k, f"{v:.1f}" if isinstance(v, float) else v)
    fig = go.Figure([go.Scatter(x=eq.index, y=eq, name="技術訊號策略"), go.Scatter(x=bh.index, y=bh, name="買進持有")])
    fig.update_layout(height=400, margin=dict(t=10), yaxis_title="淨值(起始=1)")
    st.plotly_chart(fig, use_container_width=True)

with tab6:
    for n in sorted(news, key=lambda x: x.get("ts", 0), reverse=True):
        _t, _u = _html.escape(n["title"]), _html.escape(n["link"], quote=True)
        # 網頁版:新分頁開啟;手機 App(WebView/PWA)常擋 target=_blank,因此另附「本頁開啟」連結
        st.markdown(
            f'<div style="margin:0 0 .8rem 0">'
            f'<a href="{_u}" target="_blank" rel="noopener noreferrer">{_t}</a><br>'
            f'<small>{n["date"]} ｜ <a href="{_u}" target="_self">📱 App 點這裡開啟</a></small></div>',
            unsafe_allow_html=True)
    if not news:
        st.write("沒有抓到新聞")

with tab7:
    t = get_div_table(code)
    if t.empty:
        st.warning("查不到配息紀錄(新上市、未配息或資料來源缺漏)。")
    else:
        price = float(last.Close)
        S = div_summary(t)
        y_ttm = S["ttm"] / price * 100
        y_avg = S["avg3"] / price * 100 if S["avg3"] else None
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("近12個月配息", f"{S['ttm']:.2f} 元", f"殖利率 {y_ttm:.2f}%")
        c2.metric("近3年平均年配息", f"{S['avg3']:.2f} 元" if S["avg3"] else "—",
                  f"殖利率 {y_avg:.2f}%" if y_avg else None)
        c3.metric("每年配息次數", f"{S['cnt']:.1f} 次" if S["cnt"] else "—")
        c4.metric("常見除息月份", "、".join(f"{m}月" for m in S["months"]) or "—")
        if S["next_m"]:
            st.info(f"依過去規律,下一次除息約在 **{S['next_m']} 月**;實際發放日通常在除息後約 3~5 週,請以公司或投信公告為準。")

        a = S["annual"]
        fig = go.Figure(go.Bar(x=a.index.astype(str), y=a.values, name="每年現金股利(元)"))
        fig.update_layout(height=300, margin=dict(t=10), yaxis_title="元/股")
        st.plotly_chart(fig, use_container_width=True)

        show = t.tail(12).iloc[::-1].copy()
        show["占現價%"] = (show["現金股利"] / price * 100).round(2)
        show["除息日"] = show["除息日"].dt.strftime("%Y-%m-%d")
        show["發放日"] = show["發放日"].dt.strftime("%Y-%m-%d") if show["發放日"].notna().any() else "—"
        st.dataframe(show[["除息日", "發放日", "現金股利", "占現價%"]], hide_index=True)
        if not t["發放日"].notna().any():
            st.caption("此標的資料來源沒有發放日(ETF 常見),僅列除息日。")

        st.subheader("配息試算")
        x1, x2 = st.columns(2)
        amt = x1.number_input("投入金額(萬元)", min_value=1.0, max_value=100000.0, value=100.0, step=10.0)
        basis = x2.radio("殖利率依據", ["近12個月", "近3年平均", "自訂"], horizontal=True)
        if basis == "近3年平均" and y_avg:
            y = y_avg
        elif basis == "自訂":
            y = st.number_input("自訂殖利率(%)", 0.0, 30.0, 4.0, 0.1)
        else:
            y = y_ttm
        cap = amt * 10000
        lots = int(cap // (price * 1000))
        odd = int((cap - lots * price * 1000) // price)
        annual_div = cap * y / 100
        n = max(int(round(S["cnt"])), 1) if S["cnt"] else 1
        per_pay = annual_div / n
        prem = per_pay * 0.0211 if per_pay >= 20000 else 0.0
        net = annual_div - prem * n
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("約可買入", f"{lots} 張 + {odd} 股", f"以現價 {price:.2f} 計")
        m2.metric("預估年配息(稅前)", f"{annual_div:,.0f} 元", f"殖利率 {y:.2f}%")
        m3.metric("平均每月", f"{annual_div / 12:,.0f} 元")
        m4.metric("每次配息(約)", f"{per_pay:,.0f} 元", f"每年約 {n} 次")
        if prem:
            st.write(f"單次配息超過 2 萬元,需扣二代健保補充保費 2.11%,約 {prem:,.0f} 元/次;扣除後年配息約 **{net:,.0f} 元**。")
        if S["months"]:
            plan = pd.DataFrame({"除息月份": [f"{m} 月" for m in S["months"]],
                                 "預估每次配息(元)": [round(per_pay)] * len(S["months"])})
            st.dataframe(plan, hide_index=True)
        st.caption("試算以歷史配息推估,配息並非保證,公司獲利或 ETF 配息政策改變會使金額不同;"
                   "ETF 配息可能含收益平準金或本金。以現價買入計算,未含個人所得稅(合併計稅或分離課稅)、"
                   "匯費與股價漲跌。資料來源可能與官方公告有差異,請以公告為準。")

if auto:
    import time as _t
    _t.sleep(secs)
    st.rerun()
