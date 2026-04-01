"""
Ottoneu Six Picks Optimizer — Streamlit Edition
================================================
Deploy free: push to GitHub + share.streamlit.io

New in this version:
  • Platoon splits  — batter stats vs opposing SP's hand (L/R)
  • Park factors    — per-stadium run environment multiplier
  • Batting order   — leadoff/2-hole get more projected PAs
  • Recent form     — last-14-day stats blended with season
  • Season fallback — uses prior year if current season is thin
  • Performance     — last board score blended into projection
"""

import re, itertools
from datetime import date
from difflib import SequenceMatcher

import requests
import streamlit as st
from bs4 import BeautifulSoup

# ── page config (must be first) ───────────────────────────────────────────────
st.set_page_config(page_title="Six Picks Optimizer", page_icon="⚾",
                   layout="centered", initial_sidebar_state="collapsed")

st.markdown("""
<style>
  .block-container { padding: 1rem 1rem 2rem; max-width: 720px; }
  .card {
    background:#1a2535; border-radius:10px; padding:10px 14px;
    margin-bottom:7px; border-left:4px solid #f0a500;
  }
  .card.best { border-left-color:#2ecc71; }
  .card .rank { color:#777; font-size:.75rem; }
  .card .pname { font-weight:700; font-size:.97rem; }
  .card .meta { color:#999; font-size:.78rem; margin-top:2px; }
  .card .pts { float:right; font-size:1.05rem; font-weight:700; color:#f0a500; }
  .opt-row {
    display:flex; justify-content:space-between; align-items:center;
    background:#101e2e; border-radius:7px; padding:7px 11px; margin-bottom:5px;
  }
  .opt-slot { color:#777; font-size:.72rem; width:34px; }
  .opt-name { font-weight:600; flex:1; padding:0 8px; font-size:.9rem; }
  .opt-badges { color:#aaa; font-size:.75rem; }
  .opt-pts { color:#2ecc71; font-weight:700; }
  #MainMenu, footer { visibility:hidden; }
</style>
""", unsafe_allow_html=True)

# =============================================================================
#  CONSTANTS
# =============================================================================
TODAY       = date.today().strftime("%Y-%m-%d")
SEASON      = date.today().year
SALARY_CAP  = 120.0
MLB_API     = "https://statsapi.mlb.com/api/v1"
BOARD_URL   = "https://ottoneu.fangraphs.com/sixpicks/baseball/board"

DEFAULT_PA      = 4.0   # typical PA per game
DEFAULT_SP_IP   = 5.5
LEADOFF_PA      = 4.6   # spots 1-2 see more PAs
BOTTOM_PA       = 3.6   # spots 7-9 see fewer
MIN_PA          = 10
MIN_IP          = 3.0
MIN_SPLIT_PA    = 30    # minimum PA in a split to trust it

