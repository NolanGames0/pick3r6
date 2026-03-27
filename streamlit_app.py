import streamlit as st

#!/usr/bin/env python3
"""
Ottoneu Six Picks Optimizer
============================
Scrapes player prices directly from the Ottoneu Six Picks "Big Board" page:
  https://ottoneu.fangraphs.com/sixpicks/baseball/board

This page is server-rendered HTML — no JavaScript, no login required.
Prices, pick%, and yesterday's PTS are all in a plain <table>.

Then combines with MLB Stats API confirmed lineups + season stats to rank
the top 5 players at each slot and find the optimal $120 lineup.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP (one-time):
  pip install requests beautifulsoup4

USAGE:
  python3 sixpicks_optimizer.py

  Run ~1 hour before first pitch for confirmed lineups.
  The board URL always returns the latest data.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import sys, re, time, itertools
from datetime import date
from difflib import SequenceMatcher

missing = []
try:    import requests
except ImportError: missing.append("requests")
try:    from bs4 import BeautifulSoup
except ImportError: missing.append("beautifulsoup4")
if missing:
    print("Missing packages. Run:\n  pip install " + " ".join(missing))
    sys.exit(1)

# =============================================================================
#  CONFIG
# =============================================================================
TODAY        = date.today().strftime("%Y-%m-%d")
SEASON       = date.today().year
SALARY_CAP   = 120.0
MLB_API      = "https://statsapi.mlb.com/api/v1"
BOARD_URL    = "https://ottoneu.fangraphs.com/sixpicks/baseball/board"

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
#  SCORING (FanGraphs Points / Six Picks rules)
# =============================================================================
H_1B=5.6; H_2B=7.5; H_3B=10.5; H_HR=14.0
H_BB=3.0; H_HBP=3.0; H_SB=1.9; H_CS=-2.8; H_AB=-1.0
P_OUT=2.8/3; P_K=2.0; P_BB=-3.0; P_HBP=-3.0; P_HR=-13.0
P_SV=5.0; P_HLD=4.0
SP_MULT = 0.5

SLOTS = ["C", "CI", "MI", "OF", "SP", "RP"]
SLOT_LABELS = {
    "C":  "CATCHER",
    "CI": "CORNER INFIELD  (1B / 3B)",
    "MI": "MIDDLE INFIELD  (2B / SS)",
    "OF": "OUTFIELD",
    "SP": "STARTING PITCHER  [pts × 0.5]",
    "RP": "RELIEF PITCHER",
}
POS_TO_SLOT = {
    "C":["C"], "1B":["CI"], "3B":["CI"],
    "2B":["MI"], "SS":["MI"],
    "LF":["OF"], "CF":["OF"], "RF":["OF"], "OF":["OF"],
}
KNOWN_CLOSERS = [
    "Edwin Diaz","Felix Bautista","Ryan Helsley","Emmanuel Clase",
    "Devin Williams","Josh Hader","Clay Holmes","Jhoan Duran",
    "Andres Munoz","Pete Fairbanks","Alexis Diaz","Jordan Romano",
    "Camilo Doval","Evan Phillips","Tanner Scott","Raisel Iglesias",
    "Kenley Jansen","David Bednar","Mason Miller","Jeff Hoffman",
]

# =============================================================================
#  STEP 1 — SCRAPE THE BIG BOARD
# =============================================================================
def fetch_board(debug: bool = False) -> list[dict]:
    """
    Scrape the Ottoneu Six Picks board (always returns latest data).
    Returns a list of dicts: {name, price, pick_pct, board_pts}

    Known structure:
      <table class="tablesorter tablesorter-default tablesorterXXXX" role="grid">
        <thead><tr><th>NAME</th><th>PRICE</th><th>PICK%</th><th>PTS</th></tr></thead>
        <tbody aria-live="polite">
          <tr role="row" class="odd|even">
            <td><a href="...">Player Name</a></td>
            <td align="center">$25.75</td>
            <td align="center">47.83%</td>
            <td align="center">19.8</td>
          </tr>
    """
    url = "https://ottoneu.fangraphs.com/sixpicks/baseball/board"
    print(f"  Fetching board: {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  ✗ Could not fetch board: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    if debug:
        all_tables = soup.find_all("table")
        print(f"  [debug] {len(all_tables)} table(s) found on page")
        for i, t in enumerate(all_tables):
            ths = [th.get_text(strip=True) for th in t.find_all("th")]
            tbody = t.find("tbody")
            tr_count = len(tbody.find_all("tr")) if tbody else 0
            print(f"  [debug] table[{i}] class={t.get('class')} headers={ths} rows={tr_count}")

    # Strategy 1: class contains "tablesorter"
    table = None
    for t in soup.find_all("table"):
        classes = " ".join(t.get("class", []))
        if "tablesorter" in classes:
            table = t
            break

    # Strategy 2: any table whose text contains "PRICE"
    if not table:
        for t in soup.find_all("table"):
            if "PRICE" in t.get_text()[:500].upper():
                table = t
                break

    # Strategy 3: any table where row 1, col 1 looks like a dollar amount
    if not table:
        for t in soup.find_all("table"):
            tbody = t.find("tbody")
            if not tbody:
                continue
            first_row = tbody.find("tr")
            if not first_row:
                continue
            cells = first_row.find_all("td")
            if len(cells) >= 2 and "$" in cells[1].get_text():
                table = t
                break

    if not table:
        print("  ✗ Could not find player table on board page.")
        if not debug:
            print("  Tip: re-run with fetch_board(debug=True) to diagnose.")
        return []

    # Detect column positions — fall back to known positions if detection fails
    thead = table.find("thead")
    col_names = []
    if thead:
        col_names = [th.get_text(strip=True).upper() for th in thead.find_all("th")]

    if debug:
        print(f"  [debug] col_names from thead: {col_names}")

    def col_idx(candidates, default):
        for c in candidates:
            for i, h in enumerate(col_names):
                if c in h:
                    return i
        return default  # hard-coded fallback to known column positions

    name_col  = col_idx(["NAME", "PLAYER"],          default=0)
    price_col = col_idx(["PRICE", "SALARY", "COST"], default=1)
    pct_col   = col_idx(["PICK", "PCT"],              default=2)
    pts_col   = col_idx(["PTS", "POINTS", "SCORE"],   default=3)

    if debug:
        print(f"  [debug] cols → name={name_col} price={price_col} pct={pct_col} pts={pts_col}")

    tbody = table.find("tbody")
    tr_list = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

    def cell_float(cells, idx, strip_chars="$%,"):
        if idx is None or idx >= len(cells):
            return None
        raw = cells[idx].get_text(strip=True)
        for ch in strip_chars:
            raw = raw.replace(ch, "")
        try:
            return float(raw.strip())
        except ValueError:
            return None

    rows = []
    for tr in tr_list:
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue

        name_cell = cells[name_col] if name_col < len(cells) else cells[0]
        a_tag = name_cell.find("a")
        name = (a_tag or name_cell).get_text(strip=True)
        if not name or name.upper() in ("NAME", "PLAYER", ""):
            continue

        price     = cell_float(cells, price_col)
        pick_pct  = cell_float(cells, pct_col)
        board_pts = cell_float(cells, pts_col)

        if price is None or not (0.5 <= price <= 200):
            if debug:
                raw_cells = [c.get_text(strip=True) for c in cells]
                print(f"  [debug] skipped '{name}': price={price} cells={raw_cells}")
            continue

        rows.append({
            "name":      name,
            "price":     price,
            "pick_pct":  pick_pct,
            "board_pts": board_pts,
        })

    print(f"  ✓ Parsed {len(rows)} players from the board.")
    return rows

def board_to_salary_dict(board_rows: list[dict]) -> dict:
    """Convert board rows to {lowercase_name: price} lookup."""
    return {row["name"].strip().lower(): row["price"] for row in board_rows}


# =============================================================================
#  STEP 2 — MLB STATS API
# =============================================================================
def fetch_today_games():
    url = (f"{MLB_API}/schedule?sportId=1&date={TODAY}"
           f"&hydrate=lineups,probablePitcher,team&gameType=R")
    try:
        r = requests.get(url, timeout=12); r.raise_for_status()
        return [g for d in r.json().get("dates",[]) for g in d.get("games",[])]
    except Exception as e:
        print(f"  [MLB API] {e}"); return []


def extract_players(games):
    batters, starters, seen = [], [], set()
    for game in games:
        for side in ("away", "home"):
            ti   = game.get("teams",{}).get(side,{})
            abbr = ti.get("team",{}).get("abbreviation","?")
            prob = ti.get("probablePitcher")
            if prob and prob.get("id") not in seen:
                seen.add(prob["id"])
                starters.append({
                    "mlb_id": prob["id"],
                    "name":   prob.get("fullName","?"),
                    "team":   abbr,
                })
        lineups = game.get("lineups",{})
        for key, side in [("awayPlayers","away"),("homePlayers","home")]:
            abbr = game.get("teams",{}).get(side,{}).get("team",{}).get("abbreviation","?")
            for p in lineups.get(key,[]):
                pid = p.get("id")
                pos = p.get("primaryPosition",{}).get("abbreviation","")
                if pid and pid not in seen and pos != "P":
                    seen.add(pid)
                    batters.append({
                        "mlb_id":   pid,
                        "name":     p.get("fullName","?"),
                        "pos_code": pos,
                        "team":     abbr,
                    })
    return batters, starters


def mlb_stats(mlb_id, group):
    try:
        url = f"{MLB_API}/people/{mlb_id}/stats?stats=season&season={SEASON}&group={group}"
        r = requests.get(url, timeout=8); r.raise_for_status()
        splits = r.json().get("stats",[{}])[0].get("splits",[])
        return splits[0].get("stat",{}) if splits else {}
    except Exception:
        return {}


def hitter_pts(stats):
    pa = float(stats.get("plateAppearances",0) or 0)
    if pa < MIN_PA: return 0.0
    ab = float(stats.get("atBats",0) or 0)
    h  = float(stats.get("hits",0) or 0)
    d  = float(stats.get("doubles",0) or 0)
    t  = float(stats.get("triples",0) or 0)
    hr = float(stats.get("homeRuns",0) or 0)
    bb = float(stats.get("baseOnBalls",0) or 0)
    hp = float(stats.get("hitByPitch",0) or 0)
    sb = float(stats.get("stolenBases",0) or 0)
    cs = float(stats.get("caughtStealing",0) or 0)
    s  = max(0.0, h - d - t - hr)
    ppa = ((s/pa)*H_1B + (d/pa)*H_2B + (t/pa)*H_3B + (hr/pa)*H_HR +
           (bb/pa)*H_BB + (hp/pa)*H_HBP + (sb/pa)*H_SB +
           (cs/pa)*H_CS + (ab/pa)*H_AB)
    return round(ppa * DEFAULT_HITTER_PA, 2)


def pitcher_pts(stats, is_sp, exp_ip):
    ip = float(stats.get("inningsPitched",0) or 0)
    if ip < MIN_IP: return 0.0
    outs = max(ip * 3, 1)
    g    = max(float(stats.get("gamesPlayed",1) or 1), 1)
    k    = float(stats.get("strikeOuts",0) or 0)
    bb   = float(stats.get("baseOnBalls",0) or 0)
    hp   = float(stats.get("hitByPitch",0) or 0)
    hr   = float(stats.get("homeRuns",0) or 0)
    sv   = float(stats.get("saves",0) or 0)
    hld  = float(stats.get("holds",0) or 0)
    eo   = exp_ip * 3
    pts  = (eo*P_OUT + (k/outs)*eo*P_K + (bb/outs)*eo*P_BB +
            (hp/outs)*eo*P_HBP + (hr/outs)*eo*P_HR +
            (sv/g)*P_SV + (hld/g)*P_HLD)
    return round(pts * (SP_MULT if is_sp else 1.0), 2)


# =============================================================================
#  SALARY LOOKUP (fuzzy name match)
# =============================================================================
def salary_lookup(name: str, sal_dict: dict) -> float | None:
    k = name.strip().lower()
    if k in sal_dict: return sal_dict[k]
    # Strip common suffixes
    k2 = re.sub(r"\s+(jr\.?|sr\.?|ii|iii|iv)$", "", k).strip()
    if k2 in sal_dict: return sal_dict[k2]
    # Fuzzy match
    best_r, best_v = 0.0, None
    for dk, dv in sal_dict.items():
        r = SequenceMatcher(None, k, dk).ratio()
        if r > best_r: best_r, best_v = r, dv
    return best_v if best_r >= 0.82 else None


# =============================================================================
#  BUILD PLAYER POOL
# =============================================================================
def build_pool(sal_dict: dict) -> list:
    print(f"  Fetching today's schedule ({TODAY})...")
    games = fetch_today_games()
    if not games:
        print("  No games found today."); return []
    print(f"  {len(games)} game(s) today.")

    batters, starters = extract_players(games)
    if not batters:
        print("  ⚠ Lineups not yet posted — re-run ~1hr before first pitch.")
    else:
        print(f"  {len(batters)} confirmed batters | {len(starters)} probable starters")

    pool = []

    print("  Fetching batter stats...")
    for i, p in enumerate(batters):
        slots = POS_TO_SLOT.get(p["pos_code"], ["CI"])
        stats = mlb_stats(p["mlb_id"], "hitting")
        sal   = salary_lookup(p["name"], sal_dict)
        pts   = hitter_pts(stats)
        pool.append({
            "name":   p["name"], "team": p["team"], "slots": slots,
            "salary": sal or 8.0, "pts": pts,
            "value":  round(pts / (sal or 8.0), 3), "sal_ok": sal is not None,
        })
        if i % 6 == 5: time.sleep(0.2)

    print("  Fetching SP stats...")
    for p in starters:
        stats = mlb_stats(p["mlb_id"], "pitching")
        sal   = salary_lookup(p["name"], sal_dict)
        pts   = pitcher_pts(stats, is_sp=True, exp_ip=DEFAULT_SP_IP)
        pool.append({
            "name":   p["name"], "team": p["team"], "slots": ["SP"],
            "salary": sal or 10.0, "pts": pts,
            "value":  round(pts / (sal or 10.0), 3), "sal_ok": sal is not None,
        })
        time.sleep(0.15)

    # Relief pitchers — no daily confirmed source; use known closer list
    existing = {p["name"].lower() for p in pool}
    for name in KNOWN_CLOSERS:
        if name.lower() in existing: continue
        sal = salary_lookup(name, sal_dict) or 9.0
        pts = round(4.2 + (sal - 9.0) * 0.22, 2)
        pool.append({
            "name":   name, "team": "?", "slots": ["RP"],
            "salary": sal, "pts": pts,
            "value":  round(pts / sal, 3), "sal_ok": True,
        })

    return pool


# =============================================================================
#  OPTIMIZER + DISPLAY
# =============================================================================
def top5(pool, slot):
    return sorted([p for p in pool if slot in p["slots"]],
                  key=lambda p: p["pts"], reverse=True)[:5]


def best_lineup(pool, top_n=14):
    pools = {s: sorted([p for p in pool if s in p["slots"]],
                        key=lambda p: p["pts"], reverse=True)[:top_n]
             for s in SLOTS}
    best = {"pts": -999, "lineup": None, "salary": 0}
    for combo in itertools.product(*[pools[s] for s in SLOTS]):
        if len({p["name"] for p in combo}) < 6: continue
        sal = sum(p["salary"] for p in combo)
        if sal > SALARY_CAP: continue
        pts = sum(p["pts"] for p in combo)
        if pts > best["pts"]:
            best = {"pts": pts, "lineup": combo, "salary": sal}
    return best


W = 74
def hr(c="="): print(c * W)
def section(t): print(); hr(); print(f"  {t}"); hr()


def show_top5(players):
    print(f"\n  {'#':<3}  {'PLAYER':<28} {'TEAM':>4}  {'SALARY':>7}  {'PROJ PTS':>9}  {'PTS/$':>6}")
    print(f"  {'─'*3}  {'─'*28}  {'─'*4}  {'─'*7}  {'─'*9}  {'─'*6}")
    for i, p in enumerate(players, 1):
        flag = " " if p["sal_ok"] else "*"
        print(f"  {i:<3}  {p['name']:<28} {p['team']:>4}  ${p['salary']:>6.2f}{flag} {p['pts']:>9.1f}  {p['value']:>6.3f}")


def show_optimal(best):
    if not best["lineup"]:
        print("\n  No valid lineup found under the $120 cap."); return
    print(f"\n  {'SLOT':<5}  {'PLAYER':<28} {'TEAM':>4}  {'SALARY':>7}  {'PROJ PTS':>9}")
    print(f"  {'─'*4}  {'─'*28}  {'─'*4}  {'─'*7}  {'─'*9}")
    for slot, p in zip(SLOTS, best["lineup"]):
        print(f"  {slot:<5}  {p['name']:<28} {p['team']:>4}  ${p['salary']:>6.2f}  {p['pts']:>9.1f}")
    rem = SALARY_CAP - best["salary"]
    print(f"\n  TOTAL  ${best['salary']:.2f}  ·  Proj pts: {best['pts']:.1f}  ·  Cap remaining: ${rem:.2f}")


def show_board_preview(board_rows: list[dict], n: int = 10):
    """Show the top N players from the raw board by price, as a sanity check."""
    section(f"BIG BOARD PREVIEW  (top {n} by price — full universe)")
    top = sorted(board_rows, key=lambda r: r["price"], reverse=True)[:n]
    print(f"\n  {'PLAYER':<28}  {'PRICE':>7}  {'PICK%':>7}  {'BOARD PTS':>9}")
    print(f"  {'─'*28}  {'─'*7}  {'─'*7}  {'─'*9}")
    for r in top:
        pct  = f"{r['pick_pct']:.2f}%" if r["pick_pct"] is not None else "  —  "
        bpts = f"{r['board_pts']:.1f}"  if r["board_pts"] is not None else "  —"
        print(f"  {r['name']:<28}  ${r['price']:>6.2f}  {pct:>7}  {bpts:>9}")


# =============================================================================
#  MAIN
# =============================================================================
def main():
    hr("█")
    print("  OTTONEU SIX PICKS OPTIMIZER")
    print(f"  {TODAY}  |  Cap: ${SALARY_CAP:.0f}  |  Slots: C · CI · MI · OF · SP · RP")
    hr("█")

    # ── 1. Scrape Big Board for prices ────────────────────────────────────────
    print(f"\n[1/3] Fetching prices from Six Picks Big Board...")
    board_rows = fetch_board(debug=True)

    if not board_rows:
        print(f"\n  ⚠ Today's board ({TODAY}) is empty — it may not be populated yet.")
        print(f"  Ottoneu typically posts the board the morning of game day.")
        print(f"  You can verify at: https://ottoneu.fangraphs.com/sixpicks/baseball/board")
        ans = input("\n  Continue without prices (all marked *)? [y/N]: ").strip().lower()
        if ans != "y": sys.exit(0)
        sal_dict = {}
    else:
        sal_dict = board_to_salary_dict(board_rows)
        show_board_preview(board_rows, n=10)

    # ── 2. Build player pool from MLB API ─────────────────────────────────────
    print(f"\n[2/3] Loading today's player pool from MLB Stats API...")
    pool = build_pool(sal_dict)
    if not pool:
        print("  Nothing to show. Try again closer to first pitch."); return

    # ── 3. Rankings ───────────────────────────────────────────────────────────
    print(f"\n[3/3] Ranking {len(pool)} players...\n")

    for slot in SLOTS:
        section(f"TOP 5 — {SLOT_LABELS[slot]}")
        players = top5(pool, slot)
        if players:
            show_top5(players)
        else:
            print("  No confirmed players at this position today.")

    section("OPTIMAL LINEUP  (highest projected pts ≤ $120 cap)")
    print("  Searching..."); show_optimal(best_lineup(pool))

    section("BEST VALUE PICK PER SLOT  (most pts per dollar)")
    print(f"\n  {'SLOT':<5}  {'PLAYER':<28} {'TEAM':>4}  {'SALARY':>7}  {'PROJ PTS':>9}  {'PTS/$':>6}")
    print(f"  {'─'*4}  {'─'*28}  {'─'*4}  {'─'*7}  {'─'*9}  {'─'*6}")
    for slot in SLOTS:
        elig = [p for p in pool if slot in p["slots"]]
        if not elig: continue
        bv = max(elig, key=lambda p: p["value"])
        print(f"  {slot:<5}  {bv['name']:<28} {bv['team']:>4}  ${bv['salary']:>6.2f}  {bv['pts']:>9.1f}   {bv['value']:>6.3f}")

    print()
    hr()
    print("  * = salary not found on today's board; $8/$10 default used.")
    print("  RP = known closer list (MLB API has no daily RP confirmation).")
    print(f"  Prices: ottoneu.fangraphs.com/sixpicks/baseball/board")
    print(f"  Stats:  MLB Stats API {SEASON} season rates × typical game volume.")
    print("  Re-run ~1hr before first pitch for confirmed lineups.")
    hr()
    print()


if __name__ == "__main__":
    main()
