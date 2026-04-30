"""
One-shot weekly baseline reset. Run this AFTER public_updater.py has run
with the singles fix in place — it pulls today's corrected players.json
and writes it as the new weekly_scores.json baseline.

Effect: this week's "fpWeekly" delta starts from 0 today, so the weekly
leaderboard shows only post-reset play through Sunday. Monday's normal
cycle then resumes with a clean full-week comparison.
"""
import json, os, base64, urllib.request, sys
from datetime import datetime

GITHUB_TOKEN = os.environ["KEVSCORES_TOKEN"]
GITHUB_USER  = "<your-github-username>"   # ← edit
GITHUB_REPO  = "<your-repo-name>"          # ← edit

def gh(method, path, payload=None):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(payload).encode() if payload else None,
        method=method,
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "KevScores-Rebaseline",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def get_file(path):
    info = gh("GET", f"/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{path}")
    return base64.b64decode(info["content"]).decode("utf-8"), info["sha"]

def put_file(path, content, sha, msg):
    gh("PUT", f"/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{path}", {
        "message": msg,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": sha,
    })

# Fetch today's corrected player FP totals
players_raw, _ = get_file("players.json")
players = json.loads(players_raw)
print(f"Loaded {len(players)} players from players.json")

# Build a fresh baseline: today's corrected fpScore is the new "Monday"
today_str = datetime.now().strftime("%Y-%m-%d")
new_weekly = {}
for p in players:
    new_weekly[p["name"]] = {
        "kev": round(float(p.get("kevScore", 0)), 2),
        "fp":  int(round(p.get("fpScore", 0) or 0)),
        # Wipe prev_week_fp — it was computed with the buggy formula and
        # would mislabel last week. Users see "—" for Prev Week column
        # until next Monday's normal cycle generates a clean value.
        "prev_week_fp": None,
    }
new_weekly["_saved_on"] = today_str

# Push to GitHub, overwriting the old (contaminated) weekly_scores.json
_, weekly_sha = get_file("weekly_scores.json")
put_file(
    "weekly_scores.json",
    json.dumps(new_weekly, indent=2),
    weekly_sha,
    f"One-shot weekly rebaseline (singles fix) {today_str}",
)
print(f"Pushed new weekly_scores.json — {len(new_weekly)-1} players reset to 0 fpWeekly")
