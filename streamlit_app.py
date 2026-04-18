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
    "Ryan Helsley","Jhoan Duran","Mason Miller",
    "Tanner Scott","Felix Bautista","Edwin Diaz",
    "Clay Holmes","Devin Williams","Pete Fairbanks","Jordan Romano",
    "David Bednar","Jeff Hoffman","Andres Munoz",
    "Camilo Doval","Evan Phillips",
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

@st.cache_data(ttl=3600, show_spinner=False)
def mlb_home_away_stats(mlb_id: int, home: bool) -> dict:
    """Batter home or away splits. Falls back to prior season."""
    sit = "h" if home else "a"
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
def mlb_pitcher_suppression(sp_mlb_id: int) -> float:
    """
    Compute a batter suppression factor (0.75–1.25) for an opposing SP.
    Based on the SP's ERA relative to league average (~4.30).
    An ace with ERA 2.50 → factor ~0.82 (batters score 18% less than average).
    A poor SP with ERA 6.00 → factor ~1.18 (batters score 18% more).
    Result is clamped to [0.75, 1.25] so one outlier doesn't dominate.
    We blend season ERA with recent ERA for stability.
    """
    LEAGUE_AVG_ERA = 4.30
    try:
        s_st = mlb_season_stats(sp_mlb_id, "pitching")
        r_st = mlb_recent_stats(sp_mlb_id, "pitching")

        def era_from(st):
            ip  = float(st.get("inningsPitched",0) or 0)
            er  = float(st.get("earnedRuns",0) or 0)
            if ip < 1: return None
            return (er / ip) * 9

        s_era = era_from(s_st)
        r_era = era_from(r_st)

        if s_era is None:
            return 1.0
        # Blend: 80% season, 20% recent (if available)
        if r_era is not None:
            era = 0.80 * s_era + 0.20 * r_era
        else:
            era = s_era

        # Factor: league_avg / pitcher_era
        # Good pitcher (low ERA) → factor < 1 → suppresses batters
        factor = LEAGUE_AVG_ERA / max(era, 1.0)
        return round(max(0.75, min(1.25, factor)), 3)
    except Exception:
        return 1.0


# =============================================================================
#  GAME CONTEXT  — the authoritative source for SP, park, and matchup data
# =============================================================================
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_team_roster(team_abbr: str) -> list[dict]:
    """
    Get the active 26-man roster for a team, returning position players only.
    Cached 1hr — rosters don't change mid-day.
    """
    try:
        # First resolve abbr -> team ID
        r = requests.get(f"{MLB_API}/teams?sportId=1&season={SEASON}", timeout=8)
        r.raise_for_status()
        team_id = None
        for t in r.json().get("teams", []):
            if t.get("abbreviation") == team_abbr:
                team_id = t["id"]
                break
        if not team_id:
            return []

        r2 = requests.get(
            f"{MLB_API}/teams/{team_id}/roster?rosterType=active&season={SEASON}",
            timeout=10
        )
        r2.raise_for_status()
        roster = r2.json().get("roster", [])

        players = []
        for entry in roster:
            pos = entry.get("position", {}).get("abbreviation", "")
            if pos in ("SP", "RP", "P", "TWP"):
                continue
            pid  = entry.get("person", {}).get("id")
            name = entry.get("person", {}).get("fullName", "")
            if pid and name:
                players.append({"mlb_id": pid, "name": name, "pos_code": pos})
        return players
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_estimated_lineup(team_abbr: str) -> list[dict]:
    """
    Build an estimated batting order for a team that hasn't posted their lineup.

    Method:
      1. Pull active roster, filter to position players
      2. Fetch season plate appearances for each (prior year fallback)
      3. Sort by PA desc — more PA = more likely a regular starter
      4. Return top 9 as the estimated lineup

    Batting order positions are assigned by PA rank (not real order), so all
    players get DEFAULT_PA in projections — conservative but honest.
    """
    roster = fetch_team_roster(team_abbr)
    if not roster:
        return []

    ranked = []
    for p in roster:
        stat = mlb_season_stats(p["mlb_id"], "hitting")
        pa   = float(stat.get("plateAppearances", 0) or 0)
        if pa < MIN_PA:
            # Try prior season directly
            try:
                url = (f"{MLB_API}/people/{p['mlb_id']}/stats"
                       f"?stats=season&season={SEASON-1}&group=hitting&gameType=R")
                rv  = requests.get(url, timeout=6); rv.raise_for_status()
                sp2 = rv.json().get("stats",[{}])[0].get("splits",[])
                if sp2:
                    pa = float(sp2[0].get("stat",{}).get("plateAppearances",0) or 0)
            except Exception:
                pass
        ranked.append((pa, p))

    ranked.sort(key=lambda x: x[0], reverse=True)

    estimated = []
    for i, (pa, p) in enumerate(ranked[:9], 1):
        estimated.append({
            "mlb_id":        p["mlb_id"],
            "name":          p["name"],
            "pos_code":      p["pos_code"],
            "team":          team_abbr,
            "batting_order": i,
            "estimated":     True,
        })
    return estimated


