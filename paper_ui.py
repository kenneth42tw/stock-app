"""
paper_ui.py — 模擬交易儀表板的外觀:三種可切換的風格 + 一組共用的 HTML 元件。

風格(側邊欄「🎨 外觀風格」切換,選擇會記在 <資料夾>/ui_prefs.json;每個風格固定搭配一種 Streamlit 主題,切換風格時自動切換):
  科技 — 深色主題:霓虹終端機,深色漸層與格線、發光卡片、跑馬燈與光掃動畫。
  標準 — 淺色主題(最初始的白底黑字):完全不套用這個模組的主題(不載入字型、不改字型、不改圖表樣板、顏色用原本的紅漲綠跌、不改任何背景或文字顏色),
         不顯示橫幅 / 跑馬燈 / KPI 卡片 / 賽道圖(由儀表板直接用原生的標題與說明文字),只多一條「指標數字一定完整顯示」的規則。
  藝術 — 淺色主題:暖色紙質與襯線字體、赤陶 / 金色點綴、柔和陰影、輕微動畫(原生主題若被手動換成深色,會自動改用墨黑金色版)。
台股慣例:紅漲綠跌。只產生 HTML / CSS 字串與 Plotly 樣板(apply / is_dark / pick_style 吃傳進來的 st),方便單獨測試。
"""
import html
import json
import os
from datetime import datetime

STYLE_NAMES = ["科技", "標準", "藝術"]
STYLE_HELP = {"科技": "*黑色風格", "標準": "*白色風格", "藝術": "*暖色風格"}
FONT_URL = ("https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Orbitron:wght@500;700;800"
            "&family=Playfair+Display:wght@500;700;800&family=Noto+Serif+TC:wght@500;700&family=Noto+Sans+TC:wght@400;500;700&display=swap")
SANS = "'Noto Sans TC',system-ui,-apple-system,'Segoe UI',sans-serif"
SERIF = "'Noto Serif TC','Playfair Display',Georgia,serif"

# ---- 配色:每個風格 × 深 / 淺。pb / pbt / pbs = 主要按鈕的背景 / 文字 / 陰影;hov = 滑過的陰影;tsel = 被選中的分頁底色;h2 / h2s = 標題色 / 光暈;
#      barU / barD = 賽道圖長條的光暈;lg = logo 光暈;fh / fn = 標題 / 數字字型;r = 圓角。
TECH_D = dict(bg0="#060a13", bg1="#0b1324", card="rgba(255,255,255,.045)", card2="rgba(255,255,255,.08)", border="rgba(0,229,255,.22)", tx="#e6edf7", sub="#8fa3bf",
              cy="#00e5ff", vi="#8b5cf6", mg="#ff3d81", up="#ff4d6d", dn="#00e676", am="#ffc107", grid="rgba(0,229,255,.06)", glow="0 0 22px rgba(0,229,255,.18)",
              hov="0 0 28px rgba(0,229,255,.28)", track="rgba(255,255,255,.07)", blue="#00b8ff", gray="#8fa3bf", r="14px",
              pb="linear-gradient(135deg,#00e5ff,#8b5cf6)", pbt="#06101c", pbs="0 0 20px rgba(0,229,255,.35)", tsel="rgba(0,229,255,.16)", h2="#00e5ff", h2s="0 0 16px rgba(0,229,255,.55)",
              barU="0 0 12px rgba(255,77,109,.55)", barD="0 0 12px rgba(0,230,118,.5)", lg="0 0 22px rgba(0,229,255,.55)", fh="'Orbitron','Noto Sans TC',sans-serif", fn=SANS)
TECH_L = dict(bg0="#f4f7fb", bg1="#e9eff7", card="rgba(255,255,255,.78)", card2="rgba(255,255,255,.95)", border="rgba(8,145,178,.30)", tx="#0f1b2d", sub="#5b6b82",
              cy="#0891b2", vi="#6d28d9", mg="#db2777", up="#e0383e", dn="#16a34a", am="#d97706", grid="rgba(8,145,178,.07)", glow="0 4px 18px rgba(8,145,178,.14)",
              hov="0 6px 24px rgba(8,145,178,.28)", track="rgba(15,27,45,.08)", blue="#2c7be5", gray="#8a94a6", r="14px",
              pb="linear-gradient(135deg,#0891b2,#6d28d9)", pbt="#ffffff", pbs="0 4px 16px rgba(8,145,178,.35)", tsel="rgba(8,145,178,.12)", h2="#0891b2", h2s="none",
              barU="none", barD="none", lg="0 4px 14px rgba(8,145,178,.4)", fh="'Orbitron','Noto Sans TC',sans-serif", fn=SANS)
STD_D = dict(bg0="#0e1117", bg1="#0e1117", card="rgba(255,255,255,.04)", card2="rgba(255,255,255,.07)", border="rgba(250,250,250,.16)", tx="#fafafa", sub="#9aa4b5",
             cy="#4da3ff", vi="#8f7cf0", mg="#e66a9a", up="#ff5a5f", dn="#2ecc71", am="#e0a030", grid="transparent", glow="none", hov="none", track="rgba(255,255,255,.12)",
             blue="#4da3ff", gray="#9aa4b5", r="10px", pb="", pbt="", pbs="", tsel="transparent", h2="inherit", h2s="none", barU="none", barD="none", lg="none", fh=SANS, fn=SANS)
