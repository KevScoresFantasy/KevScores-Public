"""
KevScores Public Updater v3.0
Runs daily via GitHub Actions — no spreadsheet needed.
"""
import sys, os, json, base64, re, unicodedata, urllib.request, urllib.error
from datetime import datetime

# ── CONFIG ──
GITHUB_TOKEN        = os.environ.get("KEVSCORES_TOKEN", "")
GITHUB_USER         = "KevScoresFantasy"
GITHUB_REPO         = "KevScores-Public"
GITHUB_FILE         = "index.html"
ESPN_BASELINE_FILE  = "espn_baseline.json"
DAILY_SNAPSHOT_FILE = "daily_snapshot.json"
WEEKLY_SCORES_FILE  = "weekly_scores.json"
SEASON              = 2026

BATTING_MAP  = {"gamesPlayed":"G","atBats":"AB","plateAppearances":"PA","hits":"H",
                "singles":"1B","doubles":"2B","triples":"3B","homeRuns":"HR","runs":"R",
                "rbi":"RBI","baseOnBalls":"BB","strikeOuts":"SO","hitByPitch":"HBP",
                "stolenBases":"SB","caughtStealing":"CS"}
PITCHING_MAP = {"wins":"W","losses":"L","gamesPlayed":"G","gamesStarted":"GS",
                "saves":"SV","holds":"HLD","inningsPitched":"IP","hits":"H",
                "earnedRuns":"ER","baseOnBalls":"BB","strikeOuts":"SO",
                "hitByPitch":"HBP","qualityStarts":"QS"}

MIN_PA = 25
MIN_IP = 5.0

def normalize(name):
    s = str(name).lower().strip()
    s = unicodedata.normalize("NFD", s).encode("ascii","ignore").decode()
    s = s.replace(".","").replace(",","").replace("'","").replace("-"," ")
    s = s.replace(" jr","").replace(" sr","").replace(" ii","").replace(" iii","")
    return " ".join(s.split())

def mlb_fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent":"Mozilla/5.0","Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def github_request(method, endpoint, payload=None):
    url  = f"https://api.github.com{endpoint}"
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json",
        "User-Agent":    "KevScores-Updater",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        raise Exception(f"GitHub {method} {endpoint} failed {e.code}: {body}")

def github_get_file(path):
    try:
        info    = github_request("GET", f"/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{path}")
        content = base64.b64decode(info["content"]).decode("utf-8")
        return content, info["sha"]
    except Exception:
        return None, None

def github_put_file(path, content, sha, message):
    encoded = base64.b64encode(content.encode()).decode()
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    return github_request("PUT", f"/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{path}", payload)

def fetch_all(group, label):
    print(f"  {label}...", end=" ", flush=True)
    all_splits, offset, limit = [], 0, 2000
    col_map = BATTING_MAP if group == "hitting" else PITCHING_MAP
    while True:
        try:
            data   = mlb_fetch(f"https://statsapi.mlb.com/api/v1/stats?stats=season&group={group}&season={SEASON}&limit={limit}&offset={offset}&sportId=1&playerPool=ALL")
            splits = data.get("stats",[{}])[0].get("splits",[])
            if not splits: break
            all_splits.extend(splits)
            if len(splits) < limit: break
            offset += limit
        except Exception as e:
            print(f"FAILED — {e}"); return {}
    rows = {}
    for s in all_splits:
        stat, player, team = s.get("stat",{}), s.get("player",{}), s.get("team",{})
        name = player.get("fullName","")
        norm = normalize(name)
        row  = {"Name": name, "Team": team.get("abbreviation",""), "mlbId": str(player.get("id",""))}
        for k, v in col_map.items():
            row[v] = stat.get(k, 0)
        rows[norm] = row
    print(f"{len(rows)} players")
    return rows