def build_game_context(games: list[dict]) -> dict:
    """
    Walk today's schedule and build a rich context dict.

    For each team in each game:
      - If their lineup HAS been posted  -> use confirmed batters (confirmed=True)
      - If their lineup has NOT been posted -> fetch_estimated_lineup() from
        active roster ranked by PA           (estimated=True)

    This means we always have a full slate of position players to project,
    regardless of whether early games have posted and night games haven't.
    """
    starters, batters = [], []
    park_by_team       = {}
    opp_hand_by_team   = {}
    game_label_by_team = {}
    lineup_status      = {}   # "confirmed" or "estimated" per team abbr
    seen               = set()

    for game in games:
        home      = game.get("teams",{}).get("home",{}).get("team",{})
        away      = game.get("teams",{}).get("away",{}).get("team",{})
        home_abbr = home.get("abbreviation","?")
        away_abbr = away.get("abbreviation","?")

        # Game time
        gt = game.get("gameDate","")
        try:
            from datetime import datetime, timezone, timedelta
            dt = datetime.fromisoformat(gt.replace("Z","+00:00"))
            # UTC-4 (EDT) / UTC-5 (EST) — use UTC-4 during baseball season (Apr-Oct)
            eastern = timezone(timedelta(hours=-4))
            loc = dt.astimezone(eastern)
            time_str = loc.strftime("%-I:%M %p ET")
        except Exception:
            time_str = ""

        park_by_team[home_abbr] = home_abbr
        park_by_team[away_abbr] = home_abbr
        game_label_by_team[home_abbr] = f"vs {away_abbr} {time_str}".strip()
        game_label_by_team[away_abbr] = f"@ {home_abbr} {time_str}".strip()

        # Probable starters
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
                opp_hand_by_team[opp] = hand

        # Lineups -- each side handled independently
        lineups = game.get("lineups", {})
        for lineup_key, abbr in [("awayPlayers", away_abbr), ("homePlayers", home_abbr)]:
            posted = lineups.get(lineup_key, [])

            if posted:
                # Confirmed lineup
                lineup_status[abbr] = "confirmed"
                for order_idx, p in enumerate(posted, 1):
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
                            "confirmed":    True,
                            "estimated":    False,
                        })
            else:
                # Estimated lineup from active roster
                lineup_status[abbr] = "estimated"
                est = fetch_estimated_lineup(abbr)
                for p in est:
                    if p["mlb_id"] in seen:
                        continue
                    seen.add(p["mlb_id"])
                    batters.append({
                        **p,
                        "opp_hand":   opp_hand_by_team.get(abbr,""),
                        "park":       park_by_team.get(abbr, abbr),
                        "game_label": game_label_by_team.get(abbr,""),
                        "confirmed":  False,
                        "estimated":  True,
                    })

    return {
        "starters":          starters,
        "batters":           batters,
        "lineup_status":     lineup_status,
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
               home_away: dict, exp_pa: float, pf: float,
               sw: float, rw: float, haw: float,
               opp_sup: float = 1.0) -> tuple[float, str]:
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

    # 3. Home/away split
    ha_pa = float(home_away.get("plateAppearances",0) or 0)
    if ha_pa >= MIN_SPLIT_PA and haw > 0:
        base = (1-haw)*base + haw*(_ppa(home_away)*exp_pa)
        notes.append(f"{'home' if ha_pa else 'away'}({int(ha_pa)}PA)")

    # 4. Park factor
    base *= pf
    if abs(pf - 1.0) > 0.005:
        notes.append(f"park×{pf:.2f}")

    # 5. Opposing pitcher suppression
    if abs(opp_sup - 1.0) > 0.02:
        base *= opp_sup
        quality = "ace" if opp_sup < 0.90 else ("tough" if opp_sup < 0.97 else "weak")
        notes.append(f"opp:{quality}(×{opp_sup:.2f})")

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

    sal_dict     = {r["name"].strip().lower(): r["price"]     for r in board_rows}
    board_dict   = {r["name"].strip().lower(): r["board_pts"] for r in board_rows}
    pickpct_dict = {r["name"].strip().lower(): (r["pick_pct"] or 0) for r in board_rows}

    starters         = ctx["starters"]
    batters          = ctx["batters"]       # confirmed + estimated, merged by build_game_context
    park_by_team     = ctx["park_by_team"]
    opp_hand_by_team = ctx["opp_hand_by_team"]

    pool       = []
    seen_names = set()

    # ── STARTING PITCHERS ────────────────────────────────────────────────────
    # Only confirmed probable starters from the MLB schedule.
    for sp in starters:
        key    = sp["name"].lower()
        seen_names.add(key)
        sal    = salary_lookup(sp["name"], sal_dict) or 10.0
        sal_ok = salary_lookup(sp["name"], sal_dict) is not None
        park   = sp["park"]
        pf     = PARK_FACTORS.get(park, 1.0)
        bp     = board_dict.get(key)

        s_st = mlb_season_stats(sp["mlb_id"], "pitching")
        r_st = mlb_recent_stats(sp["mlb_id"], "pitching")
        pts, notes = pitcher_pts(s_st, r_st, is_sp=True, rw=rw)
        pts = round(pts / pf, 2)   # hitter-friendly park hurts SP inversely
        pts = blend_board(pts, bp, bw)

        pool.append({
            "name":      sp["name"],
            "team":      sp["team"],
            "slots":     ["SP"],
            "salary":    sal,
            "pts":       pts,
            "value":     round(pts/sal, 3) if sal else 0,
            "sal_ok":    sal_ok,
            "confirmed": True,
            "estimated": False,
            "badges":    f"{sp.get('game_label','')} · 🤚{sp.get('hand','?')} · {notes}",
            "park":      park,
            "pf":        pf,
            "pick_pct":  pickpct_dict.get(key, 0),
        })

    # ── BATTERS ───────────────────────────────────────────────────────────────
    # ctx["batters"] contains both confirmed lineup players AND estimated starters
    # (from active roster) for teams that haven't posted their lineup yet.
    # Both are scored identically; the badge shows "~est" for estimated players.
    for p in batters:
        key = p["name"].lower()
        if key in seen_names: continue
        seen_names.add(key)
        bp = board_dict.get(key)

        mlb_id   = p["mlb_id"]
        pos_code = p.get("pos_code","")
        team     = p.get("team","")

        if pos_code in ("SP","RP","P"): continue

        slots      = POS_TO_SLOT.get(pos_code, ["CI"])
        sal        = salary_lookup(p["name"], sal_dict) or 8.0
        sal_ok     = salary_lookup(p["name"], sal_dict) is not None
        park       = p.get("park") or park_by_team.get(team, team)
        opp_hand   = p.get("opp_hand") or opp_hand_by_team.get(team,"")
        pf         = PARK_FACTORS.get(park, 1.0)
        game_label = p.get("game_label") or ctx["game_label_by_team"].get(team,"")
        order      = p.get("batting_order", 5)
        exp_pa     = LEADOFF_PA if order <= 2 else (BOTTOM_PA if order >= 7 else DEFAULT_PA)

        s_st  = mlb_season_stats(mlb_id, "hitting")
        sp_st = mlb_split_stats(mlb_id, opp_hand) if opp_hand else {}
        r_st  = mlb_recent_stats(mlb_id, "hitting")
        is_home = park_by_team.get(team,"") == team
        ha_st   = mlb_home_away_stats(mlb_id, is_home)
        opp_sp  = next((s for s in starters if s.get("opp_team","") == team), None)
        opp_sup = mlb_pitcher_suppression(opp_sp["mlb_id"]) if opp_sp else 1.0

        pts, notes = hitter_pts(s_st, sp_st, r_st, ha_st, exp_pa, pf, sw, rw,
                                haw=settings.get("ha_weight", 0.20), opp_sup=opp_sup)
        pts = blend_board(pts, bp, bw)

        park_icon   = "🏔" if pf>1.02 else ("🏟" if pf<0.97 else "")
        hand_badge  = f"vs{opp_hand}" if opp_hand else ""
        order_badge = f"#{order}" if order else ""
        est_badge   = " ~est" if p.get("estimated") else ""

        pool.append({
            "name":      p["name"],
            "team":      team,
            "slots":     slots,
            "salary":    sal,
            "pts":       pts,
            "value":     round(pts/sal, 3) if sal else 0,
            "sal_ok":    sal_ok,
            "confirmed": p.get("confirmed", False),
            "estimated": p.get("estimated", False),
            "badges":    f"{game_label} {park_icon}{park} {hand_badge} {order_badge}{est_badge} · {notes}".strip(),
            "park":      park,
            "pf":        pf,
            "pick_pct":  pickpct_dict.get(key, 0),
        })

    # ── RELIEF PITCHERS ─────────────────────────────────────────────────────
    # Look up real stats for each known closer via MLB API.
    for name in KNOWN_CLOSERS:
        key = name.lower()
        if key in seen_names: continue
        sal = salary_lookup(name, sal_dict) or 9.0
        bp  = board_dict.get(key)

        # Try to find MLB ID via people search (cached)
        try:
            q = requests.utils.quote(name)
            r_resp = requests.get(f"{MLB_API}/people/search?names={q}&sportId=1", timeout=6)
            r_resp.raise_for_status()
            people = r_resp.json().get("people",[])
            if people:
                px     = min(people, key=lambda x: abs(len(x.get("fullName",""))-len(name)))
                rp_id  = px["id"]
                rp_team= px.get("currentTeam",{}).get("abbreviation","?")
                s_st   = mlb_season_stats(rp_id, "pitching")
                r_st   = mlb_recent_stats(rp_id, "pitching")
                pts, notes = pitcher_pts(s_st, r_st, is_sp=False, rw=rw)
                if pts == 0.0:
                    # Fall back to salary estimate if no stats
                    pts = round(4.2 + (sal - 9.0) * 0.22, 2)
                    notes = "closer est."
            else:
                rp_team = "?"
                pts = round(4.2 + (sal - 9.0) * 0.22, 2)
                notes = "closer est."
        except Exception:
            rp_team = "?"
            pts = round(4.2 + (sal - 9.0) * 0.22, 2)
            notes = "closer est."

        pts = blend_board(pts, bp, bw)
        pool.append({
            "name":      name,
            "team":      rp_team,
            "slots":     ["RP"],
            "salary":    sal,
            "pts":       pts,
            "value":     round(pts/sal,3),
            "sal_ok":    True,
            "confirmed": False,
            "badges":    notes,
            "park":      "",
        })

    return pool


