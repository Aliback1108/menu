from __future__ import annotations

import os
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, render_template, request
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
import requests_cache

# --------------------------------------------------------------------------------------
# Environment & configuration
# --------------------------------------------------------------------------------------
load_dotenv()

API_TOKEN = os.getenv("FOOTBALL_DATA_API_TOKEN", "")
BASE_URL = "https://api.football-data.org/v4"
REQUEST_TIMEOUT_SECONDS = 15

if not API_TOKEN:
	# Allow app to start with a clear error message in UI instead of crashing
	print("[WARN] FOOTBALL_DATA_API_TOKEN is not set. API calls will fail.")

app = Flask(__name__)

# VIP flag (replace by proper auth later)
IS_VIP_DEFAULT = False

# --------------------------------------------------------------------------------------
# HTTP session with retries and cache
# --------------------------------------------------------------------------------------
retry_strategy = Retry(
	total=5,
	backoff_factor=0.5,
	status_forcelist=[429, 500, 502, 503, 504],
	allowed_methods=["GET", "POST"],
	respect_retry_after_header=True,
)

# Cache GET requests to reduce rate limits pressure (15 minutes TTL)
requests_cache.install_cache(
	cache_name="football_cache",
	backend="memory",
	expire_after=900,
	allowable_methods=("GET",),
)

session = requests.Session()
session.headers.update({"X-Auth-Token": API_TOKEN})
session.mount("https://", HTTPAdapter(max_retries=retry_strategy))
session.mount("http://", HTTPAdapter(max_retries=retry_strategy))

# --------------------------------------------------------------------------------------
# Static mapping (can be moved to a DB or API lookup later)
# --------------------------------------------------------------------------------------
TEAM_IDS: Dict[str, int] = {
	"Manchester City": 65, "Liverpool": 64, "Paris Saint-Germain": 524, "Real Madrid": 86,
	"Chelsea": 61, "Arsenal": 57, "Brentford": 402, "Ipswich Town": 349, "Club Brugge": 851,
	"Nottingham Forest": 351, "Lille": 521, "PSV": 674, "Barcelona": 81, "Atlético Madrid": 78,
	"Inter Milan": 108, "Lazio": 110, "Angers SCO": 556, "Stade de Reims": 547,
	"Brighton & Hove Albion": 397, "Fulham": 63, "AFC Bournemouth": 1044,
	"Wolverhampton Wanderers": 76, "Crystal Palace": 354, "Aston Villa": 58,
	"Southampton": 340, "Bayern Munich": 5, "Benfica": 503, "Manchester United": 66,
	"Tottenham Hotspur": 73, "Juventus": 109, "AC Milan": 98, "Napoli": 113,
	"AS Roma": 100, "Borussia Dortmund": 4, "RB Leipzig": 721, "Porto": 497, "Ajax": 678,
	"Real Sociedad": 92, "Getafe": 82, "Newcastle United": 67, "Club Deportivo Leganés": 745,
	"Leicester City": 338, "Everton": 62, "West Ham United": 563, "Valencia": 95,
	"Sevilla": 559, "Bayer Leverkusen": 3, "Atalanta": 102, "Fiorentina": 99,
	"Sporting CP": 498, "Villarreal": 94
}

# --------------------------------------------------------------------------------------
# Helpers: robust HTTP, data guards
# --------------------------------------------------------------------------------------

def safe_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
	try:
		resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
		if not resp.ok:
			print(f"[HTTP] GET {url} failed: {resp.status_code} {resp.text[:200]}")
			return None
		return resp.json()
	except requests.RequestException as exc:
		print(f"[HTTP] Exception for {url}: {exc}")
		return None


def get_team_logo(team_id: int) -> str:
	url = f"{BASE_URL}/teams/{team_id}"
	data = safe_get_json(url)
	if data and isinstance(data, dict):
		return data.get("crest", "") or ""
	return ""


def get_team_matches(team_id: int) -> List[Dict[str, Any]]:
	url = f"{BASE_URL}/teams/{team_id}/matches"
	params = {
		"status": "FINISHED",
		# Use a longer window to stabilize stats but still recent (~6 months)
		"dateFrom": (datetime.utcnow() - timedelta(days=180)).strftime('%Y-%m-%d'),
		"dateTo": datetime.utcnow().strftime('%Y-%m-%d'),
	}
	data = safe_get_json(url, params=params)
	if not data or "matches" not in data:
		return []
	matches: List[Dict[str, Any]] = data.get("matches", [])
	# Optional: filter friendlies if needed (competition.type == "LEAGUE" or "CUP")
	filtered: List[Dict[str, Any]] = []
	for m in matches:
		comp = (m.get("competition") or {}).get("type")
		if comp in {"LEAGUE", "CUP"}:
			filtered.append(m)
	# Limit to last 20 for performance
	return sorted(filtered, key=lambda x: x.get("utcDate", ""), reverse=True)[:20]