def compute_fp(rows, sheet_type):
    fp_total, fp_pg, eligible = {}, {}, {}
    for norm, r in rows.items():
        def g(c): return float(r.get(c, 0) or 0)
        if sheet_type == "batting":
            fp    = g("1B")+g("2B")*2+g("3B")*3+g("HR")*4+g("R")+g("RBI")+g("BB")+g("HBP")+g("SB")*2-g("CS")-g("SO")
            games = max(g("G"), 1)
            elig  = g("PA") >= MIN_PA
        else:
            fp    = g("W")*5-g("L")*5+g("SV")*5+g("HLD")*4+g("IP")*3+g("SO")-g("ER")*2-g("H")-g("BB")-g("HBP")
            games = max(g("G"), 1)
            elig  = g("IP") >= MIN_IP
        fp_total[norm] = round(fp, 1)
        fp_pg[norm]    = round(fp/games, 3) if elig else None
        eligible[norm] = elig
    return fp_total, fp_pg, eligible

def rank_scores(fp_pg):
    items = sorted([(n,v) for n,v in fp_pg.items() if v is not None], key=lambda x: -x[1])
    n = len(items)
    return {norm: round(100*(1-i/(n-1)), 4) if n>1 else 100.0 for i,(norm,_) in enumerate(items)}