STD_L = dict(bg0="#ffffff", bg1="#ffffff", card="rgba(0,0,0,.025)", card2="rgba(0,0,0,.05)", border="rgba(49,51,63,.18)", tx="#31333f", sub="#6b7280",
             cy="#2c7be5", vi="#6f42c1", mg="#d6336c", up="#e0383e", dn="#1f9d55", am="#d98b00", grid="transparent", glow="none", hov="none", track="rgba(49,51,63,.10)",
             blue="#2c7be5", gray="#8a94a6", r="10px", pb="", pbt="", pbs="", tsel="transparent", h2="inherit", h2s="none", barU="none", barD="none", lg="none", fh=SANS, fn=SANS)
ART_L = dict(bg0="#f7f1e6", bg1="#efe6d4", card="rgba(255,252,246,.80)", card2="rgba(255,252,246,.96)", border="rgba(122,92,58,.28)", tx="#2b2118", sub="#7a6a58",
             cy="#b4533c", vi="#6b5b95", mg="#c2547a", up="#c0392b", dn="#3f7d58", am="#b8860b", grid="rgba(122,92,58,.05)", glow="0 6px 22px rgba(90,60,30,.10)",
             hov="0 12px 30px rgba(90,60,30,.20)", track="rgba(122,92,58,.14)", blue="#4a5d8a", gray="#8c7f70", r="18px",
             pb="linear-gradient(135deg,#b4533c,#d08a4a)", pbt="#fffaf2", pbs="0 4px 14px rgba(180,83,60,.35)", tsel="rgba(180,83,60,.10)", h2="#8f3d2b", h2s="none",
             barU="0 2px 8px rgba(192,57,43,.25)", barD="0 2px 8px rgba(63,125,88,.25)", lg="0 4px 14px rgba(180,83,60,.35)", fh="'Playfair Display','Noto Serif TC',Georgia,serif", fn="'Playfair Display','Noto Serif TC',Georgia,serif")
ART_D = dict(bg0="#15110e", bg1="#201a15", card="rgba(255,244,225,.05)", card2="rgba(255,244,225,.09)", border="rgba(212,168,90,.30)", tx="#f1e8d8", sub="#b4a58f",
             cy="#d4a85a", vi="#c88b6b", mg="#d98880", up="#e57373", dn="#7fb892", am="#e0b050", grid="rgba(212,168,90,.04)", glow="0 8px 28px rgba(0,0,0,.35)",
             hov="0 12px 32px rgba(0,0,0,.55)", track="rgba(255,244,225,.10)", blue="#9bb0d6", gray="#a39380", r="18px",
             pb="linear-gradient(135deg,#d4a85a,#e8c98a)", pbt="#201a15", pbs="0 4px 16px rgba(212,168,90,.30)", tsel="rgba(212,168,90,.14)", h2="#e8c98a", h2s="none",
             barU="0 2px 10px rgba(229,115,115,.35)", barD="0 2px 10px rgba(127,184,146,.35)", lg="0 4px 16px rgba(212,168,90,.4)", fh="'Playfair Display','Noto Serif TC',Georgia,serif",
             fn="'Playfair Display','Noto Serif TC',Georgia,serif")
PALS = {("科技", True): TECH_D, ("科技", False): TECH_L, ("標準", True): STD_D, ("標準", False): STD_L, ("藝術", True): ART_D, ("藝術", False): ART_L}
LOGO = {"科技": "◈", "標準": "▣", "藝術": "✦"}


def norm_style(style):
    return style if style in STYLE_NAMES else "科技"


def pal(style="科技", dark=False):
    return PALS[(norm_style(style), bool(dark))]


def colors(dark, style="科技"):
    """(UP, DN, BLUE, GRAY):給 Plotly 折線 / 長條用的顏色(紅漲綠跌)。"""
    if norm_style(style) == "標準":
        return "#e0383e", "#1f9d55", "#2c7be5", "#8a94a6"                    # 最早原本的顏色(紅漲綠跌),不管深淺主題
    p = pal(style, dark)
    return p["up"], p["dn"], p["blue"], p["gray"]


def is_dark(st):
    """目前實際的 Streamlit 主題是不是深色。
    先看設定值 theme.base(config.toml 或本模組動態切換時 set_option 設的,而且會立刻更新);沒有設定才用瀏覽器回報的 st.context.theme.type。
    ⚠️ 不能只靠後者:它是連線當下讀一次的,動態切換主題後不會跟著更新,會造成「外框已經深色、我的卡片還用淺色」的錯亂。"""
    try:
        base = st.get_option("theme.base")
        if base in ("dark", "light"):
            return base == "dark"
    except Exception:                                                       # noqa: BLE001
        pass
    try:
        return str(st.context.theme.type) == "dark"
    except Exception:                                                       # noqa: BLE001
        return False


