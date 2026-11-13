#!/usr/bin/env python3
"""
paper_optimize.py — 自動尋找模擬交易參數(含短線 / 波段 / 中長線),並用「沒看過的資料」驗證

為什麼要這樣做
  只在同一段歷史上調參數,幾乎一定能調出「過去很漂亮」的組合,但那是過度擬合,未來多半失效。
  所以本程式把歷史切成兩段:
    訓練段 — 只用來搜尋、排名參數
    驗證段 — 從頭到尾沒被拿來調參,用來檢驗最佳參數「在沒看過的行情」是否真的不輸 0050
  最後的判定只看驗證段。若沒有任何參數在驗證段不輸 0050,程式會明講「沒有找到」,不會硬湊。

用法(需與 paper_trade.py、app1.py 同資料夾;第一次會下載股價,之後用快取):
  python paper_optimize.py --style swing  --trials 120
  python paper_optimize.py --style short  --train-end 2025-03-31 --test-start 2025-04-01
  python paper_optimize.py --style long   --max-stocks 300
  python paper_optimize.py --style all    --trials 200          # 同時搜尋三種風格
  python paper_optimize.py --selftest --trials 12                 # 假資料驗證流程(數字無意義)

風格(持有天數同時決定「歷史情境勝率」的統計天期):
  short 短線 持有 5~10 日 | swing 波段 持有 20 日 | long 中長線 持有 40~60 日

輸出(--out 資料夾):best_params.json(可直接給 paper_live.py init --params 或 paper_trade.py --params)、
  trials.csv(所有嘗試與成績)。本工具僅供資訊整理,不構成投資建議。
"""
import argparse
import json
import os
import random
import sys
import time

import numpy as np
import pandas as pd

import paper_trade as pt

STYLES = {"short": dict(hold=[5, 10], stop=[0.03, 0.04, 0.05, 0.06]),
          "swing": dict(hold=[20], stop=[0.05, 0.07, 0.10]),
          "long": dict(hold=[40, 60], stop=[0.08, 0.10, 0.15, 0.20])}
LABEL = {"short": "短線", "swing": "波段", "long": "中長線"}
BUY_POOL = ["B4", "B5", "B6a", "B6b", "B7"]
EXIT_CHOICES = [["S5", "S7", "S7L"], ["S2b", "S5", "S7", "S7L"], ["S1", "S2b", "S5", "S7", "S7L"], []]


CORE_CHOICES = [0.0, 0.25, 0.5, 0.65]        # 核心 ETF 占「閒置資金」的比例;沒有 1.0(不 all-in),上限 pt.CORE_MAX
CORE_ETFS = ["0050", "006208", "0056", "00878"]


def sample(rng, styles, core_max=0.65, core_codes=("0050",)):
    style = rng.choice(styles)
    cfg = STYLES[style]
    ex = set(rng.choice(EXIT_CHOICES))
    mp = rng.choice([4, 6, 8, 10, 15])
    cp = rng.choice([c for c in CORE_CHOICES if c <= core_max] or [0.0])
    core = dict(core_pct=cp, core_code=rng.choice(list(core_codes)) if cp else "0050", park=cp > 0)
    return dict(style=style, buy=set(rng.sample(BUY_POOL, rng.randint(1, len(BUY_POOL)))),
                block={"S5", "S7"} if "S5" in ex else set(), exit=ex,
                wl=rng.choice([0, 20, 30, 40, 45, 50]), edge=rng.choice([-100, 0, 3, 5, 8]), min_eff=rng.choice([3, 5, 8, 12]),
                stop=rng.choice(cfg["stop"]), hold=rng.choice(cfg["hold"]), max_pos=mp, pct=round(0.95 / mp, 3),
                ind_max=rng.choice([2, 3, 99]), above60=rng.choice([False, True]), **core)


def make_window(stocks, bench, start, end):
    cal = pd.DatetimeIndex(sorted(set().union(*[set(s["idx"][(s["idx"] >= start) & (s["idx"] <= end)]) for s in stocks.values()])))
    b = bench["Adj Close"].reindex(cal).ffill().bfill()
    dd = (b / b.cummax() - 1).min()
    return dict(cal=cal, maps=pt.day_index_maps(stocks, cal), mult=pt.bench_mult(bench, cal),
                bench_ret=float(b.iloc[-1] / b.iloc[0] - 1), bench_mdd=float(dd))


def evaluate(P, stocks, W, cap):
    e, tr, _ = pt.run_profile("x", P, stocks, W["cal"], 0, cap, W["mult"], W["maps"])
    ret = float(e.equity.iloc[-1] / cap - 1)
    mdd = float((e.equity / e.equity.cummax() - 1).min())
    return dict(ret=ret, mdd=mdd, trades=len(tr), win=float((tr["報酬率"] > 0).mean()) if len(tr) else 0.0,
                expo=float(e.exposure.mean()), excess=ret - W["bench_ret"])


def jsonable(P):
    return {k: (sorted(v) if isinstance(v, set) else v) for k, v in P.items() if k != "style"}