def get_week_number():
    weeks = max(0, (datetime.now() - datetime(SEASON,4,1)).days // 7)
    return min(weeks+1, 26)

def adjusted_espn_value(espn_value, rank_score):
    week        = get_week_number()
    stat_weight = min(0.20 + (week-1)*0.01, 0.46)
    return round((1-stat_weight)*espn_value + stat_weight*rank_score, 4)

def build_players(bat_rows, pit_rows, bat_total, bat_pg, bat_rs,
                  pit_total, pit_pg, pit_rs, espn_baseline):
    rows = []
    for norm in set(bat_rows)|set(pit_rows):
        is_pit = norm in pit_rows and norm not in bat_rows
        # Use the correct source row for each type to get accurate team/ID
        raw       = pit_rows.get(norm) if is_pit else bat_rows.get(norm)
        if raw is None:
            raw   = pit_rows.get(norm) or bat_rows.get(norm)
        name      = raw["Name"]
        mlb_team  = raw.get("Team", "")
        mlb_id    = raw.get("mlbId", "")
        # Fallback: if team is empty, try the other source
        if not mlb_team:
            other = bat_rows.get(norm) if is_pit else pit_rows.get(norm)
            if other:
                mlb_team = other.get("Team", "")

        if is_pit:
            rs, fp, fpg, pb = pit_rs.get(norm), pit_total.get(norm,0), pit_pg.get(norm), "Pitcher"
        else:
            rs, fp, fpg, pb = bat_rs.get(norm), bat_total.get(norm,0), bat_pg.get(norm), "Batter"

        not_elig   = rs is None
        espn_data  = espn_baseline.get(name) or espn_baseline.get(
            next((k for k in espn_baseline if normalize(k)==norm), None) or "") or {}
        espn_rank  = espn_data.get("rank",  999)
        espn_value = espn_data.get("value", 0)
        pos        = espn_data.get("pos", "SP" if is_pit else "OF")

        rs_val    = rs if not not_elig else 0
        kev_score = round(adjusted_espn_value(espn_value, rs_val), 4)
        if kev_score < 0.5 and espn_rank >= 900:
            continue

        rows.append({"name":name,"kev":kev_score,"espn_rk":espn_rank,"espn_val":espn_value,
                     "pb":pb,"pos":pos,"mlb_team":mlb_team,"mlb_id":mlb_id,
                     "fp":fp,"fp_pg":fpg,"rs":rs_val,"not_elig":not_elig})
    rows.sort(key=lambda x: -x["kev"])
    return rows

def assign_ratings(rows):
    pos_cnt = {}
    for r in rows:
        pos, pb = str(r.get("pos","")).upper(), r.get("pb","")
        if   "SP" in pos and pb=="Pitcher": prefix="SP"
        elif "RP" in pos and pb=="Pitcher": prefix="RP"
        elif pb=="Pitcher":                  prefix="RP"
        elif "SS" in pos: prefix="SS"
        elif "3B" in pos: prefix="3B"
        elif "2B" in pos: prefix="2B"
        elif "1B" in pos: prefix="1B"
        elif "C"  in pos and len(pos.split("/")[0])<=2: prefix="C"
        elif "OF" in pos: prefix="OF"
        elif "DH" in pos: prefix="DH"
        else:              prefix="OF"
        pos_cnt[prefix] = pos_cnt.get(prefix,0)+1
        r["rating"] = f"{prefix}{pos_cnt[prefix]}"
    return rows

def build_json(rows, daily_prev, weekly_prev):
    players, overall = [], []
    for i, r in enumerate(rows):
        kev_rank  = i+1
        kev       = round(r["kev"], 2)
        espn_rk   = r["espn_rk"]
        weighted  = round(kev*(200-espn_rk)/100, 2) if espn_rk<900 else round(kev*0.2, 2)
        rank_diff = (espn_rk-kev_rank) if espn_rk<900 else None
        rating    = r.get("rating","")
        if espn_rk>=900 and rating.startswith("RP"):
            kev = round(kev*0.75, 2)

        prev       = daily_prev.get(r["name"])
        kev_change = round(kev-prev, 2) if prev is not None else None
        wp         = weekly_prev.get(r["name"], {})
        fp_weekly  = round(r["fp"]-(wp.get("fp",r["fp"]) if isinstance(wp,dict) else r["fp"]), 1)

        players.append({"name":r["name"],"kevScore":kev,"kevRank":kev_rank,
                        "espnRank":espn_rk,"rating":rating,"type":r["pb"],
                        "team":r["mlb_team"],"weighted":weighted,"mlbId":r["mlb_id"],
                        "fpScore":r["fp"]})
        overall.append({"kevRank":kev_rank,"kevRating":rating,"kevScore":kev,
                        "name":r["name"],"mlbTeam":r["mlb_team"],"pos":r["pos"],
                        "espnRank":espn_rk,"rankDiff":rank_diff,"type":r["pb"],
                        "fantasyTeam":"","mlbId":r["mlb_id"],"kevChange":kev_change,
                        "fpScore":r["fp"],"fpWeekly":fp_weekly,"fpPG":r.get("fp_pg"),
                        "notEligible":r.get("not_elig",False)})
    return players, overall

def inject(html, players, overall):
    today = datetime.now().strftime("%B %d, %Y")
    html  = re.sub(r'const LAST_UPDATED = "[^"]*";',
                   lambda m: f'const LAST_UPDATED = "{today}";', html)
    html  = re.sub(r'const PLAYERS = \[.*?\];',
                   lambda m: f'const PLAYERS = {json.dumps(players)};',
                   html, flags=re.DOTALL, count=1)
    html  = re.sub(r'const OVERALL = \[.*?\];',
                   lambda m: f'const OVERALL = {json.dumps(overall)};',
                   html, flags=re.DOTALL, count=1)
    return html

def main():
    print()
    print("="*55)
    print(f"  KevScores Public Updater v3.0 — {datetime.now().strftime('%B %d, %Y')}")
    print("="*55)

    if not GITHUB_TOKEN:
        print("ERROR: KEVSCORES_TOKEN not set"); sys.exit(1)

    # ── Fetch stats ──
    print("\nDownloading MLB stats...")
    batting  = fetch_all("hitting",  "Batting")
    pitching = fetch_all("pitching", "Pitching")

    # ── Compute FP + rank scores ──
    bat_total, bat_pg, bat_elig = compute_fp(batting,  "batting")
    pit_total, pit_pg, pit_elig = compute_fp(pitching, "pitching")
    bat_rs = rank_scores(bat_pg)
    pit_rs = rank_scores(pit_pg)
    week   = get_week_number()
    print(f"  Week {week}: stat weight {min(20+(week-1),46)}%")
    print(f"  Eligible: {sum(bat_elig.values())}B / {sum(pit_elig.values())}P")

    # ── Load ESPN baseline ──
    print("\nLoading ESPN baseline...")
    espn_raw, _ = github_get_file(ESPN_BASELINE_FILE)
    espn_baseline = json.loads(espn_raw) if espn_raw else {}
    print(f"  {len(espn_baseline)} players")

    # ── Load snapshots ──
    daily_raw,  daily_sha  = github_get_file(DAILY_SNAPSHOT_FILE)
    weekly_raw, weekly_sha = github_get_file(WEEKLY_SCORES_FILE)
    daily_prev  = json.loads(daily_raw)  if daily_raw  else {}
    weekly_prev = json.loads(weekly_raw) if weekly_raw else {}

    # ── Build players ──
    print("\nComputing Kev Scores...")
    rows = build_players(batting, pitching,
                         bat_total, bat_pg, bat_rs,
                         pit_total, pit_pg, pit_rs, espn_baseline)
    rows = assign_ratings(rows)
    players, overall = build_json(rows, daily_prev, weekly_prev)
    print(f"  {len(players)} players computed")

    # ── Daily changes ──
    today_str = datetime.now().strftime("%Y-%m-%d")
    saved_on  = daily_prev.get("_saved_on","")
    if daily_prev:
        changers = sum(1 for p in players if p.get("kevChange") and p["kevChange"]!=0)
        print(f"  Daily changes: {changers} players moved")
    else:
        print("  First baseline — risers populate tomorrow")

    # Save daily snapshot once per day
    if saved_on != today_str:
        new_daily = {p["name"]: p["kevScore"] for p in players}
        new_daily["_saved_on"] = today_str
        try:
            github_put_file(DAILY_SNAPSHOT_FILE, json.dumps(new_daily,indent=2),
                            daily_sha, f"Daily snapshot {today_str}")
            print(f"  Snapshot saved")
        except Exception as e:
            print(f"  Snapshot warning: {e}")

    # Save weekly baseline on Mondays
    if datetime.now().weekday() == 0:
        new_weekly = {p["name"]:{"kev":p["kevScore"],"fp":p.get("fpScore",0)} for p in players}
        new_weekly["_saved_on"] = today_str
        try:
            github_put_file(WEEKLY_SCORES_FILE, json.dumps(new_weekly,indent=2),
                            weekly_sha, f"Weekly baseline {today_str}")
            print("  Weekly baseline saved (Monday)")
        except Exception as e:
            print(f"  Weekly warning: {e}")

    # ── Read template, inject, push ──
    print("\nBuilding website...")
    html_raw, html_sha = github_get_file(GITHUB_FILE)
    if not html_raw:
        print("ERROR: index.html not found"); sys.exit(1)

    # ── DIAGNOSTICS ──
    print(f"  Template: {len(html_raw):,} chars, SHA={html_sha[:8] if html_sha else 'none'}")
    print(f"  Has MLB_TEAM_COLORS : {'MLB_TEAM_COLORS' in html_raw}")
    print(f"  Has 2026 Season     : {'2026 Season' in html_raw}")
    print(f"  Has ESPN hyperlink  : {'support.espn.com' in html_raw}")
    has_overall = bool(re.search(r'const OVERALL = \[', html_raw))
    has_players = bool(re.search(r'const PLAYERS = \[', html_raw))
    print(f"  Has OVERALL pattern : {has_overall}")
    print(f"  Has PLAYERS pattern : {has_players}")

    new_html = inject(html_raw, players, overall)

    # Verify inject worked
    m = re.search(r'const OVERALL = (\[.*?\]);', new_html, re.DOTALL)
    if m:
        sample = json.loads(m.group(1))
        first  = sample[0] if sample else {}
        print(f"  Injected {len(sample)} players, first mlbTeam={repr(first.get('mlbTeam','?'))}")
    else:
        print("  WARNING: OVERALL not found after inject!")

    result  = github_put_file(GITHUB_FILE, new_html, html_sha,
                              f"Daily stats update {datetime.now().strftime('%b %d, %Y')}")
    new_sha = result["content"]["sha"]
    print(f"  Pushed ✓ — SHA: {new_sha[:12]}")
    print(f"  Live at: https://kev-scores-public.vercel.app")
    print("\nAll done!")
    print("="*55)

if __name__ == "__main__":
    main()
