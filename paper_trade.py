#!/usr/bin/env python3
"""
paper_trade.py — 台股模擬交易引擎(保守型 / 穩健型 / 積極型)

直接重用 app1.py 裡的 indicators / tech_components / sstreak / build_signals(以 ast 抽出,不會執行 Streamlit 介面),
訊號邏輯與你的儀表板完全一致。

用法(app1.py 放在同一資料夾):
  python paper_trade.py --start 2023-01-02 --end 2025-12-31 --capital 1000000
  python paper_trade.py --start 2022-01-03 --end 2024-12-31 --profiles 穩健型 --max-stocks 300
  python paper_trade.py --selftest          # 用假資料驗證引擎(不需連網,數字沒有任何意義)

重點設計
  1. 無未來函數:每一天的「情境勝率」只使用「截至當天已經實現」的樣本。
  2. T 日收盤後判斷訊號,T+1 開盤成交(買,賣皆同);停損盤中觸價,成交取 min(停損價, 開盤價)。
  3. 成本:買進手續費 0.1425%、賣出手續費 0.1425% + 證交稅 0.3%、單邊滑價 0.1%。
  4. 現金股利:由「還原收盤/收盤」的比值反推除息金額,除息日入帳(未扣補充保費)。
  5. 期末不強制賣出,持股以最後收盤價計價。
本工具僅供資訊整理,不構成投資建議。
"""
import argparse
import ast
import json
import os
import pickle
import sys
import time
from collections import Counter

import numpy as np
import pandas as pd

FEE, TAX, SLIP = 0.001425, 0.003, 0.001            # 交易成本屬於引擎;「什麼時候買賣」的規則在 strategy_rules.py
TAX_ETF = 0.001                                     # ETF 賣出證交稅 0.1%
CORE_MAX = 0.8                                      # 「核心 ETF」占閒置資金的上限:永遠至少留 20% 閒置資金為現金,不 all-in
REBAL_BAND = 0.03                                   # 核心部位與目標差距超過總資產 3% 才調整(避免天天小額進出、白付手續費)
ETF_LIST = {"0050": "元大台灣50", "006208": "富邦台50", "0056": "元大高股息", "00878": "國泰永續高股息", "00713": "元大台灣高息低波",
            "00692": "富邦公司治理", "00850": "元大臺灣ESG永續", "00881": "國泰台灣5G+", "00919": "群益台灣精選高息", "00929": "復華台灣科技優息"}

import strategy_rules as sr  # noqa: E402
from strategy_rules import (COST_RT, HORIZON, MIN_AMT, Z, PROFILES, NEED_SIGS, load_params,  # noqa: E402,F401
                            asof_stats, entry_candidate)


# ------------------------------------------------------------------ 輔助與選股函式
def etf_rows():
    """ETF 清單(FinMind 的股票池只收 4 碼個股,ETF 要另外加)。industry_category 固定為 'ETF'。"""
    return pd.DataFrame([dict(stock_id=c, stock_name=n, industry_category="ETF", ticker=f"{c}.TW") for c, n in ETF_LIST.items()])


def load_etf_universe():
    """FinMind 的全部上市櫃 ETF(代碼 00 開頭,例如 0050、00878、006208、00679B)。給手動操盤選股用;回測股票池仍用 ETF_LIST。"""
    import requests
    p = {"dataset": "TaiwanStockInfo"}
    if os.environ.get("FINMIND_TOKEN"):
        p["token"] = os.environ["FINMIND_TOKEN"]
    df = pd.DataFrame(requests.get("https://api.finmindtrade.com/api/v4/data", params=p, timeout=30).json().get("data", []))
    df = df[df["type"].isin(["twse", "tpex"])].copy()
    df["stock_id"] = df["stock_id"].astype(str)
    df = df[df["stock_id"].str.fullmatch(r"00[0-9]{2,4}[A-Z]?")].drop_duplicates("stock_id")
    df["ticker"] = df["stock_id"] + np.where(df["type"] == "tpex", ".TWO", ".TW")
    df["industry_category"] = "ETF"
    return df[["stock_id", "stock_name", "industry_category", "ticker"]].reset_index(drop=True)