# ------------------------------------------------------------------ 偏好(記住選的風格)
PREF_FILE = "ui_prefs.json"
# 每個風格固定搭配的 Streamlit 主題(切換風格時自動切換,不需要另外選):標準 = 最初始的白底黑字,科技 = 深色,藝術 = 淺色(暖色紙質)
STYLE_THEME = {"科技": "dark", "標準": "light", "藝術": "light"}


def _prefs(base):
    try:
        j = json.load(open(os.path.join(base, PREF_FILE), encoding="utf-8"))
        return j if isinstance(j, dict) else {}
    except Exception:                                                       # noqa: BLE001
        return {}


def _save_prefs(base, **kv):
    try:
        os.makedirs(base, exist_ok=True)
        json.dump({**_prefs(base), **kv}, open(os.path.join(base, PREF_FILE), "w", encoding="utf-8"), ensure_ascii=False)
        return True
    except Exception:                                                       # noqa: BLE001
        return False                                                        # 唯讀或沒權限:這次照樣生效,只是下次不會記住


def load_style(base):
    return norm_style(_prefs(base).get("style"))


def save_style(base, style):
    return _save_prefs(base, style=norm_style(style))


_THEME_KEYS = ("primaryColor", "backgroundColor", "secondaryBackgroundColor", "textColor")


def set_native_theme(mode):
    """改 Streamlit 自己的主題為 light / dark。用的是它的設定函式(非公開 API,版本不同可能失效),成功回傳 True。
    顏色一併清成預設,免得 config.toml 裡舊的顏色和新的 base 混在一起。
    ⚠️ 設定是整個 Streamlit 程序共用的:同一台主機上所有連線的人都會一起變。(個人電腦本機使用沒問題。)"""
    try:
        from streamlit import config
        config.set_option("theme.base", mode)
        for k in _THEME_KEYS:
            config.set_option(f"theme.{k}", None)
        return True
    except Exception:                                                       # noqa: BLE001
        return False


def sync_theme(st, style):
    """讓 Streamlit 的主題符合這個風格(STYLE_THEME):不一致就設定一次再重跑(重跑時瀏覽器才會收到新主題)。
    設定值會立刻更新,所以不會無限重跑;只有設定函式不能用時才提示改用 config.toml。"""
    want = STYLE_THEME[norm_style(style)]
    try:
        now = st.get_option("theme.base")
    except Exception:                                                       # noqa: BLE001
        now = None
    if now == want:
        return
    if set_native_theme(want):
        st.rerun()
    st.sidebar.warning(f"沒辦法自動切換成{'白底黑字' if want == 'light' else '深色'}主題(你的 Streamlit 版本不支援)。請把附的 "
                       f"{'config.toml' if want == 'light' else 'config_dark.toml'} 放到 .streamlit/config.toml 後重新啟動。")


def pick_style(st, base):
    """側邊欄「🎨 外觀風格」(科技 / 標準 / 藝術),選擇會記在 <資料夾>/ui_prefs.json。回傳目前風格。"""
    if st.session_state.get("ui_style") not in STYLE_NAMES:
        st.session_state["ui_style"] = load_style(base)
    with st.sidebar.expander("🎨 外觀風格", expanded=False):                 # 收在展開區裡,標準風格的側邊欄版面才會跟最早的一樣
        cur = st.selectbox("風格", STYLE_NAMES, key="ui_style", format_func=lambda x: f"{x}　{STYLE_HELP[x]}")
    if cur != load_style(base):
        save_style(base, cur)
    return cur


