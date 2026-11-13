"""
模擬交易帳戶 — 圖形化網頁(Streamlit)

放在 paper_live.py / paper_trade.py / app.py 同一個資料夾,執行:
    streamlit run paper_dashboard.py

四個功能(左側切換):
  📒 模擬帳戶      開新帳戶、一鍵更新、看電腦每天買賣什麼、目前持股、帳戶剩多少錢、和 0050 比較、明天的計畫
     (開新帳戶可選「✍️ 手動操盤」:自己每天選股下單,用同樣的成交規則和電腦帳戶比賽)
  🧪 手動回測與調參  手動調整所有參數 → 回測 → 與 0050 對照;可切成「調整期間 / 檢驗期間」避免過度擬合;並排比較多次回測
  🔍 自動優化      短線 / 波段 / 中長線自動搜尋參數,用沒看過的資料驗證;可一鍵套用到回測頁或存成參數檔
  ⭐ 目前策略參數  從已存在的參數檔挑一組,設為「目前採用參數」;app.py 單股頁的「策略判斷」與全市場掃描的買入策略會改用這組
帳戶、股價快取與參數檔都放在同一個資料夾,與命令列版(paper_live.py / paper_optimize.py)共用。
本工具僅供資訊整理,不構成投資建議。
"""
import argparse
import contextlib
import functools
import io
import json
import os
import pickle
import re
import sys
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import paper_live as pl           # noqa: E402
import paper_trade as pt          # noqa: E402
import paper_optimize as po       # noqa: E402
import paper_manual as pm         # noqa: E402
import paper_ui as ui             # noqa: E402
import strategy_rules as sr       # noqa: E402

UP, DN, BLUE, GRAY = "#e0383e", "#1f9d55", "#2c7be5", "#8a94a6"      # 台股慣例:紅漲綠跌
ICON = {"買進": "🟢", "賣出": "🔴", "股息": "💰", "略過": "⚪"}


# ================================================================== 資料整理(不依賴畫面,可單獨測試)
def name_map(s):
    return {r["stock_id"]: r["stock_name"] for r in s["universe"]}


def eq_frame(s):
    a = s["acct"]
    df = pd.DataFrame(a["eq"], columns=["日期", "總資產", "持股水位", "現金"])
    if df.empty:
        return df
    df["日期"] = pd.to_datetime(df["日期"])
    prev = df["總資產"].shift().fillna(s["capital"])
    df["日損益"] = df["總資產"] - prev
    df["日報酬%"] = df["日損益"] / prev * 100
    df["累計報酬%"] = (df["總資產"] / s["capital"] - 1) * 100
    df["持股水位%"] = df["持股水位"] * 100
    return df


def bench_frame(s):
    """0050 同期走勢,換算成同樣本金,方便和帳戶放在同一張圖比較。"""
    b = pd.DataFrame(s["bench"], columns=["日期", "價"])
    if b.empty:
        return b
    b["日期"] = pd.to_datetime(b["日期"])
    b["0050 同期"] = s["capital"] * b["價"] / b["價"].iloc[0]
    return b


def holdings_frame(s, quotes=None):
    """quotes = 即時報價 {代碼: dict}(盤中才給):現價改用即時價,沒有成交報價的仍用上一個收盤價。"""
    quotes = quotes or {}
    a, nm, rows = s["acct"], name_map(s), []
    live = lambda c, dflt: float((quotes.get(c) or {}).get("price") or dflt)           # noqa: E731
    for c, p in a["pos"].items():
        px = live(c, a["last_close"].get(c, p["ep"]))
        rows.append({"代碼": c, "名稱": nm.get(c, ""), "股數": p["sh"], "成本價": p["ep"], "現價": px,
                     "損益%": (px / p["ep"] - 1) * 100, "未實現損益": p["sh"] * px - p["cost"], "市值": p["sh"] * px,
                     "持有日": p["n"], "停損價": p["stop"]})
    core = a.get("core") or {}
    if core.get("sh", 0) > 0:
        code = s["params"].get("core_code", "0050")
        px = live(code, a["last_close"].get(code, core["cost"] / core["sh"]))
        rows.append({"代碼": code, "名稱": nm.get(code, "") + "(核心)", "股數": core["sh"], "成本價": core["cost"] / core["sh"], "現價": px,
                     "損益%": (px * core["sh"] / core["cost"] - 1) * 100, "未實現損益": core["sh"] * px - core["cost"], "市值": core["sh"] * px,
                     "持有日": None, "停損價": None})
    return pd.DataFrame(rows)


def plan_frame(s):
    a, nm, rows = s["acct"], name_map(s), []
    for c in a["pend_sell"]:
        rows.append({"動作": "賣出", "代碼": c, "名稱": nm.get(c, ""),
                     "依據": "、".join(pl.SIGN.get(x, x) for x in a["sell_why"].get(c, [])), "情境勝率下限%": None, "勝率優勢%": None})
    for c in a["pend_buy"]:
        i = a["pend_info"].get(c, {})
        rows.append({"動作": "買進", "代碼": c, "名稱": nm.get(c, ""), "依據": "、".join(pl.SIGN.get(x, x) for x in i.get("sigs", [])),
                     "情境勝率下限%": i.get("wl"), "勝率優勢%": i.get("edge")})
    return pd.DataFrame(rows)


def orders_frame(s):
    """買賣流水帳:每一次成交一列(已賣出的交易拆成買、賣兩列;仍持有的只有買進)。"""
    a, rows = s["acct"], []
    nm = name_map(s) if s.get("universe") else {}                 # 手動帳戶的存檔沒有股票池,名稱改用持股裡記的
    for t in a["trades"]:
        rows.append({"日期": t["買進日"], "動作": "買進", "代碼": t["代碼"], "名稱": t.get("名稱") or nm.get(t["代碼"], ""), "股數": t["股數"], "成交價": t["買價"],
                     "金額": t["股數"] * t["買價"], "說明": ""})
        rows.append({"日期": t["賣出日"], "動作": "賣出", "代碼": t["代碼"], "名稱": t.get("名稱") or nm.get(t["代碼"], ""), "股數": t["股數"], "成交價": t["賣價"],
                     "金額": t["股數"] * t["賣價"], "說明": f"{t.get('原因', '賣出')},單筆 {t['報酬率'] * 100:+.1f}%"})
    for c, p in a["pos"].items():
        rows.append({"日期": p["d"], "動作": "買進", "代碼": c, "名稱": p.get("name") or nm.get(c, ""), "股數": p["sh"], "成交價": p["ep"],
                     "金額": p["sh"] * p["ep"], "說明": "持有中"})
    df = pd.DataFrame(rows)
    return df.sort_values(["日期", "動作"], ascending=[False, True]).reset_index(drop=True) if len(df) else df


def trades_frame(s):
    t = pd.DataFrame(s["acct"]["trades"])
    if len(t):
        t["報酬率%"] = t["報酬率"] * 100
        t = t.drop(columns=["報酬率"]).iloc[::-1].reset_index(drop=True)
    return t


def quote_date(quotes):
    """報價的交易日(證交所 MIS 的 d 欄位,例如 20261030 → 2026-10-30)。沒有就回傳 None。"""
    q = (quotes or {}).get("0050") or {}
    d = str(q.get("date") or "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if re.fullmatch(r"\d{8}", d) else None


def live_view(s, quotes, bench_close=None):
    """顯示用的「最新成交價版」帳戶複本:持股(與核心 ETF)現價換成最新成交價,把這個總資產當成最新一筆,0050 同期也換成最新成交價。
    只要「報價的交易日 > 帳戶最後處理日」就會疊上去,不限盤中:
      · 盤中 → 即時價;
      · 收盤後、還沒按「更新到最新交易日」處理當天收盤資料前 → 當天最後成交價(否則畫面會退回到上一次處理的舊數字,看起來像回到本金)。
    ⚠️ 只能拿來顯示,絕對不要存檔(不會改到傳進來的 s,但複本裡有一筆還沒正式處理的資料點)。
    沒有報價、已結案、今天還沒開盤成交(0050 成交量 0)、或帳戶已處理到那一天時,原樣回傳。"""
    a = s["acct"]
    qd, q0 = quote_date(quotes), (quotes or {}).get("0050")
    if not quotes or not qd or not q0 or not q0.get("volume") or s.get("finished") or not a["eq"] or s["last_date"] >= qd:
        return s
    core = a.get("core") or {}
    cc = (s.get("params") or {}).get("core_code", "0050")
    lc = dict(a["last_close"])
    for c in list(a["pos"]) + ([cc] if core.get("sh", 0) > 0 else []):
        q = quotes.get(c)
        if q and q.get("volume") and q.get("price"):                       # 還沒成交的(volume=0)價格只是參考價,不用
            lc[c] = float(q["price"])
    mv = sum(p["sh"] * lc.get(c, p["ep"]) for c, p in a["pos"].items())
    if core.get("sh", 0) > 0:
        mv += core["sh"] * lc.get(cc, core["cost"] / core["sh"])
    eq = a["cash"] + mv
    out = dict(s, acct=dict(a, last_close=lc, eq=a["eq"] + [[qd, float(eq), float(mv / eq) if eq else 0.0, float(a["cash"])]]),
               live_at=pm.taipei_now().strftime("%H:%M:%S"),
               live_note="盤中即時" if pm.session_open() else f"{qd} 最後成交價(這天的收盤資料還沒處理,按左側「更新到最新交易日」後會正式入帳)")
    c0 = (bench_close or {}).get(s["last_date"])
    if q0.get("price") and c0 and s["bench"]:                              # 0050 同期:最後處理日的值 × 最新成交價 ÷ 最後處理日收盤
        out["bench"] = s["bench"] + [[qd, float(s["bench"][-1][1] * float(q0["price"]) / c0)]]
    return out


def summary(s):
    a = s["acct"]
    last = a["eq"][-1] if a["eq"] else None
    eq = last[1] if last else s["capital"]
    b = (s["bench"][-1][1] / s["bench"][0][1] - 1) * 100 if len(s["bench"]) > 1 else None
    return {"帳戶": s["name"], "風險型": s["profile"], "狀態": "已結案" if s["finished"] else "進行中",
            "參數": param_summary(s["params"]), "進度": f"{max(s['day_no'], 0)}/{s['target_days']}", "最新日": (last[0] + (f" ⚡{s['live_at']}" if s.get("live_at") else "")) if last else "-", "總資產": eq,
            "損益": eq - s["capital"], "報酬%": (eq / s["capital"] - 1) * 100, "0050 同期%": b, "持股檔數": len(a["pos"]),
            "現金": last[3] if last else s["capital"]}


def day_counts(events):
    return sum(e.startswith("買進") for e in events), sum(e.startswith("賣出") for e in events)


def quiet(fn, *args):
    """執行會 print 的函式,把輸出收起來顯示;遇到 sys.exit(錯誤訊息)也不讓網頁當掉。回傳 (成功?, 輸出文字, 函式回傳值)。"""
    buf, ok, res = io.StringIO(), True, None
    try:
        with contextlib.redirect_stdout(buf):
            res = fn(*args)
    except SystemExit as e:
        ok = False
        buf.write(f"\n{e}\n")
    except Exception as e:                                  # noqa: BLE001
        ok = False
        buf.write(f"\n發生錯誤:{type(e).__name__}: {e}\n")
    return ok, buf.getvalue(), res


def call_quiet(fn, *args):
    ok, text, _ = quiet(fn, *args)
    return ok, text


def _color(v):
    if v is None or v != v:
        return ""
    return f"color:{UP}" if v > 0 else f"color:{DN}" if v < 0 else ""


def _style(df, pnl_cols, fmt):
    sty = df.style.format(fmt, na_rep="—")
    f = getattr(sty, "map", None) or sty.applymap
    return f(_color, subset=pnl_cols)


# ================================================================== 畫面
def money(x, d=0):
    return f"{x:,.{d}f}"


def equity_chart(s, height=360):
    e, b = eq_frame(s), bench_frame(s)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=e["日期"], y=e["總資產"], name="模擬帳戶", mode="lines+markers", line=dict(color=BLUE, width=3),
                             hovertemplate="%{x|%Y-%m-%d}<br>總資產 %{y:,.0f}<extra></extra>"))
    if len(b):
        fig.add_trace(go.Scatter(x=b["日期"], y=b["0050 同期"], name="0050 同期(同本金)", mode="lines",
                                 line=dict(color=GRAY, width=2, dash="dash"),
                                 hovertemplate="%{x|%Y-%m-%d}<br>0050 同期 %{y:,.0f}<extra></extra>"))
    fig.add_hline(y=s["capital"], line_dash="dot", line_color="rgba(128,128,128,.5)", annotation_text="本金")
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified",
                      legend=dict(orientation="h", y=1.08), yaxis_title="資產(元)")
    return fig


def show_events(events):
    if not events:
        st.write("⚪ 當天沒有任何買賣")
    for e in events:
        st.write(f"{ICON.get(e[:2], '•')} {e}")