REQ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,*/*", "Accept-Language": "en-US,en;q=0.9",
}

# =============================================================================
#  SCORING (FanGraphs Points / Six Picks rules)
# =============================================================================
H_1B=5.6; H_2B=7.5; H_3B=10.5; H_HR=14.0
H_BB=3.0; H_HBP=3.0; H_SB=1.9; H_CS=-2.8; H_AB=-1.0
P_OUT=2.8/3; P_K=2.0; P_BB=-3.0; P_HBP=-3.0; P_HR=-13.0
P_SV=5.0; P_HLD=4.0
SP_MULT = 0.5

SLOTS = ["C","CI","MI","OF","SP","RP"]
SLOT_LABELS = {"C":"Catcher","CI":"Corner IF","MI":"Middle IF",
               "OF":"Outfield","SP":"Starter","RP":"Reliever"}
SLOT_ICONS  = {"C":"🎯","CI":"💪","MI":"⚡","OF":"🏃","SP":"🔥","RP":"🔒"}

POS_TO_SLOT = {
    "C":["C"],"1B":["CI"],"3B":["CI"],
    "2B":["MI"],"SS":["MI"],
    "LF":["OF"],"CF":["OF"],"RF":["OF"],"OF":["OF"],
}

KNOWN_CLOSERS = [
    "Ryan Helsley", "Jhoan Duran","Mason Miller",
    "Tanner Scott","Josh Hader","Felix Bautista","Edwin Diaz",
    "Clay Holmes","Devin Williams","Pete Fairbanks","Jordan Romano",
    "David Bednar","Jeff Hoffman","Andres Munoz",
    "Camilo Doval","Evan Phillips",
]

# =============================================================================
#  PARK FACTORS  (FanGraphs basic park factor, 100 = neutral, per team abbr)
#  Values > 100 favour hitters; < 100 favour pitchers.
#  Source: FanGraphs Park Factors (3-yr rolling average, updated annually)
# =============================================================================
PARK_FACTORS: dict[str, float] = {
    # Hitter-friendly
    "COL": 1.15,  # Coors Field
    "CIN": 1.07,  # Great American Ball Park
    "ARI": 1.05,  # Chase Field
    "BOS": 1.04,  # Fenway Park
    "TEX": 1.03,  # Globe Life Field
    "PHI": 1.03,  # Citizens Bank Park
    "CHC": 1.02,  # Wrigley Field
    "NYY": 1.02,  # Yankee Stadium
    "CWS": 1.02,  # Guaranteed Rate Field
    "TOR": 1.01,  # Rogers Centre
    "BAL": 1.01,  # Camden Yards
    # Neutral
    "STL": 1.00,  # Busch Stadium
    "MIL": 1.00,  # American Family Field
    "WSH": 1.00,  # Nationals Park
    "HOU": 0.99,  # Minute Maid Park
    "CLE": 0.99,  # Progressive Field
    "ATL": 0.99,  # Truist Park
    # Pitcher-friendly
    "MIN": 0.98,  # Target Field
    "KC":  0.97,  # Kauffman Stadium
    "DET": 0.97,  # Comerica Park
    "LAA": 0.97,  # Angel Stadium
    "ATH": 0.97,  # Sacramento Ballpark
    "NYM": 0.97,  # Citi Field
    "PIT": 0.96,  # PNC Park
    "LAD": 0.96,  # Dodger Stadium
    "TB":  0.96,  # Tropicana Field
    "MIA": 0.95,  # loanDepot park
    "SEA": 0.95,  # T-Mobile Park
    "SD":  0.94,  # Petco Park
    "SF":  0.93,  # Oracle Park
}
DEFAULT_PARK_FACTOR = 1.00

# =============================================================================
#  MLB STATS API  (all cached)
# =============================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_board() -> list[dict]:
    try:
        r = requests.get(BOARD_URL, headers=REQ_HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        st.error(f"Board fetch failed: {e}"); return []

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
        return []

    thead = table.find("thead")
    cols  = [th.get_text(strip=True).upper()
             for th in thead.find_all("th")] if thead else []

    def cidx(keys, default):
        for k in keys:
            for i, h in enumerate(cols):
                if k in h: return i
        return default

    nc = cidx(["NAME","PLAYER"], 0)
    pc = cidx(["PRICE","SALARY","COST"], 1)
    ec = cidx(["PICK","PCT"], 2)
    tc = cidx(["PTS","POINTS","SCORE"], 3)

    def flt(cells, i):
        if i is None or i >= len(cells): return None
        raw = cells[i].get_text(strip=True).replace("$","").replace("%","").replace(",","")
        try: return float(raw.strip())
        except ValueError: return None

    rows = []
    tbody = table.find("tbody")
    for tr in (tbody.find_all("tr") if tbody else table.find_all("tr")[1:]):
        cells = tr.find_all("td")
        if len(cells) < 2: continue
        nc_cell = cells[nc] if nc < len(cells) else cells[0]
        a = nc_cell.find("a")
        name = (a or nc_cell).get_text(strip=True)
        if not name or name.upper() in ("NAME","PLAYER",""): continue
        price = flt(cells, pc)
        if price is None or not (0.5 <= price <= 200): continue
        rows.append({"name":name, "price":price,
                     "pick_pct":flt(cells, ec), "board_pts":flt(cells, tc)})
    return rows


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_today_games() -> list[dict]:
    """Returns enriched game dicts including venue team abbr and SP hand."""
    url = (f"{MLB_API}/schedule?sportId=1&date={TODAY}"
           f"&hydrate=lineups,probablePitcher(pitchHand),team,venue&gameType=R")
    try:
        r = requests.get(url, timeout=12); r.raise_for_status()
        return [g for d in r.json().get("dates",[]) for g in d.get("games",[])]
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def mlb_season_stats(mlb_id: int, group: str) -> dict:
    """Season stats with prior-year fallback if current season is thin."""
    for season in [SEASON, SEASON - 1]:
        try:
            url = (f"{MLB_API}/people/{mlb_id}/stats"
                   f"?stats=season&season={season}&group={group}&gameType=R")
            r = requests.get(url, timeout=8); r.raise_for_status()
            splits = r.json().get("stats",[{}])[0].get("splits",[])
            if splits:
                stat = splits[0].get("stat", {})
                if group == "hitting"  and float(stat.get("plateAppearances",0) or 0) >= MIN_PA:
                    return stat
                if group == "pitching" and float(stat.get("inningsPitched",0) or 0) >= MIN_IP:
                    return stat
        except Exception:
            pass
    return {}


@st.cache_data(ttl=3600, show_spinner=False)
def mlb_split_stats(mlb_id: int, hand: str) -> dict:
    """
    Hitting stats vs a specific pitcher hand.
    hand: "L" or "R"
    Uses statSplits with sitCodes vl (vs LHP) or vr (vs RHP).
    Falls back to prior season if current is thin.
    """
    sit = "vl" if hand == "L" else "vr"
    for season in [SEASON, SEASON - 1]:
        try:
            url = (f"{MLB_API}/people/{mlb_id}/stats"
                   f"?stats=statSplits&season={season}&group=hitting"
                   f"&gameType=R&sitCodes={sit}")
            r = requests.get(url, timeout=8); r.raise_for_status()
            splits = r.json().get("stats",[{}])[0].get("splits",[])
            if splits:
                stat = splits[0].get("stat", {})
                if float(stat.get("plateAppearances", 0) or 0) >= MIN_SPLIT_PA:
                    return stat
        except Exception:
            pass
    return {}


@st.cache_data(ttl=3600, show_spinner=False)
def mlb_recent_stats(mlb_id: int, group: str, days: int = 14) -> dict:
    """Last N days stats (uses lastXDays stat type)."""
    try:
        url = (f"{MLB_API}/people/{mlb_id}/stats"
               f"?stats=lastXDays&season={SEASON}&group={group}"
               f"&gameType=R&limit={days}")
        r = requests.get(url, timeout=8); r.raise_for_status()
        splits = r.json().get("stats",[{}])[0].get("splits",[])
        if splits:
            stat = splits[0].get("stat", {})
            if group == "hitting"  and float(stat.get("plateAppearances",0) or 0) >= 5:
                return stat
            if group == "pitching" and float(stat.get("inningsPitched",0) or 0) >= 1:
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
def _hitter_ppa(stats: dict) -> float:
    """Points per plate appearance from a stats dict."""
    pa = float(stats.get("plateAppearances",0) or 0)
    if pa < 1: return 0.0
    ab=float(stats.get("atBats",0) or 0)
    h=float(stats.get("hits",0) or 0)
    d=float(stats.get("doubles",0) or 0)
    t=float(stats.get("triples",0) or 0)
    hr=float(stats.get("homeRuns",0) or 0)
    bb=float(stats.get("baseOnBalls",0) or 0)
    hp=float(stats.get("hitByPitch",0) or 0)
    sb=float(stats.get("stolenBases",0) or 0)
    cs=float(stats.get("caughtStealing",0) or 0)
    s=max(0.0, h-d-t-hr)
    return ((s/pa)*H_1B+(d/pa)*H_2B+(t/pa)*H_3B+(hr/pa)*H_HR+
            (bb/pa)*H_BB+(hp/pa)*H_HBP+(sb/pa)*H_SB+
            (cs/pa)*H_CS+(ab/pa)*H_AB)


def hitter_pts(season_stats: dict, split_stats: dict, recent_stats: dict,
               expected_pa: float, park_factor: float,
               recent_weight: float, split_weight: float) -> tuple[float, str]:
    """
    Compute projected FG pts for a hitter.
    Returns (projected_pts, explanation_string).

    Methodology:
      1. Base  = season stats ppa × expected_pa
      2. Split = vs-hand stats ppa × expected_pa  (if enough sample)
      3. Recent= last-14-days ppa × expected_pa   (if enough sample)
      4. Blend = (1 - split_w) * base + split_w * split
               then (1 - recent_w) * blended + recent_w * recent
      5. Park  = blended × park_factor
    """
    notes = []

    base_ppa = _hitter_ppa(season_stats)
    pa_season = float(season_stats.get("plateAppearances",0) or 0)
    if pa_season < MIN_PA:
        return 0.0, "insufficient season data"

    base_pts = base_ppa * expected_pa

    # Platoon split
    split_pa = float(split_stats.get("plateAppearances", 0) or 0)
    if split_pa >= MIN_SPLIT_PA and split_weight > 0:
        split_ppa = _hitter_ppa(split_stats)
        blended = (1 - split_weight) * base_pts + split_weight * (split_ppa * expected_pa)
        notes.append(f"split({int(split_pa)}PA)")
    else:
        blended = base_pts
        if split_weight > 0:
            notes.append("split(n/a)")

    # Recent form
    recent_pa = float(recent_stats.get("plateAppearances", 0) or 0)
    if recent_pa >= 5 and recent_weight > 0:
        recent_ppa  = _hitter_ppa(recent_stats)
        recent_pts  = recent_ppa * expected_pa
        blended = (1 - recent_weight) * blended + recent_weight * recent_pts
        notes.append(f"L14({int(recent_pa)}PA)")

    # Park factor
    blended *= park_factor
    if park_factor != 1.0:
        notes.append(f"park×{park_factor:.2f}")

    explanation = ", ".join(notes) if notes else "season only"
    return round(blended, 2), explanation


def pitcher_pts(season_stats: dict, recent_stats: dict,
                is_sp: bool, recent_weight: float) -> tuple[float, str]:
    """Compute projected FG pts for a pitcher. Returns (pts, explanation)."""
    ip = float(season_stats.get("inningsPitched",0) or 0)
    if ip < MIN_IP:
        return 0.0, "insufficient data"

    def _pts_from(stats, exp_ip):
        ip_ = float(stats.get("inningsPitched",0) or 0)
        if ip_ < 0.5: return 0.0
        outs=max(ip_*3,1); g=max(float(stats.get("gamesPlayed",1) or 1),1)
        k=float(stats.get("strikeOuts",0) or 0)
        bb=float(stats.get("baseOnBalls",0) or 0)
        hp=float(stats.get("hitByPitch",0) or 0)
        hr=float(stats.get("homeRuns",0) or 0)
        sv=float(stats.get("saves",0) or 0)
        hld=float(stats.get("holds",0) or 0)
        eo=exp_ip*3
        pts=(eo*P_OUT+(k/outs)*eo*P_K+(bb/outs)*eo*P_BB+
             (hp/outs)*eo*P_HBP+(hr/outs)*eo*P_HR+
             (sv/g)*P_SV+(hld/g)*P_HLD)
        return pts*(SP_MULT if is_sp else 1.0)

    exp_ip = DEFAULT_SP_IP if is_sp else 1.0
    base   = _pts_from(season_stats, exp_ip)
    notes  = []

    rip = float(recent_stats.get("inningsPitched",0) or 0) if recent_stats else 0
    if rip >= 1.0 and recent_weight > 0:
        recent = _pts_from(recent_stats, exp_ip)
        base   = (1 - recent_weight) * base + recent_weight * recent
        notes.append(f"L14({rip:.1f}IP)")

    return round(base, 2), (", ".join(notes) if notes else "season only")


def blend_board(proj_pts: float, board_pts: float | None,
                board_weight: float) -> float:
    if board_pts is None or board_weight == 0: return proj_pts
    return round((1 - board_weight) * proj_pts + board_weight * board_pts, 2)


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
#  GAME / LINEUP EXTRACTION
# =============================================================================
def extract_game_context(games: list[dict]) -> tuple[list, list, dict, dict]:
    """
    Returns:
      confirmed_batters  — list of batter dicts (with batting_order, opp_sp_hand, home_team)
      confirmed_sps      — list of SP dicts
      venue_by_team      — {team_abbr: home_team_abbr}  (for park factor lookup)
      sp_hand_by_team    — {batting_team_abbr: pitcher_hand "L"/"R"}
    """
    batters, starters, seen = [], [], set()
    venue_by_team   = {}   # team → home team (for park factor)
    sp_hand_by_team = {}   # batting team → opposing SP hand

    for game in games:
        home_team = game.get("teams",{}).get("home",{}).get("team",{}).get("abbreviation","?")
        away_team = game.get("teams",{}).get("away",{}).get("team",{}).get("abbreviation","?")

        # Both teams play in the home park
        venue_by_team[home_team] = home_team
        venue_by_team[away_team] = home_team

        for side in ("away","home"):
            ti    = game.get("teams",{}).get(side,{})
            abbr  = ti.get("team",{}).get("abbreviation","?")
            prob  = ti.get("probablePitcher")
            if prob and prob.get("id") not in seen:
                seen.add(prob["id"])
                hand = prob.get("pitchHand",{}).get("code","") or ""
                starters.append({
                    "mlb_id": prob["id"],
                    "name":   prob.get("fullName","?"),
                    "team":   abbr,
                    "hand":   hand,
                })
                # The opposing batters face this pitcher
                opp = away_team if side == "home" else home_team
                sp_hand_by_team[opp] = hand

        lineups = game.get("lineups",{})
        for key, side in [("awayPlayers","away"),("homePlayers","home")]:
            abbr = game.get("teams",{}).get(side,{}).get("team",{}).get("abbreviation","?")
            for order_idx, p in enumerate(lineups.get(key,[]), 1):
                pid = p.get("id")
                pos = p.get("primaryPosition",{}).get("abbreviation","")
                if pid and pid not in seen and pos != "P":
                    seen.add(pid)
                    batters.append({
                        "mlb_id":       pid,
                        "name":         p.get("fullName","?"),
                        "pos_code":     pos,
                        "team":         abbr,
                        "batting_order": order_idx,
                        "opp_sp_hand":  sp_hand_by_team.get(abbr, ""),
                        "home_team":    home_team,
                    })

    return batters, starters, venue_by_team, sp_hand_by_team


def pa_for_order(order: int) -> float:
    """Adjust expected PA by lineup position."""
    if order <= 2: return LEADOFF_PA
    if order <= 6: return DEFAULT_PA
    return BOTTOM_PA

# =============================================================================
#  BUILD POOL
# =============================================================================
def build_pool(board_rows: list[dict], settings: dict) -> list[dict]:
    split_weight  = settings["split_weight"]
    recent_weight = settings["recent_weight"]
    board_weight  = settings["board_weight"]

    sal_dict   = {r["name"].strip().lower(): r["price"] for r in board_rows}
    board_dict = {r["name"].strip().lower(): r["board_pts"] for r in board_rows}

    games = fetch_today_games()
    if not games:
        st.warning("No games found today."); return []

    batters, starters, venue_by_team, sp_hand_by_team = extract_game_context(games)
    lineups_posted = len(batters) > 0

    conf_batter_map = {p["name"].lower(): p for p in batters}
    conf_sp_map     = {p["name"].lower(): p for p in starters}

    pool       = []
    seen_names = set()

    # ── Board players (primary universe) ─────────────────────────────────────
    for row in board_rows:
        name  = row["name"]
        price = row["price"]
        key   = name.lower()
        if key in seen_names: continue
        seen_names.add(key)
        bp = board_dict.get(key)

        # ── Confirmed SP ─────────────────────────────────────────────────────
        if key in conf_sp_map:
            info       = conf_sp_map[key]
            home_team  = venue_by_team.get(info["team"], info["team"])
            season_st  = mlb_season_stats(info["mlb_id"], "pitching")
            recent_st  = mlb_recent_stats(info["mlb_id"], "pitching")
            pts, notes = pitcher_pts(season_st, recent_st, is_sp=True,
                                     recent_weight=recent_weight)
            pts = blend_board(pts, bp, board_weight)
            pool.append({
                "name":info["name"],"team":info["team"],"slots":["SP"],
                "salary":price,"pts":pts,"value":round(pts/price,3) if price else 0,
                "sal_ok":True,"confirmed":True,
                "badges":f"🤚{info['hand']}  {notes}",
                "hand":info["hand"],"park":home_team,
            })
            continue

        # ── Confirmed batter ──────────────────────────────────────────────────
        if key in conf_batter_map and lineups_posted:
            info       = conf_batter_map[key]
            slots      = POS_TO_SLOT.get(info["pos_code"],["CI"])
            home_team  = venue_by_team.get(info["team"], info["team"])
            pf         = PARK_FACTORS.get(home_team, DEFAULT_PARK_FACTOR)
            opp_hand   = info.get("opp_sp_hand","")
            exp_pa     = pa_for_order(info.get("batting_order", 5))

            season_st  = mlb_season_stats(info["mlb_id"], "hitting")
            split_st   = mlb_split_stats(info["mlb_id"], opp_hand) if opp_hand else {}
            recent_st  = mlb_recent_stats(info["mlb_id"], "hitting")

            pts, notes = hitter_pts(season_st, split_st, recent_st,
                                    exp_pa, pf, recent_weight, split_weight)
            pts = blend_board(pts, bp, board_weight)
            hand_tag   = f"vs{opp_hand}" if opp_hand else ""
            park_tag   = f"{'🏔' if pf>1.02 else '🏟' if pf<0.97 else ''}{home_team}"
            pool.append({
                "name":info["name"],"team":info["team"],"slots":slots,
                "salary":price,"pts":pts,"value":round(pts/price,3) if price else 0,
                "sal_ok":True,"confirmed":True,
                "badges":f"{hand_tag} {park_tag} #{info['batting_order']}  {notes}",
                "opp_hand":opp_hand,"park":home_team,"pf":pf,
            })
            continue

        # ── Board player not in confirmed lineup ──────────────────────────────
        info = mlb_player_info(name)
        if not info: continue
        pos_code  = info.get("pos_code","")
        mlb_id    = info.get("mlb_id")
        team      = info.get("team","?")
        home_team = venue_by_team.get(team, team)
        pf        = PARK_FACTORS.get(home_team, DEFAULT_PARK_FACTOR)
        opp_hand  = sp_hand_by_team.get(team,"")

        if pos_code in ("SP","RP","P"):
            slots = ["SP"] if pos_code in ("SP","P") else ["RP"]
            s_st  = mlb_season_stats(mlb_id, "pitching")
            r_st  = mlb_recent_stats(mlb_id, "pitching")
            pts, notes = pitcher_pts(s_st, r_st, is_sp=(slots==["SP"]),
                                     recent_weight=recent_weight)
        else:
            slots  = POS_TO_SLOT.get(pos_code,["CI"])
            s_st   = mlb_season_stats(mlb_id, "hitting")
            sp_st  = mlb_split_stats(mlb_id, opp_hand) if opp_hand else {}
            r_st   = mlb_recent_stats(mlb_id, "hitting")
            pts, notes = hitter_pts(s_st, sp_st, r_st, DEFAULT_PA, pf,
                                    recent_weight, split_weight)

        pts = blend_board(pts, bp, board_weight)
        pool.append({
            "name":name,"team":team,"slots":slots,
            "salary":price,"pts":pts,"value":round(pts/price,3) if price else 0,
            "sal_ok":True,"confirmed":False,
            "badges":notes,"opp_hand":opp_hand,"park":home_team,
        })

    # ── SPs not on board ──────────────────────────────────────────────────────
    for p in starters:
        if p["name"].lower() in seen_names: continue
        sal   = salary_lookup(p["name"], sal_dict) or 10.0
        s_st  = mlb_season_stats(p["mlb_id"], "pitching")
        r_st  = mlb_recent_stats(p["mlb_id"], "pitching")
        pts, notes = pitcher_pts(s_st, r_st, is_sp=True, recent_weight=recent_weight)
        bp    = board_dict.get(p["name"].lower())
        pts   = blend_board(pts, bp, board_weight)
        pool.append({
            "name":p["name"],"team":p["team"],"slots":["SP"],
            "salary":sal,"pts":pts,"value":round(pts/sal,3) if sal else 0,
            "sal_ok":sal is not None,"confirmed":True,
            "badges":f"🤚{p['hand']}  {notes}","hand":p["hand"],
        })
        seen_names.add(p["name"].lower())

    # ── Known closers ─────────────────────────────────────────────────────────
    for name in KNOWN_CLOSERS:
        if name.lower() in seen_names: continue
        sal = salary_lookup(name, sal_dict) or 9.0
        pts = round(4.2 + (sal - 9.0) * 0.22, 2)
        bp  = board_dict.get(name.lower())
        pts = blend_board(pts, bp, board_weight)
        pool.append({
            "name":name,"team":"?","slots":["RP"],
            "salary":sal,"pts":pts,"value":round(pts/sal,3),
            "sal_ok":True,"confirmed":False,"badges":"closer est.",
        })

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
#  UI HELPERS
# =============================================================================
def player_card(rank: int, p: dict):
    top = rank == 1
    conf = " ✅" if p.get("confirmed") else ""
    badges = p.get("badges","")
    st.markdown(f"""
    <div class="card {'best' if top else ''}">
      <span class="pts">{p['pts']:.1f}</span>
      <div class="rank">#{rank}</div>
      <div class="pname">{p['name']}{conf}</div>
      <div class="meta">{p['team']} · ${p['salary']:.2f} · {p['value']:.3f} pts/$
        {'· <b style="color:#f0a500">' + badges + '</b>' if badges else ''}
      </div>
    </div>""", unsafe_allow_html=True)


def optimal_card(best: dict):
    if not best["lineup"]:
        st.warning("No valid lineup found under the $120 cap."); return
    for slot, p in zip(SLOTS, best["lineup"]):
        badges = p.get("badges","")
        st.markdown(f"""
        <div class="opt-row">
          <span class="opt-slot">{SLOT_ICONS[slot]} {slot}</span>
          <span class="opt-name">{p['name']} <small style="color:#555">{p['team']}</small></span>
          <span class="opt-badges">{badges[:30] if badges else ''}</span>
          <span style="color:#aaa;font-size:.8rem;margin:0 8px">${p['salary']:.2f}</span>
          <span class="opt-pts">{p['pts']:.1f}</span>
        </div>""", unsafe_allow_html=True)
    rem = SALARY_CAP - best["salary"]
    st.markdown(f"""
    <div style="display:flex;justify-content:space-between;padding:6px 11px;
                border-top:1px solid #1e3050;font-size:.85rem;margin-top:2px">
      <span>Total <b>${best['salary']:.2f}</b></span>
      <span>Remaining <b style="color:#aaa">${rem:.2f}</b></span>
      <span>Proj pts <b style="color:#2ecc71">{best['pts']:.1f}</b></span>
    </div>""", unsafe_allow_html=True)

# =============================================================================
#  MAIN APP
# =============================================================================
def main():
    st.markdown("## ⚾ Six Picks Optimizer")
    st.caption(f"{TODAY} · Cap ${SALARY_CAP:.0f} · C · CI · MI · OF · SP · RP")

    # ── Settings ──────────────────────────────────────────────────────────────
    with st.expander("⚙️ Settings", expanded=False):
        st.markdown("**Projection weights** — how much each factor adjusts the base season projection")
        c1, c2, c3 = st.columns(3)
        split_weight = c1.slider("Platoon split", 0.0, 1.0, 0.35, 0.05,
            help="Weight given to batter's stats vs opposing pitcher's hand (L/R). "
                 "Higher = more emphasis on platoon advantage.")
        recent_weight = c2.slider("Recent form (L14)", 0.0, 1.0, 0.20, 0.05,
            help="Weight given to last-14-day stats. "
                 "Higher = hot/cold streaks matter more.")
        board_weight = c3.slider("Last board score", 0.0, 1.0, 0.15, 0.05,
            help="Weight given to yesterday's actual Six Picks score. "
                 "0 = ignore prior day, 1 = use only prior score.")
        st.caption(
            f"Final projection = season stats blended with: "
            f"{split_weight*100:.0f}% platoon split · "
            f"{recent_weight*100:.0f}% L14 form · "
            f"{board_weight*100:.0f}% last board score · "
            f"then multiplied by park factor"
        )

    if st.button("🔄 Refresh all data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    settings = {"split_weight": split_weight,
                "recent_weight": recent_weight,
                "board_weight": board_weight}

    # ── Load board ────────────────────────────────────────────────────────────
    with st.spinner("Fetching Big Board…"):
        board_rows = fetch_board()

    if not board_rows:
        st.error("Big Board not available yet. Check back later.")
        st.markdown(f"[Open Big Board ↗]({BOARD_URL})")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Board players", len(board_rows))
    c2.metric("Cap", f"${SALARY_CAP:.0f}")
    c3.metric("Split weight", f"{split_weight*100:.0f}%")
    c4.metric("Park factor", "ON")

    # ── Build pool ────────────────────────────────────────────────────────────
    with st.spinner("Loading lineups, splits, park factors…"):
        pool = build_pool(board_rows, settings)

    if not pool:
        st.error("Could not build player pool. Try again closer to game time."); return

    lineups_posted = any(p.get("confirmed") for p in pool)
    conf_count     = sum(1 for p in pool if p.get("confirmed"))

    if not lineups_posted:
        st.warning("⏳ Lineups not yet posted. Showing board projections — re-run ~1hr before first pitch.")
    else:
        st.success(f"✅ {conf_count} confirmed starters loaded with platoon + park adjustments")

    # Park factor summary
    home_teams_today = {p.get("park","") for p in pool if p.get("park")}
    pf_notes = []
    for ht in sorted(home_teams_today):
        pf = PARK_FACTORS.get(ht, DEFAULT_PARK_FACTOR)
        if pf >= 1.03:   pf_notes.append(f"🏔 {ht} ({pf:.2f}x)")
        elif pf <= 0.96: pf_notes.append(f"🏟 {ht} ({pf:.2f}x)")
    if pf_notes:
        st.caption("Park factors in play: " + "  ·  ".join(pf_notes))

    st.divider()

    # ── Top 5 per slot ────────────────────────────────────────────────────────
    for slot in SLOTS:
        top = sorted([p for p in pool if slot in p["slots"]],
                     key=lambda p: p["pts"], reverse=True)[:5]
        with st.expander(f"{SLOT_ICONS[slot]} **{SLOT_LABELS[slot]}**",
                         expanded=(slot in ("SP","OF","CI"))):
            if not top:
                st.caption("No players found for this slot today.")
            else:
                for i, p in enumerate(top, 1):
                    player_card(i, p)

    st.divider()

    # ── Optimal lineup ────────────────────────────────────────────────────────
    st.markdown("### 🏆 Optimal Lineup")
    with st.spinner("Optimizing…"):
        best = best_lineup(pool)
    optimal_card(best)

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
            <div class="card">
              <span class="pts">{bv['value']:.3f} pts/$</span>
              <div class="rank">{SLOT_ICONS[slot]} {SLOT_LABELS[slot]}</div>
              <div class="pname">{bv['name']}</div>
              <div class="meta">${bv['salary']:.2f} · {bv['pts']:.1f} pts · {bv.get('badges','')[:35]}</div>
            </div>""", unsafe_allow_html=True)

    st.divider()

    # ── Full board ────────────────────────────────────────────────────────────
    with st.expander("📋 Full Big Board", expanded=False):
        import pandas as pd
        df = pd.DataFrame([{
            "Player":   r["name"],
            "Price":    f"${r['price']:.2f}",
            "Pick%":    f"{r['pick_pct']:.1f}%" if r["pick_pct"] is not None else "—",
            "Last PTS": r["board_pts"] if r["board_pts"] is not None else "—",
        } for r in sorted(board_rows, key=lambda x: x["price"], reverse=True)])
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.caption(
        f"Prices: [ottoneu.fangraphs.com/sixpicks/baseball/board]({BOARD_URL}) · "
        f"Stats: MLB Stats API {SEASON} (→{SEASON-1} fallback) · "
        f"Park factors: FanGraphs 3yr rolling avg"
    )


if __name__ == "__main__":
    main()