def etf_universe(base=None):
    """全部上市櫃 ETF 清單。有 <base>/etf_universe.csv 就直接讀,沒有就向 FinMind 抓並存起來;連不上就退回內建的 ETF_LIST(不存檔,下次再試)。"""
    f = os.path.join(base, "etf_universe.csv") if base else None
    if f and os.path.exists(f):
        df = pd.read_csv(f, dtype=str).fillna("")
        if len(df):
            return df
    try:
        df = load_etf_universe()
    except Exception:                                       # noqa: BLE001
        df = pd.DataFrame()
    if len(df):
        if f:
            os.makedirs(base, exist_ok=True)
            df.to_csv(f, index=False, encoding="utf-8-sig")
        return df
    return etf_rows()


def with_all_etfs(uni, base=None):
    """股票池 + 全部 ETF(含內建清單裡的,已存在的代碼不重複加)。"""
    return with_etfs(pd.concat([uni, etf_universe(base).loc[lambda e: ~e.stock_id.isin(uni.stock_id)]], ignore_index=True))


def with_etfs(uni):
    """把 ETF 併進股票池(已存在的代碼不重複加)。"""
    e = etf_rows()
    return pd.concat([uni, e[~e.stock_id.isin(uni.stock_id)]], ignore_index=True)


def select_and_prepare(uni, data, start, max_stocks, ns):
    """根據起始日前一年的平均成交值挑選前 N 檔個股(ETF 不佔名額,資料夠就全部納入),並預先計算指標與訊號"""
    pre = {}
    for t, df in data.items():
        if len(df.loc[:start]) >= 300:
            d = df.loc[:start].tail(250)
            pre[t] = float((d.Close * d.Volume).mean())
    rows = {r["ticker"]: r for r in uni.to_dict("records")}
    ranked = sorted(pre, key=pre.get, reverse=True)
    is_etf = lambda t: rows.get(t, {}).get("industry_category") == "ETF"      # noqa: E731
    keep = [t for t in ranked if not is_etf(t)][:max_stocks] + [t for t in ranked if is_etf(t)]
    stocks = {}
    for t in keep:
        if t in rows and t in data:
            s = prepare(rows[t], data[t], ns)
            stocks[s["code"]] = s
    return stocks


def day_index_maps(stocks, cal):
    """為每個交易日預先建立各股票的索引對應，加速回測"""
    return [{c: int(s["idx"].get_indexer([d])[0]) for c, s in stocks.items() if s["idx"].get_indexer([d])[0] >= 0} for d in cal]


def bench_mult(bench, cal):
    """計算對照組的基準倍數"""
    return 1.0


# ------------------------------------------------------------------ 載入 app1.py 的訊號函式
def load_app_funcs(path):
    if not os.path.exists(path):
        sys.exit(f"找不到 {path},請用 --app 指定 app1.py 的路徑")
    tree = ast.parse(open(path, encoding="utf-8").read())
    want = {"indicators", "tech_components", "sstreak", "build_signals"}
    ns = {"np": np, "pd": pd}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            exec(compile(ast.Module(body=[node], type_ignores=[]), path, "exec"), ns)
    miss = want - set(ns)
    if miss:
        sys.exit(f"app1.py 缺少函式:{miss}")
    return ns


# ------------------------------------------------------------------ 資料載入
def load_universe():
    import requests
    p = {"dataset": "TaiwanStockInfo"}
    if os.environ.get("FINMIND_TOKEN"):
        p["token"] = os.environ["FINMIND_TOKEN"]
    df = pd.DataFrame(requests.get("https://api.finmindtrade.com/api/v4/data", params=p, timeout=30).json().get("data", []))
    df = df[df["type"].isin(["twse", "tpex"])].copy()
    df["stock_id"] = df["stock_id"].astype(str)
    df = df[df["stock_id"].str.fullmatch(r"[1-9][0-9]{3}")].drop_duplicates("stock_id")
    df["ticker"] = df["stock_id"] + np.where(df["type"] == "tpex", ".TWO", ".TW")
    if "industry_category" not in df:
        df["industry_category"] = ""
    return df[["stock_id", "stock_name", "industry_category", "ticker"]].reset_index(drop=True)


def _clean(h):
    cols = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    h = h[cols].dropna(subset=["Open", "High", "Low", "Close"])
    h = h[(h.Volume > 0) & (h.Close > 0) & (h.Open > 0)]
    h["Adj Close"] = h["Adj Close"].fillna(h["Close"])
    h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
    return h[~h.index.duplicated()].sort_index()