def intraday_panel(s, base, ns_common, fake):
    """電腦帳戶的「下單時機」:盤後(隔天開盤成交)或盤中(按下執行時用即時報價判斷並立刻成交)。"""
    n = s["name"]
    cur = s.get("mode") == "intraday"
    with st.container(border=True):
        choice = st.radio("下單時機", ["🌙 盤後(收盤後算訊號,隔天開盤價成交)", "⚡ 盤中(你按「立即執行」時,用即時報價重算訊號並立刻成交)"], index=1 if cur else 0,
                          horizontal=True, key=f"mode_{pm.safe(n)}", disabled=s["finished"])
        want = choice.startswith("⚡")
        if want != cur and not s["finished"]:
            pl.set_mode(base, n, "intraday" if want else "after")
            st.rerun()
        if not cur or s["finished"]:
            if not s["finished"]:
                st.caption("盤後模式:每天收盤後(14:30 起)按左側「🔄 更新到最新交易日」,電腦會算好明天的買賣計畫,隔天開盤價成交。")
            return
        live_ok = (not fake) and pm.session_open()
        st.caption("盤中模式:這個帳戶在你按「立即執行」時用即時價交易;那天有執行,收盤後的更新只做計價、停損、到期。"
                   "**那天沒執行(電腦關著、忘了按)**,收盤更新時會照盤後模式產生隔日單,隔天開盤成交,所以不會整天空白,但那一天的盤中無法補做。"
                   "今天的量是用「目前成交量 ÷ 這個時間通常已成交的比例」推估的,開盤前半小時誤差較大,放量類訊號請保守看待。")
        if not live_ok:
            st.warning("現在不是盤中(平日 09:00–13:30)" + ("(假資料模式沒有即時報價)" if fake else "") + ",不能執行。")
        st.caption("有多個盤中帳戶時,頁面最上方有「⚡ 盤中一鍵執行」可以一次全部執行,不用一個一個按。")
        c1, c2 = st.columns(2)
        run = None
        if c1.button("👀 預覽(只看會買賣什麼,不成交)", key=f"idp_{pm.safe(n)}", disabled=not live_ok, use_container_width=True):
            run = False
        if c2.button("⚡ 立即執行(用即時價成交)", key=f"idx_{pm.safe(n)}", type="primary", disabled=not live_ok, use_container_width=True):
            run = True
        if run is not None:
            with st.spinner("抓即時報價、重算訊號…(約半分鐘)"):
                try:
                    res = pl.run_intraday(argparse.Namespace(**ns_common), n, execute=run)
                except Exception as e:                                   # noqa: BLE001
                    res = dict(ok=False, msg=f"{type(e).__name__}: {e}", events=[], skipped=[])
            res["at"] = pm.taipei_now().strftime("%H:%M:%S")
            res["executed"] = run
            st.session_state[f"idr_{pm.safe(n)}"] = res
            if run and res.get("ok"):
                st.rerun()
        res = st.session_state.get(f"idr_{pm.safe(n)}")
        if res:
            (st.success if res.get("ok") else st.error)(f"{res['at']}  {res['msg']}")
            for e in res.get("events", []):
                st.write(e)
            for e in res.get("skipped", []):
                st.write(f"⚪ 略過 {e}")
            if res.get("ok") and not res["events"]:
                st.write("⚪ 目前沒有符合條件的買賣")


def all_intraday_panel(base, ns_common, fake, bots):
    """一鍵對全部「盤中」模式的電腦帳戶做盤中判斷並成交(報價只抓一次、同一檔股票的訊號只算一次)。你自己的手動帳戶不在內。"""
    live_ok = (not fake) and pm.session_open()
    last = st.session_state.get("idr_all")
    with st.expander(f"⚡ 盤中一鍵執行:全部機器人({len(bots)} 個:{'、'.join(bots)})", expanded=bool(last) or live_ok):
        st.caption("對上面這些「盤中」模式、還沒結案的電腦帳戶,一次用即時報價重算訊號並立刻成交。盤後模式的帳戶不受影響;你自己的手動帳戶不在內,要自己下單。"
                   "報價只抓一次、同一檔股票的訊號只算一次,所以比一個一個按快很多。")
        if not live_ok:
            st.warning("現在不是盤中(平日 09:00–13:30)" + ("(假資料模式沒有即時報價)" if fake else "") + ",不能執行。")
        c1, c2 = st.columns(2)
        run = None
        if c1.button("👀 全部預覽(不成交)", key="idp_all", disabled=not live_ok, use_container_width=True):
            run = False
        if c2.button(f"⚡ 全部立即執行({len(bots)} 個帳戶)", key="idx_all", type="primary", disabled=not live_ok, use_container_width=True):
            run = True
        if run is not None:
            with st.spinner(f"抓即時報價、重算 {len(bots)} 個帳戶的訊號…(約半分鐘)"):
                try:
                    res = pl.run_intraday_all(argparse.Namespace(**ns_common), bots, execute=run)
                except Exception as e:                                   # noqa: BLE001
                    res = {n: dict(ok=False, msg=f"{type(e).__name__}: {e}", events=[], skipped=[], buys=[], sells=[]) for n in bots}
            st.session_state["idr_all"] = dict(res=res, executed=run, at=pm.taipei_now().strftime("%H:%M:%S"))
            for n in bots:                                               # 個別帳戶頁不要再顯示舊的單帳戶結果
                st.session_state.pop(f"idr_{pm.safe(n)}", None)
            if run and any(r.get("ok") for r in res.values()):
                st.rerun()
        last = st.session_state.get("idr_all")
        if last:
            st.markdown(f"**{last['at']} {'已成交' if last['executed'] else '預覽(沒有成交)'}**")
            st.dataframe(pd.DataFrame([{"帳戶": n, "結果": r["msg"], "買進": len(r.get("buys", [])), "賣出": len(r.get("sells", [])), "略過": len(r.get("skipped", []))}
                                       for n, r in last["res"].items()]), hide_index=True, use_container_width=True)
            for n, r in last["res"].items():
                if r.get("events") or r.get("skipped"):
                    with st.expander(f"{n}:{r['msg']}"):
                        for e in r.get("events", []):
                            st.write(e)
                        for e in r.get("skipped", []):
                            st.write(f"⚪ 略過 {e}")


def live_banner(note, at, text):
    """盤中 / 最後成交價的提示:標準風格用原本的說明文字,其他風格用閃爍標籤。"""
    if STYLE == "標準":
        st.caption(f"⚡ {note}({at} 更新):{text}")
    else:
        st.markdown(ui.live_note(note, at, text), unsafe_allow_html=True)


def show_orders(s):
    """🧾 買賣流水帳(電腦帳戶與手動帳戶共用)。"""
    o = orders_frame(s)
    if o.empty:
        st.info("還沒有任何成交。")
    else:
        st.dataframe(o, hide_index=True, use_container_width=True,
                     column_config={"股數": st.column_config.NumberColumn(format="%d"),
                                    "成交價": st.column_config.NumberColumn(format="%.2f"),
                                    "金額": st.column_config.NumberColumn(format="%d")})
        st.download_button("⬇ 下載流水帳 CSV", o.to_csv(index=False).encode("utf-8-sig"), f"{s['name']}_orders.csv", "text/csv")


def show_closed(s):
    """📊 已了結交易(電腦帳戶與手動帳戶共用)。"""
    t = trades_frame(s)
    if t.empty:
        st.info("還沒有已賣出的交易。")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("已了結筆數", f"{len(t)}")
        m2.metric("勝率", f"{(t['報酬率%'] > 0).mean() * 100:.0f}%")
        m3.metric("平均單筆報酬", f"{t['報酬率%'].mean():+.2f}%")
        m4.metric("已實現損益", f"{t['損益'].sum():+,.0f} 元")
        st.dataframe(_style(t, ["損益", "報酬率%"], {"買價": "{:.2f}", "賣價": "{:.2f}", "股數": "{:,.0f}", "損益": "{:+,.0f}", "報酬率%": "{:+.1f}"}),
                     hide_index=True, use_container_width=True)
        st.download_button("⬇ 下載已了結交易 CSV", t.to_csv(index=False).encode("utf-8-sig"), f"{s['name']}_trades.csv", "text/csv")


def show_account(s, base=None, ns_common=None, fake=False):
    a, nm = s["acct"], name_map(s)
    sm = summary(s)
    st.subheader(f"{s['name']}  ·  {s['profile']}")
    st.caption(f"⚙️ 使用參數:{s['profile']}  ·  {param_summary(s['params'])}")
    st.caption(f"起始日 {s['start_date']}  |  本金 {money(s['capital'])} 元  |  模擬 {s['target_days']} 個交易日  |  最新處理日 {sm['最新日']}")
    if not a["eq"]:
        st.info("這個帳戶還沒有處理過任何交易日,請按左側「更新到最新」。")
        return
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("帳戶總資產", money(sm["總資產"]) + " 元", f"{sm['損益']:+,.0f} 元({sm['報酬%']:+.2f}%)", delta_color="inverse")
    c2.metric("現金", money(sm["現金"]) + " 元")
    c3.metric("持股水位", f"{a['eq'][-1][2] * 100:.0f}%", f"{sm['持股檔數']} 檔")
    c4.metric("同期 0050", "—" if sm["0050 同期%"] is None else f"{sm['0050 同期%']:+.2f}%",
              None if sm["0050 同期%"] is None else f"帳戶{'領先' if sm['報酬%'] >= sm['0050 同期%'] else '落後'} {abs(sm['報酬%'] - sm['0050 同期%']):.2f} 個百分點",
              delta_color="off")
    c5.metric("進度", sm["進度"] + " 個交易日", "已結案" if s["finished"] else f"還剩 {s['target_days'] - s['day_no']} 天", delta_color="off")
    if s["finished"]:
        liq, _ = pl.liquidation_value(a)
        st.success(f"**結案**:帳面總資產 **{money(sm['總資產'])}** 元。若期末全部賣出(扣手續費、證交稅、滑價)約 **{money(liq)}** 元"
                   f"({(liq / s['capital'] - 1) * 100:+.2f}%)。")
    if s.get("live_at"):
        live_banner(s["live_note"], s["live_at"], "總資產、損益、持股現價、持股水位、同期 0050 和下方走勢最後一點,都用最新成交價計算。")
    if base is not None:
        intraday_panel(s, base, ns_common, fake)
    st.plotly_chart(equity_chart(s), use_container_width=True)

    last = a["log"][-1] if a["log"] else None
    cl, cr = st.columns(2)
    with cl:
        st.markdown(f"#### 📅 最新一個交易日的動作({last['date'] if last else '-'})")
        show_events(last["events"] if last else [])
    with cr:
        st.markdown("#### 🔮 明天的計畫")
        if s["finished"]:
            st.write("帳戶已結案,不再產生新的買賣。")
        elif s.get("mode") == "intraday":
            st.write("⚡ 盤中模式不產生隔日單。下一個交易日盤中,按上面的「立即執行」才會交易。")
        else:
            pf = plan_frame(s)
            if pf.empty:
                st.write("⚪ 明天沒有買賣計畫")
            else:
                for _, r in pf.iterrows():
                    wl = r["情境勝率下限%"]
                    extra = "" if wl is None or wl != wl else f"(情境勝率下限 {wl:.0f}%)"
                    st.write(f"{'🟢' if r['動作'] == '買進' else '🔴'} **{r['動作']}** {r['代碼']} {r['名稱']}:{r['依據']}{extra}")
            st.caption("以今天收盤後的訊號決定,下一個交易日開盤價成交。")

    t1, t2, t3, t4, t5 = st.tabs(["📅 每日動作", "📦 目前持股", "🧾 買賣流水帳", "📊 已了結交易", "⚙️ 規則"])
    with t1:
        e = eq_frame(s).set_index(eq_frame(s)["日期"].dt.strftime("%Y-%m-%d"))
        for L in reversed(a["log"]):
            nb, ns_ = day_counts(L["events"])
            row = e.loc[L["date"]] if L["date"] in e.index else None
            head = f"{L['date']}  ｜  買 {nb} 賣 {ns_}"
            if row is not None:
                head += f"  ｜  總資產 {money(row['總資產'])}({row['日損益']:+,.0f},{row['日報酬%']:+.2f}%)"
            with st.expander(head, expanded=(L is a["log"][-1])):
                show_events(L["events"])
    with t2:
        h = holdings_frame(s)
        if h.empty:
            st.info("目前空手。")
        else:
            st.dataframe(_style(h, ["損益%", "未實現損益"], {"股數": "{:,.0f}", "成本價": "{:.2f}", "現價": "{:.2f}", "損益%": "{:+.1f}",
                                                       "未實現損益": "{:+,.0f}", "市值": "{:,.0f}", "停損價": "{:.2f}"}),
                         hide_index=True, use_container_width=True)
            alloc = pd.concat([h[["名稱", "市值"]].rename(columns={"名稱": "項目"}),
                               pd.DataFrame([{"項目": "現金", "市值": sm["現金"]}])])
            fig = go.Figure(go.Pie(labels=alloc["項目"], values=alloc["市值"], hole=.5, sort=False,
                                   hovertemplate="%{label}<br>%{value:,.0f} 元(%{percent})<extra></extra>"))
            fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)
    with t3:
        show_orders(s)
    with t4:
        show_closed(s)
    with t5:
        P = s["params"]
        st.write(f"**參數組合**:{s['profile']}(開戶時固定,之後不會自動更換;要換參數請另開新帳戶)")
        st.write(f"- 買進訊號:{'、'.join(pl.SIGN.get(x, x) for x in sorted(P['buy']))}")
        st.write(f"- 有以下訊號就不買:{'、'.join(pl.SIGN.get(x, x) for x in sorted(P['block']))}")
        st.write(f"- 賣出訊號:{'、'.join(pl.SIGN.get(x, x) for x in sorted(P['exit']))}")
        st.write(f"- 買進門檻:情境勝率下限 ≥ {P['wl']}%、勝率優勢 ≥ {P['edge']}%、獨立樣本 ≥ {P['min_eff']}"
                 + ("、須站上季線" if P["above60"] else ""))
        st.write(f"- 停損 −{P['stop'] * 100:.0f}%;持有 {P['hold']} 個交易日到期賣出")
        st.write(f"- 最多 {P['max_pos']} 檔、每檔約總資產 {P['pct'] * 100:.0f}%;同產業最多 {P['ind_max']} 檔")
        cp = P.get("core_pct") or (0.5 if P.get("park") else 0.0)
        st.write(f"- 閒置資金:{('約 %.0f%% 放在核心 ETF %s,其餘留現金;個股要買、現金不夠時先賣核心' % (cp * 100, P.get('core_code', '0050'))) if cp else '放現金'}"
                 "(ETF 賣出證交稅 0.1%,核心部位差距超過總資產 3% 才調整)")
        st.write(f"- 成本:買進手續費 {pt.FEE * 100:.4f}%、賣出手續費 {pt.FEE * 100:.4f}% + 證交稅 {pt.TAX * 100:.1f}%、單邊滑價 {pt.SLIP * 100:.1f}%")
        st.caption("成交假設:收盤後出訊號,下一交易日開盤價(含滑價)成交;停損盤中觸價,取停損價與開盤價較低者;開盤接近漲停則買不到。")


def summary_manual(S):
    """手動帳戶的一列摘要,欄位和 summary() 一樣,才能放進同一張比較表。"""
    a = S["acct"]
    last = a["eq"][-1]
    b = (S["bench"][-1][1] / S["bench"][0][1] - 1) * 100 if len(S["bench"]) > 1 else None
    return {"帳戶": f"✍️ {S['name']}", "風險型": "手動操盤", "狀態": "已結案" if S["finished"] else "進行中", "參數": "自己決定(無固定參數)",
            "進度": f"{S['day_no']}/{S['target_days']}", "最新日": last[0] + (f" ⚡{S['live_at']}" if S.get("live_at") else ""), "總資產": last[1], "損益": last[1] - S["capital"],
            "報酬%": (last[1] / S["capital"] - 1) * 100, "0050 同期%": b, "持股檔數": len(a["pos"]), "現金": last[3]}


