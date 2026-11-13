"""
strategy_rules.py — 「策略規則」的唯一來源(模擬交易、自動優化、app1 單股頁共用)

為什麼獨立成一個檔案
  以前「什麼情況會買」只寫在 paper_trade.step_day 裡,app1 完全不知道。現在模擬交易與 app1 都呼叫這裡的函式,
  規則只有一份,不會各自改著改著就不一致。本檔不依賴 streamlit,可單獨測試。

內容
  PROFILES / load_params        內建三種風險型、讀參數檔
  prepare_stock / asof_stats    逐檔預先計算、「截至當天已實現」的情境勝率(無未來函數)
  entry_candidate / explain_entry   買進判斷(前者給回測迴圈用、快;後者給畫面用,逐條列出過不過)
  resolve_params                「目前採用參數」:手動指定 > 最新最佳化(優先驗證通過者) > 內建穩健型
本工具僅供資訊整理,不構成投資建議。
"""
import json
import os
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
COST_RT = 0.00585          # 與 app1.py 的 COST_RT 一致,僅用於「歷史情境」的未來報酬統計
HORIZON = 20               # 沒指定持有天數時,情境勝率的預設天期
MIN_AMT = 30e6             # 近 20 日均成交值下限(元)
MIN_BARS = 250             # 至少要有幾根 K 棒才判斷
Z = 1.96
STAT_HORIZONS = (5, 10, 20, 40, 60)
ACTIVE_FILE = os.path.join(HERE, "active_params.json")

PROFILES = {
    "保守型": dict(buy={"B5", "B6b", "B7"}, block={"S1", "S2b", "S5", "S7"}, exit={"S1", "S2b", "S5", "S7", "S7L"},
                 wl=55, edge=8, min_eff=12, stop=0.05, hold=20, max_pos=12, pct=0.07, ind_max=2, above60=True),
    "穩健型": dict(buy={"B4", "B5", "B6b", "B7"}, block={"S1", "S2b", "S5", "S7"}, exit={"S2b", "S5", "S7", "S7L"},
                 wl=50, edge=5, min_eff=10, stop=0.07, hold=20, max_pos=10, pct=0.09, ind_max=3, above60=False),
    "積極型": dict(buy={"B4", "B5", "B6a", "B6b", "B7"}, block={"S5", "S7"}, exit={"S5", "S7", "S7L"},
                 wl=45, edge=3, min_eff=8, stop=0.10, hold=20, max_pos=6, pct=0.16, ind_max=3, above60=False),
}
NEED_SIGS = sorted(set().union(*[p["buy"] | p["block"] | p["exit"] for p in PROFILES.values()]))
SIGN_NAME = {"B4": "放量突破", "B5": "RSI/MACD/均線同步轉強", "B6a": "跌深反彈(較弱)", "B6b": "趨勢反轉", "B7": "站上5/10/20日線翻多",
             "S1": "跌破前10/20日低", "S2b": "跌破20日線", "S5": "破底放量", "S7": "跌破5/10/20/60日線+長黑/跳空", "S7L": "長線翻空(季線失守)"}


# ------------------------------------------------------------------ 參數
def norm_params(P):
    """補齊缺的欄位(舊參數檔沒有 park / ind_max / above60),並把訊號清單轉成 set。"""
    Q = dict(P)
    for k, v in (("park", False), ("ind_max", 3), ("above60", False)):
        Q.setdefault(k, v)
    for k in ("buy", "block", "exit"):
        Q[k] = set(Q.get(k, ()))
    # 核心 ETF:閒置資金只放 core_pct(0~0.8)在 core_code,其餘留現金供個股買進。舊檔只有 park=True 的,視為 50%(舊版 park 其實沒有生效)。
    Q.setdefault("core_pct", 0.5 if Q.get("park") else 0.0)
    Q.setdefault("core_code", "0050")
    Q["core_pct"] = min(max(float(Q["core_pct"]), 0.0), 0.8)
    Q["park"] = Q["core_pct"] > 0
    return Q


def to_jsonable(P):
    return {k: (sorted(v) if isinstance(v, set) else v) for k, v in P.items() if k != "style"}


def load_params(path):
    """讀參數檔(paper_optimize / 儀表板存的 json,或單純的參數 dict)→ (標籤, 參數)。缺的欄位用穩健型補齊。"""
    j = json.load(open(path, encoding="utf-8"))
    raw = j.get("params", j)
    P = {k: (set(v) if isinstance(v, (list, set)) else v) for k, v in PROFILES["穩健型"].items()}
    P.update({k: v for k, v in raw.items() if k != "style"})
    return j.get("label", os.path.splitext(os.path.basename(path))[0]), norm_params(P)


def _roots(roots=None):
    out = []
    for r in (roots or (HERE, os.getcwd())):
        r = os.path.abspath(r)
        if r not in out:
            out.append(r)
    return out