# ------------------------------------------------------------------ CSS
def theme_css(style="科技", dark=False):
    style = norm_style(style)
    p = pal(style, dark)
    v = ";".join(f"--{k}:{val}" for k, val in p.items() if k not in ("pb", "pbt", "pbs") or val)
    # ① 所有風格共用:指標數字一定完整顯示、自訂元件
    metric = """
[data-testid="stMetricValue"],[data-testid="stMetricValue"] *{overflow:visible!important;text-overflow:clip!important;white-space:nowrap!important;max-width:none!important}
[data-testid="stMetricValue"]{font-size:2.25rem;font-weight:400;line-height:1.2;letter-spacing:0;font-variant-numeric:lining-nums tabular-nums}
[data-testid="stMetricValue"] *{font-size:inherit!important;font-weight:inherit!important}
@supports (font-size:1cqw){[data-testid="stMetricValue"]{font-size:clamp(1.15rem,15cqw,2.25rem)}}
[data-testid="stMetric"]{container-type:inline-size}
"""
    comp = f"""
.pt-hero{{position:relative;display:flex;justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap;padding:20px 26px;margin:0 0 14px;border-radius:var(--r);overflow:hidden;
  background:var(--herobg);border:1px solid var(--border);box-shadow:var(--glow)}}
.pt-brand{{display:flex;align-items:center;gap:14px;position:relative;z-index:1}}
.pt-logo{{width:46px;height:46px;border-radius:calc(var(--r) - 2px);display:flex;align-items:center;justify-content:center;font-size:0;background:var(--logobg);box-shadow:var(--lg);color:var(--logofg)}}
.pt-logo:after{{content:"{LOGO[style]}";font-size:24px;line-height:1}}
.pt-title{{font-family:var(--fh);font-weight:800;font-size:1.55rem;letter-spacing:{'2px' if style != '藝術' else '3px'};line-height:1.1;color:var(--tx)}}
.pt-title b{{color:var(--cy)}}
.pt-sub{{color:var(--sub);font-size:.82rem;margin-top:4px;letter-spacing:.5px}}
.pt-hero-r{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;position:relative;z-index:1}}
.pt-chip{{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:999px;font-size:.78rem;font-weight:700;letter-spacing:.6px;border:1px solid var(--border);
  background:var(--card);color:var(--tx);font-family:'JetBrains Mono','Noto Sans TC',monospace;white-space:nowrap}}
.pt-chip.green{{color:var(--dn);border-color:var(--dn);background:rgba(0,200,100,.10)}}.pt-chip.red{{color:var(--up);border-color:var(--up);background:rgba(255,77,109,.10)}}
.pt-chip.cyan{{color:var(--cy);border-color:var(--cy);background:rgba(128,128,128,.08)}}.pt-chip.violet{{color:var(--vi);border-color:var(--vi);background:rgba(128,128,128,.08)}}
.pt-chip.amber{{color:var(--am);border-color:var(--am);background:rgba(255,193,7,.10)}}.pt-chip.gray{{color:var(--sub)}}
.pt-dot{{width:9px;height:9px;border-radius:50%;background:currentColor;display:inline-block}}
.pt-chip.live .pt-dot{{animation:ptpulse 1.6s infinite}}
.pt-live{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:2px 0 10px;color:var(--sub);font-size:.84rem}}
.pt-ticker{{overflow:hidden;white-space:nowrap;border:1px solid var(--border);border-radius:calc(var(--r) - 2px);background:var(--card);margin:0 0 14px;padding:8px 0;position:relative}}
.pt-ticker:before,.pt-ticker:after{{content:"";position:absolute;top:0;bottom:0;width:48px;z-index:2;pointer-events:none}}
.pt-ticker:before{{left:0;background:linear-gradient(90deg,var(--bg0),transparent)}}.pt-ticker:after{{right:0;background:linear-gradient(270deg,var(--bg0),transparent)}}
.pt-track{{display:inline-block;white-space:nowrap;animation:ptmarquee var(--dur,40s) linear infinite}}
.pt-ticker:hover .pt-track{{animation-play-state:paused}}
.pt-tk{{display:inline-block;margin:0 22px;font-family:'JetBrains Mono','Noto Sans TC',monospace;font-size:.88rem}}
.pt-tk i{{font-style:normal;color:var(--sub);margin-right:8px}}.pt-up{{color:var(--up)}}.pt-dn{{color:var(--dn)}}.pt-flat{{color:var(--sub)}}
.pt-kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:6px 0 14px}}
.pt-kpi{{position:relative;padding:14px 16px;border-radius:var(--r);background:var(--card);border:1px solid var(--border);box-shadow:var(--glow);overflow:hidden;animation:ptfade .5s ease both}}
.pt-kpi:before{{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(var(--cy),var(--vi))}}
.pt-kpi .l{{color:var(--sub);font-size:.76rem;letter-spacing:1.2px;text-transform:uppercase}}
.pt-kpi .v{{font-family:var(--fn);font-variant-numeric:lining-nums tabular-nums;font-weight:800;font-size:1.65rem;margin-top:4px;color:var(--tx)}}
.pt-kpi .v.tu{{color:var(--up)}}.pt-kpi .v.td{{color:var(--dn)}}
.pt-kpi .s{{color:var(--sub);font-size:.78rem;margin-top:3px}}
.pt-race{{margin:6px 0 16px;padding:14px 16px 8px;border-radius:var(--r);background:var(--card);border:1px solid var(--border);box-shadow:var(--glow)}}
.pt-race h4{{margin:0 0 10px;font-size:.9rem;letter-spacing:1.4px;color:var(--sub);font-weight:700}}
.pt-row{{display:grid;grid-template-columns:34px minmax(110px,230px) 1fr 76px;gap:10px;align-items:center;margin-bottom:9px;font-size:.88rem}}
.pt-row.me .pt-name{{color:var(--cy);font-weight:800}}
.pt-rank{{text-align:center;font-family:'JetBrains Mono',monospace;color:var(--sub)}}.pt-name{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.pt-track2{{position:relative;height:16px;border-radius:9px;background:var(--track)}}
.pt-zero{{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--sub);opacity:.7}}
.pt-bar{{position:absolute;top:2px;bottom:2px;border-radius:7px;animation:ptgrow .8s cubic-bezier(.2,.8,.2,1) both}}
.pt-bar.up{{background:linear-gradient(90deg,rgba(255,77,109,.35),var(--up));box-shadow:var(--barU)}}
.pt-bar.dn{{background:linear-gradient(270deg,rgba(0,200,100,.35),var(--dn));box-shadow:var(--barD)}}
.pt-bench{{position:absolute;top:-5px;bottom:-5px;width:0;border-left:2px dashed var(--am);opacity:.95}}
.pt-bench.first:after{{content:"0050";position:absolute;top:-13px;left:-14px;font-size:.6rem;color:var(--am);letter-spacing:.5px}}
.pt-val{{text-align:right;font-family:'JetBrains Mono',monospace;font-weight:700}}
.pt-legend{{color:var(--sub);font-size:.72rem;margin:2px 0 4px;text-align:right}}
.pt-brand-s{{display:flex;align-items:center;gap:10px;margin:2px 0 8px}}
.pt-brand-s .pt-logo{{width:34px;height:34px;border-radius:10px}}.pt-brand-s .pt-logo:after{{font-size:18px}}.pt-brand-s .pt-title{{font-size:1.02rem;letter-spacing:1.6px}}
@keyframes ptpulse{{0%{{box-shadow:0 0 0 0 currentColor}}70%{{box-shadow:0 0 0 9px transparent}}100%{{box-shadow:0 0 0 0 transparent}}}}
@keyframes ptmarquee{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
@keyframes ptsheen{{0%,55%{{left:-40%}}100%{{left:120%}}}}
@keyframes ptfade{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
@keyframes ptgrow{{from{{clip-path:inset(0 100% 0 0)}}to{{clip-path:inset(0 0 0 0)}}}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important;transition:none!important}}}}
"""
    if style == "標準":                                                       # 標準:最早原本的樣子 — 只有「指標數字完整顯示」,沒有字型、沒有 @import、沒有其他樣式
        return "<style>" + metric + "</style>"
    # 每個風格自己的自訂元件底色
    herobg = {"科技": "linear-gradient(120deg,rgba(0,229,255,.14),rgba(139,92,246,.16) 55%,rgba(255,61,129,.10))" if dark else "linear-gradient(120deg,rgba(8,145,178,.12),rgba(109,40,217,.12) 55%,rgba(219,39,119,.08))",
              "標準": "var(--card)",
              "藝術": "linear-gradient(120deg,rgba(180,83,60,.12),rgba(212,168,90,.18) 60%,rgba(107,91,149,.10))" if not dark else "linear-gradient(120deg,rgba(212,168,90,.14),rgba(200,139,107,.12) 60%,rgba(107,91,149,.10))"}[style]
    logobg = {"科技": "linear-gradient(135deg,var(--cy),var(--vi))", "標準": "var(--cy)", "藝術": "linear-gradient(135deg,var(--cy),var(--vi))"}[style]
    logofg = "#06101c" if (style == "科技" and dark) else "#ffffff" if style != "藝術" or not dark else "#201a15"
    # ② 風格專屬:原生元件、背景
    native = ""
    if style in ("科技", "藝術"):
        art = style == "藝術"
        page = ""
        if style == "科技" and dark:
            page = f"""
.stApp{{background-color:{p['bg0']};
  background-image:linear-gradient({p['grid']} 1px,transparent 1px),linear-gradient(90deg,{p['grid']} 1px,transparent 1px),
    radial-gradient(1100px 560px at 6% -8%,rgba(0,229,255,.13),transparent 60%),radial-gradient(900px 520px at 102% 6%,rgba(139,92,246,.15),transparent 58%),
    linear-gradient(180deg,{p['bg0']},{p['bg1']});
  background-size:46px 46px,46px 46px,auto,auto,auto;background-attachment:fixed}}
[data-testid="stHeader"]{{background:transparent}}
[data-testid="stSidebar"]{{background:linear-gradient(180deg,rgba(10,18,34,.94),rgba(6,10,19,.97));border-right:1px solid rgba(0,229,255,.16)}}
.stApp h2{{text-shadow:var(--h2s)}}
.pt-kpi .v{{text-shadow:0 0 14px rgba(0,229,255,.25)}}.pt-kpi .v.tu{{text-shadow:0 0 14px rgba(255,77,109,.4)}}.pt-kpi .v.td{{text-shadow:0 0 14px rgba(0,230,118,.4)}}
.pt-row.me .pt-name{{text-shadow:0 0 10px rgba(0,229,255,.5)}}
.pt-hero:after{{content:"";position:absolute;top:0;left:-40%;width:35%;height:100%;background:linear-gradient(100deg,transparent,rgba(255,255,255,.10),transparent);animation:ptsheen 6s ease-in-out infinite}}
"""
        if art:
            grain = ("url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2'/>"
                     "<feColorMatrix values='0 0 0 0 .45  0 0 0 0 .35  0 0 0 0 .22  0 0 0 .55 0'/></filter><rect width='100%' height='100%' filter='url(%23n)' opacity='.5'/></svg>\")")
            page = f"""
.stApp{{background-color:{p['bg0']};background-image:{grain},radial-gradient(1000px 520px at 0% -10%,{'rgba(180,83,60,.10)' if not dark else 'rgba(212,168,90,.10)'},transparent 60%),
  radial-gradient(900px 520px at 100% 0%,{'rgba(107,91,149,.09)' if not dark else 'rgba(200,139,107,.08)'},transparent 58%),linear-gradient(180deg,{p['bg0']},{p['bg1']});background-attachment:fixed}}
[data-testid="stHeader"]{{background:transparent}}
[data-testid="stSidebar"]{{background:linear-gradient(180deg,{'#f2e9d8,#ebe0cb' if not dark else '#1d1814,#15110e'});border-right:1px solid var(--border)}}
.stApp{{color:var(--tx)}}
.stApp h1,.stApp h2,.stApp h3{{font-family:var(--fh);letter-spacing:.5px}}
.pt-hero:after{{content:"";position:absolute;right:-40px;top:-40px;width:180px;height:180px;border-radius:50%;background:radial-gradient(circle,{'rgba(212,168,90,.25)' if not dark else 'rgba(212,168,90,.16)'},transparent 70%)}}
"""
        native = page + f"""
.stApp h2{{color:var(--h2);font-weight:800;letter-spacing:.4px}}
.stApp h3{{font-weight:800;border-left:4px solid var(--cy);padding-left:10px}}
::-webkit-scrollbar{{width:9px;height:9px}}::-webkit-scrollbar-thumb{{background:linear-gradient(var(--cy),var(--vi));border-radius:9px}}::-webkit-scrollbar-track{{background:transparent}}
[data-testid="stMetric"]{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px 16px;box-shadow:var(--glow);backdrop-filter:blur(8px);
  transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease}}
[data-testid="stMetric"]:hover{{transform:translateY(-2px);border-color:var(--cy);box-shadow:var(--hov)}}
[data-testid="stMetricLabel"]{{opacity:.9}}
[data-baseweb="tab-list"]{{gap:6px;border-bottom:1px solid var(--border)}}
button[data-baseweb="tab"]{{border-radius:10px 10px 0 0;padding:8px 16px;transition:all .15s ease}}
button[data-baseweb="tab"]:hover{{background:var(--card2)}}
button[data-baseweb="tab"][aria-selected="true"]{{background:linear-gradient(180deg,var(--tsel),transparent)}}
[data-baseweb="tab-highlight"]{{background:linear-gradient(90deg,var(--cy),var(--vi))!important;height:3px!important}}
.stButton>button,.stDownloadButton>button,button[kind="secondary"],button[data-testid^="stBaseButton"]{{border-radius:{'999px' if art else '12px'};border:1px solid var(--border);background:var(--card);
  transition:all .16s ease;font-weight:600}}
.stButton>button:hover:not(:disabled),button[data-testid^="stBaseButton"]:hover:not(:disabled){{border-color:var(--cy);box-shadow:var(--hov);transform:translateY(-1px)}}
button[kind="primary"],button[data-testid="stBaseButton-primary"]{{background:{p['pb']}!important;color:{p['pbt']}!important;border:none!important;box-shadow:{p['pbs']};font-weight:800}}
button[kind="primary"]:hover:not(:disabled),button[data-testid="stBaseButton-primary"]:hover:not(:disabled){{filter:brightness(1.07)}}
button:disabled{{opacity:.38!important}}
[data-baseweb="input"]>div,[data-baseweb="select"]>div,[data-baseweb="textarea"]{{border-radius:10px!important;border:1px solid var(--border)!important}}
[data-baseweb="input"]:focus-within>div,[data-baseweb="select"]:focus-within>div{{border-color:var(--cy)!important;box-shadow:0 0 0 1px var(--cy)}}
[data-testid="stExpander"]{{border:1px solid var(--border);border-radius:var(--r);background:var(--card);box-shadow:var(--glow);overflow:hidden}}
[data-testid="stAlert"]{{border-radius:12px;border:1px solid var(--border);backdrop-filter:blur(6px)}}
[data-testid="stDataFrame"]{{border-radius:12px;overflow:hidden;border:1px solid var(--border);box-shadow:var(--glow)}}
[data-testid="stPlotlyChart"]{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:6px;box-shadow:var(--glow)}}
[data-testid="stSidebar"] [role="radiogroup"] label{{border-radius:10px;padding:6px 8px;transition:background .15s ease}}
[data-testid="stSidebar"] [role="radiogroup"] label:hover{{background:var(--card2)}}
"""
    else:
        native = ""
    return (f"<style>\n@import url('{FONT_URL}');\n:root{{{v};--herobg:{herobg};--logobg:{logobg};--logofg:{logofg}}}\n"
            f"html,body,.stApp{{font-family:{SANS}}}\n{metric}{comp}{native}</style>")