def show_compare(states, manual=None):
    manual = manual or {}
    if any(x.get("live_at") for x in list(states.values()) + list(manual.values())):
        _lv = next(x for x in list(states.values()) + list(manual.values()) if x.get("live_at"))
        live_banner(_lv["live_note"], _lv["live_at"], "下表的總資產、損益、報酬%、0050 同期%,以及走勢圖的最後一點,都用最新成交價計算(進度與「最新日」仍是正式處理到的那一天)。")
    rows = [summary(s) for s in states.values()] + [summary_manual(S) for S in manual.values()]
    df = pd.DataFrame(rows)
    if STYLE != "標準":
        st.markdown(ui.compare_panel(df), unsafe_allow_html=True)
    st.subheader("📊 全部帳戶比較")
    st.dataframe(_style(df, ["損益", "報酬%", "0050 同期%"], {"總資產": "{:,.0f}", "損益": "{:+,.0f}", "報酬%": "{:+.2f}", "0050 同期%": "{:+.2f}", "現金": "{:,.0f}"}),
                 hide_index=True, use_container_width=True)
    fig = go.Figure()
    for n, s in states.items():
        e = eq_frame(s)
        if len(e):
            fig.add_trace(go.Scatter(x=e["日期"], y=e["累計報酬%"], name=n, mode="lines"))
    for n, S in manual.items():                             # 手動帳戶:紅色粗線,一眼看出哪條是你
        e = eq_frame(S)
        if len(e):
            fig.add_trace(go.Scatter(x=e["日期"], y=e["累計報酬%"], name=f"✍️ {n}", mode="lines", line=dict(color=UP, width=3)))
    first = next((s for s in list(states.values()) + list(manual.values()) if len(s["bench"]) > 1), None)
    if first is not None:
        b = bench_frame(first)
        fig.add_trace(go.Scatter(x=b["日期"], y=(b["價"] / b["價"].iloc[0] - 1) * 100, name="0050", mode="lines", line=dict(color=GRAY, dash="dash")))
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified", yaxis_title="累計報酬(%)")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("各帳戶起始日可能不同,圖以各自第 0 天對齊日期顯示。")


# ================================================================== 參數檔、資料與快取(不依賴畫面的部分)
SIGS_B = sorted(x for x in pt.NEED_SIGS if x.startswith("B"))
SIGS_S = sorted(x for x in pt.NEED_SIGS if x.startswith("S"))
MODES = ["📒 模擬帳戶", "🧪 手動回測與調參", "🔍 自動優化", "⭐ 目前策略參數"]
STYLE_QUICK = {"短線": (5, 4.0), "波段": (20, 7.0), "中長線": (60, 12.0)}


ACTIVE_KEY = "⭐ 目前採用參數"


def sig_fmt(x):
    return f"{x} {pl.SIGN.get(x, '')}".strip()


def params_dir(base):
    return os.path.join(base, "params")


def safe_name(x):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", x).strip("_") or "params"


def save_params(base, label, P, extra=None):
    os.makedirs(params_dir(base), exist_ok=True)
    path = os.path.join(params_dir(base), safe_name(label) + ".json")
    j = dict(label=label, params=po.jsonable(P), saved=time.strftime("%Y-%m-%d %H:%M"))
    if extra:
        j.update(extra)
    json.dump(j, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=float)
    return path


def all_presets(base):
    """名稱 → 參數。內建三種風險型 + 資料夾內已存的參數檔 + 命令列優化器的輸出。"""
    out = {k: {kk: (set(v) if isinstance(v, set) else v) for kk, v in P.items()} for k, P in pt.PROFILES.items()}
    files = []
    d = params_dir(base)
    if os.path.isdir(d):
        files += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".json")]
    if os.path.exists(os.path.join("paper_opt", "best_params.json")):
        files.append(os.path.join("paper_opt", "best_params.json"))
    for f in files:
        try:
            _, P = pt.load_params(f)
            out["📁 " + os.path.splitext(os.path.basename(f))[0]] = P
        except Exception:                                   # noqa: BLE001
            pass
    a = sr.read_active()
    if a:
        out[ACTIVE_KEY] = a["P"]
    o = sr.find_optimized(bases=(base,))
    if o:                                                   # 最佳化器的輸出可能在別的資料夾,確保預設那組一定在選單裡
        out.setdefault("📁 " + os.path.splitext(os.path.basename(o["path"]))[0], o["P"])
    return out


def best_optimized(base):
    """預設該用的參數(與 app 用同一套規則):⭐手動指定 > 最佳化(優先驗證通過者,取最新)> None(用內建預設)。
    回傳 dict(key, kind, label, P, passed, verdict) 或 None。"""
    r = sr.resolve_params(bases=(base,))
    if r["kind"] == "default":
        return None
    key = ACTIVE_KEY if r["kind"] == "active" else "📁 " + os.path.splitext(os.path.basename(r["path"]))[0]
    return dict(r, key=key)


def load_uni(base, fake, force=False, data=None):
    if fake:
        return pt.synthetic()[0]
    f = os.path.join(base, "universe.csv")
    if os.path.exists(f) and not force:
        return pd.read_csv(f, dtype=str).fillna("")
    try:
        uni = pt.load_universe()
        os.makedirs(base, exist_ok=True)
        uni.to_csv(f, index=False, encoding="utf-8-sig")
        return uni
    except Exception:                                       # noqa: BLE001
        if data is None:
            raise
        return pd.DataFrame([dict(stock_id=t.split(".")[0], stock_name=t.split(".")[0], industry_category="", ticker=t)
                             for t in data if t != "0050.TW"])


def filter_uni(uni, codes_text):
    codes = [c for c in re.split(r"[,\s]+", codes_text or "") if c]
    return uni[uni.stock_id.isin(codes)] if codes else uni


def cache_path(base):
    return os.path.join(base, "prices.pkl")


@st.cache_resource(show_spinner=False)
def cached_prices(path, stamp):
    return pickle.load(open(path, "rb"))


@st.cache_resource(show_spinner=False)
def cached_synth():
    _, data = pt.synthetic()
    for c, src in zip(("0050", "0056"), list(data)[:2]):
        data[f"{c}.TW"] = data[src].copy()
    return data


@st.cache_resource(show_spinner=False)
def app_ns(path):
    return pt.load_app_funcs(path)


def get_data(base, fake):
    if fake:
        return cached_synth()
    path = cache_path(base)
    return cached_prices(path, os.path.getmtime(path)) if os.path.exists(path) else None


def data_stamp(base, fake):
    return "fake" if fake else (os.path.getmtime(cache_path(base)) if os.path.exists(cache_path(base)) else None)


@st.cache_resource(show_spinner="計算指標與訊號中(第一次較久,之後很快)…")
def prepared(stamp, ref_iso, n, codes, fake, base, app):
    data = get_data(base, fake)
    uni = pt.with_etfs(filter_uni(load_uni(base, fake, data=data), codes))      # ETF 不受「只用這些代碼」限制,也不佔股票數名額
    return pt.select_and_prepare(uni, data, pd.Timestamp(ref_iso), int(n), app_ns(app))


# ================================================================== 回測頁的資料整理(不依賴畫面,可單獨測試)
def norm_params(P):
    return sr.norm_params(P)


def load_params_into_widgets(P):
    P = norm_params(P)
    ss = st.session_state
    ss["bt_buy"] = [x for x in SIGS_B if x in P["buy"]]
    ss["bt_block"] = [x for x in SIGS_S if x in P["block"]]
    ss["bt_exit"] = [x for x in SIGS_S if x in P["exit"]]
    ss["bt_wl"], ss["bt_edge"] = float(P["wl"]), float(P["edge"])
    ss["bt_min_eff"], ss["bt_ind_max"] = int(P["min_eff"]), int(P["ind_max"])
    ss["bt_stop"], ss["bt_hold"] = float(round(P["stop"] * 100, 2)), int(P["hold"])
    ss["bt_max_pos"] = int(P["max_pos"])
    ss["bt_pct"] = float(round(P["pct"] * 100, 2))
    ss["bt_pct_auto"] = abs(P["pct"] - round(0.95 / P["max_pos"], 3)) < 1e-9
    ss["bt_above60"] = bool(P["above60"])
    ss["bt_core_pct"], ss["bt_core_code"] = int(round(P["core_pct"] * 100)), P["core_code"]


def params_from_widgets():
    ss = st.session_state
    mp = int(ss["bt_max_pos"])
    return dict(buy=set(ss["bt_buy"]), block=set(ss["bt_block"]), exit=set(ss["bt_exit"]), wl=float(ss["bt_wl"]), edge=float(ss["bt_edge"]),
                min_eff=int(ss["bt_min_eff"]), stop=float(ss["bt_stop"]) / 100, hold=int(ss["bt_hold"]), max_pos=mp,
                pct=round(0.95 / mp, 3) if ss["bt_pct_auto"] else float(ss["bt_pct"]) / 100, ind_max=int(ss["bt_ind_max"]),
                above60=bool(ss["bt_above60"]), core_pct=int(ss["bt_core_pct"]) / 100, core_code=ss["bt_core_code"],
                park=int(ss["bt_core_pct"]) > 0)


def param_summary(P):
    return sr.param_text(sr.norm_params(P))


def execute_backtest(stocks, bench, P, start, end, cap):
    W = po.make_window(stocks, bench, pd.Timestamp(start), pd.Timestamp(end))
    if len(W["cal"]) < 20:
        raise ValueError("這段期間的交易日太少(或資料不足),請調整日期")
    e, tr, info = pt.run_profile("bt", P, stocks, W["cal"], 0, cap, W["mult"], W["maps"])
    b = bench["Adj Close"].reindex(W["cal"]).ffill().bfill()
    return dict(e=e, tr=tr, m=pt.metrics(e, tr, cap), W=W, bench=cap * b / b.iloc[0], P=P, cap=cap,
                start=str(W["cal"][0].date()), end=str(W["cal"][-1].date()), open_positions=info["open_positions"])


def reason_table(tr):
    if tr is None or not len(tr):
        return pd.DataFrame()
    t = tr.copy()
    t["出場方式"] = t["原因"].where(~t["原因"].str.startswith("賣出訊號"), "賣出訊號")
    g = t.groupby("出場方式").agg(筆數=("報酬率", "size"), 勝率=("報酬率", lambda x: (x > 0).mean() * 100), 平均報酬=("報酬率", lambda x: x.mean() * 100),
                                 損益合計=("損益", "sum"), 平均持有天=("持有天", "mean")).reset_index()
    return g.rename(columns={"勝率": "勝率%", "平均報酬": "平均報酬%"}).sort_values("筆數", ascending=False)


def record_run(name, r):
    cap = r["cap"]
    return dict(name=name, start=r["start"], end=r["end"], ret=r["m"]["總報酬%"], bench_ret=r["W"]["bench_ret"] * 100,
                mdd=r["m"]["最大回撤%"], bench_mdd=r["W"]["bench_mdd"] * 100, trades=r["m"]["交易次數"],
                expo=r["m"]["平均持股水位%"], summary=param_summary(r["P"]),
                curve=(r["e"]["equity"] / cap - 1) * 100, bench_curve=(r["bench"] / cap - 1) * 100)


def runs_frame(runs):
    return pd.DataFrame([{"名稱": x["name"], "期間": f"{x['start']}~{x['end']}", "報酬%": x["ret"], "0050 同期%": x["bench_ret"],
                          "超額%": x["ret"] - x["bench_ret"], "最大回撤%": x["mdd"], "0050 回撤%": x["bench_mdd"], "交易數": x["trades"],
                          "持股水位%": x["expo"], "參數": x["summary"]} for x in runs])


def top_frame(res):
    rows = []
    for n, x in enumerate(res["top"], 1):
        P, r, v = x["P"], x["tr"], x["te"]
        rows.append({"#": n, "風格": po.LABEL[P["style"]], "持有日": P["hold"], "停損%": P["stop"] * 100, "最多檔": P["max_pos"],
                     "勝率下限%": P["wl"], "優勢%": P["edge"], "買進訊號": "+".join(sorted(P["buy"])), "賣出訊號": "+".join(sorted(P["exit"])) or "無",
                     "核心ETF": f"{P['core_code']} {P['core_pct'] * 100:.0f}%" if P["core_pct"] else "無", "訓練報酬%": r["ret"] * 100, "訓練超額%": r["excess"] * 100,
                     "驗證報酬%": v["ret"] * 100, "驗證超額%": v["excess"] * 100, "驗證回撤%": v["mdd"] * 100, "驗證交易數": v["trades"],
                     "判定": "✅ 不輸 0050" if x["pass"] else "❌ 輸給 0050"})
    return pd.DataFrame(rows)


# ================================================================== 資料面板
def common_ns(base, fake):
    return dict(dir=base, app=os.path.join(HERE, "app.py"), asof=None, synthetic=fake)


def data_panel(base, fake):
    """回傳 (stocks, bench);資料還沒準備好時回傳 None。"""
    ss = st.session_state
    ss.setdefault("dp_codes", "")
    ss.setdefault("dp_max", 300)
    ss.setdefault("dp_ref", pd.Timestamp("2023-01-02").date())
    data = get_data(base, fake)
    with st.expander("📦 資料與股票池" + ("" if data is not None else "(尚未下載,請先下載)"), expanded=data is None):
        c1, c2, c3 = st.columns(3)
        c1.text_input("只用這些代碼(逗號分隔;留空=全市場)", key="dp_codes", help="例如 2330,2317,2454。想先快速試跑,可以只填幾十檔")
        c2.number_input("納入股票數(依成交值排序)", min_value=20, max_value=1500, step=20, key="dp_max", help="越多越慢;300 檔通常夠用")
        c3.date_input("股票池基準日", key="dp_ref", help="以這天之前一年的成交值挑出前 N 檔。改這個會重新計算指標(較久)")
        if fake:
            st.caption("🧪 假資料模式:數字沒有任何意義,只用來測試介面。")
        else:
            if data is not None:
                b = data.get("0050.TW")
                st.caption(f"已下載 {len(data) - ('0050.TW' in data)} 檔,資料到 {b.index[-1].date() if b is not None else '?'}。快取檔:{os.path.abspath(cache_path(base))}")
            st.caption("⚠️ 第一次下載全市場需要 10 分鐘以上,之後只補最近一個月。快取與命令列版(paper_live.py)共用。")
            if st.button("📥 下載 / 更新股價資料", key="dp_dl"):
                with st.spinner("下載中…請不要關閉頁面"):
                    try:
                        uni = load_uni(base, fake, data=data)
                        tick = list(filter_uni(uni, ss["dp_codes"]).ticker) + list(pt.etf_rows().ticker)
                        ok, text, _ = quiet(pl.refresh, base, tick)
                    except Exception as e:              # noqa: BLE001
                        ok, text = False, f"{type(e).__name__}: {e}"
                cached_prices.clear()
                prepared.clear()
                ss["dp_log"] = (ok, text)
                st.rerun()
            if ss.get("dp_log"):
                ok, text = ss["dp_log"]
                (st.success if ok else st.error)("上次下載完成" if ok else "上次下載有問題")
                st.code(text[-2500:] or "(無輸出)")
    if data is None:
        st.info("還沒有股價資料。請展開上方「資料與股票池」按「下載 / 更新股價資料」。")
        return None
    bench = data.get("0050.TW")
    if bench is None:
        st.error("資料裡沒有 0050,無法比較。請重新下載。")
        return None
    stocks = prepared(data_stamp(base, fake), str(pd.Timestamp(ss["dp_ref"]).date()), int(ss["dp_max"]), ss["dp_codes"], fake, base,
                      os.path.join(HERE, "app.py"))
    if not stocks:
        st.error("股票池是空的(基準日前的資料不足 300 天,或代碼填錯)。")
        return None
    st.caption(f"目前股票池 {len(stocks)} 檔,基準日 {ss['dp_ref']}。")
    return stocks, bench