def find_optimized(bases=("paper_live",), roots=None):
    """找最佳化器存的參數:各 <base>/params/優化*.json 與 paper_opt/best_params.json。
    優先「驗證段不輸 0050」者,同級取最新;都沒通過就取最新一份(passed=False)。找不到回傳 None。"""
    files = []
    for root in _roots(roots):
        for b in bases:
            d = os.path.join(root, b, "params")
            if os.path.isdir(d):
                files += [os.path.join(d, f) for f in os.listdir(d) if f.startswith("優化") and f.endswith(".json")]
        cli = os.path.join(root, "paper_opt", "best_params.json")
        if os.path.exists(cli):
            files.append(cli)
    found = []
    for f in dict.fromkeys(files):
        try:
            j = json.load(open(f, encoding="utf-8"))
            label, P = load_params(f)
            found.append(dict(path=f, label=label, P=P, mtime=os.path.getmtime(f), verdict=j.get("verdict", "(無驗證資訊)"),
                              passed="不輸" in str(j.get("verdict", "")), info=j))
        except Exception:                                   # noqa: BLE001
            pass
    return max(found, key=lambda x: (x["passed"], x["mtime"])) if found else None


def read_active():
    if not os.path.exists(ACTIVE_FILE):
        return None
    try:
        j = json.load(open(ACTIVE_FILE, encoding="utf-8"))
        label, P = load_params(ACTIVE_FILE)
        return dict(path=ACTIVE_FILE, label=label, P=P, verdict=j.get("verdict", "(無驗證資訊)"),
                    passed=("不輸" in str(j.get("verdict", ""))) if j.get("verdict") else None, info=j)
    except Exception:                                       # noqa: BLE001
        return None