# ------------------------------------------------------------------ 元件(三種風格共用,外觀由上面的 CSS 決定)
def _e(x):
    return html.escape(str(x))


def market_state(now=None):
    """(文字, 色調, 是否閃爍):依台北時間粗略判斷(不含國定假日)。"""
    t = now or datetime.now()
    if t.weekday() >= 5:
        return "休市日", "gray", False
    hm = (t.hour, t.minute)
    if hm < (9, 0):
        return "盤前", "amber", False
    if hm <= (13, 30):
        return "盤中 OPEN", "green", True
    return "已收盤", "violet", False


def chip(text, tone="cyan", live=False):
    return f'<span class="pt-chip {tone}{" live" if live else ""}"><span class="pt-dot"></span>{_e(text)}</span>'


def hero(now=None, extra=()):
    """頂部橫幅。extra = [(文字, 色調)] 會附加在右側。"""
    t = now or datetime.now()
    st_, tone, live = market_state(t)
    chips = chip(st_, tone, live) + f'<span class="pt-chip cyan">{t:%Y-%m-%d %H:%M:%S} 台北</span>' + "".join(chip(a, b) for a, b in extra)
    return (f'<div class="pt-hero"><div class="pt-brand"><div class="pt-logo">◈</div><div><div class="pt-title">PAPER<b>·</b>TRADER</div>'
            f'<div class="pt-sub">模擬交易指揮中心 ／ 電腦 vs 你 — 同樣規則,公平比賽(僅供資訊整理,不構成投資建議)</div></div></div>'
            f'<div class="pt-hero-r">{chips}</div></div>')


