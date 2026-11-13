#!/usr/bin/env python3
"""
paper_live.py — 台股「前向」模擬帳戶:從今天開始,每天自動看股價與訊號、模擬買賣,N 個交易日後檢視帳戶

需與 paper_trade.py、app1.py 放在同一資料夾。

  # 1) 開戶(建議收盤後 14:30 以後執行)。本金 100 萬、模擬 20 個交易日、穩健型
  python paper_live.py init --profile 穩健型 --capital 1000000 --days 20

  # 2) 之後每個交易日收盤後執行一次(漏跑也沒關係,下次會自動補上漏掉的交易日)
  python paper_live.py run

  # 3) 隨時查看帳戶(不連網)
  python paper_live.py status

可同時開三個帳戶比較:  init --profile 保守型 / 穩健型 / 積極型 ;  run 不加 --name 會一次更新全部帳戶。
自動化:Windows「工作排程器」或 Linux cron,平日 14:40 執行  python paper_live.py run

運作方式
  - 起始日 = 最近一個「已收盤」的交易日;以該日收盤後的訊號決定隔天要買什麼,隔天開盤成交。
  - 之後每個交易日:開盤執行昨天收盤後決定的買賣 → 盤中檢查停損 → 到期賣出 → 收盤計價 → 產生明天的買賣計畫。
  - 第 N 個交易日收盤後帳戶結案,顯示帳面總資產,以及「若全部賣出扣完成本」的估計值。
  - 規則與參數在開戶時存進帳戶,之後改程式內的預設值不影響已開的帳戶。

兩種下單時機(每個帳戶各自設定,開戶時 --mode,或在儀表板切換):
  after(預設,盤後)  收盤後算訊號,隔天開盤價成交。
  intraday(盤中)    平日 09:00–13:30 執行  python paper_live.py intraday  (可先加 --dry-run 只預覽):
                    抓證交所即時報價,把「今天這根還沒收的 K 棒」併進歷史,重算訊號,符合買進/賣出就用現價立刻成交。
                    那天有執行過 → 收盤後 run 只做計價、停損、到期,不再產生隔日單。
                    那天沒執行(電腦關著、忘了按)→ 後備:收盤後 run 照盤後模式產生隔日單,隔天開盤成交,所以不會整天空白;
                    但那一天「盤中」本身無法補做(不能回到過去用當時的即時價)。
                    今天的量用「目前成交量 ÷ 到這個時間通常已成交的比例」推估(見 VOL_CURVE,是粗估),放量類訊號(B4、S5)在開盤初期誤差較大。
本工具僅供資訊整理,不構成投資建議。
"""
import argparse
import copy
import json
import os
import pickle
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import paper_manual as pm          # 即時報價與盤中時間判斷
import paper_trade as pt

TW = timezone(timedelta(hours=8))
CUT = (14, 30)          # 台北時間 14:30 後才把「今天」的日K視為已收盤
SIGN = {"B4": "放量突破", "B5": "RSI/MACD/均線同步轉強", "B6a": "跌深反彈(較弱)", "B6b": "趨勢反轉", "B7": "站上5/10/20日線翻多",
        "S1": "跌破前10/20日低", "S2b": "跌破20日線", "S5": "破底放量", "S7": "跌破5/10/20/60日線", "S7L": "長線翻空"}


def now_tw():
    return datetime.now(TW)


def cutoff_date(asof=None):
    if asof:
        return pd.Timestamp(asof)
    n = now_tw()
    d = pd.Timestamp(n.date())
    return d if (n.hour, n.minute) >= CUT else d - pd.Timedelta(days=1)