# ================================================================== 手動回測與調參頁
def cb_preset(base):
    name = st.session_state.get("bt_preset")
    P = all_presets(base).get(name)
    if P:
        load_params_into_widgets(P)


def cb_style(hold, stop):
    st.session_state["bt_hold"], st.session_state["bt_stop"] = int(hold), float(stop)


def cb_apply(P):
    load_params_into_widgets(P)
    st.session_state["paper_mode"] = MODES[1]


def param_widgets(base):
    ss = st.session_state
    if "bt_hold" not in ss:
        load_params_into_widgets(pt.PROFILES["穩健型"])
    st.selectbox("載入預設 / 已存參數(選了會把參數填進下面)", list(all_presets(base)), index=None, key="bt_preset",
                 on_change=cb_preset, args=(base,), placeholder="選擇一組參數…")
    q = st.columns(4)
    q[0].caption("快速設定持有天數與停損:")
    for col, (nm, (h, sp)) in zip(q[1:], STYLE_QUICK.items()):
        col.button(f"{nm}({h}日/停損{sp:.0f}%)", key=f"bt_q_{nm}", on_click=cb_style, args=(h, sp), use_container_width=True)
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**持有與風控**")
        st.number_input("持有天數(到期賣出;同時決定歷史勝率的統計天期)", min_value=3, max_value=120, step=1, key="bt_hold")
        st.number_input("停損 %(盤中跌破就賣)", min_value=1.0, max_value=40.0, step=0.5, key="bt_stop")
        st.number_input("最多同時持有幾檔", min_value=1, max_value=40, step=1, key="bt_max_pos")
        st.checkbox("每檔比重自動(95% ÷ 檔數)", key="bt_pct_auto")
        st.number_input("每檔占總資產 %", min_value=1.0, max_value=100.0, step=1.0, key="bt_pct", disabled=bool(ss["bt_pct_auto"]))
        st.number_input("同產業最多幾檔(99=不限)", min_value=1, max_value=99, step=1, key="bt_ind_max")
    with c2:
        st.markdown("**進場門檻(歷史情境勝率)**")
        st.number_input("勝率下限 ≥ %(0=不限制)", min_value=0.0, max_value=100.0, step=1.0, key="bt_wl",
                        help="Wilson 信賴下限,比單純勝率嚴格。太高會幾乎不交易")
        st.number_input("勝率優勢 ≥ %(−100=不限制)", min_value=-100.0, max_value=50.0, step=1.0, key="bt_edge", help="該情境勝率減去全部樣本基準勝率")
        st.number_input("最少獨立樣本數", min_value=1, max_value=50, step=1, key="bt_min_eff")
        st.checkbox("只買站上季線(60 日線)的股票", key="bt_above60")
        st.slider("核心 ETF 占閒置資金 %(0=全放現金)", 0, int(pt.CORE_MAX * 100), step=5, key="bt_core_pct",
                  help="閒置資金 = 總資產 − 持有/明天要買的個股。只有這個比例會放在核心 ETF,其餘留現金;個股要買、現金不夠時會先賣一部分核心。上限 80%,不會 all-in")
        st.selectbox("核心 ETF", list(pt.ETF_LIST), key="bt_core_code", format_func=lambda c: f"{c} {pt.ETF_LIST[c]}", disabled=int(ss["bt_core_pct"]) == 0)
    with c3:
        st.markdown("**買賣訊號**")
        st.multiselect("買進訊號(任一出現)", SIGS_B, format_func=sig_fmt, key="bt_buy")
        st.multiselect("有這些賣出訊號就不買", SIGS_S, format_func=sig_fmt, key="bt_block")
        st.multiselect("持股遇到這些訊號就賣(隔日開盤)", SIGS_S, format_func=sig_fmt, key="bt_exit")


def equity_compare_chart(r, height=340):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=r["e"].index, y=r["e"]["equity"], name="策略", line=dict(color=BLUE, width=3),
                             hovertemplate="%{x|%Y-%m-%d}<br>策略 %{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=r["bench"].index, y=r["bench"].values, name="0050 買進持有", line=dict(color=GRAY, width=2, dash="dash"),
                             hovertemplate="%{x|%Y-%m-%d}<br>0050 %{y:,.0f}<extra></extra>"))
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified", legend=dict(orientation="h", y=1.1), yaxis_title="資產(元)")
    return fig


def drawdown_chart(r, height=220):
    e, b = r["e"]["equity"], r["bench"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=e.index, y=(e / e.cummax() - 1) * 100, name="策略回撤", fill="tozeroy", line=dict(color=DN, width=1)))
    fig.add_trace(go.Scatter(x=b.index, y=(b / b.cummax() - 1) * 100, name="0050 回撤", line=dict(color=GRAY, dash="dash")))
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified", yaxis_title="回撤 %", legend=dict(orientation="h", y=1.15))
    return fig


def show_result(r, tag):
    m, W, cap = r["m"], r["W"], r["cap"]
    bret, bmdd = W["bench_ret"] * 100, W["bench_mdd"] * 100
    ex = m["總報酬%"] - bret
    yrs = max((r["e"].index[-1] - r["e"].index[0]).days / 365.25, 0.01)
    bann = ((1 + W["bench_ret"]) ** (1 / yrs) - 1) * 100
    c = st.columns(6)
    c[0].metric("期末資產", money(m["期末資產"]), f"{m['期末資產'] - cap:+,.0f}", delta_color="inverse")
    c[1].metric("總報酬", f"{m['總報酬%']:+.1f}%", f"比 0050 {'多' if ex >= 0 else '少'} {abs(ex):.1f} 個百分點", delta_color="inverse")
    c[2].metric("年化報酬", f"{m['年化報酬%']:+.1f}%", f"0050 {bann:+.1f}%", delta_color="off")
    c[3].metric("最大回撤", f"{m['最大回撤%']:.1f}%", f"0050 {bmdd:.1f}%", delta_color="off")
    c[4].metric("交易筆數", f"{m['交易次數']}", f"勝率 {m.get('勝率%', 0):.0f}%", delta_color="off")
    c[5].metric("平均持股水位", f"{m['平均持股水位%']:.0f}%", f"期末持有 {r['open_positions']} 檔", delta_color="off")
    (st.success if ex >= 0 else st.warning)(
        f"{r['start']} ~ {r['end']}:策略 {m['總報酬%']:+.1f}%,0050 買進持有 {bret:+.1f}%,"
        f"{'不輸 0050' if ex >= 0 else '輸給 0050'}({ex:+.1f} 個百分點);回撤 {m['最大回撤%']:.1f}% vs 0050 {bmdd:.1f}%。")
    if m["交易次數"] < 30:
        st.info(f"只有 {m['交易次數']} 筆交易,樣本太少,結果很大程度靠運氣,不具代表性。門檻可能太嚴。")
    st.plotly_chart(equity_compare_chart(r), use_container_width=True)
    st.plotly_chart(drawdown_chart(r), use_container_width=True)
    t1, t2 = st.tabs(["🧾 交易明細", "📊 依出場方式統計"])
    with t1:
        tr = r["tr"]
        if tr is None or not len(tr):
            st.info("這段期間沒有任何交易。")
        else:
            show = tr.copy()
            show["報酬率%"] = show["報酬率"] * 100
            show = show.drop(columns=["報酬率"]).iloc[::-1].reset_index(drop=True)
            st.dataframe(_style(show, ["損益", "報酬率%"], {"買價": "{:.2f}", "賣價": "{:.2f}", "股數": "{:,.0f}", "損益": "{:+,.0f}", "報酬率%": "{:+.1f}"}),
                         hide_index=True, use_container_width=True)
            st.download_button("⬇ 下載交易明細 CSV", show.to_csv(index=False).encode("utf-8-sig"), f"backtest_{tag}.csv", "text/csv", key=f"dl_{tag}")
    with t2:
        g = reason_table(r["tr"])
        if g.empty:
            st.info("沒有資料")
        else:
            st.dataframe(_style(g, ["損益合計", "平均報酬%"], {"勝率%": "{:.0f}", "平均報酬%": "{:+.2f}", "損益合計": "{:+,.0f}", "平均持有天": "{:.1f}"}),
                         hide_index=True, use_container_width=True)
            st.caption("停損太多代表進場點偏弱或停損太緊;持有到期占多數代表主要靠時間換報酬。")


def page_backtest(base, fake):
    ss = st.session_state
    st.header("🧪 回測與參數調整")
    st.caption("調整參數 → 執行回測 → 和 0050 對照。**建議只用「調整期間」的成績來調參,調好後再看一次「檢驗期間」**:"
               "檢驗期間沒參與調參,才看得出參數是真的有效還是只是配合過去。")
    got = data_panel(base, fake)
    if got is None:
        return
    stocks, bench = got
    with st.expander("⚙️ 策略參數", expanded=True):
        param_widgets(base)
    c1, c2, c3 = st.columns(3)
    ss.setdefault("bt_s", pd.Timestamp("2023-01-02").date())
    ss.setdefault("bt_e", pd.Timestamp("2025-03-31").date())
    ss.setdefault("bt_oos", True)
    ss.setdefault("bt_s2", pd.Timestamp("2025-04-01").date())
    ss.setdefault("bt_e2", pd.Timestamp.today().date())
    ss.setdefault("bt_cap", 1_000_000)
    c1.date_input("調整期間:開始", key="bt_s")
    c1.date_input("調整期間:結束", key="bt_e")
    c2.checkbox("同時用「檢驗期間」驗證(建議開)", key="bt_oos")
    c2.date_input("檢驗期間:開始", key="bt_s2", disabled=not ss["bt_oos"])
    c2.date_input("檢驗期間:結束", key="bt_e2", disabled=not ss["bt_oos"])
    c3.number_input("本金(元)", min_value=100_000, max_value=100_000_000, step=100_000, key="bt_cap")
    c3.text_input("這次回測的名稱(方便比較;留空自動編號)", key="bt_name")
    if pd.Timestamp(ss["bt_s"]) < pd.Timestamp(ss["dp_ref"]):
        st.caption("⚠️ 回測起始日早於股票池基準日:股票池選自較晚時間,結果可能偏樂觀。")
    if st.button("▶ 執行回測", type="primary", key="bt_run"):
        P = params_from_widgets()
        if not P["buy"]:
            st.error("請至少選一個買進訊號")
        elif ss["bt_oos"] and not (pd.Timestamp(ss["bt_s"]) < pd.Timestamp(ss["bt_e"]) < pd.Timestamp(ss["bt_s2"]) < pd.Timestamp(ss["bt_e2"])):
            st.error("日期需為:調整期間開始 < 調整期間結束 < 檢驗期間開始 < 檢驗期間結束")
        elif not pd.Timestamp(ss["bt_s"]) < pd.Timestamp(ss["bt_e"]):
            st.error("開始日必須早於結束日")
        else:
            try:
                with st.spinner("回測中…(第一次會建立快取,較久;之後同一股票池很快)"):
                    r1 = execute_backtest(stocks, bench, P, ss["bt_s"], ss["bt_e"], float(ss["bt_cap"]))
                    r2 = execute_backtest(stocks, bench, P, ss["bt_s2"], ss["bt_e2"], float(ss["bt_cap"])) if ss["bt_oos"] else None
            except Exception as e:                          # noqa: BLE001
                st.error(f"回測失敗:{type(e).__name__}: {e}")
            else:
                runs = ss.setdefault("bt_runs", [])
                nm = (ss.get("bt_name") or "").strip() or f"回測#{len(runs) // (2 if r2 else 1) + 1}"
                runs[:] = [x for x in runs if x["name"] not in (nm, nm + " (調整)", nm + " (檢驗)")]
                runs.append(record_run(nm + (" (調整)" if r2 else ""), r1))
                if r2:
                    runs.append(record_run(nm + " (檢驗)", r2))
                del runs[:-16]
                key = (ss["bt_s"], ss["bt_e"])
                cnt = ss.setdefault("bt_count", {})
                cnt[key] = cnt.get(key, 0) + 1
                ss["bt_last"] = dict(main=r1, oos=r2, P=P, name=nm, count=cnt[key])
    last = ss.get("bt_last")
    if not last:
        st.info("設定好參數後按「▶ 執行回測」。")
        return
    if last["count"] >= 6:
        st.warning(f"你已經在同一段調整期間反覆調了 {last['count']} 次。調得越多,越可能只是在配合這段歷史(過度擬合)。"
                   "請看「檢驗期間」的成績:如果調整期間很好、檢驗期間很差,就是過度擬合的典型徵兆。")
    st.markdown(f"#### 結果:{last['name']}  ·  {param_summary(last['P'])}")
    if last["oos"]:
        t1, t2 = st.tabs(["調整期間", "🔍 檢驗期間(沒參與調參)"])
        with t1:
            show_result(last["main"], "main")
        with t2:
            show_result(last["oos"], "oos")
            a, b = last["main"]["m"]["總報酬%"] - last["main"]["W"]["bench_ret"] * 100, last["oos"]["m"]["總報酬%"] - last["oos"]["W"]["bench_ret"] * 100
            st.info(f"調整期間比 0050 {a:+.1f} 個百分點 → 檢驗期間比 0050 {b:+.1f} 個百分點。"
                    + ("兩段都不輸,但仍只是過去的表現。" if a >= 0 and b >= 0 else "檢驗期間輸給 0050,不建議直接採用。" if b < 0 else ""))
    else:
        show_result(last["main"], "main")

    runs = ss.get("bt_runs", [])
    if len(runs) >= 2:
        st.markdown("#### 📊 多次回測並排比較")
        rf = runs_frame(runs)
        st.dataframe(_style(rf, ["報酬%", "超額%"], {"報酬%": "{:+.1f}", "0050 同期%": "{:+.1f}", "超額%": "{:+.1f}", "最大回撤%": "{:.1f}", "0050 回撤%": "{:.1f}", "持股水位%": "{:.0f}"}),
                     hide_index=True, use_container_width=True)
        fig = go.Figure()
        for x in runs:
            fig.add_trace(go.Scatter(x=x["curve"].index, y=x["curve"].values, name=x["name"], mode="lines"))
        fig.add_trace(go.Scatter(x=runs[-1]["bench_curve"].index, y=runs[-1]["bench_curve"].values, name="0050(最近一次期間)", line=dict(color=GRAY, dash="dash")))
        fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified", yaxis_title="累計報酬 %")
        st.plotly_chart(fig, use_container_width=True)
        st.button("🗑 清除比較紀錄", key="bt_clear", on_click=lambda: st.session_state.update(bt_runs=[], bt_count={}))

    with st.expander("💾 存成參數檔 / 🚀 用這組參數開前向模擬帳戶"):
        P = last["P"]
        s1, s2 = st.columns(2)
        with s1:
            st.text_input("參數檔名稱", key="bt_save_name")
            if st.button("💾 存成參數檔", key="bt_save"):
                nm = (ss.get("bt_save_name") or "").strip() or last["name"]
                path = save_params(base, nm, P, extra=dict(note=param_summary(P)))
                st.success(f"已存:{path}。之後可在「載入預設/已存參數」選到,也能給 paper_live.py init --params 使用。")
        with s2:
            st.text_input("帳戶名稱", key="bt_live_name")
            st.number_input("模擬幾個交易日後結案", min_value=5, max_value=250, value=20, step=5, key="bt_live_days")
            st.caption("開戶會用最新資料,以最近一個收盤日的訊號決定第一批買進。")
            if st.button("🚀 開戶並開始前向模擬", key="bt_live"):
                nm = (ss.get("bt_live_name") or "").strip() or last["name"]
                path = save_params(base, nm, P, extra=dict(note=param_summary(P)))
                a = argparse.Namespace(**common_ns(base, fake), profile="穩健型", params=path, capital=float(ss["bt_cap"]), days=int(ss["bt_live_days"]),
                                       name=safe_name(nm), max_stocks=int(ss["dp_max"]), codes=ss["dp_codes"], force=False, wl=None, edge=None,
                                       min_eff=None, stop=None, max_pos=None, pct=None, hold=None, park0050=False)
                with st.spinner("開戶中…"):
                    ok, text, _ = quiet(pl.cmd_init, a)
                (st.success if ok else st.error)("已開戶!到左側「📒 模擬帳戶」查看" if ok else "開戶失敗")
                st.code(text[-2500:])


