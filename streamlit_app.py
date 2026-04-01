"""
Ottoneu Six Picks Optimizer — Streamlit Edition
================================================
Deploy: push to GitHub + share.streamlit.io  (requirements.txt: streamlit requests beautifulsoup4 pandas)

Key design principles:
  SP pool    → ONLY confirmed probable pitchers from today's MLB schedule.
               The Big Board is used only for prices/pick%; a board player
               is NEVER added to the SP slot unless the schedule confirms
               they are starting today.

  Park factor → Derived directly from the game matchup.
               "Cole Ragans @ MIN" → park = MIN (Kauffman is irrelevant,
               Cole pitches at Target Field today).  Just like the Six Picks
               "Game" column shows "MIN 1:10 PM" vs "@KCR 1:10 PM".

  Hitters    → Confirmed lineup batters only once lineups post (~1hr before
               first pitch).  Board players used for price lookups only.
"""

import re, itertools
from datetime import date
from difflib import SequenceMatcher

import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Six Picks Optimizer", page_icon="⚾",
                   layout="centered", initial_sidebar_state="collapsed")

st.markdown("""
<style>
  .block-container { padding:1rem 1rem 2rem; max-width:720px; }
  .card { background:#1a2535; border-radius:10px; padding:10px 14px;
          margin-bottom:7px; border-left:4px solid #f0a500; }
  .card.best { border-left-color:#2ecc71; }
  .card .rank { color:#777; font-size:.75rem; }
  .card .pname { font-weight:700; font-size:.97rem; }
  .card .meta { color:#999; font-size:.78rem; margin-top:2px; }
  .card .pts  { float:right; font-size:1.05rem; font-weight:700; color:#f0a500; }
  .opt-row { display:flex; justify-content:space-between; align-items:center;
             background:#101e2e; border-radius:7px; padding:7px 11px; margin-bottom:5px; }
  .opt-slot  { color:#777; font-size:.72rem; width:34px; }
  .opt-name  { font-weight:600; flex:1; padding:0 8px; font-size:.9rem; }
  .opt-badge { color:#aaa; font-size:.75rem; }
  .opt-sal   { color:#aaa; font-size:.8rem; margin:0 8px; }
  .opt-pts   { color:#2ecc71; font-weight:700; }
  #MainMenu, footer { visibility:hidden; }
</style>
""", unsafe_allow_html=True)

# =============================================================================
#  CONSTANTS
# =============================================================================
TODAY      = date.today().strftime("%Y-%m-%d")
SEASON     = date.today().year
SALARY_CAP = 120.0
MLB_API    = "https://statsapi.mlb.com/api/v1"
BOARD_URL  = "https://ottoneu.fangraphs.com/sixpicks/baseball/board"

DEFAULT_PA   = 4.0
LEADOFF_PA   = 4.6   # batting spots 1–2
BOTTOM_PA    = 3.6   # batting spots 7–9
DEFAULT_IP   = 5.5   # typical SP outing
MIN_PA       = 10
MIN_IP       = 3.0
MIN_SPLIT_PA = 30    # minimum PA to trust a platoon split

REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,*/*",
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
    "Ryan Helsley","Emmanuel Clase","Jhoan Duran","Mason Miller",
    "Tanner Scott","Josh Hader","Felix Bautista","Edwin Diaz",
    "Clay Holmes","Devin Williams","Pete Fairbanks","Jordan Romano",
    "David Bednar","Jeff Hoffman","Alexis Diaz","Andres Munoz",
    "Camilo Doval","Evan Phillips","Raisel Iglesias","Kenley Jansen",
]

# Park factors (FanGraphs 3-yr rolling avg, 1.00 = neutral)
# Keyed by the HOME team abbreviation — the team that owns the ballpark.
PARK_FACTORS: dict[str, float] = {
    "COL":1.15,"CIN":1.07,"ARI":1.05,"BOS":1.04,"TEX":1.03,
    "PHI":1.03,"CHC":1.02,"NYY":1.02,"CWS":1.02,"TOR":1.01,
    "BAL":1.01,"STL":1.00,"MIL":1.00,"WSH":1.00,"HOU":0.99,
    "CLE":0.99,"ATL":0.99,"MIN":0.98,"KC":0.97,"DET":0.97,
    "LAA":0.97,"ATH":0.97,"NYM":0.97,"PIT":0.96,"LAD":0.96,
    "TB":0.96,"MIA":0.95,"SEA":0.95,"SD":0.94,"SF":0.93,
}

# =============================================================================
#  DATA LAYER  (all cached)
# =============================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_board() -> list[dict]:
    """Scrape the Six Picks Big Board. Returns [{name, price, pick_pct, board_pts}]."""
    try:
        r = requests.get(BOARD_URL, headers=REQ_HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        st.error(f"Board fetch failed: {e}"); return []

    soup  = BeautifulSoup(r.text, "html.parser")
    table = None
    for t in soup.find_all("table"):
        if "tablesorter" in " ".join(t.get("class",[])):
            table = t; break
    if not table:
        for t in soup.find_all("table"):
            if "PRICE" in t.get_text()[:500].upper():
                table = t; break
    if not table:
        return []

    thead = table.find("thead")
    cols  = [th.get_text(strip=True).upper() for th in thead.find_all("th")] if thead else []

    def cidx(keys, default):
        for k in keys:
            for i,h in enumerate(cols):
                if k in h: return i
        return default

    nc = cidx(["NAME","PLAYER"],0); pc = cidx(["PRICE","SALARY","COST"],1)
    ec = cidx(["PICK","PCT"],2);    tc = cidx(["PTS","POINTS","SCORE"],3)

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
        name = (nc_cell.find("a") or nc_cell).get_text(strip=True)
        if not name or name.upper() in ("NAME","PLAYER",""): continue
        price = flt(cells, pc)
        if price is None or not (0.5 <= price <= 200): continue
        rows.append({"name":name,"price":price,
                     "pick_pct":flt(cells,ec),"board_pts":flt(cells,tc)})
    return rows


@st.cache_data(ttl=600, show_spinner=False)   # refresh every 10 min — lineups update
def fetch_today_games() -> list[dict]:
    """
    Fetch today's schedule with lineups, probable pitchers (including pitch hand),
    teams, and venue.  This is our authoritative source for:
      - Who is starting (SP)
      - Home team / park
      - Opposing SP hand for platoon splits
    """
    url = (f"{MLB_API}/schedule?sportId=1&date={TODAY}"
           f"&hydrate=lineups,probablePitcher(pitchHand),team,venue&gameType=R")
    try:
        r = requests.get(url, timeout=12); r.raise_for_status()
        return [g for d in r.json().get("dates",[]) for g in d.get("games",[])]
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def mlb_season_stats(mlb_id: int, group: str) -> dict:
    """Season stats with prior-year fallback."""
    for season in [SEASON, SEASON-1]:
        try:
            url = (f"{MLB_API}/people/{mlb_id}/stats"
                   f"?stats=season&season={season}&group={group}&gameType=R")
            r = requests.get(url, timeout=8); r.raise_for_status()
            splits = r.json().get("stats",[{}])[0].get("splits",[])
            if splits:
                stat = splits[0].get("stat",{})
                if group=="hitting"  and float(stat.get("plateAppearances",0) or 0)>=MIN_PA: return stat
                if group=="pitching" and float(stat.get("inningsPitched",0) or 0)>=MIN_IP:    return stat
        except Exception:
            pass
    return {}


@st.cache_data(ttl=3600, show_spinner=False)
def mlb_split_stats(mlb_id: int, hand: str) -> dict:
    """Batter stats vs L or R pitching. Falls back to prior season."""
    sit = "vl" if hand=="L" else "vr"
    for season in [SEASON, SEASON-1]:
        try:
            url = (f"{MLB_API}/people/{mlb_id}/stats"
                   f"?stats=statSplits&season={season}&group=hitting"
                   f"&gameType=R&sitCodes={sit}")
            r = requests.get(url, timeout=8); r.raise_for_status()
            splits = r.json().get("stats",[{}])[0].get("splits",[])
            if splits:
                stat = splits[0].get("stat",{})
                if float(stat.get("plateAppearances",0) or 0) >= MIN_SPLIT_PA:
                    return stat
        except Exception:
            pass
    return {}


@st.cache_data(ttl=3600, show_spinner=False)
def mlb_recent_stats(mlb_id: int, group: str, days: int=14) -> dict:
    """Last N days stats."""
    try:
        url = (f"{MLB_API}/people/{mlb_id}/stats"
               f"?stats=lastXDays&season={SEASON}&group={group}"
               f"&gameType=R&limit={days}")
        r = requests.get(url, timeout=8); r.raise_for_status()
        splits = r.json().get("stats",[{}])[0].get("splits",[])
        if splits:
            stat = splits[0].get("stat",{})
            if group=="hitting"  and float(stat.get("plateAppearances",0) or 0)>=5: return stat
            if group=="pitching" and float(stat.get("inningsPitched",0) or 0)>=1:   return stat
    except Exception:
        pass
    return {}

# =============================================================================
#  GAME CONTEXT  — the authoritative source for SP, park, and matchup data
# =============================================================================
def build_game_context(games: list[dict]) -> dict:
    """
    Walk today's schedule and build a rich context dict:

      context["starters"]         → list of today's confirmed SP dicts
      context["batters"]          → list of confirmed lineup batters
      context["park_by_team"]     → {team_abbr: home_team_abbr}
                                    e.g. "KCR" → "MIN"  (Ragans pitches AT MIN today)
      context["opp_hand_by_team"] → {batting_team: opp_SP_pitchHand}
      context["game_label_by_team"]→ {team_abbr: "@ MIN 1:10 PM"}  (display)

    The park_by_team mapping mirrors exactly what the Six Picks "Game" column
    shows: if a team is AWAY ("@MIN 1:10 PM") their park factor is MIN's,
    not their own.
    """
    starters, batters = [], []
    park_by_team       = {}   # team → home team abbr (for park factor lookup)
    opp_hand_by_team   = {}   # batting team → opposing SP hand
    game_label_by_team = {}   # display string like "@ MIN 1:10 PM"
    seen               = set()

    for game in games:
        home = game.get("teams",{}).get("home",{}).get("team",{})
        away = game.get("teams",{}).get("away",{}).get("team",{})
        home_abbr = home.get("abbreviation","?")
        away_abbr = away.get("abbreviation","?")

        # Game time (best-effort)
        gt = game.get("gameDate","")
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(gt.replace("Z","+00:00"))
            import pytz
            eastern = pytz.timezone("US/Eastern")
            loc = dt.astimezone(eastern)
            time_str = loc.strftime("%-I:%M %p ET")
        except Exception:
            time_str = ""

        # Both teams play in the home ballpark today
        park_by_team[home_abbr] = home_abbr          # home team plays at home
        park_by_team[away_abbr] = home_abbr           # AWAY team also plays at HOME park

        game_label_by_team[home_abbr] = f"vs {away_abbr} {time_str}".strip()
        game_label_by_team[away_abbr] = f"@ {home_abbr} {time_str}".strip()

        for side in ("away","home"):
            ti   = game.get("teams",{}).get(side,{})
            abbr = ti.get("team",{}).get("abbreviation","?")
            prob = ti.get("probablePitcher")
            if prob and prob.get("id") not in seen:
                seen.add(prob["id"])
                hand = (prob.get("pitchHand",{}) or {}).get("code","") or "?"
                opp  = away_abbr if side=="home" else home_abbr
                starters.append({
                    "mlb_id":    prob["id"],
                    "name":      prob.get("fullName","?"),
                    "team":      abbr,
                    "hand":      hand,
                    "opp_team":  opp,
                    "park":      park_by_team.get(abbr, abbr),
                    "game_label":game_label_by_team.get(abbr,""),
                })
                # The opposing batters face this pitcher
                opp_hand_by_team[opp] = hand

        # Confirmed lineup batters
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
                        "batting_order":order_idx,
                        "opp_hand":     opp_hand_by_team.get(abbr,""),
                        "park":         park_by_team.get(abbr, abbr),
                        "game_label":   game_label_by_team.get(abbr,""),
                    })

    return {
        "starters":          starters,
        "batters":           batters,
        "park_by_team":      park_by_team,
        "opp_hand_by_team":  opp_hand_by_team,
        "game_label_by_team":game_label_by_team,
    }

# =============================================================================
#  SCORING HELPERS
# =============================================================================
def _ppa(s: dict) -> float:
    """Points per plate appearance from a stat dict."""
    pa = float(s.get("plateAppearances",0) or 0)
    if pa < 1: return 0.0
    ab=float(s.get("atBats",0) or 0); h=float(s.get("hits",0) or 0)
    d=float(s.get("doubles",0) or 0); t=float(s.get("triples",0) or 0)
    hr=float(s.get("homeRuns",0) or 0); bb=float(s.get("baseOnBalls",0) or 0)
    hp=float(s.get("hitByPitch",0) or 0); sb=float(s.get("stolenBases",0) or 0)
    cs=float(s.get("caughtStealing",0) or 0); sg=max(0.0,h-d-t-hr)
    return ((sg/pa)*H_1B+(d/pa)*H_2B+(t/pa)*H_3B+(hr/pa)*H_HR+(bb/pa)*H_BB+
            (hp/pa)*H_HBP+(sb/pa)*H_SB+(cs/pa)*H_CS+(ab/pa)*H_AB)


def hitter_pts(season: dict, split: dict, recent: dict,
               exp_pa: float, pf: float,
               sw: float, rw: float) -> tuple[float, str]:
    """Projected pts for a hitter. Returns (pts, notes_string)."""
    if float(season.get("plateAppearances",0) or 0) < MIN_PA:
        return 0.0, "no data"
    notes = []
    base  = _ppa(season) * exp_pa

    # 1. Platoon split
    split_pa = float(split.get("plateAppearances",0) or 0)
    if split_pa >= MIN_SPLIT_PA and sw > 0:
        base = (1-sw)*base + sw*(_ppa(split)*exp_pa)
        notes.append(f"split({int(split_pa)}PA)")
    elif sw > 0:
        notes.append("split(n/a)")

    # 2. Recent form
    rec_pa = float(recent.get("plateAppearances",0) or 0)
    if rec_pa >= 5 and rw > 0:
        base = (1-rw)*base + rw*(_ppa(recent)*exp_pa)
        notes.append(f"L14({int(rec_pa)}PA)")

    # 3. Park factor
    base *= pf
    if abs(pf - 1.0) > 0.005:
        notes.append(f"park×{pf:.2f}")

    return round(base, 2), (", ".join(notes) or "season")


def pitcher_pts(season: dict, recent: dict,
                is_sp: bool, rw: float) -> tuple[float, str]:
    """Projected pts for a pitcher. Returns (pts, notes_string)."""
    ip = float(season.get("inningsPitched",0) or 0)
    if ip < MIN_IP: return 0.0, "no data"

    def _from(s, exp_ip):
        ip_=float(s.get("inningsPitched",0) or 0)
        if ip_ < 0.5: return 0.0
        outs=max(ip_*3,1); g=max(float(s.get("gamesPlayed",1) or 1),1)
        k=float(s.get("strikeOuts",0) or 0); bb=float(s.get("baseOnBalls",0) or 0)
        hp=float(s.get("hitByPitch",0) or 0); hr=float(s.get("homeRuns",0) or 0)
        sv=float(s.get("saves",0) or 0); hld=float(s.get("holds",0) or 0)
        eo=exp_ip*3
        pts=(eo*P_OUT+(k/outs)*eo*P_K+(bb/outs)*eo*P_BB+(hp/outs)*eo*P_HBP+
             (hr/outs)*eo*P_HR+(sv/g)*P_SV+(hld/g)*P_HLD)
        return pts*(SP_MULT if is_sp else 1.0)

    exp_ip = DEFAULT_IP if is_sp else 1.0
    base   = _from(season, exp_ip)
    notes  = []
    rip    = float(recent.get("inningsPitched",0) or 0) if recent else 0
    if rip >= 1.0 and rw > 0:
        base = (1-rw)*base + rw*_from(recent, exp_ip)
        notes.append(f"L14({rip:.1f}IP)")
    return round(base, 2), (", ".join(notes) or "season")


def blend_board(proj: float, board_pts, bw: float) -> float:
    if board_pts is None or bw == 0: return proj
    return round((1-bw)*proj + bw*board_pts, 2)


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
def build_pool(board_rows: list[dict], ctx: dict, settings: dict) -> list[dict]:
    sw = settings["split_weight"]
    rw = settings["recent_weight"]
    bw = settings["board_weight"]

    sal_dict   = {r["name"].strip().lower(): r["price"]      for r in board_rows}
    board_dict = {r["name"].strip().lower(): r["board_pts"]  for r in board_rows}

    starters         = ctx["starters"]     # CONFIRMED today's SPs from schedule
    batters          = ctx["batters"]      # confirmed lineup batters
    park_by_team     = ctx["park_by_team"]
    opp_hand_by_team = ctx["opp_hand_by_team"]
    lineups_posted   = len(batters) > 0

    conf_batter_map = {p["name"].lower(): p for p in batters}

    pool       = []
    seen_names = set()

    # ── STARTING PITCHERS ────────────────────────────────────────────────────
    # ONLY players confirmed as today's starters by the MLB schedule API.
    # The board is used solely to look up their price.
    for sp in starters:
        key  = sp["name"].lower()
        seen_names.add(key)
        sal  = salary_lookup(sp["name"], sal_dict) or 10.0
        sal_ok = salary_lookup(sp["name"], sal_dict) is not None
        park = sp["park"]                          # home ballpark for THIS game
        pf   = PARK_FACTORS.get(park, 1.0)         # park factor at today's venue
        bp   = board_dict.get(key)

        s_st = mlb_season_stats(sp["mlb_id"], "pitching")
        r_st = mlb_recent_stats(sp["mlb_id"], "pitching")
        pts, notes = pitcher_pts(s_st, r_st, is_sp=True, rw=rw)

        # SP park factor: a pitcher in a hitter's park gives up more runs → lower pts
        pts = round(pts / pf, 2)  # inverse: hitter-friendly park hurts SP
        pts = blend_board(pts, bp, bw)

        opp  = sp.get("opp_team","")
        hand = sp.get("hand","?")
        game_label = sp.get("game_label","")

        pool.append({
            "name":      sp["name"],
            "team":      sp["team"],
            "slots":     ["SP"],
            "salary":    sal,
            "pts":       pts,
            "value":     round(pts/sal, 3) if sal else 0,
            "sal_ok":    sal_ok,
            "confirmed": True,
            "badges":    f"{game_label} · 🤚{hand} · {notes}",
            "park":      park,
            "pf":        pf,
        })

    # ── BATTERS ───────────────────────────────────────────────────────────────
    # Use confirmed lineup batters; fall back to board players before lineups post.
    batter_source = batters if lineups_posted else []

    # If lineups posted, only use confirmed batters.
    # If not yet posted, walk the board and add any non-pitcher players found
    # via MLB people search (positional players only).
    if not lineups_posted:
        for row in board_rows:
            key = row["name"].lower()
            if key in seen_names: continue
            # Skip players who are today's SPs
            if any(s["name"].lower() == key for s in starters): continue
            # We'll add them below with team/park from board context
            batter_source.append({
                "mlb_id":       None,    # will look up
                "name":         row["name"],
                "pos_code":     "",      # unknown until lookup
                "team":         "",
                "batting_order": 5,      # assume middle of order
                "opp_hand":     "",
                "park":         "",
                "game_label":   "",
                "_from_board":  True,
                "_price":       row["price"],
            })

    for p in batter_source:
        key = p["name"].lower()
        if key in seen_names: continue
        seen_names.add(key)
        bp = board_dict.get(key)

        # If we don't have MLB ID (pre-lineup board player), look it up
        mlb_id   = p.get("mlb_id")
        pos_code = p.get("pos_code","")
        team     = p.get("team","")

        if p.get("_from_board") or not mlb_id:
            from difflib import SequenceMatcher as SM
            # Try to find this player in the confirmed batter map first
            if key in conf_batter_map:
                info = conf_batter_map[key]
                mlb_id   = info["mlb_id"]
                pos_code = info["pos_code"]
                team     = info["team"]
            else:
                # MLB people search — only if not already looked up
                try:
                    q = requests.utils.quote(p["name"])
                    r = requests.get(f"{MLB_API}/people/search?names={q}&sportId=1",
                                     timeout=8)
                    r.raise_for_status()
                    people = r.json().get("people",[])
                    if not people: continue
                    px = min(people, key=lambda x: abs(len(x.get("fullName",""))-len(p["name"])))
                    mlb_id   = px["id"]
                    pos_code = px.get("primaryPosition",{}).get("abbreviation","")
                    team     = px.get("currentTeam",{}).get("abbreviation","?")
                except Exception:
                    continue

        # Skip pitchers in batter loop
        if pos_code in ("SP","RP","P"): continue

        slots = POS_TO_SLOT.get(pos_code, ["CI"])
        sal   = (p.get("_price") or salary_lookup(p["name"], sal_dict) or 8.0)
        sal_ok= salary_lookup(p["name"], sal_dict) is not None

        # Park and matchup context
        park       = p.get("park") or park_by_team.get(team, team)
        opp_hand   = p.get("opp_hand") or opp_hand_by_team.get(team,"")
        pf         = PARK_FACTORS.get(park, 1.0)
        game_label = p.get("game_label") or ctx["game_label_by_team"].get(team,"")
        exp_pa     = {1:LEADOFF_PA,2:LEADOFF_PA}.get(p.get("batting_order",5),
                      BOTTOM_PA if p.get("batting_order",5)>=7 else DEFAULT_PA)

        s_st = mlb_season_stats(mlb_id, "hitting")
        sp_st= mlb_split_stats(mlb_id, opp_hand) if opp_hand else {}
        r_st = mlb_recent_stats(mlb_id, "hitting")
        pts, notes = hitter_pts(s_st, sp_st, r_st, exp_pa, pf, sw, rw)
        pts  = blend_board(pts, bp, bw)

        park_icon  = "🏔" if pf>1.02 else ("🏟" if pf<0.97 else "")
        hand_badge = f"vs{opp_hand}" if opp_hand else ""
        order_badge= f"#{p.get('batting_order','')}" if p.get("batting_order") else ""

        pool.append({
            "name":      p["name"],
            "team":      team,
            "slots":     slots,
            "salary":    sal,
            "pts":       pts,
            "value":     round(pts/sal, 3) if sal else 0,
            "sal_ok":    sal_ok,
            "confirmed": p.get("batting_order") is not None and lineups_posted,
            "badges":    f"{game_label} {park_icon}{park} {hand_badge} {order_badge} · {notes}".strip(),
            "park":      park,
            "pf":        pf,
        })

    # ── RELIEF PITCHERS ───────────────────────────────────────────────────────
    # Known closers list; use board price if available, else estimate
    for name in KNOWN_CLOSERS:
        key = name.lower()
        if key in seen_names: continue
        sal = salary_lookup(name, sal_dict) or 9.0
        bp  = board_dict.get(key)
        pts = round(4.2 + (sal - 9.0) * 0.22, 2)
        pts = blend_board(pts, bp, bw)
        team      = ""
        park      = park_by_team.get(team, "")
        game_lbl  = ctx["game_label_by_team"].get(team,"")
        pool.append({
            "name":p["name"] if False else name,
            "team":      "?",
            "slots":     ["RP"],
            "salary":    sal,
            "pts":       pts,
            "value":     round(pts/sal,3),
            "sal_ok":    True,
            "confirmed": False,
            "badges":    "closer est.",
            "park":      "",
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
#  UI
# =============================================================================
def player_card(rank: int, p: dict):
    top  = rank == 1
    conf = " ✅" if p.get("confirmed") else ""
    st.markdown(f"""
    <div class="card {'best' if top else ''}">
      <span class="pts">{p['pts']:.1f}</span>
      <div class="rank">#{rank}</div>
      <div class="pname">{p['name']}{conf}</div>
      <div class="meta">{p['team']} · ${p['salary']:.2f} · {p['value']:.3f} pts/$
        {' · <b style="color:#c8a838">' + p['badges'] + '</b>' if p.get('badges') else ''}
      </div>
    </div>""", unsafe_allow_html=True)


def optimal_card(best: dict):
    if not best["lineup"]:
        st.warning("No valid lineup found under the $120 cap."); return
    for slot, p in zip(SLOTS, best["lineup"]):
        badge = (p.get("badges","") or "")[:40]
        st.markdown(f"""
        <div class="opt-row">
          <span class="opt-slot">{SLOT_ICONS[slot]} {slot}</span>
          <span class="opt-name">{p['name']} <small style="color:#555">{p['team']}</small></span>
          <span class="opt-badge">{badge}</span>
          <span class="opt-sal">${p['salary']:.2f}</span>
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
#  MAIN
# =============================================================================
def main():
    st.markdown("## ⚾ Six Picks Optimizer")
    st.caption(f"{TODAY} · Cap ${SALARY_CAP:.0f} · C · CI · MI · OF · SP · RP")

    with st.expander("⚙️ Settings", expanded=False):
        st.markdown("**Projection weights**")
        c1,c2,c3 = st.columns(3)
        sw = c1.slider("Platoon split",    0.0, 1.0, 0.35, 0.05,
            help="How much to weight batter's stats vs the specific pitcher handedness (L/R). "
                 "Requires 30+ PA in the split to activate.")
        rw = c2.slider("Recent form L14",  0.0, 1.0, 0.20, 0.05,
            help="Weight given to last-14-day stats vs full-season rates.")
        bw = c3.slider("Last board score", 0.0, 1.0, 0.15, 0.05,
            help="How much yesterday's actual Six Picks score influences today's projection.")
        st.caption(f"Season stats · {sw*100:.0f}% platoon split · {rw*100:.0f}% L14 · "
                   f"{bw*100:.0f}% last board · park multiplier always on")

    if st.button("🔄 Refresh all data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    # ── Load data ──────────────────────────────────────────────────────────────
    with st.spinner("Fetching Big Board…"):
        board_rows = fetch_board()
    if not board_rows:
        st.error("Big Board not available yet.")
        st.markdown(f"[Open Big Board ↗]({BOARD_URL})"); return

    with st.spinner("Fetching today's schedule, lineups, and pitchers…"):
        games = fetch_today_games()

    if not games:
        st.error("No games found today — may be an off day."); return

    ctx            = build_game_context(games)
    starters       = ctx["starters"]
    batters        = ctx["batters"]
    lineups_posted = len(batters) > 0

    # Summary bar
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Games today",  len(games))
    c2.metric("Conf. SPs",    len(starters))
    c3.metric("Board players",len(board_rows))
    c4.metric("Lineups",      "✅ Posted" if lineups_posted else "⏳ Pending")

    if not lineups_posted:
        st.warning("⏳ Lineups not yet posted. SP data is ready; batter projections "
                   "use board players. Re-run ~1hr before first pitch for full accuracy.")
    else:
        st.success(f"✅ {len(batters)} confirmed batters loaded with platoon + park adjustments")

    # Today's SP summary
    with st.expander(f"🔥 Today's confirmed starters ({len(starters)})", expanded=True):
        if not starters:
            st.caption("No probable pitchers posted yet.")
        else:
            rows_per_col = (len(starters) + 1) // 2
            col1, col2  = st.columns(2)
            for i, sp in enumerate(starters):
                park = sp.get("park","")
                pf   = PARK_FACTORS.get(park, 1.0)
                pf_str = f" {'🏔' if pf>=1.03 else '🏟' if pf<=0.96 else '⚪'} {park} ×{pf:.2f}"
                col = col1 if i < rows_per_col else col2
                col.markdown(
                    f"**{sp['name']}** ({sp['team']}) 🤚{sp['hand']}  "
                    f"*{sp['game_label']}*{pf_str}"
                )

    st.divider()

    # ── Build pool ─────────────────────────────────────────────────────────────
    with st.spinner("Computing projections…"):
        pool = build_pool(board_rows, ctx, {"split_weight":sw,"recent_weight":rw,"board_weight":bw})

    if not pool:
        st.error("Could not build player pool."); return

    # ── Top 5 per slot ─────────────────────────────────────────────────────────
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

    # ── Optimal lineup ─────────────────────────────────────────────────────────
    st.markdown("### 🏆 Optimal Lineup")
    with st.spinner("Optimizing…"):
        best = best_lineup(pool)
    optimal_card(best)

    st.divider()

    # ── Best value ─────────────────────────────────────────────────────────────
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
              <div class="meta">${bv['salary']:.2f} · {bv['pts']:.1f} pts</div>
            </div>""", unsafe_allow_html=True)

    st.divider()

    with st.expander("📋 Full Big Board", expanded=False):
        import pandas as pd
        df = pd.DataFrame([{
            "Player":  r["name"],
            "Price":   f"${r['price']:.2f}",
            "Pick%":   f"{r['pick_pct']:.1f}%" if r["pick_pct"] is not None else "—",
            "Last PTS":r["board_pts"] if r["board_pts"] is not None else "—",
        } for r in sorted(board_rows, key=lambda x: x["price"], reverse=True)])
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.caption(
        f"SPs: MLB Stats API schedule (confirmed starters only) · "
        f"Prices: [ottoneu.fangraphs.com/sixpicks/baseball/board]({BOARD_URL}) · "
        f"Park: home team from game matchup · Stats: {SEASON} (→{SEASON-1} fallback)"
    )


if __name__ == "__main__":
    main()