"""
paper_manual.py — 手動操盤帳戶(自己每天決定買賣,最後跟電腦帳戶比賽)

和電腦帳戶用「同一套成交規則」,比賽才公平:
  · 收盤後下單,下一個交易日「開盤價」成交,買進加 0.1% 滑價、賣出減 0.1% 滑價
  · 手續費 0.1425%;賣出證交稅 0.3%(ETF 0.1%)
  · 開盤接近漲停(≥ 前收 ×1.095)買不到;現金不夠(連預定金額的一半都不到)就略過
  · 停損:買進時可自訂停損 %,盤中觸價就賣,成交取 min(停損價, 開盤價);0 = 不設停損
  · 現金股利由「還原收盤/收盤」比值反推,除息日入帳
  · 資料到期(進度 = 目標天數)就結案,持股以最後收盤價計價,不強制賣出
和電腦帳戶不同的地方:沒有最多檔數、沒有持有天數上限——這些由你自己決定。

兩種下單方式(各單獨標示,不會混在一起):
  🌙 隔日單   任何時間都能下。成交日 = 下單時間之後的下一個「開盤」:平日 09:00 前下單 → 當天開盤價;
             平日 09:00 之後(盤中、盤後)或假日下單 → 下一個交易日開盤價。
  ⚡ 盤中即時  只有平日 09:00–13:30 能用。以證交所即時報價(現價)加 0.1% 滑價「立刻」成交。
             下即時單時,之前排的隔日單若成交日已經到了(今天開盤就該成交的),會先以今天的實際開盤價補成交,順序才正確。
手動帳戶的日 K 要等收盤後(14:30 以後)才會處理,盤中下載到的「今天」那根未完成 K 棒會被丟掉,避免用到半天的資料。

存檔:<資料夾>/manual/<帳戶名>.json(純 JSON,不依賴 paper_live.py)。本檔不依賴 streamlit,可單獨測試。
本工具僅供資訊整理,不構成投資建議。
"""
import json
import os
import pickle
import re
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import paper_trade as pt

FEE, TAX, SLIP, TAX_ETF = pt.FEE, pt.TAX, pt.SLIP, pt.TAX_ETF
BENCH = "0050.TW"


# ------------------------------------------------------------------ 存檔
def mdir(base):
    return os.path.join(base, "manual")


