"""
KevScores Public Site Updater
==============================
Runs daily via GitHub Actions — no spreadsheet, no laptop needed.
Fetches MLB stats, computes Kev Scores, pushes fresh HTML to GitHub.
Vercel auto-deploys on every push.
"""

import sys, os, json, base64, re, unicodedata, urllib.request, urllib.error, urllib.parse, time
from datetime import datetime

# ── CONFIGURATION ──
GITHUB_TOKEN  = os.environ.get("KEVSCORES_TOKEN", "")
GITHUB_USER   = "KevScoresFantasy"
GITHUB_REPO   = "KevScores-Public"
GITHUB_FILE   = "index.html"
SEASON        = 2026

# Paths to data files (stored in repo alongside the script)
ESPN_BASELINE_FILE  = "espn_baseline.json"   # hardcoded Week 1 ESPN values
DAILY_HISTORY_FILE  = "daily_history.json"   # stores several days of score snapshots
WEEKLY_SCORES_FILE  = "weekly_scores.json"   # Monday baseline for weekly FP

# Keep only the most recent N days of daily history
MAX_HISTORY_DAYS = 8  # keep 8 so "7 days ago" is always available for kevChange7d

# Headshot configuration
HEADSHOTS_DIR = "headshots"
ESPN_MAP_PATH = "espn_id_map.json"

HEADERS_JSON = {
    "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept":        "application/json",
}

BATTING_MAP = {
    "gamesPlayed":"G","atBats":"AB","plateAppearances":"PA",
    "hits":"H","singles":"1B","doubles":"2B","triples":"3B","homeRuns":"HR",
    "runs":"R","rbi":"RBI","baseOnBalls":"BB","intentionalWalks":"IBB",
    "strikeOuts":"SO","hitByPitch":"HBP","sacFlies":"SF","sacBunts":"SH",
    "groundIntoDoublePlay":"GDP","stolenBases":"SB","caughtStealing":"CS","avg":"AVG",
}
PITCHING_MAP = {
    "wins":"W","losses":"L","era":"ERA","gamesPlayed":"G","gamesStarted":"GS",
    "qualityStarts":"QS","completeGames":"CG","shutouts":"ShO","saves":"SV",
    "holds":"HLD","blownSaves":"BS","inningsPitched":"IP","battersFaced":"TBF",
    "hits":"H","runs":"R","earnedRuns":"ER","homeRuns":"HR","baseOnBalls":"BB",
    "intentionalWalks":"IBB","hitByPitch":"HBP","wildPitches":"WP","balks":"BK",
    "strikeOuts":"SO",
}

def normalize(name):
    s = str(name).lower().strip()
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()
    s = s.replace(".", "").replace(",", "").replace("'", "").replace("-", " ")
    s = s.replace(" jr", "").replace(" sr", "").replace(" ii", "").replace(" iii", "")
    return " ".join(s.split())

def mlb_fetch(url):
    req = urllib.request.Request(url, headers=HEADERS_JSON)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def github_request(method, endpoint, payload=None):
    url  = f"https://api.github.com{endpoint}"
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json",
        "User-Agent":    "KevScores-Public-Updater",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def github_get_file(path):
    """Get file content and SHA from repo."""
    try:
        info = github_request("GET", f"/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{path}")
        content = base64.b64decode(info["content"]).decode("utf-8")
        return content, info["sha"]
    except Exception:
        return None, None

def github_put_file(path, content, sha, message):
    """Create or update a file in the repo."""
    encoded = base64.b64encode(content.encode()).decode()
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    try:
        return github_request("PUT", f"/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{path}", payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, 'read') else ''
        raise Exception(f"GitHub PUT {path} failed {e.code}: {body[:200]}") from e

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
# HEADSHOT DOWNLOADING FUNCTIONS
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def load_espn_map():
    """Load ESPN player ID map from local file."""
    if not os.path.exists(ESPN_MAP_PATH): 
        return {}
    try:
        with open(ESPN_MAP_PATH) as f:
            data = json.load(f)
        return data.get("by_name", {})
    except: 
        return {}