def run_search(stocks, bench, tr0, tr1, te0, te1, styles, trials, top_n=8, min_trades=30, seed=1, cap=1_000_000, progress=None,
               core_max=0.65, core_codes=None):
    """隨機搜尋參數:訓練段排名 → 前 top_n 名拿去驗證段檢驗。progress(done, total) 可用來更新進度條。
    core_max = 核心 ETF 占閒置資金的上限(0 = 不使用核心部位)。core_codes 預設為股票池裡有的 CORE_ETFS。"""
    Wtr, Wte = make_window(stocks, bench, tr0, tr1), make_window(stocks, bench, te0, te1)
    core_max = min(float(core_max), pt.CORE_MAX)
    core_codes = [c for c in (core_codes or CORE_ETFS) if c in stocks] or (["0050"] if "0050" in stocks else [])
    if not core_codes:
        core_max = 0.0
    rng, rows = random.Random(seed), []
    for k in range(trials):
        P = sample(rng, styles, core_max, core_codes or ("0050",))
        r = evaluate(P, stocks, Wtr, cap)
        rows.append(dict(P=P, tr=r, ok=r["trades"] >= min_trades))
        if progress:
            progress(k + 1, trials)
    valid = sorted((x for x in rows if x["ok"]), key=lambda x: -x["tr"]["excess"])
    top = valid[:top_n]
    for x in top:
        x["te"] = evaluate(x["P"], stocks, Wte, cap)
        x["pass"] = x["te"]["ret"] >= Wte["bench_ret"]
    return dict(Wtr=Wtr, Wte=Wte, rows=rows, top=top, passed=[x for x in top if x["pass"]],
                windows=(str(tr0.date()), str(tr1.date()), str(te0.date()), str(te1.date())))


def recs_frame(rows):
    recs = []
    for x in rows:
        P, r = x["P"], x["tr"]
        recs.append({"風格": LABEL[P["style"]], "持有日": P["hold"], "停損%": P["stop"] * 100, "最多檔": P["max_pos"], "勝率下限%": P["wl"],
                     "勝率優勢%": P["edge"], "最少樣本": P["min_eff"], "買進訊號": "+".join(sorted(P["buy"])), "賣出訊號": "+".join(sorted(P["exit"])) or "無",
                     "站上季線": P["above60"], "核心ETF": P["core_code"] if P["core_pct"] else "無", "核心比例%": P["core_pct"] * 100, "訓練報酬%": r["ret"] * 100, "訓練超額%": r["excess"] * 100,
                     "訓練回撤%": r["mdd"] * 100, "訓練交易數": r["trades"], "訓練勝率%": r["win"] * 100, "訓練持股水位%": r["expo"] * 100,
                     "有效": x["ok"]})
    return pd.DataFrame(recs)