def safe(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "manual"


def path(base, name):
    return os.path.join(mdir(base), safe(name) + ".json")


def list_manual(base):
    d, out = mdir(base), []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith(".json"):
                try:
                    j = json.load(open(os.path.join(d, f), encoding="utf-8"))
                    if j.get("kind") == "manual":
                        out.append(j["name"])
                except Exception:                                   # noqa: BLE001
                    pass
    return out


def load(base, name):
    return json.load(open(path(base, name), encoding="utf-8"))


def save(base, S):
    os.makedirs(mdir(base), exist_ok=True)
    p = path(base, S["name"])
    tmp = p + ".tmp"
    json.dump(S, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)
    os.replace(tmp, p)


def delete(base, name):
    """改名備份(.deleted-時間戳),不是真的刪除。回傳 (成功?, 說明)。"""
    p = path(base, name)
    if not os.path.exists(p):
        return False, "找不到存檔"
    q = f"{p}.deleted-{time.strftime('%Y%m%d_%H%M%S')}"
    os.replace(p, q)
    return True, f"已刪除(存檔改名為 {os.path.basename(q)},需要時可改回原檔名還原)"


# ------------------------------------------------------------------ 開戶與下單
def new_account(name, capital, days, bench, as_of=None, opponent=""):
    """bench = 0050 的 DataFrame(用它的最後一個交易日當起點,也當交易日曆)。as_of 只給假資料測試用。"""
    b = bench if as_of is None else bench.loc[:pd.Timestamp(as_of)]
    if b.empty:
        raise ValueError("起始日之前沒有 0050 資料")
    d0 = str(b.index[-1].date())
    return dict(kind="manual", name=name, capital=float(capital), target_days=int(days), day_no=0, finished=False,
                created=time.strftime("%Y-%m-%d %H:%M"), start_date=d0, last_date=d0, opponent=opponent,
                acct=dict(cash=float(capital), pos={}, orders=[], last_close={}, eq=[[d0, float(capital), 0.0, float(capital)]],
                          trades=[], log=[]),
                bench=[[d0, float(b["Adj Close"].iloc[-1])]])


def taipei_now():
    return datetime.utcnow() + timedelta(hours=8)


def session_open(now=None):
    """平日 09:00–13:30(台北時間)才是盤中。"""
    t = now or taipei_now()
    return t.weekday() < 5 and (9, 0) <= (t.hour, t.minute) <= (13, 30)


def last_session_date(now=None):
    """最近一個「應該已經有成交資料」的平日:平日 09:00 之後是今天,其餘往前找到平日(假日不判斷,休市日抓到舊資料時不會被當成新資料)。"""
    t = now or taipei_now()
    d = t.date()
    if not (t.weekday() < 5 and (t.hour, t.minute) >= (9, 0)):
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return str(d)


def order_boundary(now=None):
    """隔日單的「下單基準日」:成交日必須晚於這一天。平日 09:00 前下單 → 前一天(今天開盤就成交);其餘 → 今天(下一個交易日開盤成交)。"""
    t = now or taipei_now()
    d = t.date()
    return str(d - timedelta(days=1) if t.weekday() < 5 and (t.hour, t.minute) < (9, 0) else d)


def add_order(S, side, code, name, ticker, amount=None, sh=None, stop_pct=0.0, after=None):
    """加入(或覆蓋同一檔同方向的)隔日委託。買進用金額(元);賣出 sh=None 表示全部賣出。回傳說明文字。
    after = 下單基準日(成交日須晚於它;見 order_boundary),不給就用目前處理到的最後一個交易日。"""
    if S["finished"]:
        raise ValueError("這個帳戶已結案,不能再下單")
    A = S["acct"]
    A["orders"] = [o for o in A["orders"] if not (o["side"] == side and o["code"] == code)]
    if side == "buy":
        if not amount or amount <= 0:
            raise ValueError("買進金額必須大於 0")
        A["orders"].append(dict(side="buy", code=code, name=name, tk=ticker, amount=float(amount), stop_pct=float(stop_pct or 0),
                                after=after or S["last_date"]))
        return f"買進 {code} {name},約 {amount:,.0f} 元"
    if code not in A["pos"]:
        raise ValueError(f"目前沒有持有 {code}")
    have = A["pos"][code]["sh"]
    if sh is not None and not (0 < sh <= have):
        raise ValueError(f"賣出股數要在 1~{have:,} 之間")
    A["orders"].append(dict(side="sell", code=code, name=name, tk=ticker, sh=None if sh is None or sh >= have else int(sh),
                            after=after or S["last_date"]))
    return f"賣出 {code} {name} " + ("全部" if sh is None or sh >= have else f"{int(sh):,} 股")


def cancel_orders(S, idxs):
    keep = [o for i, o in enumerate(S["acct"]["orders"]) if i not in set(idxs)]
    S["acct"]["orders"] = keep


# ------------------------------------------------------------------ 逐日推進
def _arr(df):
    C = df.Close.values.astype(float)
    adj = df["Adj Close"] if "Adj Close" in df.columns else df.Close
    f = (adj / df.Close).values
    div = np.zeros(len(C))
    div[1:] = np.clip(C[:-1] * (1 - f[1:] / f[:-1]), 0, None)
    div[div < 1e-3] = 0
    return dict(idx=df.index, O=df.Open.values.astype(float), H=df.High.values.astype(float), L=df.Low.values.astype(float), C=C, div=div)


def advance(S, data, now=None):
    """把 last_date 之後、資料裡已有的交易日逐日處理掉(成交成交日已到的隔日單)。回傳處理了幾天。
    今天的日 K 要等 14:30 以後才處理(和 paper_live 的 CUT 一致)(盤中下載到的是未完成的 K 棒)。"""
    bench = data.get(BENCH)
    if bench is None or S["finished"]:
        return 0
    t = now or taipei_now()
    days = bench.index[bench.index > pd.Timestamp(S["last_date"])]
    if (t.hour, t.minute) < (14, 30):
        days = days[days < pd.Timestamp(t.date())]
    cache, n = {}, 0

    def row(tk, d):
        if tk not in data:
            return None
        if tk not in cache:
            cache[tk] = _arr(data[tk])
        a = cache[tk]
        i = int(a["idx"].get_indexer([d])[0])
        return (a, i) if i >= 0 else None

    for d in days:
        if S["finished"]:
            break
        _step(S, d, row, data)
        n += 1
    return n


def _sell(A, ds, code, sh, px, why, ev):
    pos = A["pos"]
    p = pos[code]
    sh = min(sh, p["sh"])
    tax = TAX_ETF if code.startswith("00") else TAX
    proceeds = sh * px * (1 - FEE - tax)
    basis = p["cost"] * sh / p["sh"]
    A["cash"] += proceeds
    A["trades"].append(dict(代碼=code, 名稱=p["name"], 買進日=p["d"], 賣出日=ds, 買價=round(p["ep"], 2), 賣價=round(float(px), 2), 股數=int(sh),
                            損益=round(float(proceeds - basis)), 報酬率=float(proceeds / basis - 1), 持有天=p["n"], 原因=why))
    ev.append(f"賣出 {code} {p['name']} {sh:,} 股 @ {px:.2f}({why},{(proceeds / basis - 1) * 100:+.1f}%)")
    p["sh"] -= sh
    p["cost"] -= basis
    if p["sh"] <= 0:
        pos.pop(code)


def _run_orders(S, ds, rowf, ev, orders, drop_missing=True):
    """成交一批委託(先賣後買)。rowf(ticker) → (價格陣列, 今天的索引) 或 None;成交價取 O[i](加減滑價),漲停判斷用 C[i-1]。
    回傳要留下的單:賣出單遇到當天沒交易就順延;drop_missing=False 時買進單也順延(盤中補成交用,報價抓不到不能當作取消)。"""
    A, pos, keep = S["acct"], S["acct"]["pos"], []
    for o in orders:
        if o["side"] != "sell" or o["code"] not in pos:
            continue
        r = rowf(o["tk"])
        if not r:
            keep.append(o)
            ev.append(f"賣出單 {o['code']} {o['name']}:今天沒有交易,順延")
            continue
        a, i = r
        _sell(A, ds, o["code"], pos[o["code"]]["sh"] if o["sh"] is None else o["sh"], float(a["O"][i]) * (1 - SLIP), "手動賣出", ev)
    for o in orders:
        if o["side"] != "buy":
            continue
        r = rowf(o["tk"])
        if not r or r[1] <= 0:
            if drop_missing:
                ev.append(f"略過 {o['code']} {o['name']}:今天沒有交易資料")
            else:
                keep.append(o)
            continue
        a, i = r
        op = float(a["O"][i])
        if op >= a["C"][i - 1] * 1.095:
            ev.append(f"略過 {o['code']} {o['name']}:價格接近漲停,買不到")
            continue
        budget = min(A["cash"], o["amount"])
        if budget < 0.5 * o["amount"]:
            ev.append(f"略過 {o['code']} {o['name']}:現金不足")
            continue
        px = op * (1 + SLIP)
        sh = int(budget // (px * (1 + FEE)))
        if sh < 1:
            ev.append(f"略過 {o['code']} {o['name']}:金額不夠買 1 股")
            continue
        cost = sh * px * (1 + FEE)
        A["cash"] -= cost
        stop = op * (1 - o["stop_pct"] / 100) if o["stop_pct"] > 0 else 0.0
        if o["code"] in pos:                                                # 加碼:成本合併、停損改用新單
            p = pos[o["code"]]
            p["ep"] = (p["ep"] * p["sh"] + px * sh) / (p["sh"] + sh)
            p["sh"] += sh
            p["cost"] += cost
            p["stop"] = stop or p["stop"]
        else:
            pos[o["code"]] = dict(sh=sh, ep=float(px), d=ds, n=1, stop=float(stop), cost=float(cost), tk=o["tk"], name=o["name"])
        ev.append(f"買進 {o['code']} {o['name']} {sh:,} 股 @ {px:.2f}(約 {cost:,.0f} 元" + (f",停損價 {stop:.2f}" if stop else ",未設停損") + ")")
    return keep


def _step(S, d, row, data):
    A, ds, ev, pos = S["acct"], str(d.date()), [], S["acct"]["pos"]
    for p in pos.values():
        p["n"] += 1
    for code, p in pos.items():                                             # 股息
        r = row(p["tk"], d)
        if r and r[0]["div"][r[1]] > 0:
            amt = p["sh"] * float(r[0]["div"][r[1]])
            A["cash"] += amt
            ev.append(f"股息 {code} {p['name']} +{amt:,.0f} 元")
    due = [o for o in A["orders"] if o.get("after", "") < ds]               # 成交日已到的單;今天才下的隔日單(after = 今天)留到下一個交易日
    wait = [o for o in A["orders"] if o.get("after", "") >= ds]
    A["orders"] = _run_orders(S, ds, lambda tk: row(tk, d), ev, due) + wait
    for code in list(pos):                                                  # 盤中停損(今天盤中新買的部位不查:最低價可能發生在買進之前)
        p = pos[code]
        r = row(p["tk"], d)
        if r and p["stop"] > 0 and p.get("intraday") != ds and r[0]["L"][r[1]] <= p["stop"]:
            _sell(A, ds, code, p["sh"], min(p["stop"], float(r[0]["O"][r[1]])) * (1 - SLIP), "停損", ev)
    for p in pos.values():
        p.pop("intraday", None)
    mv = 0.0
    for code, p in pos.items():
        r = row(p["tk"], d)
        if r:
            A["last_close"][code] = float(r[0]["C"][r[1]])
        mv += p["sh"] * A["last_close"].get(code, p["ep"])
    eq = A["cash"] + mv
    A["eq"].append([ds, float(eq), float(mv / eq), float(A["cash"])])
    A["log"].append(dict(date=ds, events=ev))
    b = data[BENCH]
    S["bench"].append([ds, float(b["Adj Close"].iloc[int(b.index.get_indexer([d])[0])])])
    S["day_no"] += 1
    S["last_date"] = ds
    if S["day_no"] >= S["target_days"]:
        S["finished"], S["finished_at"] = True, ds


# ------------------------------------------------------------------ 盤中即時成交
def _f(x):
    try:
        v = float(str(x).replace(",", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def parse_mis(q):
    """證交所 MIS 一筆報價 → dict(price, open, high, low, volume(股), prev(昨收), name, date, time)。還沒成交時 price 只是參考價、volume=0。"""
    price = _f(q.get("z"))
    if price is None:
        b, a = _f(str(q.get("b", "")).split("_")[0]), _f(str(q.get("a", "")).split("_")[0])
        price = (b + a) / 2 if b and a else _f(q.get("y"))
    if price is None:
        return None
    return dict(price=price, open=_f(q.get("o")) or price, high=_f(q.get("h")) or price, low=_f(q.get("l")) or price,
                volume=int((_f(q.get("v")) or 0) * 1000), prev=_f(q.get("y")), name=q.get("n", ""), date=q.get("d"),
                time=f"{q.get('d')} {q.get('t')}", source="證交所即時")


def fetch_quotes(codes, chunk=40):
    """批次抓證交所即時報價 {代碼: dict}。抓不到的代碼不會出現在結果裡(不退回有延遲的資料,以免用過期價格成交)。"""
    import requests
    out, codes = {}, sorted(set(codes))
    if not codes:
        return out
    try:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
        sess.get("https://mis.twse.com.tw/stock/index.jsp", timeout=8)
        for i in range(0, len(codes), chunk):
            ex = "|".join(f"{m}_{c}.tw" for c in codes[i:i + chunk] for m in ("tse", "otc"))
            try:
                r = sess.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp", params={"ex_ch": ex, "json": 1, "delay": 0, "_": int(time.time() * 1000)},
                             timeout=10).json()
            except Exception:                                               # noqa: BLE001
                continue
            for q in r.get("msgArray", []):
                if q.get("n") and q.get("c") and q["c"] not in out:
                    v = parse_mis(q)
                    if v:
                        out[q["c"]] = v
    except Exception:                                                       # noqa: BLE001
        pass
    return out


def _live_rowf(quotes, use):
    """把即時報價包成 _run_orders 要的格式。use='open' → 以今天實際開盤價成交(補成交隔日單);use='last' → 以現價成交(盤中即時單)。"""
    def f(tk):
        q = quotes.get(tk.split(".")[0])
        if not q or not q.get("volume"):
            return None
        p = float(q["price"])
        y = float(q.get("prev") or p)
        o = float(q.get("open") or p) if use == "open" else p
        return dict(O=np.array([y, o]), C=np.array([y, p]), L=np.array([y, float(q.get("low") or p)]), div=np.zeros(2)), 1
    return f


def live_codes(S, extra=()):
    """下即時單時需要哪些代碼的報價:這次要交易的 + 今天開盤就該成交的隔日單。"""
    ds = str(taipei_now().date())
    return sorted(set(extra) | {o["code"] for o in S["acct"]["orders"] if o.get("after", "") < ds})


def trade_now(S, quotes, side, code, name, ticker, amount=None, sh=None, stop_pct=0.0, now=None):
    """盤中立即成交(買用金額、賣 sh=None 為全部)。回傳說明文字;不能成交就丟 ValueError(狀態不會被改動到一半)。"""
    t = now or taipei_now()
    ds = str(t.date())
    if S["finished"]:
        raise ValueError("這個帳戶已結案,不能再下單")
    if not session_open(t):
        raise ValueError("現在不是盤中(平日 09:00–13:30),請改用隔日單")
    if ds <= S["last_date"]:
        raise ValueError(f"{ds} 的日 K 已經處理過,不能再盤中成交")
    q = quotes.get(code)
    if not q or not q.get("volume"):
        raise ValueError(f"{code} 沒有即時成交報價(可能還沒開盤成交、暫停交易或報價抓不到),沒有成交")
    A = S["acct"]
    snap = json.dumps(A, default=float)                                      # 失敗時整份還原
    ev, hhmm = [], t.strftime("%H:%M")
    try:
        due = [o for o in A["orders"] if o.get("after", "") < ds]            # 今天開盤就該成交的隔日單,先用實際開盤價補成交
        wait = [o for o in A["orders"] if o.get("after", "") >= ds]
        before = set(A["pos"])
        A["orders"] = _run_orders(S, ds, _live_rowf(quotes, "open"), ev, due, drop_missing=False) + wait
        for c in set(A["pos"]) - before:
            A["pos"][c]["n"] = 0                                             # 收盤處理時會 +1,維持跟隔日單一樣的持有天數
        n_settle = len(ev)
        if side == "sell":
            if code not in A["pos"]:
                raise ValueError(f"目前沒有持有 {code}")
            have = A["pos"][code]["sh"]
            if sh is not None and not (0 < sh <= have):
                raise ValueError(f"賣出股數要在 1~{have:,} 之間")
            o = dict(side="sell", code=code, name=name, tk=ticker, sh=None if sh is None or sh >= have else int(sh), after=ds)
        else:
            if not amount or amount <= 0:
                raise ValueError("買進金額必須大於 0")
            o = dict(side="buy", code=code, name=name, tk=ticker, amount=float(amount), stop_pct=float(stop_pct or 0), after=ds)
        before = set(A["pos"])
        keep = _run_orders(S, ds, _live_rowf(quotes, "last"), ev, [o], drop_missing=False)
        mine = ev[n_settle:]
        if keep or not mine or any(e.startswith(("略過", "賣出單")) for e in mine):
            raise ValueError(next((e for e in mine if e.startswith(("略過", "賣出單"))), "沒有成交"))
        for c in set(A["pos"]) - before:
            A["pos"][c]["n"] = 0
            A["pos"][c]["intraday"] = ds
    except Exception:
        S["acct"] = json.loads(snap)
        raise
    A = S["acct"]
    A["log"].append(dict(date=ds, live=True, events=[f"⚡{hhmm} " + ("補成交隔日單(開盤價) " if i < n_settle else "") + e for i, e in enumerate(ev)]))
    return "成交:" + "；".join(ev[n_settle:])


def live_equity(S, quotes):
    """盤中估值:現金 + 持股 × 即時價(沒有報價的用最近收盤價)。"""
    A = S["acct"]
    return A["cash"] + sum(p["sh"] * float((quotes.get(c) or {}).get("price") or A["last_close"].get(c, p["ep"])) for c, p in A["pos"].items())


# ------------------------------------------------------------------ 成績
def eq_series(eq):
    """[[日期, 總資產, 持股水位, 現金], ...] → DataFrame(index=日期)"""
    e = pd.DataFrame(eq, columns=["date", "equity", "exposure", "cash"])
    e["date"] = pd.to_datetime(e["date"])
    return e.set_index("date")


def stats(eq, trades, capital):
    e = eq_series(eq)
    r = {"總資產": float(e.equity.iloc[-1]), "報酬%": (e.equity.iloc[-1] / capital - 1) * 100,
         "最大回撤%": float((e.equity / e.equity.cummax() - 1).min() * 100), "平均持股水位%": float(e.exposure.mean() * 100),
         "已了結交易": len(trades)}
    r["勝率%"] = float(np.mean([t["報酬率"] > 0 for t in trades]) * 100) if trades else None
    return r


def stock_brief(df):
    """選股參考:最新價、漲跌、近 5/20 日報酬、近 20 日均成交值(百萬)。"""
    c = df.Close
    ch = lambda n: (c.iloc[-1] / c.iloc[-1 - n] - 1) * 100 if len(c) > n else None    # noqa: E731
    return dict(date=str(df.index[-1].date()), close=float(c.iloc[-1]), d1=ch(1), d5=ch(5), d20=ch(20),
                amt20=float((df.Close * df.Volume).tail(20).mean() / 1e6))


# ------------------------------------------------------------------ 只更新少數幾檔的股價快取(合併,不會覆蓋整個快取)
def merge_prices(old, new):
    """把新下載的最近幾個月併進舊資料。新舊 Adj Close 的還原基準可能不同,用重疊日的比值把舊的調整過去。"""
    if old is None or old.empty:
        return new
    old = old.copy()
    ov = old.index.intersection(new.index)
    if len(ov):
        f = new.loc[ov[-1], "Adj Close"] / old.loc[ov[-1], "Adj Close"]
        if np.isfinite(f) and f > 0:
            old["Adj Close"] = old["Adj Close"] * f
    return pd.concat([old[old.index < new.index[0]], new]).sort_index()


def drop_incomplete(df, now=None):
    """14:30 以前,今天那根日 K 還沒收完(或剛收完還沒定案),丟掉。"""
    t = now or taipei_now()
    if len(df) and (t.hour, t.minute) < (14, 30):
        df = df[df.index < pd.Timestamp(t.date())]
    return df


def refresh_tickers(cache, tickers, period="3mo"):
    """只更新指定幾檔(持股、委託、0050)並合併進快取檔。回傳成功更新的檔數。"""
    import yfinance as yf
    data = pickle.load(open(cache, "rb")) if os.path.exists(cache) else {}
    tickers = sorted(set(tickers))
    raw = yf.download(tickers, period=period, interval="1d", auto_adjust=False, group_by="ticker", threads=True, progress=False)
    n = 0
    for t in tickers:
        try:
            new = pt._clean(raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw)
        except Exception:                                                   # noqa: BLE001
            continue
        new = drop_incomplete(new)
        if len(new):
            data[t] = merge_prices(data.get(t), new)
            n += 1
    if n:
        tmp = cache + ".tmp"
        pickle.dump(data, open(tmp, "wb"))
        os.replace(tmp, cache)
    return n


def needed_tickers(S):
    A = S["acct"]
    return {BENCH} | {p["tk"] for p in A["pos"].values()} | {o["tk"] for o in A["orders"]}