def write_active(P, label, extra=None):
    """把一組參數設為「目前採用」(app1 與儀表板開戶的預設)。extra 可帶 verdict / train / test。"""
    j = dict(label=label, params=to_jsonable(P), set_at=time.strftime("%Y-%m-%d %H:%M"))
    if extra:
        j.update({k: v for k, v in extra.items() if k not in ("params", "label")})
    json.dump(j, open(ACTIVE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)
    return ACTIVE_FILE


def clear_active():
    if os.path.exists(ACTIVE_FILE):
        os.remove(ACTIVE_FILE)


def resolve_params(bases=("paper_live",), roots=None):
    """目前該用哪組參數:手動指定 > 最新最佳化 > 內建穩健型。回傳 dict(kind, label, P, verdict, passed, path)。"""
    a = read_active()
    if a:
        return dict(a, kind="active")
    o = find_optimized(bases, roots)
    if o:
        return dict(o, kind="optimized")
    return dict(kind="default", label="穩健型(內建預設)", P=norm_params(PROFILES["穩健型"]), verdict="尚未做最佳化,使用內建預設", passed=None, path="")


def param_text(P):
    return (f"持有{P['hold']}日/停損{P['stop'] * 100:.0f}%/{P['max_pos']}檔/勝率下限{P['wl']:.0f}・優勢{P['edge']:.0f}"
            f"/買{'+'.join(sorted(P['buy']))}" + (f"/核心{P.get('core_code', '0050')} {P['core_pct'] * 100:.0f}%" if P.get("core_pct") else ""))


# ------------------------------------------------------------------ 逐檔預先計算與情境勝率
def fwd_for(s, h):
    """持有 h 日、扣成本後的未來報酬(依 h 快取在 s 裡)。"""
    d = s.setdefault("fwd_h", {})
    if h not in d:
        C = s["C"]
        f = np.full(len(C), np.nan)
        if 0 < h < len(C):
            f[:-h] = C[h:] / C[:-h] - 1 - COST_RT
        d[h] = f
    return d[h]


def prepare_stock(row, df, funcs, sigs=None):
    """funcs 需含 build_signals / tech_components(來自 app1.py)。訊號一律不帶法人資料(inst=None),
    與模擬交易、最佳化驗證時完全一致。"""
    F = funcs["build_signals"](df[["Open", "High", "Low", "Close", "Volume"]], None)
    x = F["x"]
    C = df.Close.values.astype(float)
    n = len(C)
    sc = funcs["tech_components"](x).sum(axis=1).values
    valid = x[["MA60", "K", "RSI", "BB_UP"]].notna().all(axis=1).values
    adj = df["Adj Close"] if "Adj Close" in df.columns else df.Close
    f = (adj / df.Close).values
    div = np.zeros(n)
    div[1:] = np.clip(C[:-1] * (1 - f[1:] / f[:-1]), 0, None)
    div[div < 1e-3] = 0
    s = dict(code=row["stock_id"], name=row["stock_name"], ind=row["industry_category"], idx=df.index,
             O=df.Open.values.astype(float), H=df.High.values.astype(float), L=df.Low.values.astype(float), C=C,
             amt20=(df.Close * df.Volume).rolling(20).mean().values, sc=sc, valid=valid, div=div,
             above=(x.Close > x.MA60).values,
             rz=pd.cut(x.RSI, [0, 30, 50, 70, 100.1], labels=False).values.astype(float),
             sig={k: F["sig"][k].values for k in (sigs or NEED_SIGS)})
    s["fwd_h"] = {}
    for h in STAT_HORIZONS:
        fwd_for(s, h)
    s["fwd"] = s["fwd_h"][HORIZON]
    return s


def asof_stats(s, i, h=None):
    """與 app1.situation_stats 相同的邏輯,但只取「截至第 i 天已實現」的樣本(j + h <= i)。h = 持有天數。"""
    h = h or HORIZON
    lim = i - h + 1
    if lim < MIN_BARS or s["valid"][:i + 1].sum() < MIN_BARS or not s["valid"][i]:
        return None
    v, sc = s["valid"][:lim], s["sc"][:lim]
    near = v & (np.abs(sc - s["sc"][i]) <= 1)
    strict = near & (s["above"][:lim] == s["above"][i]) & (s["rz"][:lim] == s["rz"][i])
    use = strict if strict.sum() >= 30 else near
    fw = fwd_for(s, h)[:lim]
    sel = fw[use & np.isfinite(fw)]
    if not len(sel):
        return None
    base = fw[v & np.isfinite(fw)]
    n_eff = max(len(sel) / h, 1.0)
    w = float((sel > 0).mean())
    den = 1 + Z * Z / n_eff
    mid = (w + Z * Z / (2 * n_eff)) / den
    half = Z * np.sqrt(w * (1 - w) / n_eff + Z * Z / (4 * n_eff ** 2)) / den
    return dict(wl=max(mid - half, 0) * 100, edge=(w - (base > 0).mean()) * 100, neff=n_eff,
                mean=float(sel.mean()), med=float(np.median(sel)), strict=bool(strict.sum() >= 30), n=int(len(sel)))


# ------------------------------------------------------------------ 買進 / 賣出判斷
def entry_candidate(P, s, i):
    """回測迴圈用(提早結束、快)。符合買進條件回傳 (勝率下限, 勝率優勢, 觸發的買進訊號),否則 None。
    條件與 explain_entry 逐條相同,tests 會驗證兩者一致。"""
    if i < MIN_BARS:
        return None
    sg = s["sig"]
    hit = [b for b in sorted(P["buy"]) if sg[b][i]]
    if not hit or any(sg[b][i] for b in P["block"]):
        return None
    if not (s["amt20"][i] >= MIN_AMT) or (P["above60"] and not s["above"][i]):
        return None
    r = asof_stats(s, i, P["hold"])
    if r and r["neff"] >= P["min_eff"] and r["wl"] >= P["wl"] and r["edge"] >= P["edge"] and r["mean"] > 0 and r["med"] > 0:
        return r["wl"], r["edge"], hit
    return None


def explain_entry(P, s, i):
    """畫面用:每個條件都算,逐條回傳過不過。ok = 全部通過(等同 entry_candidate 非 None)。"""
    sg, checks = s["sig"], []

    def add(name, ok, now, need):
        checks.append(dict(條件=name, 通過=bool(ok), 目前=now, 門檻=need))

    hit = [b for b in sorted(P["buy"]) if sg[b][i]]
    blk = [b for b in sorted(P["block"]) if sg[b][i]]
    add("資料足夠", i >= MIN_BARS, f"{i + 1} 根K棒", f"至少 {MIN_BARS + 1} 根")
    add("有買進訊號", bool(hit), "、".join(f"{b} {SIGN_NAME.get(b, '')}" for b in hit) or "沒有", "任一:" + "/".join(sorted(P["buy"])))
    add("沒有排除訊號", not blk, "、".join(f"{b} {SIGN_NAME.get(b, '')}" for b in blk) or "沒有", "不可出現:" + ("/".join(sorted(P["block"])) or "(不限)"))
    amt = float(s["amt20"][i]) if np.isfinite(s["amt20"][i]) else 0.0
    add("流動性", amt >= MIN_AMT, f"近20日均成交值 {amt / 1e6:.0f} 百萬", f"≥ {MIN_AMT / 1e6:.0f} 百萬")
    if P["above60"]:
        add("站上季線", bool(s["above"][i]), "站上" if s["above"][i] else "季線之下", "必須站上")
    r = asof_stats(s, i, P["hold"]) if i >= MIN_BARS else None
    if r is None:
        add("歷史情境勝率", False, "樣本不足,算不出來", "需足夠歷史")
    else:
        add("有效獨立樣本", r["neff"] >= P["min_eff"], f"{r['neff']:.1f}", f"≥ {P['min_eff']}")
        add(f"情境勝率下限(持有{P['hold']}日)", r["wl"] >= P["wl"], f"{r['wl']:.0f}%", f"≥ {P['wl']:.0f}%")
        add("勝率優勢(相對全期)", r["edge"] >= P["edge"], f"{r['edge']:+.0f}%", f"≥ {P['edge']:.0f}%")
        add("平均與中位數報酬為正", r["mean"] > 0 and r["med"] > 0, f"平均 {r['mean'] * 100:+.1f}% / 中位 {r['med'] * 100:+.1f}%", "皆 > 0")
    return dict(ok=all(c["通過"] for c in checks), checks=checks, hit=hit, stats=r,
                failed=[c["條件"] for c in checks if not c["通過"]])


def exit_hits(P, s, i):
    """若已持有:今天觸發了哪些賣出訊號。"""
    return [e for e in sorted(P["exit"]) if s["sig"][e][i]]