def side_brand():
    return '<div class="pt-brand-s"><div class="pt-logo">◈</div><div class="pt-title">PAPER<b>·</b>TRADER</div></div>'


def live_note(note, at, text=""):
    """盤中 / 最後成交價的提示列:閃爍的標籤 + 說明文字。"""
    tone, live = ("green", True) if "即時" in note else ("amber", False)
    return f'<div class="pt-live">{chip("LIVE " + note.split("(")[0] if live else note.split("(")[0], tone, live)}<span>{_e(at)} 更新　{_e(text)}</span></div>'


def _pcls(v):
    return "pt-up" if v > 0 else "pt-dn" if v < 0 else "pt-flat"


def ticker(items, secs_per_item=3.2):
    """跑馬燈。items = [(名稱, 報酬%)]。滑鼠移上去會暫停。"""
    if not items:
        return ""
    one = "".join(f'<span class="pt-tk"><i>{_e(n)}</i><span class="{_pcls(r)}">{"▲" if r > 0 else "▼" if r < 0 else "■"} {r:+.2f}%</span></span>' for n, r in items)
    one = one * max(1, -(-10 // len(items)))                               # 項目太少時重複幾次,確保一組的寬度夠長、不會出現空白
    return f'<div class="pt-ticker"><div class="pt-track" style="--dur:{max(18, secs_per_item * len(items)):.0f}s">{one}{one}</div></div>'


_TONE = {"up": "tu", "dn": "td"}                                    # KPI 色調 → CSS 類別(不用太泛用的名稱,避免和別的樣式撞名)


def kpi_cards(items):
    """items = [dict(label, value, sub="", tone="")];tone: up / dn / ""。"""
    cards = "".join(f'<div class="pt-kpi" style="animation-delay:{i * 0.06:.2f}s"><div class="l">{_e(k["label"])}</div>'
                    f'<div class="v {_TONE.get(k.get("tone", ""), "")}">{_e(k["value"])}</div><div class="s">{_e(k.get("sub", ""))}</div></div>' for i, k in enumerate(items))
    return f'<div class="pt-kpis">{cards}</div>'


def race_bars(rows, title="報酬賽道"):
    """rows = [dict(name, ret, bench=None, me=False)];依報酬由高到低排。
    橫軸是報酬%,細線 = 0%,黃色虛線 = 這個帳戶自己的「同期 0050」。紅 = 賺、綠 = 賠(台股慣例)。"""
    if not rows:
        return ""
    rows = sorted(rows, key=lambda r: -r["ret"])
    vals = [0.0] + [r["ret"] for r in rows] + [r["bench"] for r in rows if r.get("bench") is not None]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    lo, hi, span = lo - span * 0.06, hi + span * 0.06, span * 1.12
    x = lambda v: (v - lo) / span * 100                                    # noqa: E731
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    out = []
    for i, r in enumerate(rows, 1):
        a, b = sorted((x(0.0), x(r["ret"])))
        bar = f'<div class="pt-bar {"up" if r["ret"] >= 0 else "dn"}" style="left:{a:.2f}%;width:{max(b - a, 0.6):.2f}%;animation-delay:{i * 0.05:.2f}s"></div>'
        bench = f'<div class="pt-bench{" first" if i == 1 else ""}" style="left:{x(r["bench"]):.2f}%"></div>' if r.get("bench") is not None else ""
        out.append(f'<div class="pt-row{" me" if r.get("me") else ""}"><div class="pt-rank">{medal.get(i, i)}</div><div class="pt-name" title="{_e(r["name"])}">{_e(r["name"])}</div>'
                   f'<div class="pt-track2"><div class="pt-zero" style="left:{x(0.0):.2f}%"></div>{bench}{bar}</div>'
                   f'<div class="pt-val {_pcls(r["ret"])}">{r["ret"]:+.2f}%</div></div>')
    return (f'<div class="pt-race"><h4>🏁 {_e(title)}</h4><div class="pt-legend">黃色虛線 = 該帳戶同期 0050 ・ 細線 = 0%</div>{"".join(out)}</div>')


def compare_panel(df):
    """全部帳戶比較的 KPI 卡片 + 賽道圖。df 需要欄位:帳戶、報酬%、0050 同期%(帳戶名稱以 ✍️ 開頭的是你自己的手動帳戶)。"""
    import pandas as pd
    d = df.reset_index(drop=True)
    if d.empty:
        return ""
    ret, ben = pd.to_numeric(d["報酬%"], errors="coerce").fillna(0.0), pd.to_numeric(d["0050 同期%"], errors="coerce")
    me = d["帳戶"].astype(str).str.startswith("✍️")
    nb, nm = int((~me).sum()), int(me.sum())
    bi = int(ret.idxmax())
    ok = ben.notna()
    kp = [dict(label="參賽帳戶", value=str(len(d)), sub=f"{nb} 個機器人" + (f" + {nm} 個手動" if nm else "")),
          dict(label="最佳報酬", value=f"{ret[bi]:+.2f}%", sub=str(d["帳戶"][bi]), tone="up" if ret[bi] > 0 else "dn" if ret[bi] < 0 else ""),
          dict(label="平均報酬", value=f"{ret.mean():+.2f}%", sub=(f"贏過各自同期 0050:{int((ret[ok] > ben[ok]).sum())}/{int(ok.sum())}" if ok.any() else "還沒有 0050 對照"),
               tone="up" if ret.mean() > 0 else "dn" if ret.mean() < 0 else "")]
    if nm and nb:
        mi = int(ret[me].idxmax())
        rank = int((ret > ret[mi]).sum()) + 1
        diff = float(ret[mi] - ret[~me].max())
        kp.append(dict(label="你的排名", value=f"#{rank} / {len(d)}", sub=f"對最強機器人 {'領先' if diff >= 0 else '落後'} {abs(diff):.2f} 個百分點", tone="up" if diff > 0 else "dn" if diff < 0 else ""))
    rows = [dict(name=str(d["帳戶"][i]), ret=float(ret[i]), bench=float(ben[i]) if ok[i] else None, me=bool(me[i])) for i in range(len(d))]
    return kpi_cards(kp) + race_bars(rows)


# ------------------------------------------------------------------ Plotly
def plotly_template(style="科技", dark=False):
    import plotly.graph_objects as go                                       # 用到才載入,HTML 元件不需要 plotly
    style = norm_style(style)
    p = pal(style, dark)
    way = {"科技": [p["cy"], p["vi"], p["mg"], p["am"], p["dn"], "#ff8a65", "#4dd0e1"],
           "標準": ["#2c7be5", "#e0383e", "#1f9d55", "#d98b00", "#6f42c1", "#17a2b8", "#8a94a6"],
           "藝術": ["#b4533c" if not dark else "#d4a85a", "#4a5d8a" if not dark else "#9bb0d6", "#3f7d58" if not dark else "#7fb892", "#b8860b", "#c2547a", "#6b5b95", "#8c7f70"]}[style]
    grid = {"科技": p["grid"] if dark else "rgba(15,27,45,.08)", "標準": "rgba(128,128,128,.20)", "藝術": "rgba(122,92,58,.14)" if not dark else "rgba(212,168,90,.12)"}[style]
    axis = dict(gridcolor=grid, zerolinecolor="rgba(143,163,191,.35)", linecolor="rgba(143,163,191,.35)")
    hover_bg = {("科技", True): "#0b1324", ("科技", False): "#ffffff", ("標準", True): "#262730", ("標準", False): "#ffffff", ("藝術", True): "#201a15", ("藝術", False): "#fffaf2"}[(style, bool(dark))]
    font = {"科技": "Noto Sans TC, JetBrains Mono, sans-serif", "標準": "Noto Sans TC, sans-serif", "藝術": "Noto Serif TC, Playfair Display, serif"}[style]
    t = go.layout.Template()
    t.layout = dict(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(color=p["tx"], family=font), colorway=way, xaxis=axis, yaxis=axis,
                    hoverlabel=dict(bgcolor=hover_bg, bordercolor=p["cy"], font=dict(color=p["tx"])), legend=dict(bgcolor="rgba(0,0,0,0)"), margin=dict(l=12, r=12, t=30, b=12))
    return t


def apply(st, style="科技", dark=None):
    """注入 CSS 並把 Plotly 預設樣板換成這個風格的版本。回傳是否為深色。"""
    import plotly.io as pio
    style = norm_style(style)
    dark = is_dark(st) if dark is None else dark
    st.markdown(theme_css(style, dark), unsafe_allow_html=True)
    if style == "標準":
        pio.templates.default = "plotly"                                    # 還原成 Plotly 原本的預設(同一個程序裡之前選過別的風格時也要還原)
    else:
        pio.templates["pt_theme"] = plotly_template(style, dark)
        pio.templates.default = "pt_theme"
    return dark