def candidate_json(x, windows, Wtr, Wte):
    """一組候選參數(含訓練/驗證成績)→ 可存檔的 dict(格式同 best_params.json)。"""
    verdict = "驗證段不輸 0050" if x["pass"] else "觸發條件僅供參考"
    return dict(label=f"優化-{LABEL[x['P']['style']]}", params=jsonable(x["P"]), verdict=verdict,
                train=dict(window=list(windows[:2]), **x["tr"], bench_ret=Wtr["bench_ret"]),
                test=dict(window=list(windows[2:]), **x["te"], bench_ret=Wte["bench_ret"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", default="swing", choices=["short", "swing", "long", "all"])
    ap.add_argument("--train-start", default="2023-01-02")
    ap.add_argument("--train-end", default="2025-03-31")
    ap.add_argument("--test-start", default="2025-04-01")
    ap.add_argument("--test-end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--trials", type=int, default=120)
    ap.add_argument("--top", type=int, default=8, help="訓練段前 N 名拿去驗證段檢驗")
    ap.add_argument("--min-trades", type=int, default=30, help="訓練段至少要有幾筆交易才算數(避免『幾乎不交易=跟 0050 一樣』的假好成績)")
    ap.add_argument("--max-stocks", type=int, default=300, help="個股數量(ETF 不佔名額,自動全部納入)")
    ap.add_argument("--core-max", type=float, default=0.65, help="核心 ETF 占閒置資金的上限 0~0.8;0 = 不使用核心部位。永遠不會 all-in")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--cache", default=os.path.join("paper_out", "prices.pkl"), help="股價快取(與 paper_trade.py 共用)")
    ap.add_argument("--app", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "app1.py"))
    ap.add_argument("--out", default="paper_opt")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    ns = pt.load_app_funcs(a.app)
    tr0, tr1, te0, te1 = (pd.Timestamp(x) for x in (a.train_start, a.train_end, a.test_start, a.test_end))
    if not tr0 < tr1 < te0 < te1:
        sys.exit("日期需為 train-start < train-end < test-start < test-end")

    if a.selftest:
        uni, data = pt.synthetic()
        for c, src in zip(("0050", "0056"), list(data)[:2]):
            data[f"{c}.TW"] = data[src].copy()
        uni = pt.with_etfs(uni)
        print("【自我測試】假資料,數字無意義,只驗證流程。")
    else:
        uni = pt.with_etfs(pt.load_universe())
        os.makedirs(os.path.dirname(a.cache) or ".", exist_ok=True)
        data = pt.download(list(uni.ticker) + ["0050.TW"], a.cache)
    bench = data.get("0050.TW")
    if bench is None:
        sys.exit("缺少 0050 資料")
    stocks = pt.select_and_prepare(uni, data, tr0, a.max_stocks, ns)
    styles = list(STYLES) if a.style == "all" else [a.style]
    t0 = time.time()
    print(f"\n搜尋 {a.trials} 組參數({'/'.join(LABEL[x] for x in styles)})…第一輪要建立快取,之後會快很多")

    def prog(k, n):
        if k % 10 == 0 or k == 1:
            print(f"  {k}/{n}  已用 {time.time() - t0:.0f} 秒", flush=True)

    try:
        res = run_search(stocks, bench, tr0, tr1, te0, te1, styles, a.trials, a.top, a.min_trades, a.seed, a.capital, prog,
                         core_max=a.core_max)
    except ValueError:
        res = None
    if not res or not res["top"]:
        sys.exit(f"沒有任何參數在訓練段達到 {a.min_trades} 筆交易,請放寬 --min-trades 或增加 --trials")
    Wtr, Wte, top = res["Wtr"], res["Wte"], res["top"]
    print(f"訓練段 {Wtr['cal'][0].date()}~{Wtr['cal'][-1].date()}:0050 {Wtr['bench_ret'] * 100:+.1f}%(最大回撤 {Wtr['bench_mdd'] * 100:.1f}%)")
    print(f"驗證段 {Wte['cal'][0].date()}~{Wte['cal'][-1].date()}:0050 {Wte['bench_ret'] * 100:+.1f}%(最大回撤 {Wte['bench_mdd'] * 100:.1f}%)")
    recs_frame(res["rows"]).to_csv(os.path.join(a.out, "trials.csv"), index=False, encoding="utf-8-sig")

    print("\n" + "=" * 100)
    print(f"訓練段前 {len(top)} 名,拿去驗證段(沒用來調參的資料)檢驗。驗證段 0050 = {Wte['bench_ret'] * 100:+.1f}%")
    print(f"{'#':>2} {'風格':<4}{'持有':>4}{'停損':>5}{'檔數':>4}{'勝率下限':>7}{'優勢':>5}{'核心ETF':>10}"
          f"{'訓練報酬':>9}{'訓練超額':>9}{'驗證報酬':>9}{'驗證超額':>9}{'驗證回撤':>9}{'驗證交易':>8}  判定")
    for n, x in enumerate(top, 1):
        P, r, v = x["P"], x["tr"], x["te"]
        print(f"{n:>2} {LABEL[P['style']]:<4}{P['hold']:>4}{P['stop'] * 100:>4.0f}%{P['max_pos']:>4}{P['wl']:>7}{P['edge']:>5}{(P['core_code'] + ' ' + format(P['core_pct'] * 100, '.0f') + '%') if P['core_pct'] else '無':>10}"
              f"{r['ret'] * 100:>+8.1f}%{r['excess'] * 100:>+8.1f}%{v['ret'] * 100:>+8.1f}%{v['excess'] * 100:>+8.1f}%{v['mdd'] * 100:>8.1f}%{v['trades']:>8}  "
              f"{'✅ 不輸 0050' if x['pass'] else '❌ 輸給 0050'}")
    passed, best = res["passed"], top[0]               # 依「訓練段」排名第一(不拿驗證段挑選,避免偷看)
    print("=" * 100)
    if passed:
        print(f"✅ 訓練段前 {len(top)} 名中有 {len(passed)} 組,在沒看過的驗證段也不輸 0050。")
        print("   但驗證段只有一段行情,通過不代表未來也會贏;建議再用 paper_live.py 前向模擬幾個月確認。")
    else:
        print("❌ 沒有找到在驗證段不輸 0050 的參數。意思是:這些訊號搭配這類參數,在沒看過的行情裡贏不了 0050。")
        print("   這是有價值的結論,不要為了讓數字好看而回頭放寬條件或縮短驗證段;可考慮改成『以 0050 為核心、訊號只當加碼』,")
        print("   或接受『目標是降低回撤而非贏過 0050』。")
    out = candidate_json(best, res["windows"], Wtr, Wte)
    json.dump(out, open(os.path.join(a.out, "best_params.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)
    print(f"\n訓練段第 1 名參數已存:{os.path.join(a.out, 'best_params.json')}({out['verdict']})")
    print(f"全部嘗試:{os.path.join(a.out, 'trials.csv')}")
    print("後續:  python paper_trade.py --params paper_opt/best_params.json   # 完整區間回測並與 0050 對照")
    print("       python paper_live.py init --params paper_opt/best_params.json --name 優化帳戶 --days 20   # 前向模擬")


if __name__ == "__main__":
    main()