def best_lineup(pool: list, mode: str = "max_pts",
                contrarian_weight: float = 0.3,
                min_spend: float = 100.0) -> dict:
    """
    Find the best valid Six Picks lineup under the $120 cap.

    min_spend   → require total salary >= this value (default $100).
                  Forces the optimizer to explore expensive players instead
                  of finding the cheapest path to the top projected score.

    mode        → "max_pts"    : highest projected points
                  "contrarian" : adjusts for pick% ownership to differentiate
                                 from the field (use 0.2–0.4 contrarian_weight)
    """
    def score(combo):
        pts = sum(p["pts"] for p in combo)
        if mode != "contrarian":
            return pts
        avg_own = sum(p.get("pick_pct", 50) for p in combo) / len(combo)
        return pts * (1.0 - contrarian_weight * (avg_own / 100.0))

    # Consider more candidates per slot so expensive players aren't pruned
    pools = {s: sorted([p for p in pool if s in p["slots"]],
                        key=lambda p: p["pts"], reverse=True)[:20]
             for s in SLOTS}

    best = {"pts": -999, "lineup": None, "salary": 0, "score": -999}

    for combo in itertools.product(*[pools[s] for s in SLOTS]):
        if len({p["name"] for p in combo}) < 6:
            continue
        sal = sum(p["salary"] for p in combo)
        if sal > SALARY_CAP:
            continue        # over cap
        if sal < min_spend:
            continue        # under minimum spend floor
        sc = score(combo)
        if sc > best["score"]:
            best = {
                "pts":    sum(p["pts"] for p in combo),
                "lineup": combo,
                "salary": sal,
                "score":  sc,
            }

    # If no lineup meets the floor, relax it by $5 increments until one is found
    if best["lineup"] is None and min_spend > 0:
        fallback_floor = min_spend - 5.0
        while fallback_floor >= 0 and best["lineup"] is None:
            for combo in itertools.product(*[pools[s] for s in SLOTS]):
                if len({p["name"] for p in combo}) < 6: continue
                sal = sum(p["salary"] for p in combo)
                if sal > SALARY_CAP or sal < fallback_floor: continue
                sc = score(combo)
                if sc > best["score"]:
                    best = {"pts":sum(p["pts"] for p in combo),
                            "lineup":combo,"salary":sal,"score":sc}
            fallback_floor -= 5.0

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
      <div class="meta">{p['team']} · ${p['salary']:.2f} · {p['value']:.3f} pts/$ · {p.get('pick_pct',0):.0f}% owned
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
    rem_color = "#e74c3c" if rem > 15 else ("#f39c12" if rem > 8 else "#aaa")
    st.markdown(f"""
    <div style="display:flex;justify-content:space-between;padding:6px 11px;
                border-top:1px solid #1e3050;font-size:.85rem;margin-top:2px">
      <span>Total <b>${best['salary']:.2f}</b></span>
      <span>Remaining <b style="color:{rem_color}">${rem:.2f}</b></span>
      <span>Proj pts <b style="color:#2ecc71">{best['pts']:.1f}</b></span>
    </div>""", unsafe_allow_html=True)
    if rem > 15:
        st.warning(f"⚠️ ${rem:.2f} left unspent. Try raising the minimum spend slider.")

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
        c4, c5 = st.columns(2)
        haw = c4.slider("Home/away split", 0.0, 1.0, 0.20, 0.05,
            help="Weight given to batter's home or away stats. "
                 "Some players hit dramatically better at home.")
        cw  = c5.slider("Contrarian weight", 0.0, 1.0, 0.0, 0.05,
            help="In contrarian mode, penalises high-ownership picks. "
                 "0 = ignore ownership, 1 = strongly prefer low-owned players. "
                 "Use 0.2–0.4 to differentiate from the field.")
        opt_mode = st.radio("Optimizer mode", ["Max pts", "Contrarian"],
            horizontal=True,
            help="Max pts: highest projected score. Contrarian: adjusts for ownership "
                 "to find lineups others aren't picking.")
        st.divider()
        min_spend = st.slider(
            "Minimum cap spend ($)", 80.0, 119.0, 100.0, 1.0,
            help="Forces the optimizer to spend at least this much of the $120 cap. "
                 "Higher = roster pricier players. $100–$115 is a good range. "
                 "If no valid lineup is found the floor is automatically relaxed."
        )
        spend_pct = min_spend / SALARY_CAP * 100
        st.caption(
            f"Season stats · {sw*100:.0f}% platoon · {rw*100:.0f}% L14 · "
            f"{haw*100:.0f}% home/away · {bw*100:.0f}% last board · "
            f"opp quality + park always on · min spend ${min_spend:.0f} ({spend_pct:.0f}% of cap)"
        )

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
    lineup_status  = ctx.get("lineup_status", {})
    n_conf_teams   = sum(1 for v in lineup_status.values() if v == "confirmed")
    n_est_teams    = sum(1 for v in lineup_status.values() if v == "estimated")

    # Summary bar
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Games today",   len(games))
    c2.metric("Conf. SPs",     len(starters))
    c3.metric("Board players", len(board_rows))
    c4.metric("Lineups", f"✅{n_conf_teams} / ~{n_est_teams}")

    # Status message
    if n_est_teams == 0:
        st.success(f"✅ All {n_conf_teams} teams confirmed · {sum(1 for p in pool if False) or ''} "
                   f"Full slate ready.")
    elif n_conf_teams == 0:
        st.warning(
            f"⏳ No lineups posted yet — all {n_est_teams} teams using estimated starters "
            f"(active roster ranked by PA). Re-run closer to first pitch."
        )
    else:
        st.info(
            f"✅ {n_conf_teams} team(s) confirmed  ·  "
            f"〜 {n_est_teams} team(s) estimated from active roster  ·  "
            f"Refresh closer to first pitch to fill in remaining lineups"
        )

    # Per-team lineup status table
    if lineup_status:
        with st.expander("📋 Lineup status by team", expanded=(n_est_teams > 0)):
            conf_teams = sorted([t for t,s in lineup_status.items() if s=="confirmed"])
            est_teams  = sorted([t for t,s in lineup_status.items() if s=="estimated"])
            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("**✅ Confirmed**")
                for t in conf_teams:
                    st.caption(t)
            with col_b:
                st.markdown("**〜 Estimated (active roster)**")
                for t in est_teams:
                    st.caption(f"{t} — using top-PA regulars")


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
        pool = build_pool(board_rows, ctx, {"split_weight":sw,"recent_weight":rw,"board_weight":bw,"ha_weight":haw})

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
        best = best_lineup(pool,
                           mode="contrarian" if opt_mode=="Contrarian" else "max_pts",
                           contrarian_weight=cw,
                           min_spend=min_spend)
    if opt_mode == "Contrarian":
        st.info(f"🎲 Contrarian mode: ownership weight {cw*100:.0f}% · "
                f"lower-owned players receive a scoring bonus")
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