def download(tickers, cache, batch=40):
    import yfinance as yf
    data = pickle.load(open(cache, "rb")) if os.path.exists(cache) else {}
    todo = [t for t in tickers if t not in data]
    for b in range(0, len(todo), batch):
        part = todo[b:b + batch]
        try:
            raw = yf.download(part, period="max", interval="1d", auto_adjust=False,
                              group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print("下載失敗,稍後重試:", e)
            continue
        for t in part:
            try:
                h = _clean(raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw)
            except Exception:
                continue
            if len(h) >= 300:
                data[t] = h
        print(f"  下載 {min(b + batch, len(todo))}/{len(todo)}", flush=True)
        pickle.dump(data, open(cache, "wb"))
        time.sleep(0.5)
    return data


def synthetic(n=60, seed=7):
    """假資料:僅用於驗證引擎邏輯,績效數字沒有任何意義。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2014-01-01", "2025-12-31")
    uni, data = [], {}
    for i in range(n):
        vol = rng.uniform(0.012, 0.03)
        r = rng.normal(0.0003, vol, len(dates))
        c = rng.uniform(20, 300) * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.004, len(dates)))
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, len(dates))))
        l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, len(dates))))
        v = rng.uniform(2e6, 2e7, len(dates)) * (1 + 3 * np.abs(r) / vol)
        df = pd.DataFrame(dict(Open=o, High=h, Low=l, Close=c, Volume=v), index=dates)
        df["Adj Close"] = df.Close
        t = f"{1101 + i}.TW"
        data[t] = df
        uni.append(dict(stock_id=t[:4], stock_name=f"測試{i}", industry_category=f"產業{i % 8}", ticker=t))
    return pd.DataFrame(uni), data


# ------------------------------------------------------------------ 逐檔預先計算
def prepare(row, df, ns):
    return sr.prepare_stock(row, df, ns)


# ------------------------------------------------------------------ 模擬主迴圈
def new_state(capital):
    """帳戶狀態,全部是可存成 JSON 的型別。"""
    return dict(cash=float(capital), pos={}, pend_sell=[], pend_buy=[], pend_info={}, sell_why={},
                last_close={}, prev_eq=float(capital), eq=[], trades=[], log=[],
                core=dict(sh=0, cost=0.0, target=0.0, realized=0.0))


def step_day(P, st, stocks, date, day_i, final=False, intraday=False):
    """處理「一個交易日」:除息 → 開盤賣出 → 開盤買進 → 盤中停損/到期 → 收盤計價 → 產生隔日訊號。
    intraday=True(盤中模式的帳戶):買賣已經在盤中用即時價做掉了,收盤只做計價/停損/到期,不再產生隔日的買賣單。"""
    d, ev, pos = str(pd.Timestamp(date).date()), [], st["pos"]
    for p in pos.values():
        p["n"] += 1
    # 核心 ETF 部位:只吃「閒置資金」的一部分(core_pct),且不佔 max_pos、不吃停損/到期;遇到個股要買會先賣一部分籌資金
    core_code, core_pct = P.get("core_code", "0050"), min(max(float(P.get("core_pct", 0.0)), 0.0), CORE_MAX)
    core = st.setdefault("core", dict(sh=0, cost=0.0, target=0.0, realized=0.0))
    core_on = core_pct > 0 and core_code in stocks
    ci = day_i.get(core_code, -1) if (core_on or core["sh"] > 0) and core_code in stocks else -1

    def core_trade(sh, px, side, why):
        if sh < 1:
            return
        if side == "buy":
            cost = sh * px * (1 + FEE)
            st["cash"] -= cost
            core["sh"] += sh
            core["cost"] += cost
            ev.append(f"核心 買進 {core_code} {stocks[core_code]['name']} {sh:,} 股 @ {px:.2f}(約 {cost:,.0f} 元,{why})")
        else:
            sh = min(sh, core["sh"])
            proceeds = sh * px * (1 - FEE - TAX_ETF)
            basis = core["cost"] * sh / core["sh"]
            st["cash"] += proceeds
            core["sh"] -= sh
            core["cost"] -= basis
            core["realized"] += proceeds - basis
            ev.append(f"核心 賣出 {core_code} {stocks[core_code]['name']} {sh:,} 股 @ {px:.2f}({why},{(proceeds / basis - 1) * 100:+.1f}%)")

    def close_pos(code, px, why):
        p = pos.pop(code)
        proceeds = p["sh"] * px * (1 - FEE - (TAX_ETF if stocks[code]["ind"] == "ETF" else TAX))
        st["cash"] += proceeds
        r = proceeds / p["cost"] - 1
        st["trades"].append(dict(代碼=code, 名稱=stocks[code]["name"], 買進日=p["d"], 賣出日=d, 買價=round(float(p["ep"]), 2),
                                 賣價=round(float(px), 2), 股數=p["sh"], 損益=round(float(proceeds - p["cost"])),
                                 報酬率=float(r), 持有天=p["n"], 原因=why))
        ev.append(f"賣出 {code} {stocks[code]['name']} {p['sh']:,} 股 @ {px:.2f}({why},{r * 100:+.1f}%)")

    for code, p in pos.items():
        i = day_i.get(code, -1)
        if i >= 0 and stocks[code]["div"][i] > 0:
            amt = p["sh"] * float(stocks[code]["div"][i])
            st["cash"] += amt
            ev.append(f"股息 {code} {stocks[code]['name']} +{amt:,.0f} 元")
    if core["sh"] > 0 and ci >= 0 and stocks[core_code]["div"][ci] > 0:
        amt = core["sh"] * float(stocks[core_code]["div"][ci])
        st["cash"] += amt
        ev.append(f"股息 核心 {core_code} +{amt:,.0f} 元")
    for code in list(st["pend_sell"]):
        i = day_i.get(code, -1)
        if i >= 0 and code in pos:
            close_pos(code, float(stocks[code]["O"][i]) * (1 - SLIP), "賣出訊號 " + "/".join(st["sell_why"].get(code, [])))
            st["pend_sell"].remove(code)
        elif code not in pos:
            st["pend_sell"].remove(code)
    if core["sh"] > 0 and ci >= 0 and st["pend_buy"]:       # 個股要買、現金不夠 → 先賣一部分核心 ETF 籌資金
        n_new = min(sum(c not in pos for c in st["pend_buy"]), P["max_pos"] - len(pos))
        short = n_new * st["prev_eq"] * P["pct"] - st["cash"]
        if short > 0:
            px = float(stocks[core_code]["O"][ci]) * (1 - SLIP)
            core_trade(int(np.ceil(short / (px * (1 - FEE - TAX_ETF)))), px, "sell", "為個股買進備資金")
    for code in st["pend_buy"]:
        if len(pos) >= P["max_pos"]:
            break
        s = stocks[code]; i = day_i.get(code, -1)
        if i <= 0 or code in pos:
            continue
        o = float(s["O"][i])
        if o >= s["C"][i - 1] * 1.095:
            ev.append(f"略過 {code} {s['name']}:開盤接近漲停,買不到")
            continue
        tgt = st["prev_eq"] * P["pct"]
        budget = min(st["cash"], tgt)
        if budget < 0.5 * tgt:
            ev.append(f"略過 {code} {s['name']}:現金不足")
            continue
        px = o * (1 + SLIP)
        sh = int(budget // (px * (1 + FEE)))
        if sh < 1:
            continue
        cost = sh * px * (1 + FEE)
        st["cash"] -= cost
        pos[code] = dict(sh=sh, ep=float(px), d=d, n=1, stop=float(o * (1 - P["stop"])), cost=float(cost))
        ev.append(f"買進 {code} {s['name']} {sh:,} 股 @ {px:.2f}(約 {cost:,.0f} 元,停損價 {o * (1 - P['stop']):.2f})")
    st["pend_buy"], st["pend_info"] = [], {}
    if ci >= 0:                                             # 核心部位向「昨天算好的目標」靠攏(差距夠大才動)
        o, band = float(stocks[core_code]["O"][ci]), REBAL_BAND * st["prev_eq"]
        gap = core["target"] - core["sh"] * o
        if gap < -band:
            px = o * (1 - SLIP)
            core_trade(int(min(core["sh"], -gap // px)), px, "sell", "閒置資金減少,調降核心")
        elif gap > band and core_on:
            px = o * (1 + SLIP)
            core_trade(int(min(gap, st["cash"]) // (px * (1 + FEE))), px, "buy", "閒置資金停泊")
    for code in list(pos):
        p, s = pos[code], stocks[code]; i = day_i.get(code, -1)
        if i < 0:
            continue
        if s["L"][i] <= p["stop"] and p.get("intraday") != d:         # 今天盤中才買的:當天最低價可能發生在買進之前,不算
            close_pos(code, min(p["stop"], float(s["O"][i])) * (1 - SLIP), "停損")
        elif p["n"] >= P["hold"]:
            close_pos(code, float(s["C"][i]) * (1 - SLIP), "持有到期")
    for p in pos.values():
        p.pop("intraday", None)
    mv = 0.0
    for code, p in pos.items():
        i = day_i.get(code, -1)
        if i >= 0:
            st["last_close"][code] = float(stocks[code]["C"][i])
        mv += p["sh"] * st["last_close"].get(code, p["ep"])
    if ci >= 0:
        st["last_close"][core_code] = float(stocks[core_code]["C"][ci])
    if core["sh"] > 0:
        mv += core["sh"] * st["last_close"].get(core_code, core["cost"] / core["sh"])
    equity = st["cash"] + mv
    st["eq"].append([d, float(equity), float(mv / equity), float(st["cash"])])
    st["prev_eq"] = float(equity)
    if not final:
        st["sell_why"] = {}
        for code in ([] if intraday else pos):
            i = day_i.get(code, -1)
            if i < 0:
                continue
            hit = [e for e in sorted(P["exit"]) if stocks[code]["sig"][e][i]]
            if hit and code not in st["pend_sell"]:
                st["pend_sell"].append(code)
                st["sell_why"][code] = hit
        slots = P["max_pos"] - (len(pos) - len(st["pend_sell"]))
        cands = []
        if slots > 0 and not intraday:
            for code, i in day_i.items():
                s = stocks[code]
                if i < 250 or code in pos or (core_on and code == core_code):
                    continue
                r = entry_candidate(P, s, i)
                if r:
                    cands.append((r[0], r[1], code, r[2]))
            cands.sort(key=lambda t: (-t[0], -t[1]))
            held = Counter(stocks[c]["ind"] for c in pos if c not in st["pend_sell"])
            for wl, edge, code, hit in cands:
                if len(st["pend_buy"]) >= slots:
                    break
                ind = stocks[code]["ind"]
                if ind and held[ind] >= P["ind_max"]:
                    continue
                st["pend_buy"].append(code)
                st["pend_info"][code] = dict(wl=round(float(wl), 1), edge=round(float(edge), 1), sigs=hit)
                held[ind] += 1
        # 明天的核心目標 = core_pct × 閒置資金;閒置資金 = 總資產 − 明天仍持有的個股 − 明天預定買進的個股(其餘留現金)
        keep_mv = sum(p["sh"] * st["last_close"].get(c, p["ep"]) for c, p in pos.items() if c not in st["pend_sell"])
        idle = max(equity - keep_mv - len(st["pend_buy"]) * P["pct"] * equity, 0.0)
        core["target"] = float(core_pct * idle) if core_on else 0.0
    st["log"].append(dict(date=d, events=ev))
    return ev


def run_profile(name, P, stocks, cal, k0, capital, mult=None, maps=None):
    st, n = new_state(capital), len(cal)
    for k in range(k0, n):
        if maps is not None:
            day_i = maps[k]
        else:
            day_i = {c: int(s["pos"][k]) for c, s in stocks.items() if s["pos"][k] >= 0}
        step_day(P, st, stocks, cal[k], day_i, final=(k == n - 1))
    e = pd.DataFrame(st["eq"], columns=["date", "equity", "exposure", "cash"])
    e["date"] = pd.to_datetime(e["date"])
    e = e.set_index("date")
    mv = float(e.exposure.iloc[-1] * e.equity.iloc[-1])
    return e, pd.DataFrame(st["trades"]), dict(open_positions=len(st["pos"]), open_mv=mv,
                                               open_cost=sum(p["cost"] for p in st["pos"].values()))


def metrics(e, tr, capital):
    yrs = max((e.index[-1] - e.index[0]).days / 365.25, 0.01)
    fin = e.equity.iloc[-1]
    m = {"期末資產": round(fin), "總報酬%": (fin / capital - 1) * 100, "年化報酬%": ((fin / capital) ** (1 / yrs) - 1) * 100,
         "最大回撤%": (e.equity / e.equity.cummax() - 1).min() * 100, "平均持股水位%": e.exposure.mean() * 100}
    if len(tr):
        m.update({"交易次數": len(tr), "勝率%": (tr["報酬率"] > 0).mean() * 100, "平均單筆%": tr["報酬率"].mean() * 100,
                  "平均持有天": tr["持有天"].mean(), "停損比例%": (tr["原因"] == "停損").mean() * 100})
    else:
        m.update({"交易次數": 0})
    return m


# ------------------------------------------------------------------ 主程式
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-02")
    ap.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--profiles", default="all", help="all 或以逗號分隔:保守型,穩健型,積極型")
    ap.add_argument("--max-stocks", type=int, default=500, help="以起始日前一年平均成交值取前 N 檔(加速)")
    ap.add_argument("--codes", default="", help="只跑指定代碼,例如 2330,2317,2454")
    ap.add_argument("--app", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "app1.py"))
    ap.add_argument("--out", default="paper_out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    ns = load_app_funcs(a.app)
    start, end = pd.Timestamp(a.start), pd.Timestamp(a.end)
    names = list(PROFILES) if a.profiles == "all" else [x.strip() for x in a.profiles.split(",")]

    if a.selftest:
        uni, data = synthetic()
        bench = None
        print("【自我測試】使用假資料,績效數字沒有任何意義,只用來驗證引擎能正常運作。")
    else:
        uni = load_universe()
        if a.codes:
            uni = uni[uni.stock_id.isin([c.strip() for c in a.codes.split(",")])]
        uni = with_etfs(uni)
        print(f"股票池 {len(uni)} 檔(含 ETF),下載/讀取快取…")
        data = download(list(uni.ticker) + ["0050.TW"], os.path.join(a.out, "prices.pkl"))
        bench = data.get("0050.TW")

    stocks = select_and_prepare(uni, data, start, a.max_stocks, ns)
    print(f"納入模擬 {len(stocks)} 檔(含 ETF)")
    cal = sorted(set().union(*[set(s["idx"][(s["idx"] >= start) & (s["idx"] <= end)]) for s in stocks.values()]))
    cal = pd.DatetimeIndex(cal)
    for s in stocks.values():
        s["pos"] = s["idx"].get_indexer(cal)
    if len(cal) < 20:
        sys.exit("模擬區間內交易日太少,請檢查日期範圍與資料")

    summary, curves = {}, {}
    for nm in names:
        print(f"模擬 {nm} …", flush=True)
        e, tr, info = run_profile(nm, PROFILES[nm], stocks, cal, 0, a.capital)
        e.to_csv(os.path.join(a.out, f"{nm}_equity.csv"), encoding="utf-8-sig")
        tr.to_csv(os.path.join(a.out, f"{nm}_trades.csv"), index=False, encoding="utf-8-sig")
        summary[nm] = {**metrics(e, tr, a.capital), "期末持股檔數": info["open_positions"]}
        curves[nm] = e.equity
    if bench is not None:
        b = bench.loc[cal[0]:cal[-1], "Adj Close"]
        if len(b) > 1:
            curves["0050 買進持有"] = b / b.iloc[0] * a.capital
            yrs = (b.index[-1] - b.index[0]).days / 365.25
            summary["0050 買進持有"] = {"期末資產": round(curves["0050 買進持有"].iloc[-1]),
                                    "總報酬%": (b.iloc[-1] / b.iloc[0] - 1) * 100,
                                    "年化報酬%": ((b.iloc[-1] / b.iloc[0]) ** (1 / yrs) - 1) * 100,
                                    "最大回撤%": (b / b.cummax() - 1).min() * 100}
    S = pd.DataFrame(summary).T
    S.to_csv(os.path.join(a.out, "summary.csv"), encoding="utf-8-sig")
    pd.set_option("display.width", 200)
    print("\n" + f"模擬區間 {cal[0].date()} ~ {cal[-1].date()},本金 {a.capital:,.0f}")
    print(S.round(1).to_string())
    try:
        import plotly.graph_objects as go
        fig = go.Figure([go.Scatter(x=v.index, y=v.values, name=k) for k, v in curves.items()])
        fig.update_layout(title="資產曲線", yaxis_title="資產(元)", hovermode="x unified")
        fig.write_html(os.path.join(a.out, "equity.html"))
        print(f"\n圖表:{os.path.join(a.out, 'equity.html')}")
    except ImportError:
        print("(未安裝 plotly,略過圖表;資產曲線在 *_equity.csv)")
    print(f"明細輸出於資料夾:{a.out}")


if __name__ == "__main__":
    main()