def get_relevant_matches(home_team: str, away_team: str) -> List[Dict[str, Any]]:
	home_matches = get_team_matches(TEAM_IDS[home_team])
	away_matches = get_team_matches(TEAM_IDS[away_team])
	# Head-to-head both directions
	h2h = [m for m in home_matches if (m.get("homeTeam", {}).get("id") == TEAM_IDS[home_team] and m.get("awayTeam", {}).get("id") == TEAM_IDS[away_team])]
	h2h += [m for m in home_matches if (m.get("homeTeam", {}).get("id") == TEAM_IDS[away_team] and m.get("awayTeam", {}).get("id") == TEAM_IDS[home_team])]
	# Deduplicate by id if present
	seen: set = set()
	unique: List[Dict[str, Any]] = []
	for m in h2h + home_matches[:5] + away_matches[:5]:
		mid = m.get("id")
		if mid in seen:
			continue
		seen.add(mid)
		unique.append(m)
	return unique


# --------------------------------------------------------------------------------------
# Stats & predictions helpers
# --------------------------------------------------------------------------------------

def get_team_stats(matches: List[Dict[str, Any]], team_id: int) -> Dict[str, float]:
	if not matches:
		return {
			"goals_avg_scored": 0.0,
			"goals_avg_conceded": 0.0,
			"half_time_win_rate": 0.0,
			"second_half_win_rate": 0.0,
			"both_teams_score_rate": 0.0,
		}

	goals_scored = 0
	goals_conceded = 0
	half_time_wins = 0
	second_half_wins = 0
	both_teams_score = 0
	games = 0

	for match in matches:
		home_team_id = (match.get("homeTeam") or {}).get("id")
		away_team_id = (match.get("awayTeam") or {}).get("id")
		score = match.get("score") or {}
		ft = score.get("fullTime") or {}
		ht = score.get("halfTime") or {}

		home_goals = ft.get("home") or 0
		away_goals = ft.get("away") or 0
		home_half = ht.get("home") or 0
		away_half = ht.get("away") or 0

		home_second = max(0, home_goals - home_half)
		away_second = max(0, away_goals - away_half)

		if team_id not in (home_team_id, away_team_id):
			continue

		games += 1

		if home_team_id == team_id:
			goals_scored += home_goals
			goals_conceded += away_goals
			half_time_wins += 1 if home_half > away_half else 0
			# FIX: compare team-specific second-half goals correctly
			second_half_wins += 1 if home_second > away_second else 0
		else:
			goals_scored += away_goals
			goals_conceded += home_goals
			half_time_wins += 1 if away_half > home_half else 0
			# FIX: should compare away_second > home_second
			second_half_wins += 1 if away_second > home_second else 0

		both_teams_score += 1 if (home_goals > 0 and away_goals > 0) else 0

	if games == 0:
		games = 1

	return {
		"goals_avg_scored": round(goals_scored / games, 3),
		"goals_avg_conceded": round(goals_conceded / games, 3),
		"half_time_win_rate": round(half_time_wins / games, 3),
		"second_half_win_rate": round(second_half_wins / games, 3),
		"both_teams_score_rate": round(both_teams_score / games, 3),
	}


# Baseline Poisson model for goals and markets derived from it

def estimate_goal_intensities(home_team: str, away_team: str, matches: List[Dict[str, Any]]) -> Tuple[float, float]:
	"""Estimate lambda_home and lambda_away using simple averages with a mild home advantage.

	This is a quick baseline: lambda_home = avg_scored_home + 0.15, lambda_away = avg_scored_away.
	Clamp to reasonable bounds.
	"""
	home_stats = get_team_stats(matches, TEAM_IDS[home_team])
	away_stats = get_team_stats(matches, TEAM_IDS[away_team])

	lambda_home = max(0.1, min(3.5, home_stats["goals_avg_scored"] + 0.15))
	lambda_away = max(0.1, min(3.5, away_stats["goals_avg_scored"]))
	return lambda_home, lambda_away


def poisson_pmf(k: int, lmbda: float) -> float:
	try:
		return math.exp(-lmbda) * (lmbda ** k) / math.factorial(k)
	except OverflowError:
		return 0.0


def score_matrix(lambda_home: float, lambda_away: float, max_goals: int = 8) -> List[List[float]]:
	matrix: List[List[float]] = []
	for i in range(0, max_goals + 1):
		row: List[float] = []
		pi = poisson_pmf(i, lambda_home)
		for j in range(0, max_goals + 1):
			pj = poisson_pmf(j, lambda_away)
			row.append(pi * pj)
		matrix.append(row)
	return matrix