# ================================================================== 自動優化頁
def page_optimizer(base, fake):
    ss = st.session_state
    st.header("🔍 自動優化")
    st.caption("程式在「訓練段」隨機搜尋大量參數組合並排名,再把前幾名拿到「驗證段」(完全沒參與搜尋的行情)檢驗是否真的不輸 0050。"
               "**判定只看驗證段**;如果沒有任何一組通過,會明確告訴你「沒有找到」,不會硬湊。")
    got = data_panel(base, fake)
    if got is None:
        return
    stocks, bench = got
    ss.setdefault("opt_style", "swing")
    ss.setdefault("opt_trials", 60)
    ss.setdefault("opt_top", 8)
    ss.setdefault("opt_min", 30)
    ss.setdefault("opt_seed", 1)
    ss.setdefault("opt_core_max", 65)
    ss.setdefault("opt_tr0", pd.Timestamp("2023-01-02").date())
    ss.setdefault("opt_tr1", pd.Timestamp("2025-03-31").date())
    ss.setdefault("opt_te0", pd.Timestamp("2025-04-01").date())
    ss.setdefault("opt_te1", pd.Timestamp.today().date())
    c = st.columns(4)
    c[0].selectbox("風格(持有天數)", ["short", "swing", "long", "all"], key="opt_style",
                   format_func=lambda x: {"short": "短線(持有 5~10 日)", "swing": "波段(持有 20 日)", "long": "中長線(持有 40~60 日)", "all": "三種都搜尋"}[x])
    c[1].number_input("搜尋幾組參數", min_value=10, max_value=1000, step=10, key="opt_trials", help="越多越久;第一輪要建立快取較慢")
    c[2].number_input("前幾名拿去驗證", min_value=1, max_value=30, step=1, key="opt_top")
    c[3].number_input("訓練段至少幾筆交易才算數", min_value=5, max_value=500, step=5, key="opt_min", help="避免『幾乎不交易=跟 0050 一樣』的假好成績")
    st.slider("核心 ETF 占閒置資金的上限 %(0 = 不使用核心部位)", 0, int(pt.CORE_MAX * 100), step=5, key="opt_core_max",
              help="搜尋時會在 0~此上限之間挑核心比例,並從 0050/006208/0056/00878 挑核心標的。其餘閒置資金留現金供個股訊號使用,不會 all-in。"
                   "ETF 也會和個股一起被訊號選為交易標的(受『同產業最多幾檔』限制)。")
    d = st.columns(4)
    d[0].date_input("訓練段:開始", key="opt_tr0")
    d[1].date_input("訓練段:結束", key="opt_tr1")
    d[2].date_input("驗證段:開始", key="opt_te0")
    d[3].date_input("驗證段:結束", key="opt_te1")
    if st.button("🔍 開始搜尋", type="primary", key="opt_run"):
        tr0, tr1, te0, te1 = (pd.Timestamp(ss[k]) for k in ("opt_tr0", "opt_tr1", "opt_te0", "opt_te1"))
        if not tr0 < tr1 < te0 < te1:
            st.error("日期需為:訓練開始 < 訓練結束 < 驗證開始 < 驗證結束")
        else:
            styles = list(po.STYLES) if ss["opt_style"] == "all" else [ss["opt_style"]]
            bar = st.progress(0.0, text="準備中…")
            t0 = time.time()

            def prog(k, n):
                bar.progress(k / n, text=f"搜尋中 {k}/{n}  已用 {time.time() - t0:.0f} 秒")
            try:
                res = po.run_search(stocks, bench, tr0, tr1, te0, te1, styles, int(ss["opt_trials"]), int(ss["opt_top"]), int(ss["opt_min"]),
                                    int(ss["opt_seed"]), 1_000_000, prog, core_max=int(ss["opt_core_max"]) / 100)
            except Exception as e:                          # noqa: BLE001
                res = None
                st.error(f"搜尋失敗:{type(e).__name__}: {e}")
            bar.empty()
            if res is not None and not res["top"]:
                st.error(f"沒有任何參數在訓練段達到 {int(ss['opt_min'])} 筆交易。請降低「至少幾筆交易」或增加搜尋組數。")
            elif res is not None:
                ss["opt_res"] = res
                ss["opt_pick"] = 1
    res = ss.get("opt_res")
    if not res:
        st.info("設定好後按「🔍 開始搜尋」。")
        return
    Wtr, Wte, top = res["Wtr"], res["Wte"], res["top"]
    m = st.columns(4)
    m[0].metric("訓練段 0050", f"{Wtr['bench_ret'] * 100:+.1f}%", f"回撤 {Wtr['bench_mdd'] * 100:.1f}%", delta_color="off")
    m[1].metric("驗證段 0050", f"{Wte['bench_ret'] * 100:+.1f}%", f"回撤 {Wte['bench_mdd'] * 100:.1f}%", delta_color="off")
    m[2].metric("搜尋組數", f"{len(res['rows'])}", f"達標(夠多交易) {sum(x['ok'] for x in res['rows'])} 組", delta_color="off")
    m[3].metric("驗證段不輸 0050", f"{len(res['passed'])} / {len(top)}", "前幾名中", delta_color="off")
    if res["passed"]:
        st.success(f"訓練段前 {len(top)} 名中有 {len(res['passed'])} 組,在沒看過的驗證段也不輸 0050。驗證段只是一段行情,通過不代表未來也贏,"
                   "建議再用前向模擬幾個月確認。")
    else:
        st.error("沒有找到在驗證段不輸 0050 的參數。意思是這些訊號搭配這類參數,在沒看過的行情裡贏不了 0050。"
                 "這是有價值的結論:不要為了讓數字好看而放寬條件或縮短驗證段;可提高「核心 ETF 比例上限」,讓訊號只當加碼,或把目標改成降低回撤。")
    tf = top_frame(res)
    st.dataframe(_style(tf, ["訓練報酬%", "訓練超額%", "驗證報酬%", "驗證超額%"],
                        {"停損%": "{:.0f}", "勝率下限%": "{:.0f}", "優勢%": "{:.0f}", "訓練報酬%": "{:+.1f}", "訓練超額%": "{:+.1f}", "驗證報酬%": "{:+.1f}",
                         "驗證超額%": "{:+.1f}", "驗證回撤%": "{:.1f}"}), hide_index=True, use_container_width=True)
    st.caption("排名依「訓練段超額報酬」;驗證段欄位只用來檢驗,不參與排名。")
    st.markdown("#### 對選定的一組參數")
    st.number_input("第幾名", min_value=1, max_value=len(top), step=1, key="opt_pick")
    x = top[int(min(max(ss.get("opt_pick", 1), 1), len(top))) - 1]
    st.write(f"**{param_summary(x['P'])}**  ·  買進訊號 {'、'.join(pl.SIGN.get(b, b) for b in sorted(x['P']['buy']))}")
    b1, b2, b3 = st.columns(3)
    b1.button("➡ 套用到「手動回測與調參」頁", key="opt_apply", on_click=cb_apply, args=(x["P"],), help="把這組參數填進回測頁,你可以再手動微調並看完整回測")
    if b2.button("💾 存成參數檔", key="opt_save"):
        label = f"優化_{po.LABEL[x['P']['style']]}_{time.strftime('%m%d_%H%M')}_第{int(ss.get('opt_pick', 1))}名"
        path = save_params(base, label, x["P"], extra=po.candidate_json(x, res["windows"], Wtr, Wte))
        st.success(f"已存:{path}")
    if b3.button("⭐ 設為目前採用", key="opt_active", help="app 單股頁的策略判斷、以及新開模擬帳戶的預設,都會改用這組參數"):
        info = po.candidate_json(x, res["windows"], Wtr, Wte)
        path = sr.write_active(x["P"], f"優化_{po.LABEL[x['P']['style']]}_第{int(ss.get('opt_pick', 1))}名", extra=info)
        st.success(f"已設為目前採用:{path}。" + ("" if x["pass"] else "觸發條件僅供參考"))
    act = sr.read_active()
    if act:
        st.caption(f"目前採用:{act['label']}({act['verdict']});{sr.param_text(act['P'])}")
    rf = po.recs_frame(res["rows"])
    fig = go.Figure()
    for flag, color, nm in ((True, BLUE, "有核心 ETF"), (False, UP, "閒置全放現金")):
        sub = rf[((rf["核心比例%"] > 0) == flag) & rf["有效"]]
        fig.add_trace(go.Scatter(x=sub["訓練交易數"], y=sub["訓練超額%"], mode="markers", name=nm, marker=dict(color=color, size=8, opacity=.7),
                                 text=sub["風格"] + " 持有" + sub["持有日"].astype(str) + "日", hovertemplate="%{text}<br>交易 %{x} 筆<br>超額 %{y:+.1f}%<extra></extra>"))
    fig.add_hline(y=0, line_dash="dot", line_color="rgba(128,128,128,.6)", annotation_text="= 0050")
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), xaxis_title="訓練段交易筆數", yaxis_title="訓練段超額報酬(相對 0050,%)")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("每個點是一組參數。點都在 0 以下,代表整個搜尋空間在訓練段就贏不了 0050。")
    with st.expander("📋 所有嘗試的參數與成績"):
        st.dataframe(rf, hide_index=True, use_container_width=True)
        st.download_button("⬇ 下載 CSV", rf.to_csv(index=False).encode("utf-8-sig"), "optimizer_trials.csv", "text/csv", key="opt_dl")


# ================================================================== 模擬帳戶頁(原本的首頁)
def param_files(base):
    """已存在的參數檔(新的在前):<資料夾>/params/*.json、paper_opt/best_params.json、最佳化器輸出。回傳路徑清單。"""
    files, seen = [], set()

    def add(p):
        ap = os.path.abspath(p)
        if ap not in seen and os.path.isfile(ap):
            seen.add(ap)
            files.append(p)

    d = params_dir(base)
    if os.path.isdir(d):
        names = [f for f in os.listdir(d) if f.endswith(".json")]
        for f in sorted(names, key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True):
            add(os.path.join(d, f))
    add(os.path.join("paper_opt", "best_params.json"))
    try:
        o = sr.find_optimized(bases=(base,))
        if o:
            add(o["path"])
    except Exception:                                       # noqa: BLE001
        pass
    return files