def refresh_espn_map():
    """Refresh ESPN player ID map by fetching from all MLB team rosters."""
    espn_map = {}
    for team_id in range(1, 31):
        url = f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams/{team_id}/roster"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "application/json"
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            for group in data.get("athletes", []):
                items = group.get("items", [group])
                for athlete in items:
                    espn_id  = str(athlete.get("id", ""))
                    fullname = athlete.get("fullName", athlete.get("displayName", ""))
                    if espn_id and fullname:
                        espn_map[normalize(fullname)] = espn_id
            time.sleep(0.1)  # Be nice to ESPN API
        except Exception:
            pass
    if espn_map:
        with open(ESPN_MAP_PATH, "w") as f:
            json.dump({"by_name": espn_map}, f)
    return espn_map

def download_headshots(players):
    """Download missing headshots for players and push to GitHub repo."""
    os.makedirs(HEADSHOTS_DIR, exist_ok=True)
    espn_map = load_espn_map()
    
    # Refresh ESPN map weekly (Monday) or if missing
    if not espn_map or datetime.now().weekday() == 0:
        print("  Refreshing ESPN player ID map...")
        fresh = refresh_espn_map()
        if fresh:
            espn_map = fresh
            print(f"  ESPN map updated: {len(espn_map)} players")
    
    if not espn_map:
        print("  ESPN map unavailable - skipping headshots")
        return

    # Find players missing headshots
    to_download = []
    for p in players:
        norm    = normalize(p["name"])
        espn_id = espn_map.get(norm, "")
        if not espn_id: 
            continue
        mlb_id  = p.get("mlbId", "")
        if not mlb_id: 
            continue
        path = os.path.join(HEADSHOTS_DIR, f"{mlb_id}.jpg")
        if not os.path.exists(path):
            to_download.append((p["name"], espn_id, mlb_id, path))

    # Report status
    players_with_espn = sum(1 for p in players if espn_map.get(normalize(p["name"])))
    has_headshot = sum(1 for p in players if p.get("mlbId") and
                       os.path.exists(os.path.join(HEADSHOTS_DIR, f"{p['mlbId']}.jpg")))
    missing = [p["name"] for p in players if p.get("mlbId") and
               espn_map.get(normalize(p["name"])) and
               not os.path.exists(os.path.join(HEADSHOTS_DIR, f"{p['mlbId']}.jpg"))]
    
    print(f"  {players_with_espn}/{len(players)} have ESPN ID | {has_headshot} headshots cached")
    if missing:
        print(f"  Will download {len(missing)}: {', '.join(missing[:5])}{'...' if len(missing)>5 else ''}")

    if not to_download:
        print("  Headshots: all up to date")
        return

    # Download new headshots
    print(f"  Downloading {len(to_download)} new headshots from ESPN...")
    downloaded = []
    for name, espn_id, mlb_id, path in to_download:
        url = f"https://a.espncdn.com/combiner/i?img=/i/headshots/mlb/players/full/{espn_id}.png&w=160&h=120"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read()
            if len(data) > 2000:  # Valid image check
                with open(path, "wb") as f:
                    f.write(data)
                downloaded.append((mlb_id, path))
        except Exception:
            pass

    if not downloaded:
        print("  No headshots downloaded")
        return

    # Push downloaded headshots to GitHub
    print(f"  Downloaded {len(downloaded)} headshots - pushing to GitHub...")
    pushed = 0
    for mlb_id, path in downloaded:
        with open(path, "rb") as f:
            img_data = f.read()
        encoded     = base64.b64encode(img_data).decode("utf-8")
        github_path = f"headshots/{mlb_id}.jpg"
        try:
            try:
                info = github_request("GET", f"/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{github_path}")
                sha  = info["sha"]
            except Exception:
                sha = None
            payload = {"message": f"Add headshot {mlb_id}", "content": encoded}
            if sha: payload["sha"] = sha
            github_request("PUT", f"/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{github_path}", payload)
            pushed += 1
            # Add delay to avoid GitHub rate limits
            time.sleep(0.5)
        except Exception:
            pass
    print(f"  Pushed {pushed}/{len(downloaded)} headshots to GitHub")