# ------------------------------------------------------------------ 資料更新(增量:只補最近一個月,舊資料保留)
def fetch(tickers, period, batch=40):
    import yfinance as yf
    out = {}
    for b in range(0, len(tickers), batch):
        part = tickers[b:b + batch]
        try:
            raw = yf.download(part, period=period, interval="1d", auto_adjust=False,
                              group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print("  下載失敗:", e)
            continue
        for t in part:
            try:
                h = pt._clean(raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw)
            except Exception:
                continue
            if len(h):
                out[t] = h
        if len(tickers) > batch:
            print(f"  下載 {min(b + batch, len(tickers))}/{len(tickers)}", flush=True)
    return out


def _merge(old, new):
    """把新下載的近一個月接到舊資料後面。Yahoo 的還原價會在除息後整段重算,所以要用重疊日把舊還原價對齊;
    若重疊日的收盤價對不上(可能有分割),回傳 None 表示該檔要整段重抓。"""
    ov = new.index.intersection(old.index)
    if not len(ov):
        return None
    t0 = ov[0]
    if abs(new.Close[t0] / old.Close[t0] - 1) > 0.002:
        return None
    o2 = old[old.index < t0].copy()
    o2["Adj Close"] = o2["Adj Close"] * (new.loc[t0, "Adj Close"] / old.loc[t0, "Adj Close"])
    return pd.concat([o2, new[~new.index.duplicated()]])


def refresh(base, tickers, asof=None, synthetic=False):
    if synthetic:                                                           # 離線自我測試用
        _, data = pt.synthetic()
        data["0050.TW"] = next(iter(data.values())).copy()
        return {t: (d.loc[:asof] if asof else d) for t, d in data.items()}
    os.makedirs(base, exist_ok=True)
    cache = os.path.join(base, "prices.pkl")
    data = pickle.load(open(cache, "rb")) if os.path.exists(cache) else {}
    today = pd.Timestamp(now_tw().date())
    full = [t for t in tickers if t not in data or len(data[t]) < 300 or data[t].index[-1] < today - pd.Timedelta(days=20)]
    tail = [t for t in tickers if t not in full]
    if full:
        print(f"完整下載 {len(full)} 檔歷史資料(第一次較久,之後只會補最近一個月)…")
        data.update({t: h for t, h in fetch(full, "max").items() if len(h) >= 300})
    if tail:
        print(f"更新 {len(tail)} 檔最近行情…")
        redo = []
        for t, new in fetch(tail, "1mo").items():
            m = _merge(data[t], new)
            if m is None:
                redo.append(t)
            else:
                data[t] = m
        if redo:
            print(f"  {len(redo)} 檔需重新完整下載(可能有分割或資料不連續)")
            data.update({t: h for t, h in fetch(redo, "max").items() if len(h) >= 300})
    pickle.dump(data, open(cache, "wb"))
    return data


# ------------------------------------------------------------------ 帳戶存取
def _jd(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (set, tuple)):
        return sorted(o)
    return str(o)


def spath(base, name):
    return os.path.join(base, name, "state.json")


def load_state(base, name):
    s = json.load(open(spath(base, name), encoding="utf-8"))
    for k in ("buy", "block", "exit"):
        s["params"][k] = set(s["params"][k])
    return s


def save_state(base, name, s):
    os.makedirs(os.path.join(base, name), exist_ok=True)
    tmp = spath(base, name) + ".tmp"
    json.dump(s, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=_jd)
    os.replace(tmp, spath(base, name))


def list_accounts(base):
    return sorted(d for d in os.listdir(base) if os.path.exists(spath(base, d))) if os.path.isdir(base) else []


def export(base, name, s):
    d = os.path.join(base, name)
    a = s["acct"]
    pd.DataFrame(a["eq"], columns=["日期", "總資產", "持股水位", "現金"]).to_csv(os.path.join(d, "equity.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(a["trades"]).to_csv(os.path.join(d, "trades.csv"), index=False, encoding="utf-8-sig")
    with open(os.path.join(d, "log.txt"), "w", encoding="utf-8") as f:
        for L in a["log"]:
            f.write(L["date"] + "\n" + "".join(f"  {e}\n" for e in L["events"]) + ("  (無動作)\n" if not L["events"] else ""))


# ------------------------------------------------------------------ 報表
def liquidation_value(a):
    mv = sum(p["sh"] * a["last_close"].get(c, p["ep"]) for c, p in a["pos"].items())
    core = a.get("core") or {}
    cmv = core.get("sh", 0) * a["last_close"].get(core.get("code", "0050"), core["cost"] / core["sh"] if core.get("sh") else 0)
    return a["cash"] + mv * (1 - pt.FEE - pt.TAX - pt.SLIP) + cmv * (1 - pt.FEE - pt.TAX_ETF - pt.SLIP), mv + cmv


def report(s, new_days=None):
    a, name = s["acct"], s["name"]
    names = {r["stock_id"]: r["stock_name"] for r in s["universe"]}
    last = a["eq"][-1] if a["eq"] else None
    st = "已結案" if s["finished"] else "進行中"
    print("\n" + "=" * 66)
    print(f"帳戶「{name}」  {s['profile']}  {st}   起始 {s['start_date']}  本金 {s['capital']:,.0f}   下單時機:{'盤中' if s.get('mode') == 'intraday' else '盤後'}")
    if not last:
        print("尚未處理任何交易日。")
        return
    print(f"最新處理日 {last[0]}   第 {max(s['day_no'], 0)}/{s['target_days']} 個交易日")
    shown = new_days if new_days is not None else ([(a["log"][-1]["date"], a["log"][-1]["events"])] if a["log"] else [])
    if shown:
        print("\n【動作紀錄】")
        for d, ev in shown:
            print(f"  {d}")
            for e in ev:
                print(f"     {e}")
            if not ev:
                print("     (無動作)")
    if a["pos"]:
        print("\n【目前持股】")
        print(f"  {'代碼':<6}{'名稱':<10}{'股數':>8}{'成本':>9}{'現價':>9}{'損益%':>8}{'市值':>11}{'持有日':>6}{'停損價':>9}")
        for c, p in a["pos"].items():
            px = a["last_close"].get(c, p["ep"])
            print(f"  {c:<6}{names.get(c, ''):<10}{p['sh']:>8,}{p['ep']:>9.2f}{px:>9.2f}{(px / p['ep'] - 1) * 100:>+8.1f}{p['sh'] * px:>11,.0f}{p['n']:>6}{p['stop']:>9.2f}")
    else:
        print("\n【目前持股】空手")
    core = a.get("core") or {}
    if core.get("sh", 0) > 0:
        cc = s["params"].get("core_code", "0050")
        px = a["last_close"].get(cc, core["cost"] / core["sh"])
        print(f"【核心 ETF】{cc} {core['sh']:,} 股  成本 {core['cost'] / core['sh']:.2f}  現價 {px:.2f}  市值 {core['sh'] * px:,.0f}  已實現 {core.get('realized', 0):+,.0f}")
    eq = last[1]
    print(f"\n【帳戶】總資產 {eq:,.0f}   損益 {eq - s['capital']:+,.0f}({(eq / s['capital'] - 1) * 100:+.2f}%)   現金 {last[3]:,.0f}   持股水位 {last[2] * 100:.0f}%")
    if len(s["bench"]) > 1:
        b = s["bench"][-1][1] / s["bench"][0][1] - 1
        print(f"        同期 0050 買進持有:{b * 100:+.2f}%")
    liq, _ = liquidation_value(a)
    if s["finished"]:
        print(f"\n【結案】帳面總資產 {eq:,.0f}")
        print(f"        若期末全部賣出(扣手續費、證交稅、滑價)約 {liq:,.0f}({(liq / s['capital'] - 1) * 100:+.2f}%)")
    else:
        if a["pend_sell"] or a["pend_buy"]:
            print("\n【明日計畫(下一個交易日開盤執行)】")
            for c in a["pend_sell"]:
                print(f"  賣出 {c} {names.get(c, '')}:{'、'.join(SIGN.get(x, x) for x in a['sell_why'].get(c, []))}")
            for c in a["pend_buy"]:
                i = a["pend_info"].get(c, {})
                print(f"  買進 {c} {names.get(c, '')}:{'、'.join(SIGN.get(x, x) for x in i.get('sigs', []))}"
                      f"(情境勝率下限 {i.get('wl', 0):.0f}%、優勢 {i.get('edge', 0):+.0f}%)")
        else:
            print("\n【明日計畫】無買賣")
        left = s["target_days"] - s["day_no"]
        print(f"\n距離結案還有 {left} 個交易日。")
    print("=" * 66)


# ------------------------------------------------------------------ 指令
def build_params(a):
    """回傳 (參數, 來源標籤)。有 --params 檔就用檔案(最佳化參數),否則用風險型預設。"""
    label = a.profile
    pf = getattr(a, "params", "") or ""
    if pf:
        label, P = pt.load_params(pf)
    else:
        P = {k: (set(v) if isinstance(v, set) else v) for k, v in pt.PROFILES[a.profile].items()}
    for key, val in (("wl", a.wl), ("edge", a.edge), ("min_eff", a.min_eff), ("stop", a.stop), ("max_pos", a.max_pos), ("pct", a.pct), ("hold", a.hold)):
        if val is not None:
            P[key] = val
    return P, label


def cmd_init(a):
    name = a.name or a.profile
    if os.path.exists(spath(a.dir, name)) and not a.force:
        sys.exit(f"帳戶「{name}」已存在。要重開請加 --force(會覆蓋),或用 --name 取別的名字。")
    if a.profile not in pt.PROFILES:
        sys.exit("--profile 請填:保守型 / 穩健型 / 積極型")
    if a.synthetic:
        uni, _ = pt.synthetic()
    else:
        uni = pt.load_universe()
        if a.codes:
            uni = uni[uni.stock_id.isin([c.strip() for c in a.codes.split(",")])]
        uni = pt.with_all_etfs(uni, a.dir)                                  # 全部 ETF 都能當交易標的與核心部位(不佔 --max-stocks 名額,開戶時再依流動性篩)
    print(f"股票池 {len(uni)} 檔(含 {int((uni.industry_category == 'ETF').sum())} 檔 ETF),準備資料…")
    data = refresh(a.dir, list(uni.ticker) + ["0050.TW"], a.asof, a.synthetic)
    bench = data.get("0050.TW")
    if bench is None:
        sys.exit("抓不到 0050 作為交易日曆,請檢查網路")
    cal = bench.index[bench.index <= cutoff_date(a.asof)]
    if len(cal) < 2:
        sys.exit("資料不足")
    S, prev = cal[-1], cal[-2]
    rows = {r["ticker"]: r for r in uni.to_dict("records")}
    score = {}
    for t, df in data.items():
        if t in rows and len(df.loc[:S]) >= 300:
            d = df.loc[:S].tail(250)
            score[t] = float((d.Close * d.Volume).mean())
    ranked = sorted(score, key=score.get, reverse=True)
    P, label = build_params(a)
    is_etf = lambda t: rows[t].get("industry_category") == "ETF"        # noqa: E731
    must = {"0050", P.get("core_code", "0050")}                         # 核心 ETF 不管流動性都要在股票池裡
    etfs = [t for t in ranked if is_etf(t) and (score[t] >= pt.sr.MIN_AMT or rows[t]["stock_id"] in must)]   # ETF 只收近一年日均成交值 ≥ 下單門檻的
    keep = [t for t in ranked if not is_etf(t)][:a.max_stocks] + etfs
    print(f"納入 {len(keep) - len(etfs)} 檔個股 + {len(etfs)} 檔 ETF(流動性夠的)")
    s = dict(name=name, profile=label, capital=float(a.capital), target_days=int(a.days), start_date=str(S.date()),
             created=now_tw().strftime("%Y-%m-%d %H:%M"), last_date=str(prev.date()), day_no=-1, finished=False,
             params=P, universe=[rows[t] for t in keep], acct=pt.new_state(a.capital), bench=[], mode=getattr(a, "mode", "after") or "after")
    save_state(a.dir, name, s)
    print(f"已開戶「{name}」:{a.profile},本金 {a.capital:,.0f},模擬 {a.days} 個交易日,起始日 {S.date()},納入 {len(keep)} 檔。")
    print("以起始日收盤後的訊號決定第一批買進,下一個交易日開盤成交。")
    run_accounts(a, [name], preloaded=data)


def _catch_up(a, s, data, ns, prepared, cut):
    """處理這個帳戶所有「已收盤、還沒處理」的交易日,回傳 [(日期, 動作)]。逐檔訊號只在真的有新交易日時才準備(盤中檢查通常沒有,所以很快)。"""
    bench = data["0050.TW"]
    days = [x for x in bench.index if pd.Timestamp(s["last_date"]) < x <= cut]
    if s["finished"] or not days:
        return []
    P, stocks, new_days, intraday_mode = s["params"], {}, [], s.get("mode") == "intraday"
    for r in s["universe"]:
        t = r["ticker"]
        if t not in data or len(data[t]) < 300:
            continue
        if t not in prepared:
            prepared[t] = pt.prepare(r, data[t], ns)
        stocks[r["stock_id"]] = prepared[t]
    for d in days:
        if s["finished"]:
            break
        s["day_no"] += 1
        day_i = {c: int(i) for c, sk in stocks.items() for i in [sk["idx"].get_indexer([d])[0]] if i >= 0}
        final = s["day_no"] >= s["target_days"]
        # 盤中模式:那天有執行過盤中判斷 → 收盤不產生隔日單;那天沒執行(電腦關著、忘了按)→ 後備,照盤後模式產生隔日單,隔天開盤成交
        ran = (s["acct"].get("intraday") or {}).get("date") == str(d.date())
        ev = pt.step_day(P, s["acct"], stocks, d, day_i, final, intraday=intraday_mode and ran)
        s["last_date"] = str(d.date())
        s["bench"].append([str(d.date()), float(bench.loc[d, "Adj Close"])])
        s["finished"] = bool(final)
        new_days.append((str(d.date()), ev))
    return new_days


def run_accounts(a, names, preloaded=None):
    states = {n: load_state(a.dir, n) for n in names}
    todo = {n: s for n, s in states.items() if not s["finished"]}
    for n, s in states.items():
        if s["finished"]:
            report(s)
    if not todo:
        return
    if preloaded is None:
        tickers = sorted({r["ticker"] for s in todo.values() for r in s["universe"]} | {"0050.TW"})
        preloaded = refresh(a.dir, tickers, a.asof, a.synthetic)
    data = preloaded
    cut = cutoff_date(a.asof)
    ns = pt.load_app_funcs(a.app)
    prepared = {}
    for n, s in todo.items():
        new_days = _catch_up(a, s, data, ns, prepared, cut)
        save_state(a.dir, n, s)
        export(a.dir, n, s)
        if not new_days:
            print(f"\n帳戶「{n}」沒有新的已收盤交易日(最新處理日 {s['last_date']}),可能今天尚未收盤或休市。")
        report(s, new_days if new_days else None)


# ------------------------------------------------------------------ 盤中:用即時報價判斷並立刻成交
VOL_CURVE = [(0, 0.0), (30, 0.22), (60, 0.35), (120, 0.52), (180, 0.68), (240, 0.88), (265, 0.96), (270, 1.0)]   # (開盤後幾分鐘, 累計成交量比例):粗估,可自行調整


def vol_frac(now):
    """到這個時間為止,通常已經成交了全天量的幾成(線性內插 VOL_CURVE)。下限 0.1,避免開盤頭幾分鐘把量放大太多倍。"""
    m = (now.hour - 9) * 60 + now.minute
    for (m0, f0), (m1, f1) in zip(VOL_CURVE, VOL_CURVE[1:]):
        if m <= m1:
            return max(f0 + (f1 - f0) * (max(m, m0) - m0) / (m1 - m0), 0.1)
    return 1.0


def _with_today(df, q, today, frac):
    """歷史日 K + 今天這根「還沒收完」的 K 棒(開高低用即時值、收盤 = 現價、量 = 目前量 ÷ 已成交比例)。"""
    base = df[df.index < today]
    price = float(q["price"])
    ratio = float(base["Adj Close"].iloc[-1] / base["Close"].iloc[-1])
    row = {c: 0.0 for c in base.columns}
    row.update(Open=float(q["open"]), High=max(float(q["high"]), price), Low=min(float(q["low"]), price), Close=price,
               **{"Adj Close": price * ratio}, Volume=float(q["volume"]) / frac)
    return pd.concat([base, pd.DataFrame([row], index=[today])[list(base.columns)]])


def _cash_for_buy(st, P, need, core_q, today_s, ev):
    """現金不夠買個股時,先賣一部分核心 ETF(和 step_day 的規則一致)。"""
    core = st.get("core") or {}
    cc = P.get("core_code", "0050")
    if core.get("sh", 0) <= 0 or st["cash"] >= need or not core_q or not core_q.get("volume"):
        return
    px = float(core_q["price"]) * (1 - pt.SLIP)
    sh = min(core["sh"], int(np.ceil((need - st["cash"]) / (px * (1 - pt.FEE - pt.TAX_ETF)))))
    proceeds = sh * px * (1 - pt.FEE - pt.TAX_ETF)
    basis = core["cost"] * sh / core["sh"]
    st["cash"] += proceeds
    core["sh"] -= sh
    core["cost"] -= basis
    core["realized"] = core.get("realized", 0.0) + proceeds - basis
    ev.append(f"核心 賣出 {cc} {sh:,} 股 @ {px:.2f}(為個股買進備資金,{(proceeds / basis - 1) * 100:+.1f}%)")


def run_intraday(a, name, execute=True, now=None, data=None, quotes=None, ns=None, prepared=None, prov_cache=None):
    """盤中判斷並成交一個帳戶。execute=False 只預覽(不存檔)。回傳 dict(ok, msg, events, buys, sells)。
    data / quotes / now 可由外部傳入(測試或已經抓好時);ns / prepared / prov_cache 讓多個帳戶共用同一份計算(見 run_intraday_all)。"""
    now = now or pm.taipei_now()
    out = dict(ok=False, msg="", events=[], buys=[], sells=[], skipped=[])
    s0 = load_state(a.dir, name)
    if s0["finished"]:
        out["msg"] = "這個帳戶已結案"
        return out
    if not pm.session_open(now):
        out["msg"] = "現在不是盤中(平日 09:00–13:30)"
        return out
    today = pd.Timestamp(now.date())
    today_s = str(today.date())
    s = copy.deepcopy(s0)                                                      # 預覽或失敗都不會動到原本的帳戶
    P, st = s["params"], s["acct"]
    codes = sorted({r["stock_id"] for r in s["universe"]} | {"0050"})
    quotes = quotes if quotes is not None else pm.fetch_quotes(codes)
    if not quotes.get("0050"):
        out["msg"] = "抓不到即時報價(證交所 MIS 連不上或今天休市)"
        return out
    tickers = sorted({r["ticker"] for r in s["universe"]} | {"0050.TW"})
    if data is None:                                                           # 快取夠新就不再下載;用 0050 的昨收對一下就知道有沒有缺昨天的資料
        cache = os.path.join(a.dir, "prices.pkl")
        data = pickle.load(open(cache, "rb")) if os.path.exists(cache) else {}
        ref = data["0050.TW"][data["0050.TW"].index < today] if "0050.TW" in data else None
        prev = quotes["0050"].get("prev")
        if ref is None or not len(ref) or prev is None or abs(float(ref.Close.iloc[-1]) - prev) > 0.015:
            data = refresh(a.dir, tickers, a.asof, a.synthetic)
    ns = ns or pt.load_app_funcs(a.app)
    new_days = _catch_up(a, s, data, ns, {} if prepared is None else prepared, today - pd.Timedelta(days=1))
    if s["last_date"] >= today_s:
        out["msg"] = f"{today_s} 的日 K 已經處理過了"
        return out
    frac = vol_frac(now)
    stocks, cur = {}, {}
    for r in s["universe"]:
        q, df = quotes.get(r["stock_id"]), data.get(r["ticker"])
        if q and q.get("volume") and df is not None and len(df[df.index < today]) >= 300:
            k = r["stock_id"]
            if prov_cache is not None and k in prov_cache:                      # 別的帳戶已經算過同一檔,直接用
                stocks[k] = prov_cache[k]
            else:
                stocks[k] = pt.prepare(r, _with_today(df, q, today, frac), ns)
                if prov_cache is not None:
                    prov_cache[k] = stocks[k]
            cur[k] = q
    g = st.setdefault("intraday", {})
    if g.get("date") != today_s:
        g.clear()
        g.update(date=today_s, sold=[], bought=[])
    pos, ev, hhmm = st["pos"], [], now.strftime("%H:%M")
    if st["pend_buy"] or st["pend_sell"]:                                      # 昨天沒執行盤中而產生的後備隔日單:改由今天的盤中判斷取代,避免收盤時又成交一次
        ev.append(f"昨天沒有執行盤中判斷,收盤時產生的隔日單({len(st['pend_buy'])} 買、{len(st['pend_sell'])} 賣)改由今天的盤中判斷取代")
        st["pend_buy"], st["pend_sell"], st["pend_info"], st["sell_why"] = [], [], {}, {}
    core_code = P.get("core_code", "0050")
    core_on = float(P.get("core_pct", 0) or 0) > 0 and core_code in stocks

    def close(code, px, why):
        p = pos.pop(code)
        px = float(px) * (1 - pt.SLIP)
        proceeds = p["sh"] * px * (1 - pt.FEE - (pt.TAX_ETF if stocks[code]["ind"] == "ETF" else pt.TAX))
        st["cash"] += proceeds
        r = proceeds / p["cost"] - 1
        st["trades"].append(dict(代碼=code, 名稱=stocks[code]["name"], 買進日=p["d"], 賣出日=today_s, 買價=round(float(p["ep"]), 2), 賣價=round(px, 2),
                                 股數=p["sh"], 損益=round(float(proceeds - p["cost"])), 報酬率=float(r),
                                 持有天=p["n"] + (0 if p.get("intraday") == today_s else 1), 原因=why + "(盤中)"))
        g["sold"].append(code)
        ev.append(f"賣出 {code} {stocks[code]['name']} {p['sh']:,} 股 @ {px:.2f}({why},{r * 100:+.1f}%)")
        out["sells"].append(code)

    for code in list(pos):                                                     # 先處理賣出:停損 → 到期 → 賣出訊號
        p, sk, q = pos[code], stocks.get(code), cur.get(code)
        if not sk or not q:
            continue
        i, price = len(sk["C"]) - 1, float(q["price"])
        if p["stop"] > 0 and p.get("intraday") != today_s and float(q["low"]) <= p["stop"]:
            close(code, min(p["stop"], price), "停損")
        elif p["n"] + 1 >= P["hold"] and p.get("intraday") != today_s:
            close(code, price, "持有到期")
        else:
            hit = pt.sr.exit_hits(P, sk, i)
            if hit:
                close(code, price, "賣出訊號 " + "/".join(hit))
    slots = P["max_pos"] - len(pos)
    cands = []
    if slots > 0:
        for code, sk in stocks.items():
            if code in pos or code in g["sold"] or (core_on and code == core_code):
                continue
            r = pt.sr.entry_candidate(P, sk, len(sk["C"]) - 1)
            if r:
                cands.append((r[0], r[1], code, r[2]))
        cands.sort(key=lambda t: (-t[0], -t[1]))
        held = Counter(stocks[c]["ind"] for c in pos)
        for wl, edge, code, hit in cands:
            if len(out["buys"]) >= slots:
                break
            sk, q = stocks[code], cur[code]
            ind = sk["ind"]
            if ind and held[ind] >= P["ind_max"]:
                continue
            price = float(q["price"])
            if q.get("prev") and price >= float(q["prev"]) * 1.095:
                out["skipped"].append(f"{code} {sk['name']}:接近漲停,買不到")
                continue
            tgt = st["prev_eq"] * P["pct"]
            _cash_for_buy(st, P, tgt, cur.get(core_code), today_s, ev)
            budget = min(st["cash"], tgt)
            if budget < 0.5 * tgt:
                out["skipped"].append(f"{code} {sk['name']}:現金不足")
                continue
            px = price * (1 + pt.SLIP)
            sh = int(budget // (px * (1 + pt.FEE)))
            if sh < 1:
                continue
            cost = sh * px * (1 + pt.FEE)
            st["cash"] -= cost
            pos[code] = dict(sh=sh, ep=float(px), d=today_s, n=0, stop=float(price * (1 - P["stop"])), cost=float(cost), intraday=today_s)
            g["bought"].append(code)
            held[ind] += 1
            out["buys"].append(code)
            ev.append(f"買進 {code} {sk['name']} {sh:,} 股 @ {px:.2f}(約 {cost:,.0f} 元,停損價 {price * (1 - P['stop']):.2f};"
                      f"{'、'.join(SIGN.get(x, x) for x in hit)},情境勝率下限 {wl:.0f}%、優勢 {edge:+.0f}%)")
    ev = [f"⚡{hhmm} " + e for e in ev]
    if frac < 0.2:
        ev.append(f"⚠️ 開盤才 {(now.hour - 9) * 60 + now.minute} 分鐘,今天的量是推估的(誤差大),放量類訊號請保守看待")
    out.update(ok=True, events=ev, skipped=out["skipped"], msg=("已成交" if execute else "預覽(沒有成交)") + f":買 {len(out['buys'])} 檔、賣 {len(out['sells'])} 檔",
               n_stocks=len(stocks), frac=frac)
    if execute:
        if ev:
            st["log"].append(dict(date=today_s, live=True, events=ev))
        save_state(a.dir, name, s)
        export(a.dir, name, s)
    return out


def intraday_accounts(base):
    """所有「盤中」模式、還沒結案的帳戶名稱。"""
    out = []
    for n in list_accounts(base):
        s = load_state(base, n)
        if s.get("mode") == "intraday" and not s["finished"]:
            out.append(n)
    return out


def run_intraday_all(a, names=None, execute=True, now=None):
    """一次對多個帳戶做盤中判斷與成交(預設 = 全部「盤中」模式的帳戶)。
    報價只抓一次、價格快取只檢查/更新一次、同一檔股票的盤中訊號只算一次,各帳戶共用;單一帳戶出錯不影響其他帳戶。
    回傳 {帳戶名: run_intraday 的結果 dict}。"""
    now = now or pm.taipei_now()
    names = list(names) if names is not None else intraday_accounts(a.dir)
    fail = lambda msg: dict(ok=False, msg=msg, events=[], buys=[], sells=[], skipped=[])        # noqa: E731
    if not names:
        return {}
    if not pm.session_open(now):
        return {n: fail("現在不是盤中(平日 09:00–13:30)") for n in names}
    states = {n: load_state(a.dir, n) for n in names}
    codes = sorted({r["stock_id"] for s in states.values() for r in s["universe"]} | {"0050"})
    quotes = pm.fetch_quotes(codes)
    if not quotes.get("0050"):
        return {n: fail("抓不到即時報價(證交所 MIS 連不上或今天休市)") for n in names}
    tickers = sorted({r["ticker"] for s in states.values() for r in s["universe"]} | {"0050.TW"})
    cache = os.path.join(a.dir, "prices.pkl")
    data = pickle.load(open(cache, "rb")) if os.path.exists(cache) else {}
    ref = data["0050.TW"][data["0050.TW"].index < pd.Timestamp(now.date())] if "0050.TW" in data else None
    prev = quotes["0050"].get("prev")
    if ref is None or not len(ref) or prev is None or abs(float(ref.Close.iloc[-1]) - prev) > 0.015:
        data = refresh(a.dir, tickers, a.asof, a.synthetic)
    ns, prepared, prov, out = pt.load_app_funcs(a.app), {}, {}, {}
    for n in names:
        try:
            out[n] = run_intraday(a, n, execute, now, data, quotes, ns=ns, prepared=prepared, prov_cache=prov)
        except Exception as e:                                                   # noqa: BLE001
            out[n] = fail(f"{type(e).__name__}: {e}")
    return out


def cmd_intraday(a):
    names = [a.name] if a.name else intraday_accounts(a.dir)
    if not names:
        sys.exit("沒有「盤中」模式的帳戶。開戶時加 --mode intraday,或用 --name 指定帳戶。")
    res = run_intraday_all(a, names, execute=not a.dry_run)
    for n, r in res.items():
        print(f"\n帳戶「{n}」:{r['msg']}")
        for e in r["events"]:
            print("  " + e)
        for e in r["skipped"]:
            print("  略過 " + e)


def set_mode(base, name, mode):
    """切換帳戶的下單時機:after(盤後)/ intraday(盤中)。"""
    if mode not in ("after", "intraday"):
        raise ValueError("mode 只能是 after 或 intraday")
    s = load_state(base, name)
    s["mode"] = mode
    save_state(base, name, s)


def cmd_run(a):
    names = [a.name] if a.name else list_accounts(a.dir)
    if not names:
        sys.exit("找不到任何帳戶,請先 init")
    run_accounts(a, names)


def cmd_status(a):
    names = [a.name] if a.name else list_accounts(a.dir)
    if not names:
        sys.exit("找不到任何帳戶,請先 init")
    for n in names:
        report(load_state(a.dir, n))


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default="paper_live", help="帳戶資料夾")
    common.add_argument("--app", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "app1.py"))
    common.add_argument("--asof", default=None, help="(測試用)把這個日期當作『今天已收盤』")
    common.add_argument("--synthetic", action="store_true", help="(測試用)使用假資料")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", parents=[common], help="開戶")
    i.add_argument("--profile", default="穩健型")
    i.add_argument("--params", default="", help="參數檔(如 paper_opt/best_params.json);有給就優先於 --profile")
    i.add_argument("--capital", type=float, default=1_000_000)
    i.add_argument("--days", type=int, default=20, help="模擬幾個交易日後結案")
    i.add_argument("--name", default=None, help="帳戶名稱(預設同風險型名稱)")
    i.add_argument("--max-stocks", type=int, default=500)
    i.add_argument("--codes", default="")
    i.add_argument("--force", action="store_true")
    i.add_argument("--mode", default="after", choices=["after", "intraday"], help="下單時機:after 盤後(隔天開盤成交)/ intraday 盤中(即時價立刻成交,要執行 intraday 指令)")
    for k, t in (("wl", float), ("edge", float), ("min-eff", float), ("stop", float), ("max-pos", int), ("pct", float), ("hold", int)):
        i.add_argument("--" + k, type=t, default=None, help="覆寫風險型的預設參數(選用)")
    sub.add_parser("run", parents=[common], help="處理新的已收盤交易日").add_argument("--name", default=None)
    sub.add_parser("status", parents=[common], help="查看帳戶").add_argument("--name", default=None)
    ii = sub.add_parser("intraday", parents=[common], help="盤中判斷並用即時價成交(只處理「盤中」模式的帳戶,或 --name 指定)")
    ii.add_argument("--name", default=None, help="不給 = 一次處理全部「盤中」模式的帳戶")
    ii.add_argument("--dry-run", action="store_true", help="只預覽會買賣什麼,不成交")
    a = ap.parse_args()
    {"init": cmd_init, "run": cmd_run, "status": cmd_status, "intraday": cmd_intraday}[a.cmd](a)


if __name__ == "__main__":
    main()