def read_param_file(path):
    """讀一個參數檔 → (標籤, 正規化後的參數, 其餘欄位(驗證結果等,設為採用時一併帶過去), 存檔時間)。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    label, P = pt.load_params(path)
    label = raw.get("label") or label or os.path.splitext(os.path.basename(path))[0]
    extra = {k: v for k, v in raw.items() if k not in ("label", "params", "saved")}
    return label, norm_params(P), extra, raw.get("saved", "")


def page_active(base, fake):
    st.subheader("⭐ 目前策略參數")
    st.caption("從已存在的參數檔挑一組設為「目前採用參數」。app.py 單股頁的「🧭 策略判斷(依目前採用參數)」、全市場掃描的買入策略、"
               "以及這裡新開模擬帳戶的預設參數,都會改用這組。優先順序:⭐ 手動指定 > 最新最佳化 > 內建預設。")
    flash = st.session_state.pop("active_flash", None)
    if flash:
        st.success(flash)
    R = sr.resolve_params(bases=(base,))
    head = {"active": "⭐ 你手動指定的參數", "optimized": "🔍 最新的最佳化參數(尚未手動指定)", "default": "📦 內建預設(尚未做最佳化)"}[R["kind"]]
    (st.warning if R["passed"] is False else st.info)(f"**目前生效:{head}**:{R['label']}\n\n{sr.param_text(R['P'])}\n\n驗證結果:{R['verdict']}")

    files = param_files(base)
    if not files:
        st.info(f"找不到參數檔。到「🧪 手動回測與調參」或「🔍 自動優化」按「💾 存成參數檔」(會存在 {params_dir(base)})後,就能在這裡挑選。")
        return

    def fmt(p):
        try:
            label, _, _, saved = read_param_file(p)
            return f"{label}" + (f"  ·  {saved}" if saved else "") + f"  ({os.path.basename(p)})"
        except Exception:                                   # noqa: BLE001
            return f"{os.path.basename(p)}(讀取失敗)"

    path = st.selectbox("選擇參數檔", files, format_func=fmt, key="act_file")
    try:
        label, P, extra, saved = read_param_file(path)
    except Exception as e:                                  # noqa: BLE001
        st.error(f"這個參數檔讀不了:{e}")
        return
    st.markdown("#### 這組參數的內容")
    st.write(f"**{param_summary(P)}**")
    st.write("買進訊號:" + ("、".join(sig_fmt(b) for b in sorted(P["buy"])) or "—")
             + "　|　擋買訊號:" + ("、".join(sig_fmt(b) for b in sorted(P["block"])) or "—")
             + "　|　賣出訊號:" + ("、".join(sig_fmt(b) for b in sorted(P["exit"])) or "—"))
    st.caption(f"檔案:{os.path.abspath(path)}" + (f"　存檔時間:{saved}" if saved else ""))
    if st.button("⭐ 設為目前採用", type="primary", key="act_set", help="寫入「目前採用參數」;app 重新整理或下次操作時就會套用"):
        try:
            out = sr.write_active(P, label, extra=extra)
        except Exception as e:                              # noqa: BLE001
            st.error(f"設定失敗:{e}")
        else:
            st.session_state["active_flash"] = f"已設為目前採用:{label}(寫入 {out})。回到 app 單股頁重新整理即可看到「策略判斷」改用這組參數。"
            st.rerun()
    if R["kind"] == "active" and R["label"] == label:
        st.caption("✔ 這組就是目前採用的參數。")


def find_account_files(base, name, state=None):
    """電腦帳戶的存檔 = <資料夾>/<帳戶名>/state.json(paper_live.spath)。只動這一個檔案,資料夾裡的 csv 匯出檔留著。"""
    p = pl.spath(base, name)
    return [p] if os.path.exists(p) else []


def delete_accounts(base, names, states=None):
    """刪除帳戶:不是真的 rm,而是把存檔改名為 *.deleted-時間戳,之後可手動改回來還原。
    刪完會用 pl.list_accounts 確認帳戶真的消失,沒消失就自動還原並回報失敗。回傳 [(帳戶, 成功?, 說明)]。"""
    stamp, out = time.strftime("%Y%m%d_%H%M%S"), []
    for n in names:
        files = find_account_files(base, n, (states or {}).get(n))
        if not files:
            out.append((n, False, "找不到這個帳戶的存檔,未刪除"))
            continue
        moved = []
        try:
            for f in files:
                os.replace(f, f + f".deleted-{stamp}")
                moved.append(f)
            still = n in pl.list_accounts(base)
        except Exception as e:                              # noqa: BLE001
            still, err = True, f"{type(e).__name__}: {e}"
        else:
            err = "刪除後帳戶仍在清單中(存檔位置可能與預期不同)"
        if still:
            for f in moved:
                if os.path.exists(f + f".deleted-{stamp}"):
                    os.replace(f + f".deleted-{stamp}", f)
            out.append((n, False, err + ",已還原,未刪除"))
        else:
            out.append((n, True, f"已刪除({len(moved)} 個檔案改名為 .deleted-{stamp},需要時可改回原檔名還原)"))
    return out


def delete_panel(base, names, states, manual_names=()):
    """側邊欄「刪除帳戶」:可複選要刪哪些帳戶(電腦帳戶、✍️ 手動帳戶都可),列出會動到的檔案,勾選確認後才執行。"""
    with st.sidebar.expander("🗑 刪除帳戶"):
        options = list(names) + [f"✍️ {m}" for m in manual_names]
        pick = st.multiselect("選擇要刪除的帳戶", options, key="del_pick", placeholder="選擇一個或多個帳戶…")
        if pick:
            for n in pick:
                if n.startswith("✍️ "):
                    S = pm.load(base, n[3:])
                    sm = manual_summary(S)
                    st.caption(f"**{n}**:{sm['狀態']},{sm['進度']} 日,總資產 {sm['總資產']:,.0f}({sm['報酬%']:+.2f}%)")
                    st.caption(f"　檔案:{os.path.relpath(pm.path(base, n[3:]))}")
                    continue
                sm = summary(states[n])
                st.caption(f"**{n}**:{sm['狀態']},{sm['進度']} 日,總資產 {sm['總資產']:,.0f}({sm['報酬%']:+.2f}%)")
                for f in find_account_files(base, n, states[n]):
                    st.caption(f"　檔案:{os.path.relpath(f)}")
            st.warning("刪除後該帳戶的買賣紀錄與資產曲線不會再顯示。檔案只是改名備份,不是永久刪除。"
                       "刪除手動帳戶不會連帶刪掉它的電腦對手帳戶,要的話請一併選取。")
        ok = st.checkbox("我確定要刪除上面選的帳戶", key="del_ok", disabled=not pick)
        if st.button("刪除選取的帳戶", key="del_go", type="primary", disabled=not (pick and ok), use_container_width=True):
            res = delete_accounts(base, [n for n in pick if not n.startswith("✍️ ")], states)
            for n in pick:
                if n.startswith("✍️ "):
                    res.append((n, *pm.delete(base, n[3:])))
            st.session_state["del_log"] = res
            st.session_state.pop("run_log", None)
            for k in ("del_pick", "del_ok"):
                st.session_state.pop(k, None)
            st.rerun()
        for n, ok_, msg in st.session_state.pop("del_log", []):
            (st.success if ok_ else st.error)(f"{n}:{msg}")


# ================================================================== 手動操盤
def manual_summary(S):
    A = S["acct"]
    eq = A["eq"][-1][1]
    return {"帳戶": S["name"], "狀態": "已結案" if S["finished"] else "進行中", "進度": f"{S['day_no']}/{S['target_days']}",
            "總資產": eq, "報酬%": (eq / S["capital"] - 1) * 100}


def manual_universe(base, fake, data):
    """手動操盤可選的全部標的:全部上市櫃股票 + 全部 ETF(含 00878、006208、00679B 這類 5 碼以上的),不管股價有沒有下載過。"""
    uni = load_uni(base, fake, data=data)
    etf = pt.etf_rows() if fake else pt.etf_universe(base)       # 電腦帳戶開戶與這裡共用同一份 etf_universe.csv
    return pt.with_etfs(pd.concat([uni, etf[~etf.stock_id.isin(uni.stock_id)]], ignore_index=True))


def manual_data(base, fake):
    """(價格快取, {代碼: (名稱, ticker)})。標的 = 全部上市櫃股票 + 全部 ETF;ticker 的 .TW/.TWO 若和快取裡的不同,以快取為準。"""
    data = get_data(base, fake)
    if data is None:
        return None, {}
    info = {}
    for r in manual_universe(base, fake, data).to_dict("records"):
        tk = r["ticker"]
        if tk not in data:
            alt = tk[:-4] + ".TW" if tk.endswith(".TWO") else tk[:-3] + ".TWO" if tk.endswith(".TW") else tk
            tk = alt if alt in data else tk
        info[r["stock_id"]] = (r["stock_name"], tk)
    for t in data:                                          # 快取裡有、清單裡沒有的(例如下架或特別股)也讓你選得到
        info.setdefault(t.split(".")[0], (t.split(".")[0], t))
    return data, info


@st.cache_data(ttl=5, show_spinner=False)
def live_quotes(codes):
    """證交所即時報價,快取 5 秒避免一直重打(codes 要是 tuple)。"""
    return pm.fetch_quotes(codes)


def fetch_stock(base, tk):
    """下載單一標的近 2 年股價並合併進快取;.TW / .TWO 會兩個都試。回傳 (成功的 ticker 或 None, 錯誤說明)。"""
    alts = [tk] + ([tk[:-3] + ".TWO"] if tk.endswith(".TW") else [tk[:-4] + ".TW"] if tk.endswith(".TWO") else [])
    err = ""
    for t in alts:
        try:
            if pm.refresh_tickers(cache_path(base), [t], period="2y"):
                cached_prices.clear()
                return t, ""
        except Exception as e:                              # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
    return None, err or "Yahoo 沒有這檔的股價資料(剛上市、已下市或代碼不支援)"


def sync_manual(base, fake, names):
    """把手動帳戶推進到快取裡最新的交易日(昨天下的單在第一個新交易日開盤成交)。"""
    data = get_data(base, fake)
    if data is None:
        return 0
    k = 0
    for n in names:
        S = pm.load(base, n)
        d = pm.advance(S, data)
        if d:
            pm.save(base, S)
            k += d
    return k


def refresh_manual(base, names):
    """只更新手動帳戶用得到的幾檔(持股、委託、0050)並合併進快取。回傳 (成功?, 說明)。"""
    tick = set()
    for n in names:
        tick |= pm.needed_tickers(pm.load(base, n))
    try:
        k = pm.refresh_tickers(cache_path(base), tick)
        cached_prices.clear()
        return True, f"手動帳戶:已更新 {k}/{len(tick)} 檔股價(持股、委託單與 0050)。"
    except Exception as e:                                  # noqa: BLE001
        return False, f"手動帳戶更新股價失敗:{type(e).__name__}: {e}"


def race_table(S, states, picks):
    """人機對決:把手動帳戶、挑選的電腦帳戶、0050 都換成「自手動帳戶起點算起的報酬%」,回傳 (曲線 dict, 成績表, 比到哪天)。"""
    start, end = pd.Timestamp(S["start_date"]), pd.Timestamp(S["last_date"])
    curves, rows = {}, []

    def add(label, eq, trades=None):
        e = pm.eq_series(eq)
        e = e[(e.index >= start) & (e.index <= end)]
        if len(e) < 1:
            return
        curves[label] = (e.equity / e.equity.iloc[0] - 1) * 100
        row = {"參賽者": label, "報酬%": float(curves[label].iloc[-1]), "最大回撤%": float((e.equity / e.equity.cummax() - 1).min() * 100)}
        if trades is not None:
            tr = [t for t in trades if t["賣出日"] >= S["start_date"]]
            row.update({"平均持股水位%": float(e.exposure.mean() * 100), "已了結交易": len(tr),
                        "勝率%": float(sum(t["報酬率"] > 0 for t in tr) / len(tr) * 100) if tr else None, "資料到": str(e.index[-1].date())})
        rows.append(row)

    add(f"✍️ {S['name']}(你)", S["acct"]["eq"], S["acct"]["trades"])
    for n in picks:
        add(f"🤖 {n}", states[n]["acct"]["eq"], states[n]["acct"]["trades"])
    b = pd.DataFrame(S["bench"], columns=["date", "p"])
    b["date"] = pd.to_datetime(b["date"])
    b = b.set_index("date").p
    curves["0050 買進持有"] = (b / b.iloc[0] - 1) * 100
    rows.append({"參賽者": "0050 買進持有", "報酬%": float(curves["0050 買進持有"].iloc[-1]),
                 "最大回撤%": float((b / b.cummax() - 1).min() * 100)})
    return curves, pd.DataFrame(rows), end


def show_race(S, states):
    comp = list(states)
    if not comp:
        st.info("目前沒有電腦帳戶可以比。開戶時勾選「同時開電腦對手帳戶」,或另外開一個電腦自動帳戶。")
        picks = []
    else:
        dft = [S["opponent"]] if S.get("opponent") in states else comp[:1]
        picks = st.multiselect("要跟哪些電腦帳戶比", comp, default=dft, key=f"race_{pm.safe(S['name'])}")
    curves, T, end = race_table(S, states, picks)
    me = T.iloc[0]
    st.markdown("#### " + ("🏁 比賽結束" if S["finished"] else "比賽進行中") + f"(比到 {end.date()},第 {S['day_no']}/{S['target_days']} 個交易日)")
    for _, r in T.iloc[1:].iterrows():
        d = me["報酬%"] - r["報酬%"]
        nm = r["參賽者"]
        extra = ""
        if nm.startswith("🤖") and r.get("資料到") and r["資料到"] < str(end.date()):
            extra = f"(電腦帳戶資料只到 {r['資料到']},請按左側「更新到最新交易日」)"
        st.write(f"- 對 **{nm}**:你 {me['報酬%']:+.2f}% vs {r['報酬%']:+.2f}% → " + (f"🏆 領先 {d:.2f} 個百分點" if d > 0 else f"落後 {-d:.2f} 個百分點" if d < 0 else "平手") + extra)
    fig = go.Figure()
    for k, v in curves.items():
        style = dict(color=UP, width=3) if k.startswith("✍️") else dict(color=GRAY, dash="dash") if k.startswith("0050") else dict(color=BLUE)
        fig.add_trace(go.Scatter(x=v.index, y=v.values, name=k, mode="lines", line=style, hovertemplate="%{x|%Y-%m-%d}<br>" + k + " %{y:+.2f}%<extra></extra>"))
    fig.add_hline(y=0, line_dash="dot", line_color="rgba(128,128,128,.6)")
    fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="報酬 %(自你的起點算起)", hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(T, hide_index=True, use_container_width=True,
                 column_config={c: st.column_config.NumberColumn(format="%.2f") for c in ("報酬%", "最大回撤%", "平均持股水位%", "勝率%")})
    st.caption("電腦帳戶有最多檔數、持有天數上限與固定資金配置;手動帳戶沒有這些限制,但成交價、手續費、稅、滑價、漲停買不到都一樣。"
               "比賽期間很短,贏或輸都不代表長期能力。")


def show_manual(base, fake, name, states, lq=None, bench_close=None):
    S = pm.load(base, name)                                    # 原始資料:下單、存檔都用它
    data, info = manual_data(base, fake)
    V = live_view(S, lq, bench_close)                          # 即時版:只拿來顯示(總資產、報酬、持股現價、對決),不存檔
    A, VA, key = S["acct"], V["acct"], pm.safe(name)
    eq = VA["eq"][-1][1]
    ret = (eq / S["capital"] - 1) * 100
    bret = (V["bench"][-1][1] / V["bench"][0][1] - 1) * 100 if len(V["bench"]) > 1 else 0.0
    st.subheader(f"✍️ {S['name']}(手動操盤)" + ("　🏁 已結案" if S["finished"] else ""))
    m = st.columns(5)
    m[0].metric("總資產", f"{eq:,.0f}", f"{eq - S['capital']:+,.0f} 元", delta_color="inverse")        # 台股慣例:賺 = 紅
    m[1].metric("報酬", f"{ret:+.2f}%", f"比 0050 {'多' if ret >= bret else '少'} {abs(ret - bret):.2f} 個百分點", delta_color="inverse")
    m[2].metric("現金", f"{A['cash']:,.0f}", f"持股水位 {VA['eq'][-1][2] * 100:.0f}%", delta_color="off")
    m[3].metric("進度", f"{S['day_no']}/{S['target_days']} 日", f"資料到 {S['last_date']}", delta_color="off")
    opp = S.get("opponent")
    if opp in states:
        _, T, _ = race_table(V, states, [opp])
        d = T.iloc[0]["報酬%"] - T.iloc[1]["報酬%"]
        m[4].metric("電腦對手", f"{T.iloc[1]['報酬%']:+.2f}%", f"你{'領先' if d >= 0 else '落後'} {abs(d):.2f} 個百分點", delta_color="inverse")
    else:
        m[4].metric("電腦對手", "—", "沒有指定對手", delta_color="off")
    live_on = (not fake) and pm.session_open() and not S["finished"]
    if V.get("live_at"):
        live_banner(V["live_note"], V["live_at"], "總資產、報酬、持股現價、同期 0050、電腦對手比較都用最新成交價計算。")
    t1, t2, t3, t3b, t4, t5 = st.tabs(["📝 下單(盤中/盤後)", "📦 目前持股", "🧾 買賣流水帳", "📊 已了結交易", "🏁 人機對決", "⚙️ 規則"])
    with t1:
        if S["finished"]:
            st.success(f"🏁 已結案({S.get('finished_at')}):最終報酬 {ret:+.2f}%,同期 0050 {bret:+.2f}%。到「🏁 人機對決」看和電腦的比較。")
        elif data is None:
            st.error("還沒有股價資料,請先到「🔍 自動優化」或「🧪 手動回測」頁的「資料與股票池」下載。")
        else:
            omode = st.radio("下單方式", ["🌙 隔日單(下一個開盤價成交)", "⚡ 盤中即時成交(現價,立刻成交)"], index=1 if live_on else 0, horizontal=True,
                             key=f"mn_ord_{key}", help="隔日單:任何時間都能下;平日 09:00 前下 → 今天開盤價成交,09:00 之後或假日下 → 下一個交易日開盤價成交。\n"
                                                       "即時單:只有平日 09:00–13:30 能用,以證交所即時報價(現價)加 0.1% 滑價立刻成交。")
            rt = omode.startswith("⚡")
            due_n = 0 if fake else sum(o.get("after", "") < str(pm.taipei_now().date()) for o in A["orders"])
            if rt and not live_on:
                st.warning("現在不是盤中(平日 09:00–13:30)" + ("(假資料模式沒有即時報價)" if fake else "") + ",即時成交不能用,請改用隔日單。")
            elif rt:
                st.info("⚡ 盤中即時:以證交所即時報價成交,成交後立刻反映在持股與現金。"
                        + (f"你有 {due_n} 張今天開盤就該成交的隔日單,下即時單時會先用今天的實際開盤價補成交。" if due_n else ""))
            else:
                st.info(f"資料到 **{S['last_date']}** 收盤。隔日單會在**下一個開盤價**成交(含 0.1% 滑價與手續費)。"
                        "建議收盤後(14:30 以後)先按左側「🔄 更新到最新交易日」,看完最新行情再下單;不下單 = 空手或續抱。")
            if st.session_state.get("mn_msg"):
                msg = st.session_state.pop("mn_msg")
                (st.error if msg.startswith("下載失敗") else st.success)(msg)
            if A["orders"]:
                today_s = str(pm.taipei_now().date())
                od = pd.DataFrame([{"動作": "買進" if o["side"] == "buy" else "賣出", "代碼": o["code"], "名稱": o["name"],
                                    "成交時機": "—" if fake else ("今天開盤價" if o.get("after", "") < today_s else "下一個交易日開盤價"),
                                    "內容": f"{o['amount']:,.0f} 元" + (f",停損 {o['stop_pct']:.1f}%" if o["stop_pct"] else ",不設停損") if o["side"] == "buy"
                                    else ("全部賣出" if o["sh"] is None else f"{o['sh']:,} 股")} for o in A["orders"]])
                st.dataframe(od, hide_index=True, use_container_width=True)
                need = sum(o["amount"] for o in A["orders"] if o["side"] == "buy")
                if need > A["cash"]:
                    st.warning(f"買進單合計 {need:,.0f} 元超過目前現金 {A['cash']:,.0f} 元(賣出單成交後的錢明天開盤不一定來得及用)。現金不夠的買單會縮小或略過。")
                labels = [f"{i + 1}. {r['動作']} {r['代碼']} {r['名稱']}" for i, r in od.iterrows()]
                cx = st.multiselect("取消哪幾張單", labels, key=f"mn_cx_{key}")
                if st.button("取消選取的單", key=f"mn_cxgo_{key}", disabled=not cx):
                    pm.cancel_orders(S, [labels.index(x) for x in cx])
                    pm.save(base, S)
                    st.session_state.pop(f"mn_cx_{key}", None)
                    st.rerun()
            else:
                st.caption("目前沒有委託單。")
            cb, cs = st.columns(2)
            with cb:
                st.markdown("**買進**")
                codes = sorted(info)
                pick = st.selectbox(f"股票 / ETF(全部上市櫃共 {len(codes)} 檔,可輸入代碼或名稱搜尋)", codes, index=None, key=f"mn_stock_{key}",
                                    placeholder="選擇股票…", format_func=lambda c: f"{c} {info[c][0]}" + ("" if info[c][1] in data else "(尚未下載股價)"))
                have = bool(pick) and info[pick][1] in data and len(data[info[pick][1]]) > 21
                if pick and not have:
                    st.info(f"{pick} {info[pick][0]} 的股價還沒下載到快取(或資料太少)。按下面的按鈕只下載這一檔,約幾秒鐘。")
                    if st.button(f"📥 下載 {pick} 股價", key=f"mn_dl_{key}"):
                        with st.spinner("下載中…"):
                            got, err = fetch_stock(base, info[pick][1])
                        if got:
                            st.session_state["mn_msg"] = f"已下載 {pick} {info[pick][0]} 的股價。"
                        else:
                            st.session_state["mn_msg"] = f"下載失敗:{err}"
                        st.rerun()
                if have:
                    br = pm.stock_brief(data[info[pick][1]])
                    px = br["close"]
                    f = lambda v: "—" if v is None else f"{v:+.1f}%"                         # noqa: E731
                    st.caption(f"{br['date']} 收盤 **{px:.2f}**(今日 {f(br['d1'])}・近5日 {f(br['d5'])}・近20日 {f(br['d20'])}),近20日均成交值 {br['amt20']:,.0f} 百萬")
                    h = data[info[pick][1]].tail(120)
                    fg = go.Figure(go.Scatter(x=h.index, y=h.Close, mode="lines", line=dict(color=BLUE)))
                    fg.update_layout(height=170, margin=dict(l=0, r=0, t=0, b=0), xaxis_title=None, yaxis_title=None)
                    st.plotly_chart(fg, use_container_width=True)
                q_now = None
                if rt and live_on and pick:
                    q_now = live_quotes(tuple(pm.live_codes(S, [pick]))).get(pick)
                    if not q_now or not q_now.get("volume"):
                        st.warning(f"{pick} 目前沒有即時成交報價(還沒開盤成交、暫停交易或報價抓不到),不能即時成交。")
                    else:
                        ch = (q_now["price"] / q_now["prev"] - 1) * 100 if q_now.get("prev") else 0.0
                        st.success(f"⚡ {pick} 即時價 **{q_now['price']:.2f}**({ch:+.2f}%)・開 {q_now['open']:.2f} 高 {q_now['high']:.2f} 低 {q_now['low']:.2f}"
                                   f"・量 {q_now['volume'] // 1000:,} 張・{q_now['time']}")
                    if st.button("🔄 更新報價", key=f"mn_rq_{key}"):
                        live_quotes.clear()
                        st.rerun()
                amt = st.number_input("買進金額(元)", 1000.0, float(max(eq * 2, 10000)), float(round(eq * 0.1, -3)), 10000.0, key=f"mn_amt_{key}")
                stp = st.number_input("停損 %(0 = 不設停損)", 0.0, 40.0, 7.0, 0.5, key=f"mn_stp_{key}", help="盤中跌破買進開盤價的這個百分比就賣")
                if rt and q_now and q_now.get("volume"):
                    st.caption(f"以即時價估算約可買 {int(amt // (q_now['price'] * (1 + pt.FEE))):,} 股。")
                elif have and not rt:
                    st.caption(f"以最新收盤估算約可買 {int(amt // (px * (1 + pt.FEE))):,} 股(實際依下一個開盤價)。")
                if st.button("⚡ 以即時價買進" if rt else "➕ 加入買進單", key=f"mn_buy_{key}", disabled=not (bool(q_now and q_now.get("volume")) if rt else have)):
                    try:
                        if rt:
                            qs = pm.fetch_quotes(pm.live_codes(S, [pick]))                           # 按下去那一刻重新抓,不用 5 秒前的快取
                            st.session_state["mn_msg"] = pm.trade_now(S, qs, "buy", pick, info[pick][0], info[pick][1], amount=amt, stop_pct=stp)
                            live_quotes.clear()
                        else:
                            after = S["last_date"] if fake else pm.order_boundary()
                            st.session_state["mn_msg"] = "已加入隔日單:" + pm.add_order(S, "buy", pick, info[pick][0], info[pick][1], amount=amt, stop_pct=stp, after=after)
                        pm.save(base, S)
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
            with cs:
                st.markdown("**賣出**")
                held = list(A["pos"])
                sp = st.selectbox("持股", held, index=None, key=f"mn_sell_{key}", placeholder="選擇要賣的持股…" if held else "目前沒有持股",
                                  format_func=lambda c: f"{c} {A['pos'][c]['name']}({A['pos'][c]['sh']:,} 股)", disabled=not held)
                mode = st.radio("賣出數量", ["全部", "部分"], horizontal=True, key=f"mn_mode_{key}", disabled=not sp)
                shs = None
                if sp and mode == "部分":
                    shs = st.number_input("股數", 1, int(A["pos"][sp]["sh"]), max(int(A["pos"][sp]["sh"]) // 2, 1), 100, key=f"mn_sh_{key}")
                if rt and live_on and sp:
                    q_s = live_quotes(tuple(pm.live_codes(S, [sp]))).get(sp)
                    if q_s and q_s.get("volume"):
                        st.caption(f"⚡ {sp} 即時價 **{q_s['price']:.2f}**・{q_s['time']}(成本 {A['pos'][sp]['ep']:.2f},約 {(q_s['price'] / A['pos'][sp]['ep'] - 1) * 100:+.1f}%)")
                    else:
                        q_s = None
                        st.warning(f"{sp} 目前沒有即時成交報價,不能即時賣出。")
                else:
                    q_s = None
                if st.button("⚡ 以即時價賣出" if rt else "➕ 加入賣出單", key=f"mn_sellgo_{key}", disabled=not (bool(q_s) if rt else bool(sp))):
                    try:
                        if rt:
                            qs = pm.fetch_quotes(pm.live_codes(S, [sp]))
                            st.session_state["mn_msg"] = pm.trade_now(S, qs, "sell", sp, A["pos"][sp]["name"], A["pos"][sp]["tk"], sh=shs)
                            live_quotes.clear()
                        else:
                            after = S["last_date"] if fake else pm.order_boundary()
                            st.session_state["mn_msg"] = "已加入隔日單:" + pm.add_order(S, "sell", sp, A["pos"][sp]["name"], A["pos"][sp]["tk"], sh=shs, after=after)
                        pm.save(base, S)
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
    with t2:
        rows = []
        if V.get("live_at"):
            st.caption("⚡ 「現價」用證交所最新成交價(沒有成交報價的用上一個收盤價)。")
        for c, p in A["pos"].items():
            px = float(VA["last_close"].get(c, p["ep"]))
            rows.append({"代碼": c, "名稱": p["name"], "股數": p["sh"], "成本價": p["ep"], "現價": px, "損益%": (px * p["sh"] / p["cost"] - 1) * 100,
                         "未實現損益": p["sh"] * px - p["cost"], "市值": p["sh"] * px, "持有日": p["n"], "停損價": p["stop"] or None})
        if rows:
            st.dataframe(_style(pd.DataFrame(rows), ["損益%", "未實現損益"], {"股數": "{:,.0f}", "成本價": "{:.2f}", "現價": "{:.2f}", "損益%": "{:+.1f}",
                                                                           "未實現損益": "{:+,.0f}", "市值": "{:,.0f}", "停損價": "{:.2f}"}),
                         hide_index=True, use_container_width=True)
        else:
            st.info("目前空手,全部是現金。")
    with t3:
        show_orders(S)
    with t3b:
        show_closed(S)
    with t4:
        show_race(V, states)
    with t5:
        st.write(f"- 本金 {S['capital']:,.0f} 元,起點 {S['start_date']},共 {S['target_days']} 個交易日,到期自動結案(持股以最後收盤價計價,不強制賣出)。")
        st.write("- 兩種下單:🌙 隔日單(平日 09:00 前下 → 今天開盤價;其餘時間 → 下一個交易日開盤價)與 ⚡ 盤中即時(平日 09:00–13:30,證交所即時價立刻成交)。"
                 "下即時單時,今天開盤就該成交的隔日單會先用今天的實際開盤價補成交。盤中新買的部位,當天不會被當天的最低價停損(最低價可能發生在買進之前)。")
        st.write("- 買進 +0.1% 滑價、賣出 −0.1% 滑價;手續費 "
                 f"{pt.FEE * 100:.4f}%;賣出證交稅 {pt.TAX * 100:.1f}%(ETF {pt.TAX_ETF * 100:.1f}%)。")
        st.write("- 開盤接近漲停(≥ 前收 ×1.095)買不到;現金連預定金額的一半都不夠就略過;停損盤中觸價,成交取停損價與開盤價較低者。")
        st.write("- 現金股利依還原股價反推,除息日入帳。可交易標的 = 股票池 + ETF 裡有股價快取的那些。")
        st.write(f"- 電腦對手:{opp if opp else '無'}。")


def page_accounts(base, fake):
    names = pl.list_accounts(base)
    manual_names = pm.list_manual(base)
    ns_common = common_ns(base, fake)
    if st.sidebar.button("🔄 更新到最新交易日", type="primary", use_container_width=True, disabled=not (names or manual_names),
                         help="下載最新行情,把還沒處理的已收盤交易日全部補上(第一次可能要幾分鐘)"):
        ok, text = True, ""
        with st.spinner("更新中…下載行情與計算訊號,請稍候"):
            if names:
                ok, text = call_quiet(pl.run_accounts, argparse.Namespace(**ns_common), names)
            if manual_names and not fake:
                ok2, text2 = refresh_manual(base, manual_names)
                ok, text = ok and ok2, (text + "\n" if text else "") + text2
        st.session_state["run_log"] = (ok, text)
        st.rerun()
    if st.session_state.get("run_log"):
        ok, text = st.session_state["run_log"]
        with st.sidebar.expander("上次更新的訊息" + ("" if ok else "(有錯誤)"), expanded=not ok):
            st.code(text[-4000:] or "(無輸出)")
    st.sidebar.caption("建議台北時間 14:30 收盤後更新。漏掉的交易日會自動補上。")
    auto_secs = st.sidebar.selectbox("⏱ 自動更新股價(盤中)", [0, 5, 10, 15, 30, 60], key="auto_secs", format_func=lambda v: "關閉" if v == 0 else f"每 {v} 秒",
                                     help="只在平日 09:00–13:30 有作用:持股即時價、盤中估值、你選的股票報價會定時更新。收盤後或假日不會重跑。"
                                          "間隔最短 5 秒(報價有 5 秒快取)。你一操作畫面就會中斷等待、立刻處理,不會被擋住。")
    if manual_names:
        sync_manual(base, fake, manual_names)

    with st.sidebar.expander("➕ 開新帳戶", expanded=not (names or manual_names)):
        kind = st.radio("帳戶類型", ["🤖 電腦自動", "✍️ 手動操盤(和電腦比賽)"], key="new_kind",
                        help="手動操盤:你每天自己決定買哪些股票,用和電腦帳戶相同的成交規則,最後比誰賺得多。")
        is_manual = kind.startswith("✍️")
        presets = all_presets(base)
        opt = best_optimized(base)
        keys = list(presets)
        d_idx = keys.index(opt["key"]) if opt and opt["key"] in presets else 1
        prof = st.selectbox("電腦對手用的參數" if is_manual else "風險型 / 已存參數", keys, index=d_idx, help="保守型:訊號嚴、停損 5%、12 檔分散;穩健型:停損 7%、10 檔;積極型:門檻寬、停損 10%、6 檔集中。"
                                                                          "「📁」開頭是你在回測頁或優化頁存的參數")
        if opt and prof == opt["key"]:
            src = "你手動指定的「目前採用參數」" if opt["kind"] == "active" else "最新的最佳化參數"
            if opt["passed"] is False:
                st.warning(f"⚠️ 預設使用{src}:{opt['label']}\n\n{opt['verdict']}")
            else:
                st.success(f"✅ 預設使用{src}:{opt['label']}\n\n{opt['verdict']}")
        elif opt:
            st.info("目前選的不是預設。預設是:" + opt["key"].replace("📁 ", "").replace("⭐ ", ""))
        else:
            st.info("還沒有最佳化參數,先用預設的「穩健型」。到「🔍 自動優化」存一組(或設為目前採用)後,這裡與 app 會自動改用。")
        st.caption("本次開戶參數:" + param_summary(norm_params(presets[prof])))
        cap = st.number_input("本金(元)", 100_000, 100_000_000, 1_000_000, 100_000)
        days = st.number_input("模擬幾個交易日後結案", 5, 250, 20, 5)
        nm = st.text_input("帳戶名稱", value="我的操盤" if is_manual else prof.replace("📁 ", "").replace("⭐ ", ""))
        mx = st.number_input("納入股票數(依成交值排序)", 50, 1500, 500, 50)
        st.caption("開戶會下載歷史股價,第一次約需數分鐘到十幾分鐘。建議收盤後(14:30 以後)開戶。")
        new_mode = st.radio("電腦下單時機", ["🌙 盤後(隔天開盤成交)", "⚡ 盤中(按下執行時用即時價成交)"], key="new_mode",
                            help="盤中模式的電腦帳戶不會自動交易,要在帳戶頁按「立即執行」。")
        with_opp, asof = False, None
        if is_manual:
            with_opp = st.checkbox("同時開一個電腦對手帳戶(同本金、同天數、用上面選的參數)", value=not fake, disabled=fake,
                                   help="電腦對手帳戶名稱會是「你的帳戶名稱_電腦」。假資料模式不能開對手(它只會從最新日開始,日期對不上)。")
            if fake and get_data(base, fake) is not None:
                bi = get_data(base, fake)["0050.TW"].index
                asof = st.date_input("起始日(假資料模式才有)", value=bi[-30].date(), min_value=bi[0].date(), max_value=bi[-1].date(), key="mn_asof")

        def init_computer(acct_name):
            params_path = ""
            if prof.startswith(("📁", "⭐")):                                   # 已存參數:寫到暫存檔給 cmd_init 讀,不污染參數清單
                os.makedirs(base, exist_ok=True)
                params_path = os.path.join(base, "_init_params.json")
                json.dump(dict(label=prof.replace("📁 ", "").replace("⭐ ", ""), params=po.jsonable(presets[prof])), open(params_path, "w", encoding="utf-8"),
                          ensure_ascii=False, default=float)
            a = argparse.Namespace(**ns_common, profile=prof if prof in pt.PROFILES else "穩健型", params=params_path, capital=float(cap), days=int(days),
                                   name=acct_name, max_stocks=int(mx), codes="", force=False, wl=None, edge=None, min_eff=None, stop=None,
                                   max_pos=None, pct=None, hold=None, park0050=False, mode="intraday" if new_mode.startswith("⚡") else "after")
            return call_quiet(pl.cmd_init, a)

        if st.button("開戶並開始比賽" if is_manual else "開戶並開始模擬", use_container_width=True):
            if not is_manual:
                with st.spinner("開戶中…下載資料並產生第一天的買賣計畫"):
                    ok, text = init_computer(nm.strip() or prof)
            else:
                mname = nm.strip() or "我的操盤"
                if mname in manual_names or mname in names or os.path.exists(pm.path(base, mname)):
                    ok, text = False, f"已經有叫「{mname}」的帳戶,請換一個名稱。"
                elif get_data(base, fake) is None:
                    ok, text = False, "還沒有股價資料,請先到「🔍 自動優化」頁的「資料與股票池」下載。"
                else:
                    ok, text, opp = True, "", ""
                    if with_opp:                                             # 先開對手(會更新資料),再用最新資料決定手動帳戶的起點,兩邊才同一天
                        with st.spinner("開電腦對手帳戶中…下載資料並產生第一天的買賣計畫"):
                            ok_o, text_o = init_computer(mname + "_電腦")
                        cached_prices.clear()
                        opp = mname + "_電腦" if ok_o else ""
                        text += text_o + "\n"
                        if not ok_o:
                            text += "⚠️ 電腦對手開戶失敗,只開了手動帳戶。\n"
                    try:
                        S = pm.new_account(mname, float(cap), int(days), get_data(base, fake)["0050.TW"], as_of=asof if fake else None, opponent=opp)
                        pm.save(base, S)
                        text += f"手動帳戶「{mname}」已開戶,起點 {S['start_date']} 收盤,共 {int(days)} 個交易日。到左側「選擇帳戶」點 ✍️ {mname} 開始下單。"
                    except Exception as e:                                  # noqa: BLE001
                        ok, text = False, text + f"手動帳戶開戶失敗:{type(e).__name__}: {e}"
            st.session_state["run_log"] = (ok, text)
            st.rerun()

    if not names and not manual_names:
        for n, ok_, msg in st.session_state.pop("del_log", []):
            (st.sidebar.success if ok_ else st.sidebar.error)(f"{n}:{msg}")
        st.info("還沒有任何帳戶。請在左側「開新帳戶」設定本金與天數,按「開戶並開始模擬」。")
        st.stop()

    states = {n: pl.load_state(base, n) for n in names}
    opts = (["📊 全部帳戶比較"] if len(names) + len(manual_names) > 1 else []) + names + [f"✍️ {m}" for m in manual_names]
    pick = st.sidebar.radio("選擇帳戶", opts)
    LQ, bench_close = {}, {}
    sd = pm.last_session_date()                                           # 最近一個應該已經有成交資料的平日
    behind = any(x["last_date"] < str(sd) and not x["finished"] for x in list(states.values()) + [pm.load(base, m_) for m_ in manual_names])
    if (not fake) and behind:                                             # 有帳戶還沒處理到最近的交易日 → 抓報價(盤中=即時價,收盤後=最後成交價),全部帳戶只抓一次(5 秒快取)
        codes = {"0050"}
        for s_ in states.values():
            codes |= set(s_["acct"]["pos"])
            if (s_["acct"].get("core") or {}).get("sh", 0) > 0:
                codes.add(s_["params"].get("core_code", "0050"))
        for m_ in manual_names:
            codes |= set(pm.load(base, m_)["acct"]["pos"])
        LQ = live_quotes(tuple(sorted(codes)))
        d0 = get_data(base, fake)
        if d0 and "0050.TW" in d0:
            bench_close = {str(i.date()): float(v) for i, v in d0["0050.TW"].Close.tail(40).items()}
        states = {n: live_view(s_, LQ, bench_close) for n, s_ in states.items()}
    delete_panel(base, names, states, manual_names)
    bots = [n for n in names if states[n].get("mode") == "intraday" and not states[n]["finished"]]
    if bots:
        all_intraday_panel(base, ns_common, fake, bots)
    man_views = {m: live_view(pm.load(base, m), LQ, bench_close) for m in manual_names}
    try:                                                                  # 頂部跑馬燈:各帳戶報酬 + 0050 同期(只是顯示,失敗就略過;標準風格不顯示)
        if STYLE == "標準":
            raise StopIteration
        tk = [(summary(s_)["帳戶"], float(summary(s_)["報酬%"])) for s_ in states.values()] + [(summary_manual(S_)["帳戶"], float(summary_manual(S_)["報酬%"])) for S_ in man_views.values()]
        bs = [summary(s_)["0050 同期%"] for s_ in states.values() if summary(s_)["0050 同期%"] is not None]
        if bs:
            tk.append(("0050 同期", float(np.median(bs))))
        st.markdown(ui.ticker(tk), unsafe_allow_html=True)
    except (Exception, StopIteration):                                    # noqa: BLE001
        pass
    if pick.startswith("📊"):
        show_compare(states, man_views)
    elif pick.startswith("✍️ "):
        show_manual(base, fake, pick[3:], states, LQ, bench_close)
    else:
        show_account(states[pick], base, ns_common, fake)
    if auto_secs and not fake and pm.session_open():                       # 和 app 相同做法:等幾秒再重跑一次(你一操作就會被打斷)
        st.sidebar.caption(f"⏱ 已開啟自動更新:每 {auto_secs} 秒(上次 {pm.taipei_now():%H:%M:%S})")
        time.sleep(auto_secs)
        st.rerun()
    elif auto_secs:
        st.sidebar.caption("⏸ 已選自動更新,但現在不是盤中(平日 09:00–13:30)或是假資料模式,不會重跑。")


# ================================================================== 主程式
def main(embedded=False):
    """embedded=True:由 app.py 的「模擬交易」模式內嵌執行 —— 不重複呼叫 set_page_config(app.py 已設定過)。
    單獨執行(streamlit run paper_dashboard.py)時照舊。"""
    global STYLE, DARK, UP, DN, BLUE, GRAY            # 各頁面函式會讀這幾個模組層級的值
    if not embedded:
        st.set_page_config(page_title="模擬交易", page_icon="⚡", layout="wide")
    brand_slot = st.sidebar.empty()                                       # 側邊欄最上面的品牌標誌:先佔位,選好風格(CSS 注入)之後再填
    base = st.sidebar.text_input("資料夾", "paper_live", help="帳戶、股價快取、參數檔都放這裡;與命令列版 paper_live.py 共用")
    st.sidebar.caption(f"實際位置:{os.path.abspath(base)}")
    fake = st.sidebar.checkbox("使用假資料測試(不連網)", value=False, help="只用來測試介面,數字沒有意義")
    STYLE = ui.pick_style(st, base)                                       # 側邊欄「🎨 外觀風格」:科技 / 標準 / 藝術(選擇會記在資料夾裡)
    ui.sync_theme(st, STYLE)                                              # 風格 → 固定搭配的主題:標準 = 白底黑字、科技 = 深色、藝術 = 淺色(不一致就設定一次再重跑)
    DARK = ui.apply(st, STYLE)                                            # 注入這個風格的 CSS + Plotly 樣板(深色 / 淺色自動配合)
    UP, DN, BLUE, GRAY = ui.colors(DARK, STYLE)                           # 圖表顏色跟著風格換(紅漲綠跌)
    if STYLE == "標準":                                                    # 標準:原本的標題與說明,不加橫幅 / 品牌標誌
        brand_slot.empty()
        st.title("🧪 模擬交易")
        st.caption("電腦依訊號與歷史勝率自動模擬買賣,不會真的下單。僅供資訊整理,不構成投資建議。")
    else:
        brand_slot.markdown(ui.side_brand(), unsafe_allow_html=True)
        st.markdown(ui.hero(pm.taipei_now(), ([("假資料模式", "amber")] if fake else []) + [(f"資料夾 {base}", "gray")]), unsafe_allow_html=True)
    st.session_state.setdefault("paper_mode", MODES[0])                   # 子頁面選擇用 paper_mode(不要用 mode,會和 app.py 的模式選單衝突)
    st.sidebar.radio("功能", MODES, key="paper_mode")
    st.sidebar.divider()
    if st.session_state["paper_mode"] == MODES[0]:
        page_accounts(base, fake)
    elif st.session_state["paper_mode"] == MODES[1]:
        page_backtest(base, fake)
    elif st.session_state["paper_mode"] == MODES[2]:
        page_optimizer(base, fake)
    else:
        page_active(base, fake)


if __name__ == "__main__":
    main()