def probs_from_matrix(matrix: List[List[float]]) -> Dict[str, float]:
	p_home = 0.0
	p_draw = 0.0
	p_away = 0.0
	p_over25 = 0.0
	p_bts = 0.0
	for i, row in enumerate(matrix):
		for j, p in enumerate(row):
			if i > j:
				p_home += p
			elif i == j:
				p_draw += p
			else:
				p_away += p
			if (i + j) > 2:
				p_over25 += p
			if i > 0 and j > 0:
				p_bts += p
	# Normalize in case of truncation at max_goals
	total = p_home + p_draw + p_away
	if total > 0:
		p_home /= total
		p_draw /= total
		p_away /= total
	return {
		"1": round(p_home, 4),
		"X": round(p_draw, 4),
		"2": round(p_away, 4),
		"OVER_2_5": round(p_over25, 4),
		"BTS": round(p_bts, 4),
	}


def most_likely_exact_score(matrix: List[List[float]]) -> str:
	best_i = 0
	best_j = 0
	best_p = -1.0
	for i, row in enumerate(matrix):
		for j, p in enumerate(row):
			if p > best_p:
				best_p = p
				best_i = i
				best_j = j
	return f"{best_i}-{best_j}"


def predict_markets(home_team: str, away_team: str, matches: List[Dict[str, Any]]) -> Dict[str, Any]:
	lambda_home, lambda_away = estimate_goal_intensities(home_team, away_team, matches)
	matrix = score_matrix(lambda_home, lambda_away)
	p = probs_from_matrix(matrix)
	# Double chance from 1X2
	proba_1x = p["1"] + p["X"]
	proba_x2 = p["X"] + p["2"]
	proba_12 = p["1"] + p["2"]
	return {
		"result": max([("1", p["1"]), ("X", p["X"]), ("2", p["2"])], key=lambda x: x[1])[0],
		"double_chance": max([("1X", proba_1x), ("X2", proba_x2), ("12", proba_12)], key=lambda x: x[1])[0],
		"goals": round(sum(i * sum(row) for i, row in enumerate(matrix)), 2),
		"exact_score": most_likely_exact_score(matrix),
		"half_winner": "1" if p["1"] > max(p["X"], p["2"]) else ("2" if p["2"] > max(p["1"], p["X"]) else "X"),
		"over_under": "Plus de 2.5 buts" if p["OVER_2_5"] >= 0.5 else "Moins de 2.5 buts",
		"both_teams_score": "Oui" if p["BTS"] >= 0.5 else "Non",
		"probabilities": p,
	}


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

@app.route('/', methods=['GET', 'POST'])
def index():
	teams = sorted(TEAM_IDS.keys())
	predictions: Optional[Dict[str, Any]] = None
	home_team = None
	away_team = None
	home_logo = ""
	away_logo = ""
	error = None
	home_stats = None
	away_stats = None

	is_vip = IS_VIP_DEFAULT

	# If API token missing, surface a warning
	if not API_TOKEN:
		error = "Le jeton API n'est pas configuré. Configurez FOOTBALL_DATA_API_TOKEN dans .env."

	if request.method == 'POST':
		home_team = request.form.get('home_team')
		away_team = request.form.get('away_team')

		if not home_team or not away_team:
			error = "Veuillez sélectionner deux équipes."
		elif home_team == away_team:
			error = "Veuillez sélectionner deux équipes différentes."
		elif home_team in TEAM_IDS and away_team in TEAM_IDS:
			home_logo = get_team_logo(TEAM_IDS[home_team])
			away_logo = get_team_logo(TEAM_IDS[away_team])
			historical_matches = get_relevant_matches(home_team, away_team)
			if historical_matches:
				home_stats = get_team_stats(historical_matches, TEAM_IDS[home_team])
				away_stats = get_team_stats(historical_matches, TEAM_IDS[away_team])
				predictions = predict_markets(home_team, away_team, historical_matches)
			else:
				predictions = "no_data"
		else:
			error = "Équipe inconnue."

	return render_template(
		'index.html',
		teams=teams,
		predictions=predictions,
		home_team=home_team,
		away_team=away_team,
		home_logo=home_logo,
		away_logo=away_logo,
		error=error,
		home_stats=home_stats,
		away_stats=away_stats,
		is_vip=is_vip,
	)


if __name__ == '__main__':
	debug_flag = os.getenv("FLASK_DEBUG", "false").lower() == "true"
	app.run(host='0.0.0.0', port=8080, debug=debug_flag)