# ── FETCH MLB STATS ──
def fetch_all(group, label):
    print(f"  {label}...", end=" ", flush=True)
    all_splits, offset, limit = [], 0, 2000
    col_map = BATTING_MAP if group == "hitting" else PITCHING_MAP

    while True:
        try:
            data = mlb_fetch(
                f"https://statsapi.mlb.com/api/v1/stats"
                f"?stats=season&group={group}&season={SEASON}"
                f"&limit={limit}&offset={offset}&sportId=1&playerPool=ALL&hydrate=team"
            )
            splits = data.get("stats", [{}])[0].get("splits", [])
            if not splits:
                break
            all_splits.extend(splits)
            if len(splits) < limit:
                break
            offset += limit
        except Exception as e:
            print(f"FAILED — {e}")
            return {}

    rows = {}
    missing_team_count = 0

    for s in all_splits:
        stat = s.get("stat", {})
        player = s.get("player", {})
        team = s.get("team", {}) or {}

        name = player.get("fullName", "")
        norm = normalize(name)
        mlb_id = str(player.get("id", ""))

        team_abbr = team.get("abbreviation", "") if isinstance(team, dict) else ""

# Fix Arizona abbreviation for logos/colors
        if team_abbr == "AZ":
            team_abbr = "ARI"

        if not team_abbr:
            missing_team_count += 1

        row = {
            "Name": name,
            "Team": team_abbr,
            "mlbId": mlb_id,
        }

        for k, v in col_map.items():
            row[v] = stat.get(k, 0)

        rows[norm] = row

    print(f"{len(rows)} players")
    print(f"    Missing teams from stats response: {missing_team_count}")
    return rows
    
# ── COMPUTE FANTASY POINTS ──
MIN_PA = 25
MIN_IP = 5.0

def convert_ip(ip):
    """
    Convert MLB innings pitched format to true decimal innings.
    MLB uses:
      5.0 = 5 innings
      5.1 = 5 and 1/3
      5.2 = 5 and 2/3
    """
    try:
        ip = float(ip or 0)
    except (TypeError, ValueError):
        return 0.0

    whole = int(ip)
    frac = round(ip - whole, 1)

    if frac == 0.1:
        return whole + (1 / 3)
    if frac == 0.2:
        return whole + (2 / 3)
    return ip

def compute_fp(rows, sheet_type):
    """
    Returns (fp_total, fp_per_game, eligible) dicts.
    Per-game FP normalizes for injuries and missed time.
    Separated by sheet type so SP/RP/Batter each rank against their own group.
    """
    fp_total = {}
    fp_per_game = {}
    eligible = {}

    for norm, r in rows.items():
        def g(col):
            return float(r.get(col, 0) or 0)

        if sheet_type == "batting":
            # ESPN Standard H2H Points:
            # TB=1 (1B=1, 2B=2, 3B=3, HR=4), R=1, RBI=1, BB=1, SB=1, K=-1
            fp = (g("1B")*1 + g("2B")*2 + g("3B")*3 + g("HR")*4
                + g("R")*1 + g("RBI")*1
                + g("BB")*1
                + g("SB")*1
                - g("SO")*1
            )
            pa = g("PA")
            games = g("G") if g("G") > 0 else 1
            elig = pa >= MIN_PA
            fpg = round(fp / games, 3) if elig else None
        else:
            ip = convert_ip(r.get("IP", 0))
            # ESPN Standard H2H Points:
            # IP=3 (1pt per out), W=5, L=-5, SV=5, K=1, ER=-2, H=-1, BB=-1
            fp = (g("W")*5 - g("L")*5 + g("SV")*5
                + ip*3 + g("SO")*1
                - g("ER")*2 - g("H")*1 - g("BB")*1
            )
            games = g("G") if g("G") > 0 else 1
            elig = ip >= MIN_IP
            fpg  = round(fp / games, 3) if elig else None

        fp_total[norm] = int(round(fp))
        fp_per_game[norm] = fpg
        eligible[norm] = elig

    return fp_total, fp_per_game, eligible

def compute_sorare(rows, sheet_type):
    """
    Sorare scoring (separate from ESPN fpScore).
    Batting:  R=3, RBI=3, 1B=2, 2B=5, 3B=8, HR=10, BB=2, K=-1, SB=5, CS=-1, HBP=2
    Pitching: IP=3, K=2, H=-0.5, ER=-2, BB=-1, HBP=-1, W=5, RA=5, SV=10, Hold=5, NoHitter=30
    """
    sorare_total = {}
    sorare_pg = {}

    for norm, r in rows.items():
        def g(col):
            return float(r.get(col, 0) or 0)

        if sheet_type == "batting":
            sr = (g("1B")*2 + g("2B")*5 + g("3B")*8 + g("HR")*10
                + g("R")*3 + g("RBI")*3
                + g("BB")*2
                + g("SB")*5
                - g("SO")*1
                - g("CS")*1
                + g("HBP")*2
            )
        else:
            ip = convert_ip(r.get("IP", 0))
            # Note: MLB API doesn't expose no-hitters directly; NoHitter=30 omitted
            # RA (Relief Appearance) not a standard MLB API stat; omitted
            sr = (g("W")*5 + g("SV")*10 + g("HLD")*5
                + ip*3 + g("SO")*2
                - g("ER")*2 - g("H")*0.5 - g("BB")*1 - g("HBP")*1
            )

        games = g("G") if g("G") > 0 else 1
        sorare_total[norm] = int(round(sr))
        sorare_pg[norm] = round(sr / games, 3)

    return sorare_total, sorare_pg


