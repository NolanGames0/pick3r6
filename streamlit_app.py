"""
Ottoneu Six Picks Optimizer — Streamlit Edition
================================================
Mobile-friendly web app. Run locally or deploy free to Streamlit Community Cloud.

LOCAL:
  pip install streamlit requests beautifulsoup4
  streamlit run sixpicks_app.py
  → opens in browser, works great on phone via your local IP

DEPLOY (free, permanent URL on your phone):
  1. Push this file to a public GitHub repo
  2. Go to share.streamlit.io → "New app" → point at your repo
  3. Done — bookmark the URL on your phone
"""

import re, time, itertools
from datetime import date
from difflib import SequenceMatcher

import requests
import streamlit as st
from bs4 import BeautifulSoup

# =============================================================================
#  PAGE CONFIG  (must be first Streamlit call)
# =============================================================================
st.set_page_config(
    page_title="Six Picks Optimizer",
    page_icon="⚾",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# Mobile-first CSS
st.markdown("""
<style>
  /* Tighten up padding on mobile */
  .block-container { padding: 1rem 1rem 2rem; max-width: 700px; }
  /* Card-style metric boxes */
  .player-card {
    background: #1e2a3a;
    border-radius: 10px;
    padding: 10px 14px;
    margin-bottom: 8px;
    border-left: 4px solid #f0a500;
  }
  .player-card.top { border-left-color: #2ecc71; }
  .player-card .rank { color: #888; font-size: 0.75rem; }
  .player-card .pname { font-weight: 700; font-size: 1rem; }
  .player-card .meta { color: #aaa; font-size: 0.8rem; margin-top: 2px; }
  .player-card .pts { float: right; font-size: 1.1rem; font-weight: 700; color: #f0a500; }
  .optimal-slot {
    display: flex; justify-content: space-between; align-items: center;
    background: #162032; border-radius: 8px;
    padding: 8px 12px; margin-bottom: 6px;
  }
  .slot-label { color: #888; font-size: 0.75rem; width: 36px; }
  .slot-name { font-weight: 600; flex: 1; padding: 0 8px; }
  .slot-sal { color: #aaa; font-size: 0.85rem; }
  .slot-pts { color: #2ecc71; font-weight: 700; font-size: 0.95rem; }
  /* Hide Streamlit hamburger / footer on mobile */
  #MainMenu, footer { visibility: hidden; }
  /* Make expanders look nicer */
  .streamlit-expanderHeader { font-weight: 600; font-size: 1rem; }
</style>
""", unsafe_allow_html=True)

# =============================================================================
#  CONFIG
# =============================================================================
TODAY         = date.today().strftime("%Y-%m-%d")
SEASON        = date.today().year
SALARY_CAP    = 120.0
MLB_API       = "https://statsapi.mlb.com/api/v1"
BOARD_URL     = "https://ottoneu.fangraphs.com/sixpicks/baseball/board"

DEFAULT_HITTER_PA = 4.0
DEFAULT_SP_IP     = 5.5
MIN_PA = 10
MIN_IP = 3.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# =============================================================================
#  SCORING
# =============================================================================
H_1B=5.6; H_2B=7.5; H_3B=10.5; H_HR=14.0
H_BB=3.0; H_HBP=3.0; H_SB=1.9; H_CS=-2.8; H_AB=-1.0
P_OUT=2.8/3; P_K=2.0; P_BB=-3.0; P_HBP=-3.0; P_HR=-13.0
P_SV=5.0; P_HLD=4.0
SP_MULT = 0.5

SLOTS = ["C", "CI", "MI", "OF", "SP", "RP"]
SLOT_LABELS = {
    "C":  "Catcher",
    "CI": "Corner IF (1B/3B)",
    "MI": "Middle IF (2B/SS)",
    "OF": "Outfield",
    "SP": "Starting P",
    "RP": "Relief P",
}
SLOT_ICONS = {"C":"🎯","CI":"💪","MI":"⚡","OF":"🏃","SP":"🔥","RP":"🔒"}

POS_TO_SLOT = {
    "C":["C"], "1B":["CI"], "3B":["CI"],
    "2B":["MI"], "SS":["MI"],
    "LF":["OF"], "CF":["OF"], "RF":["OF"], "OF":["OF"],
}

# ── Your custom closer list ──────────────────────────────────────────────────
KNOWN_CLOSERS = [
    "Ryan Helsley", "Emmanuel Clase", "Jhoan Duran", "Mason Miller",
    "Tanner Scott", "Josh Hader", "Felix Bautista", "Edwin Diaz",
    "Clay Holmes", "Devin Williams", "Pete Fairbanks", "Jordan Romano",
    "David Bednar", "Jeff Hoffman", "Alexis Diaz", "Andres Munoz",
    "Camilo Doval", "Evan Phillips", "Raisel Iglesias", "Kenley Jansen",
]

# =============================================================================
#  DATA FETCHING  (all cached so re-renders don't re-fetch)
# =============================================================================
@st.cache_data(ttl=1800, show_spinner=False)   # cache 30 min
def fetch_board() -> list[dict]:
    try:
        r = requests.get(BOARD_URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        st.error(f"Board fetch failed: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    table = None
    for t in soup.find_all("table"):
        if "tablesorter" in " ".join(t.get("class", [])):
            table = t; break
    if not table:
        for t in soup.find_all("table"):
            if "PRICE" in t.get_text()[:500].upper():
                table = t; break
    if not table:
        for t in soup.find_all("table"):
            tbody = t.find("tbody")
            if tbody:
                first = tbody.find("tr")
                if first:
                    cells = first.find_all("td")
                    if len(cells) >= 2 and "$" in cells[1].get_text():
                        table = t; break
    if not table:
        return []

    thead = table.find("thead")
    col_names = [th.get_text(strip=True).upper() for th in thead.find_all("th")] if thead else []

    def col_idx(candidates, default):
        for c in candidates:
            for i, h in enumerate(col_names):
                if c in h: return i
        return default

    name_col  = col_idx(["NAME","PLAYER"], 0)
    price_col = col_idx(["PRICE","SALARY","COST"], 1)
    pct_col   = col_idx(["PICK","PCT"], 2)
    pts_col   = col_idx(["PTS","POINTS","SCORE"], 3)

    def cell_float(cells, idx):
        if idx is None or idx >= len(cells): return None
        raw = cells[idx].get_text(strip=True).replace("$","").replace("%","").replace(",","")
        try: return float(raw.strip())
        except ValueError: return None

    rows = []
    tbody = table.find("tbody")
    for tr in (tbody.find_all("tr") if tbody else table.find_all("tr")[1:]):
        cells = tr.find_all("td")
        if len(cells) < 2: continue
        name_cell = cells[name_col] if name_col < len(cells) else cells[0]
        a_tag = name_cell.find("a")
        name = (a_tag or name_cell).get_text(strip=True)
        if not name or name.upper() in ("NAME","PLAYER",""): continue
        price = cell_float(cells, price_col)
        if price is None or not (0.5 <= price <= 200): continue
        rows.append({
            "name":      name,
            "price":     price,
            "pick_pct":  cell_float(cells, pct_col),
            "board_pts": cell_float(cells, pts_col),
        })
    return rows


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_today_games() -> list:
    url = (f"{MLB_API}/schedule?sportId=1&date={TODAY}"
           f"&hydrate=lineups,probablePitcher,team&gameType=R")
    try:
        r = requests.get(url, timeout=12); r.raise_for_status()
        return [g for d in r.json().get("dates",[]) for g in d.get("games",[])]
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def mlb_stats(mlb_id: int, group: str) -> dict:
    for season in [SEASON, SEASON - 1]:
        try:
            url = f"{MLB_API}/people/{mlb_id}/stats?stats=season&season={season}&group={group}"
            r = requests.get(url, timeout=8); r.raise_for_status()
            splits = r.json().get("stats",[{}])[0].get("splits",[])
            if splits:
                stat = splits[0].get("stat", {})
                if group == "hitting" and float(stat.get("plateAppearances",0) or 0) >= MIN_PA:
                    return stat
                if group == "pitching" and float(stat.get("inningsPitched",0) or 0) >= MIN_IP:
                    return stat
        except Exception:
            pass
    return {}


@st.cache_data(ttl=86400, show_spinner=False)
def mlb_player_info(name: str) -> dict:
    try:
        q = requests.utils.quote(name)
        r = requests.get(f"{MLB_API}/people/search?names={q}&sportId=1", timeout=8)
        r.raise_for_status()
        people = r.json().get("people", [])
        if not people: return {}
        p = min(people, key=lambda x: abs(len(x.get("fullName","")) - len(name)))
        return {
            "mlb_id":   p["id"],
            "pos_code": p.get("primaryPosition",{}).get("abbreviation",""),
            "team":     p.get("currentTeam",{}).get("abbreviation","?"),
        }
    except Exception:
        return {}

# =============================================================================
#  SCORING HELPERS
# =============================================================================
def hitter_pts(stats: dict) -> float:
    pa = float(stats.get("plateAppearances",0) or 0)
    if pa < MIN_PA: return 0.0
    ab=float(stats.get("atBats",0) or 0); h=float(stats.get("hits",0) or 0)
    d=float(stats.get("doubles",0) or 0); t=float(stats.get("triples",0) or 0)
    hr=float(stats.get("homeRuns",0) or 0); bb=float(stats.get("baseOnBalls",0) or 0)
    hp=float(stats.get("hitByPitch",0) or 0); sb=float(stats.get("stolenBases",0) or 0)
    cs=float(stats.get("caughtStealing",0) or 0); s=max(0.0,h-d-t-hr)
    ppa=((s/pa)*H_1B+(d/pa)*H_2B+(t/pa)*H_3B+(hr/pa)*H_HR+(bb/pa)*H_BB+
         (hp/pa)*H_HBP+(sb/pa)*H_SB+(cs/pa)*H_CS+(ab/pa)*H_AB)
    return round(ppa*DEFAULT_HITTER_PA, 2)


def pitcher_pts(stats: dict, is_sp: bool) -> float:
    ip=float(stats.get("inningsPitched",0) or 0)
    if ip < MIN_IP: return 0.0
    outs=max(ip*3,1); g=max(float(stats.get("gamesPlayed",1) or 1),1)
    k=float(stats.get("strikeOuts",0) or 0); bb=float(stats.get("baseOnBalls",0) or 0)
    hp=float(stats.get("hitByPitch",0) or 0); hr=float(stats.get("homeRuns",0) or 0)
    sv=float(stats.get("saves",0) or 0); hld=float(stats.get("holds",0) or 0)
    eo=DEFAULT_SP_IP*3
    pts=(eo*P_OUT+(k/outs)*eo*P_K+(bb/outs)*eo*P_BB+(hp/outs)*eo*P_HBP+
         (hr/outs)*eo*P_HR+(sv/g)*P_SV+(hld/g)*P_HLD)
    return round(pts*(SP_MULT if is_sp else 1.0), 2)


def blend_pts(season_pts: float, board_pts: float | None, weight: float) -> float:
    """
    Blend season-rate projection with last game's actual board score.
    weight = 0.0  → pure season projection
    weight = 1.0  → pure board score
    If board_pts is None, fall back to season projection entirely.
    """
    if board_pts is None or weight == 0.0:
        return season_pts
    return round((1 - weight) * season_pts + weight * board_pts, 2)


def salary_lookup(name: str, sal_dict: dict) -> float | None:
    k = name.strip().lower()
    if k in sal_dict: return sal_dict[k]
    k2 = re.sub(r"\s+(jr\.?|sr\.?|ii|iii|iv)$","",k).strip()
    if k2 in sal_dict: return sal_dict[k2]
    best_r, best_v = 0.0, None
    for dk, dv in sal_dict.items():
        r = SequenceMatcher(None, k, dk).ratio()
        if r > best_r: best_r, best_v = r, dv
    return best_v if best_r >= 0.82 else None

# =============================================================================
#  BUILD POOL
# =============================================================================
def build_pool(board_rows: list[dict], perf_weight: float) -> list:
    sal_dict = {r["name"].strip().lower(): r["price"] for r in board_rows}
    board_pts_dict = {r["name"].strip().lower(): r["board_pts"] for r in board_rows}

    # Confirmed lineups + SPs
    games = fetch_today_games()
    confirmed_batters, confirmed_sps = [], []
    if games:
        seen = set()
        for game in games:
            for side in ("away","home"):
                ti   = game.get("teams",{}).get(side,{})
                abbr = ti.get("team",{}).get("abbreviation","?")
                prob = ti.get("probablePitcher")
                if prob and prob.get("id") not in seen:
                    seen.add(prob["id"])
                    confirmed_sps.append({"mlb_id":prob["id"],
                                          "name":prob.get("fullName","?"),"team":abbr})
            lineups = game.get("lineups",{})
            for key, side in [("awayPlayers","away"),("homePlayers","home")]:
                abbr = game.get("teams",{}).get(side,{}).get("team",{}).get("abbreviation","?")
                for p in lineups.get(key,[]):
                    pid = p.get("id"); pos = p.get("primaryPosition",{}).get("abbreviation","")
                    if pid and pid not in seen and pos != "P":
                        seen.add(pid)
                        confirmed_batters.append({"mlb_id":pid,"name":p.get("fullName","?"),
                                                  "pos_code":pos,"team":abbr})

    confirmed_batter_map = {p["name"].lower(): p for p in confirmed_batters}
    confirmed_sp_map     = {p["name"].lower(): p for p in confirmed_sps}
    lineups_posted       = len(confirmed_batters) > 0

    pool = []
    seen_names = set()

    for row in board_rows:
        name  = row["name"]
        price = row["price"]
        key   = name.lower()
        if key in seen_names: continue
        seen_names.add(key)
        bp = board_pts_dict.get(key)

        if key in confirmed_sp_map:
            info  = confirmed_sp_map[key]
            stats = mlb_stats(info["mlb_id"], "pitching")
            sp    = pitcher_pts(stats, is_sp=True)
            pts   = blend_pts(sp, bp, perf_weight)
            pool.append({"name":name,"team":info["team"],"slots":["SP"],
                         "salary":price,"pts":pts,"season_pts":sp,"board_pts":bp,
                         "value":round(pts/price,3),"confirmed":True})
            continue

        if key in confirmed_batter_map and lineups_posted:
            info  = confirmed_batter_map[key]
            slots = POS_TO_SLOT.get(info["pos_code"],["CI"])
            stats = mlb_stats(info["mlb_id"], "hitting")
            sp    = hitter_pts(stats)
            pts   = blend_pts(sp, bp, perf_weight)
            pool.append({"name":name,"team":info["team"],"slots":slots,
                         "salary":price,"pts":pts,"season_pts":sp,"board_pts":bp,
                         "value":round(pts/price,3),"confirmed":True})
            continue

        # Not in confirmed lineup — look up via MLB search
        info = mlb_player_info(name)
        if not info: continue
        pos_code = info.get("pos_code",""); mlb_id = info.get("mlb_id")
        team     = info.get("team","?")

        if pos_code in ("SP","RP","P"):
            slots = ["SP"] if pos_code in ("SP","P") else ["RP"]
            stats = mlb_stats(mlb_id, "pitching")
            sp    = pitcher_pts(stats, is_sp=(slots==["SP"]))
        else:
            slots = POS_TO_SLOT.get(pos_code, ["CI"])
            stats = mlb_stats(mlb_id, "hitting")
            sp    = hitter_pts(stats)

        pts = blend_pts(sp, bp, perf_weight)
        pool.append({"name":name,"team":team,"slots":slots,
                     "salary":price,"pts":pts,"season_pts":sp,"board_pts":bp,
                     "value":round(pts/price,3),"confirmed":False})

    # Add SPs not on board
    for p in confirmed_sps:
        if p["name"].lower() in seen_names: continue
        sal   = salary_lookup(p["name"], sal_dict) or 10.0
        stats = mlb_stats(p["mlb_id"], "pitching")
        sp    = pitcher_pts(stats, is_sp=True)
        bp    = board_pts_dict.get(p["name"].lower())
        pts   = blend_pts(sp, bp, perf_weight)
        pool.append({"name":p["name"],"team":p["team"],"slots":["SP"],
                     "salary":sal,"pts":pts,"season_pts":sp,"board_pts":bp,
                     "value":round(pts/sal,3),"confirmed":True})
        seen_names.add(p["name"].lower())

    # Add closers
    for name in KNOWN_CLOSERS:
        if name.lower() in seen_names: continue
        sal = salary_lookup(name, sal_dict) or 9.0
        sp  = round(4.2 + (sal - 9.0) * 0.22, 2)
        bp  = board_pts_dict.get(name.lower())
        pts = blend_pts(sp, bp, perf_weight)
        pool.append({"name":name,"team":"?","slots":["RP"],
                     "salary":sal,"pts":pts,"season_pts":sp,"board_pts":bp,
                     "value":round(pts/sal,3),"confirmed":False})

    return pool


def best_lineup(pool: list) -> dict:
    pools = {s: sorted([p for p in pool if s in p["slots"]],
                        key=lambda p: p["pts"], reverse=True)[:14]
             for s in SLOTS}
    best = {"pts":-999,"lineup":None,"salary":0}
    for combo in itertools.product(*[pools[s] for s in SLOTS]):
        if len({p["name"] for p in combo}) < 6: continue
        sal = sum(p["salary"] for p in combo)
        if sal > SALARY_CAP: continue
        pts = sum(p["pts"] for p in combo)
        if pts > best["pts"]: best = {"pts":pts,"lineup":combo,"salary":sal}
    return best

# =============================================================================
#  UI COMPONENTS
# =============================================================================
def player_card(rank: int, p: dict):
    is_top = rank == 1
    confirmed_badge = " ✅" if p.get("confirmed") else ""
    bp_str = f"  ·  Last game: {p['board_pts']:+.1f}" if p.get("board_pts") is not None else ""
    sal_flag = "" if p.get("sal_ok", True) else " *"

    st.markdown(f"""
    <div class="player-card {'top' if is_top else ''}">
      <span class="pts">{p['pts']:.1f} pts</span>
      <div class="rank">#{rank}</div>
      <div class="pname">{p['name']}{confirmed_badge}</div>
      <div class="meta">{p['team']}  ·  ${p['salary']:.2f}{sal_flag}  ·  {p['value']:.3f} pts/$  {bp_str}</div>
    </div>
    """, unsafe_allow_html=True)


def optimal_card(slots, lineup):
    total_sal = sum(p["salary"] for p in lineup)
    total_pts = sum(p["pts"] for p in lineup)
    rem = SALARY_CAP - total_sal
    for slot, p in zip(slots, lineup):
        st.markdown(f"""
        <div class="optimal-slot">
          <span class="slot-label">{slot}</span>
          <span class="slot-name">{p['name']} <small style="color:#666">{p['team']}</small></span>
          <span class="slot-sal">${p['salary']:.2f}</span>
          <span class="slot-pts">{p['pts']:.1f}</span>
        </div>""", unsafe_allow_html=True)
    st.markdown(f"""
    <div style="display:flex;justify-content:space-between;padding:8px 12px;
                margin-top:4px;border-top:1px solid #2a3a4a;font-size:0.9rem">
      <span>Total: <b>${total_sal:.2f}</b></span>
      <span>Remaining: <b style="color:#aaa">${rem:.2f}</b></span>
      <span>Proj pts: <b style="color:#2ecc71">{total_pts:.1f}</b></span>
    </div>""", unsafe_allow_html=True)

# =============================================================================
#  MAIN APP
# =============================================================================
def main():
    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown("## ⚾ Six Picks Optimizer")
    st.caption(f"{TODAY}  ·  Cap: ${SALARY_CAP:.0f}  ·  C · CI · MI · OF · SP · RP")

    # ── Controls ──────────────────────────────────────────────────────────────
    with st.expander("⚙️ Settings", expanded=False):
        perf_weight = st.slider(
            "Recent performance weight",
            min_value=0.0, max_value=1.0, value=0.25, step=0.05,
            help=(
                "Blends last game's actual score (board PTS) into the projection. "
                "0 = pure season-rate stats, 1 = pure last game score, "
                "0.25 = 75% season + 25% last game (recommended)"
            ),
        )
        st.caption(f"Projection = {(1-perf_weight)*100:.0f}% season stats + {perf_weight*100:.0f}% last game")

    refresh = st.button("🔄 Refresh Data", use_container_width=True)
    if refresh:
        st.cache_data.clear()

    # ── Load data ─────────────────────────────────────────────────────────────
    with st.spinner("Fetching Big Board prices..."):
        board_rows = fetch_board()

    if not board_rows:
        st.error("Could not load the Big Board. It may not be posted yet — check back later.")
        st.markdown(f"[Open Big Board ↗]({BOARD_URL})")
        return

    # Board summary
    col1, col2, col3 = st.columns(3)
    col1.metric("Board players", len(board_rows))
    col2.metric("Date", TODAY)
    col3.metric("Cap", f"${SALARY_CAP:.0f}")

    with st.spinner("Loading lineup + stats data..."):
        games  = fetch_today_games()
        pool   = build_pool(board_rows, perf_weight)

    lineups_posted = any(p.get("confirmed") for p in pool)
    if not lineups_posted:
        st.warning("⏳ Lineups not yet posted — showing board player projections. Re-run ~1hr before first pitch.")
    else:
        conf = sum(1 for p in pool if p.get("confirmed"))
        st.success(f"✅ {conf} confirmed starters in today's pool ({len(games)} games)")

    st.divider()

    # ── Top 5 per slot ─────────────────────────────────────────────────────────
    for slot in SLOTS:
        icon  = SLOT_ICONS[slot]
        label = SLOT_LABELS[slot]
        top   = sorted([p for p in pool if slot in p["slots"]],
                        key=lambda p: p["pts"], reverse=True)[:5]

        with st.expander(f"{icon} **{label}**", expanded=(slot in ("SP","OF","CI"))):
            if not top:
                st.caption("No players found for this slot today.")
            else:
                for i, p in enumerate(top, 1):
                    player_card(i, p)

    st.divider()

    # ── Optimal lineup ────────────────────────────────────────────────────────
    st.markdown("### 🏆 Optimal Lineup")
    with st.spinner("Calculating best lineup..."):
        best = best_lineup(pool)

    if best["lineup"]:
        optimal_card(SLOTS, best["lineup"])
    else:
        st.warning("No valid lineup found under the $120 cap.")

    st.divider()

    # ── Best value per slot ───────────────────────────────────────────────────
    st.markdown("### 💰 Best Value per Slot")
    cols = st.columns(2)
    for i, slot in enumerate(SLOTS):
        elig = [p for p in pool if slot in p["slots"]]
        if not elig: continue
        bv = max(elig, key=lambda p: p["value"])
        with cols[i % 2]:
            st.markdown(f"""
            <div class="player-card">
              <span class="pts">{bv['value']:.3f}</span>
              <div class="rank">{SLOT_ICONS[slot]} {SLOT_LABELS[slot]}</div>
              <div class="pname">{bv['name']}</div>
              <div class="meta">${bv['salary']:.2f}  ·  {bv['pts']:.1f} pts</div>
            </div>""", unsafe_allow_html=True)

    st.divider()

    # ── Big Board table ───────────────────────────────────────────────────────
    with st.expander("📋 Full Big Board", expanded=False):
        import pandas as pd
        df = pd.DataFrame([{
            "Player":    r["name"],
            "Price":     f"${r['price']:.2f}",
            "Pick%":     f"{r['pick_pct']:.1f}%" if r["pick_pct"] else "—",
            "Last PTS":  r["board_pts"] if r["board_pts"] is not None else "—",
        } for r in sorted(board_rows, key=lambda x: x["price"], reverse=True)])
        st.dataframe(df, use_container_width=True, hide_index=True)

    # ── Footer ────────────────────────────────────────────────────────────────
    st.caption(
        f"Prices: [ottoneu.fangraphs.com/sixpicks/baseball/board]({BOARD_URL})  ·  "
        f"Stats: MLB API {SEASON} (fallback to {SEASON-1})  ·  "
        f"* = price not on board, default used"
    )


if __name__ == "__main__":
    main()