def rank_scores(fp_per_game_dict):
    """
    Rank scores from per-game FP — only eligible players ranked.
    Ineligible players get rank_score=None.
    """
    eligible_items = [(n, v) for n, v in fp_per_game_dict.items() if v is not None]
    eligible_items.sort(key=lambda x: -x[1])
    n = len(eligible_items)
    rs = {}
    for i, (norm, _) in enumerate(eligible_items):
        rs[norm] = round(100 * (1 - i / (n - 1)), 4) if n > 1 else 100.0
    return rs

# ── ADJUSTED ESPN VALUE ──
def get_week_number():
    """Current MLB week (1-26). Season starts ~April 1."""
    season_start = datetime(SEASON, 4, 1)
    weeks_elapsed = max(0, (datetime.now() - season_start).days // 7)
    return min(weeks_elapsed + 1, 26)

def adjusted_espn_value(espn_value, rank_score):
    """
    Shifts 1% per week from ESPN toward stat-based rank score.
    Week 1: 80% ESPN + 20% rank  →  Week 26: 54% ESPN + 46% rank
    """
    week = get_week_number()
    stat_weight = min(0.20 + (week - 1) * 0.01, 0.46)
    espn_weight = 1 - stat_weight
    return round(espn_weight * espn_value + stat_weight * rank_score, 4)

# ── BUILD PLAYERS LIST ──
def build_players(batting_rows, pitching_rows,
                  bat_fp_total, bat_fp_pg, bat_rs,
                  pit_fp_total, pit_fp_pg, pit_rs,
                  bat_sr_total, bat_sr_pg,
                  pit_sr_total, pit_sr_pg,
                  espn_baseline):
    rows = []
    all_norms = set(batting_rows.keys()) | set(pitching_rows.keys())

    for norm in all_norms:
        in_pitching = norm in pitching_rows
        in_batting = norm in batting_rows

        if in_pitching and in_batting:
            # Two-way player: use ESPN baseline position to decide.
            # Fall back to whichever role has more accumulated stats (IP vs PA).
            espn_pos_check = ""
            espn_data_check = espn_baseline.get(
                next((k for k in espn_baseline if normalize(k) == norm), None) or ""
            ) or {}
            espn_pos_check = str(espn_data_check.get("pos", "")).upper()
            if "SP" in espn_pos_check or "RP" in espn_pos_check:
                is_pitcher = True
            else:
                pit_ip = float(pitching_rows[norm].get("IP", 0) or 0)
                bat_pa = float(batting_rows[norm].get("PA", 0) or 0)
                is_pitcher = pit_ip > bat_pa
        else:
            is_pitcher = in_pitching and not in_batting

        raw = pitching_rows.get(norm) or batting_rows.get(norm)
        name = raw["Name"]
        mlb_team = raw["Team"]
        mlb_id = raw["mlbId"]

        if is_pitcher:
            rs = pit_rs.get(norm)
            fp = pit_fp_total.get(norm, 0)
            fp_pg = pit_fp_pg.get(norm)
            sr = pit_sr_total.get(norm, 0)
            sr_pg = pit_sr_pg.get(norm, 0)
            pb = "Pitcher"
            stats_src = pitching_rows.get(norm, {})
        else:
            rs = bat_rs.get(norm)
            fp = bat_fp_total.get(norm, 0)
            fp_pg = bat_fp_pg.get(norm)
            sr = bat_sr_total.get(norm, 0)
            sr_pg = bat_sr_pg.get(norm, 0)
            pb = "Batter"
            stats_src = batting_rows.get(norm, {})

        not_eligible = rs is None

        espn_data = espn_baseline.get(name) or espn_baseline.get(
            next((k for k in espn_baseline if normalize(k) == norm), None) or ""
        ) or {}

        espn_rank = espn_data.get("rank", 999)
        espn_value = espn_data.get("value", 0)
        pos = espn_data.get("pos", "SP" if is_pitcher else "OF")

        rs_for_calc = rs if not not_eligible else 0
        adj_value = adjusted_espn_value(espn_value, rs_for_calc)
        kev_score = round(adj_value, 4)

        if kev_score < 0.5 and espn_rank >= 900:
            continue

        rows.append({
            "name": name,
            "kev": kev_score,
            "espn_rk": espn_rank,
            "espn_val": espn_value,
            "adj_val": adj_value,
            "pb": pb,
            "pos": pos,
            "mlb_team": mlb_team,
            "mlb_id": mlb_id,
            "fp": fp,
            "fp_pg": fp_pg,
            "rs": rs if rs is not None else 0,
            "not_eligible": not_eligible,
            "sr": sr,
            "sr_pg": sr_pg,
            "stats_src": stats_src,
        })

    rows.sort(key=lambda x: -x["kev"])
    return rows

def assign_ratings(rows):
    """Assign positional ratings (SP1, OF3 etc) by Kev Score rank within position."""
    pos_counters = {}
    for r in rows:
        pos_raw = str(r.get("pos", "")).upper()
        pb = r.get("pb", "")

        if "SP" in pos_raw and pb == "Pitcher":
            prefix = "SP"
        elif "RP" in pos_raw and pb == "Pitcher":
            prefix = "RP"
        elif pb == "Pitcher":
            prefix = "RP"
        elif "SS" in pos_raw:
            prefix = "SS"
        elif "3B" in pos_raw:
            prefix = "3B"
        elif "2B" in pos_raw:
            prefix = "2B"
        elif "1B" in pos_raw:
            prefix = "1B"
        elif "C" in pos_raw and len(pos_raw.split("/")[0]) <= 2:
            prefix = "C"
        elif "OF" in pos_raw:
            prefix = "OF"
        elif "DH" in pos_raw:
            prefix = "DH"
        else:
            prefix = "OF"

        pos_counters[prefix] = pos_counters.get(prefix, 0) + 1
        r["rating"] = f"{prefix}{pos_counters[prefix]}"

    return rows

def build_json(rows, weekly_prev):
    players, overall = [], []

    for i, r in enumerate(rows):
        kev_rank = i + 1
        kev = round(r["kev"], 2)
        espn_rk = r["espn_rk"]
        weighted = round(kev * (200 - espn_rk) / 100, 2) if espn_rk < 900 else round(kev * 0.2, 2)
        rank_diff = (espn_rk - kev_rank) if espn_rk < 900 else None
        rating = r.get("rating", "")
        is_unr_rp = espn_rk >= 900 and rating.startswith("RP")
        kev = round(kev * 0.75, 2) if is_unr_rp else kev

        wp = weekly_prev.get(r["name"], {}) if isinstance(weekly_prev, dict) else {}

        current_fp = int(round(r["fp"]))
        if isinstance(wp, dict) and "fp" in wp:
            baseline_fp = int(round(wp["fp"]))
            fp_weekly = current_fp - baseline_fp
            fp_prev_week = int(round(wp["prev_week_fp"])) if "prev_week_fp" in wp else None
        else:
            baseline_fp = current_fp
            fp_weekly = None
            fp_prev_week = None

        baseline_kev = wp.get("kev") if isinstance(wp, dict) else None
        kev_weekly = round(kev - float(baseline_kev), 2) if baseline_kev is not None else None

        # Export full season stats for Sleeper-style modal popup
        s = r.get("stats_src", {}) or {}
        def _num(x, default=0):
            try:
                if x is None or x == "":
                    return default
                return float(x)
            except Exception:
                return default

        if r["pb"] == "Batter":
            stats = {
                "G": int(_num(s.get("G", 0))),
                "AB": int(_num(s.get("AB", 0))),
                "PA": int(_num(s.get("PA", 0))),
                "H": int(_num(s.get("H", 0))),
                "HR": int(_num(s.get("HR", 0))),
                "RBI": int(_num(s.get("RBI", 0))),
                "R": int(_num(s.get("R", 0))),
                "BB": int(_num(s.get("BB", 0))),
                "SO": int(_num(s.get("SO", 0))),
                "SB": int(_num(s.get("SB", 0))),
                "TB": int(_num(s.get("TB", 0))) if "TB" in s else int(_num(s.get("1B", 0)) + 2*_num(s.get("2B", 0)) + 3*_num(s.get("3B", 0)) + 4*_num(s.get("HR", 0))),
                "AVG": round(_num(s.get("AVG", 0.0), 0.0), 3) if s.get("AVG", "") not in ("", None) else "—",
            }
        else:
            stats = {
                "G": int(_num(s.get("G", 0))),
                "GS": int(_num(s.get("GS", 0))),
                "IP": round(_num(s.get("IP", 0.0), 0.0), 1),
                "W": int(_num(s.get("W", 0))),
                "L": int(_num(s.get("L", 0))),
                "SV": int(_num(s.get("SV", 0))),
                "HLD": int(_num(s.get("HLD", 0))),
                "SO": int(_num(s.get("SO", 0))),
                "ER": int(_num(s.get("ER", 0))),
                "H": int(_num(s.get("H", 0))),
                "BB": int(_num(s.get("BB", 0))),
                "ERA": round(_num(s.get("ERA", 0.0), 0.0), 2) if s.get("ERA", "") not in ("", None) else "—",
            }

        players.append({
            "name": r["name"],
            "kevScore": kev,
            "kevRank": kev_rank,
            "espnRank": espn_rk,
            "rating": rating,
            "type": r["pb"],
            "team": r["mlb_team"],
            "weighted": weighted,
            "mlbId": r["mlb_id"],
            "fpScore": current_fp,
            "fpWeekly": fp_weekly,
            "fpPrevWeek": fp_prev_week,
            "kevWeekly": kev_weekly,
            "kevChange": None,
            "sorareScore": int(round(r.get("sr", 0))),
            "sorarePG": round(r.get("sr_pg", 0), 3),
            "stats": stats,
        })

        overall.append({
            "kevRank": kev_rank,
            "kevRating": rating,
            "kevScore": kev,
            "name": r["name"],
            "mlbTeam": r["mlb_team"],
            "pos": r["pos"],
            "espnRank": espn_rk,
            "rankDiff": rank_diff,
            "type": r["pb"],
            "fantasyTeam": "",
            "mlbId": r["mlb_id"],
            "kevChange": None,
            "fpScore": current_fp,
            "fpWeekly": fp_weekly,
            "fpPrevWeek": fp_prev_week,
            "kevWeekly": kev_weekly,
            "fpPG": r.get("fp_pg"),
            "notEligible": r.get("not_eligible", False),
            "sorareScore": int(round(r.get("sr", 0))),
            "sorarePG": round(r.get("sr_pg", 0), 3),
            "stats": stats,
        })

    return players, overall

def clean_zero(v):
    """Turn -0.0 into 0.0 to avoid JS display quirks."""
    return 0.0 if v == 0 else v

def apply_daily_changes(players, overall, daily_history, today_str):
    """
    Compare today's scores against:
      - the most recent prior saved day (kevChange, ~1 day back)
      - the oldest saved day, which is ~7 days back (kevChange7d, rolling week)
    Keeps both values stable across same-day reruns.
    """
    prior_dates = sorted(d for d in daily_history.keys() if d < today_str)
    if not prior_dates:
        print("  Daily history: no prior day found — changes set to None")
        return players, overall

    # 1-day baseline (yesterday)
    baseline_date = prior_dates[-1]
    baseline_scores = daily_history.get(baseline_date, {})
    print(f"  Daily change baseline:  {baseline_date}")

    # 7-day baseline (oldest entry in history; with MAX_HISTORY_DAYS=8 this is 7 days back)
    week_date = prior_dates[0]
    week_scores = daily_history.get(week_date, {})
    print(f"  7-day change baseline:  {week_date}")

    for p in players:
        prev = baseline_scores.get(p["name"])
        p["kevChange"] = clean_zero(round(p["kevScore"] - prev, 2)) if prev is not None else None
        prev7 = week_scores.get(p["name"])
        p["kevChange7d"] = clean_zero(round(p["kevScore"] - prev7, 2)) if prev7 is not None else None

    for o in overall:
        prev = baseline_scores.get(o["name"])
        o["kevChange"] = clean_zero(round(o["kevScore"] - prev, 2)) if prev is not None else None
        prev7 = week_scores.get(o["name"])
        o["kevChange7d"] = clean_zero(round(o["kevScore"] - prev7, 2)) if prev7 is not None else None

    changers = sum(1 for o in overall if o.get("kevChange") not in (None, 0, 0.0))
    changers7 = sum(1 for o in overall if o.get("kevChange7d") not in (None, 0, 0.0))
    print(f"  Daily changes: {changers} players moved (1-day)")
    print(f"  Weekly changes: {changers7} players moved (7-day)")
    return players, overall

def update_daily_history(daily_history, players, today_str):
    """
    Save today's baseline once. Same-day reruns do not overwrite it.
    """
    if today_str in daily_history:
        print(f"  Daily history: today already saved ({today_str})")
        return daily_history, False

    daily_history[today_str] = {p["name"]: p["kevScore"] for p in players}

    keep_dates = sorted(daily_history.keys())[-MAX_HISTORY_DAYS:]
    daily_history = {d: daily_history[d] for d in keep_dates}

    print(f"  Daily history: saved new baseline for {today_str}")
    return daily_history, True

def _find_js_const_bounds(html, const_name):
    """Find the start/end of a JS const array like: const NAME = [...];"""
    prefix = f"const {const_name} = ["
    start = html.find(prefix)
    if start == -1:
        return None, None

    bracket_start = start + len(prefix) - 1
    depth = 0
    i = bracket_start
    in_str = False
    str_char = None

    while i < len(html):
        ch = html[i]
        if in_str:
            if ch == '\\':
                i += 2
                continue
            if ch == str_char:
                in_str = False
        else:
            if ch in ('"', "'"):
                in_str = True
                str_char = ch
            elif ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    end = html.find(';', i)
                    return start, end + 1
        i += 1

    return None, None

def inject(html, players, overall):
    """
    Update LAST_UPDATED inside index.html. PLAYERS and OVERALL are now
    written as separate JSON files (players.json, overall.json) so that
    index.html stays small and the browser can cache data independently.
    The players/overall args are kept for signature compatibility.
    Returns the (possibly unchanged) html string.
    """
    today = datetime.now().strftime("%B %d, %Y")

    if re.search(r'const LAST_UPDATED = "[^"]*";', html):
        html = re.sub(
            r'const LAST_UPDATED = "[^"]*";',
            f'const LAST_UPDATED = "{today}";',
            html
        )
        print(f"  Updated LAST_UPDATED to {today} ✓")
    else:
        print("  WARNING: LAST_UPDATED pattern not found in HTML")

    return html

def main():
    print()
    print("=" * 55)
    print(f"  KevScores Public Updater v4.0 — {datetime.now().strftime('%B %d, %Y')}")
    print("=" * 55)

    if not GITHUB_TOKEN:
        print("ERROR: KEVSCORES_TOKEN secret not set in GitHub Actions")
        sys.exit(1)

    print("\nDownloading MLB stats...")
    batting = fetch_all("hitting", "Batting")
    pitching = fetch_all("pitching", "Pitching")

    bat_fp_total, bat_fp_pg, bat_elig = compute_fp(batting, "batting")
    pit_fp_total, pit_fp_pg, pit_elig = compute_fp(pitching, "pitching")
    bat_sr_total, bat_sr_pg = compute_sorare(batting, "batting")
    pit_sr_total, pit_sr_pg = compute_sorare(pitching, "pitching")
    bat_rs = rank_scores(bat_fp_pg)
    pit_rs = rank_scores(pit_fp_pg)

    print(f"  Current week: {get_week_number()} (stat weight: {min(20 + (get_week_number() - 1), 46)}%)")
    print(f"  Eligible batters: {sum(bat_elig.values())} / {len(bat_elig)}")
    print(f"  Eligible pitchers: {sum(pit_elig.values())} / {len(pit_elig)}")

    print("\nLoading ESPN baseline...")
    espn_raw, _ = github_get_file(ESPN_BASELINE_FILE)
    if espn_raw:
        espn_baseline = json.loads(espn_raw)
        print(f"  {len(espn_baseline)} players in baseline")
    else:
        print("  WARNING: espn_baseline.json not found in repo — ESPN values will be 0")
        espn_baseline = {}

    print("\nLoading history/baselines...")
    history_raw, history_sha = github_get_file(DAILY_HISTORY_FILE)
    weekly_raw, weekly_sha = github_get_file(WEEKLY_SCORES_FILE)

    daily_history = json.loads(history_raw) if history_raw else {}
    weekly_prev = json.loads(weekly_raw) if weekly_raw else {}

    print(f"  Daily history dates loaded: {len(daily_history)}")

    print("\nComputing Kev Scores...")
    rows = build_players(
        batting, pitching,
        bat_fp_total, bat_fp_pg, bat_rs,
        pit_fp_total, pit_fp_pg, pit_rs,
        bat_sr_total, bat_sr_pg,
        pit_sr_total, pit_sr_pg,
        espn_baseline
    )
    rows = assign_ratings(rows)
    players, overall = build_json(rows, weekly_prev)
    print(f"  {len(players)} players computed")

    teams_filled = sum(1 for p in players if p.get("team"))
    teams_empty = sum(1 for p in players if not p.get("team"))
    print(f"  Teams populated: {teams_filled} / {len(players)} (empty: {teams_empty})")
    if players:
        p0 = players[0]
        print(f"  Sample: {p0['name']} → team='{p0.get('team', '')}', mlbId={p0.get('mlbId', '')}")

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Download and push new headshots
    print("\nChecking headshots...")
    download_headshots(players)

    # Then save today's baseline once, without affecting same-day reruns
    daily_history, history_changed = update_daily_history(daily_history, players, today_str)
    if history_changed:
        try:
            github_put_file(
                DAILY_HISTORY_FILE,
                json.dumps(daily_history, indent=2),
                history_sha,
                f"Daily history {today_str}"
            )
            print("  Daily history pushed ✓")
        except Exception as e:
            print(f"  WARNING: Could not save daily history: {e}")
            
    if datetime.now().weekday() == 0 and weekly_prev.get("_saved_on") != today_str:
        new_weekly = {}
        for p in players:
            old = weekly_prev.get(p["name"], {})
            old_baseline = int(round(old["fp"])) if isinstance(old, dict) and "fp" in old else None
            current = int(round(p.get("fpScore", 0)))
            prev_week_fp = (current - old_baseline) if old_baseline is not None else None
            new_weekly[p["name"]] = {
                "kev": round(p["kevScore"], 2),
                "fp": current,
                "prev_week_fp": prev_week_fp,
            }
        new_weekly["_saved_on"] = today_str
        
        try:
            github_put_file(
                WEEKLY_SCORES_FILE,
                json.dumps(new_weekly, indent=2),
                weekly_sha,
                f"Weekly baseline hard reset {today_str}"
            )
            print("  Weekly baseline hard reset to current values")
        except Exception as e:
            print(f"  WARNING: Could not save weekly snapshot: {e}")

    print("\nWriting data files...")

    # Push players.json
    players_raw, players_sha = github_get_file("players.json")
    try:
        github_put_file(
            "players.json",
            json.dumps(players),
            players_sha,
            f"Update players data {datetime.now().strftime('%b %d, %Y')}"
        )
        print(f"  players.json pushed ✓ ({len(players)} entries)")
    except Exception as e:
        print(f"  ERROR pushing players.json: {e}")
        sys.exit(1)

    # Push overall.json
    overall_raw, overall_sha = github_get_file("overall.json")
    try:
        github_put_file(
            "overall.json",
            json.dumps(overall),
            overall_sha,
            f"Update overall data {datetime.now().strftime('%b %d, %Y')}"
        )
        print(f"  overall.json pushed ✓ ({len(overall)} entries)")
    except Exception as e:
        print(f"  ERROR pushing overall.json: {e}")
        sys.exit(1)

    print("\nUpdating index.html (LAST_UPDATED only)...")
    html_raw, html_sha = github_get_file(GITHUB_FILE)
    if not html_raw:
        print("ERROR: index.html not found in repo")
        sys.exit(1)

    new_html = inject(html_raw, players, overall)

    # Only push index.html if LAST_UPDATED actually changed
    if new_html != html_raw:
        result = github_put_file(
            GITHUB_FILE,
            new_html,
            html_sha,
            f"Daily stats update {datetime.now().strftime('%b %d, %Y')}"
        )
        new_sha = result["content"]["sha"]
        print(f"  index.html pushed ✓ — SHA: {new_sha[:12]}")
    else:
        print("  index.html unchanged — skipping push")

    print("  Live at: https://kev-scores-public.vercel.app")
    print()
    print("All done!")
    print("=" * 55)

if __name__ == "__main__":
    main()
