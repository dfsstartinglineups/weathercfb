import os
import sys
import json
import re
import requests
import datetime
from datetime import timezone, timedelta
import zoneinfo
import firebase_admin
from firebase_admin import credentials, firestore

# ==========================================
# 1. PATH CONFIGURATION & INIT
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR) if os.path.basename(SCRIPT_DIR) == 'scripts' else SCRIPT_DIR

TEAM_PAGES_DIR = os.path.join(ROOT_DIR, 'team_pages')
MAIN_INDEX_FILE = os.path.join(ROOT_DIR, 'index.html')
SITEMAP_FILE = os.path.join(ROOT_DIR, 'sitemap.xml')
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")

os.makedirs(TEAM_PAGES_DIR, exist_ok=True)
EST_TZ = zoneinfo.ZoneInfo("America/New_York")

# Initialize Firebase Admin SDK (Assumes GOOGLE_APPLICATION_CREDENTIALS is set in env)
if not firebase_admin._apps:
    firebase_admin.initialize_app()
db = firestore.client()

# ==========================================
# 2. UTILITY & HELPERS
# ==========================================
def slugify(text):
    text = str(text).lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[\s_-]+', '-', text)

def get_short_team_name(full_name):
    if not full_name or full_name == "TBD": return "TBD"
    parts = full_name.split()
    return parts[-1]

def write_if_changed(filepath, new_content):
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            old_content = f.read()
        if old_content == new_content:
            return False
            
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    return True

# ==========================================
# 3. FIREBASE DISCOVERY & GEOCODING
# ==========================================
def geocode_venue_multi_stage(stadium_name, city, home_team):
    """Fallback cascading geocoder mimicking the WeatherFootball architecture"""
    base_url = "https://geocoding-api.open-meteo.com/v1/search"
    queries = [
        f"{stadium_name} {city}",
        stadium_name,
        city,
        f"{home_team} stadium"
    ]
    
    for q in queries:
        if not q or not str(q).strip(): 
            continue
        try:
            res = requests.get(base_url, params={"name": q.strip(), "count": 1}, timeout=5)
            if res.status_code == 200:
                results = res.json().get('results')
                if results:
                    return results[0]['latitude'], results[0]['longitude']
        except Exception as e:
            print(f"⚠️ Geocoding failed for {q}: {e}")
            
    return 0.0, 0.0

# ==========================================
# 4. WEATHER FETCHING ENGINE
# ==========================================
def fetch_weather_api_hourly(lat, lon, game_iso_time, days_diff):
    if not WEATHER_API_KEY: return None

    utc_time = datetime.datetime.fromisoformat(game_iso_time.replace('Z', '+00:00'))
    req_days = max(1, min(14, days_diff + 2))
    url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_API_KEY}&q={lat},{lon}&days={req_days}&aqi=no&alerts=no"

    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200: return None
        
        data = res.json()
        current_data = data.get('current', {})
        current_epoch = int(datetime.datetime.now(timezone.utc).timestamp())
        
        all_hours = []
        for day in data.get('forecast', {}).get('forecastday', []):
            all_hours.extend(day.get('hour', []))
            
        target_epoch = int(utc_time.replace(minute=0, second=0, microsecond=0).timestamp())
        start_idx = next((i for i, h in enumerate(all_hours) if h['time_epoch'] == target_epoch), None)
        
        if start_idx is None: return None

        actual_start = max(0, start_idx - 1)
        actual_end = min(len(all_hours), start_idx + 4)

        hourly_slice = []
        for i in range(actual_start, actual_end):
            hour = all_hours[i]
            chance = hour.get('chance_of_rain', 0)
            condition_text = hour.get('condition', {}).get('text', '').lower()
            
            is_thunder = "thunder" in condition_text and "possible" not in condition_text
            is_snow = any(x in condition_text for x in ["snow", "ice", "blizzard", "sleet"])

            hour_iso = datetime.datetime.fromtimestamp(hour['time_epoch'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            hourly_slice.append({
                "timestamp": hour_iso,
                "temp": round(hour.get('temp_f', 72)),
                "precipChance": chance,
                "isThunderstorm": is_thunder,
                "isSnow": is_snow
            })

        kickoff_hour = all_hours[start_idx] if len(all_hours) > start_idx else (all_hours[0] if all_hours else {})
        
        return {
            "status": "ok",
            "temp": round(kickoff_hour.get('temp_f', 72)),
            "windSpeed": round(kickoff_hour.get('wind_mph', 0)),
            "precip": round(float(kickoff_hour.get('precip_in', 0.0)), 2),
            "hourly": hourly_slice
        }
    except Exception as e:
        print(f"⚠️ WeatherAPI Fetch Error: {e}")
        return None

def fetch_game_weather(lat, lon, game_iso_time):
    utc_time = datetime.datetime.fromisoformat(game_iso_time.replace('Z', '+00:00'))
    today_utc = datetime.datetime.now(timezone.utc).date()
    days_diff = (utc_time.date() - today_utc).days

    if days_diff > 14 or days_diff < -1:
        return {"status": "too_early", "temp": "--", "windSpeed": 0, "precip": 0, "hourly": []}

    weather = fetch_weather_api_hourly(lat, lon, game_iso_time, days_diff)
    if weather: return weather
    return {"status": "error", "temp": "--", "windSpeed": 0, "precip": 0, "hourly": []}

# ==========================================
# 5. SCHEDULE & FIREBASE MERGE
# ==========================================
def get_current_cfb_schedule(venues_dict, teams_dict):
    now = datetime.datetime.now()
    season_year = now.year if now.month > 2 else now.year - 1
    
    stype, wk = 2, 1
    
    # 1. Fetch current active week state
    try:
        base_url = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80"
        data = requests.get(base_url, timeout=10).json()
        stype = data.get('season', {}).get('type', 2)
        wk = data.get('week', {}).get('number', 1)
    except Exception as e:
        print(f"⚠️ ESPN Scoreboard fetch error: {e}")

    week_label = f"Week {wk}" if stype == 2 else f"Postseason Week {wk}"
    
    # 2. Fetch full schedule for FBS (group=80)
    schedule_url = f"https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&dates={season_year}&seasontype={stype}&week={wk}"
    games_list = []
    
    try:
        res_data = requests.get(schedule_url, timeout=10).json()
        events = res_data.get('events', [])
        
        # --- AUTO-ADVANCE WEEK CHECK ---
        all_final = len(events) > 0 and all(e.get('status', {}).get('type', {}).get('state') == 'post' for e in events)
        if all_final:
            print(f"🏁 All games for {week_label} are FINAL. Automatically advancing to the next week's slate...")
            if stype == 2 and wk >= 15:
                stype, wk = 3, 1
            else:
                wk += 1

            week_label = f"Week {wk}" if stype == 2 else f"Postseason Week {wk}"
            schedule_url = f"https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&dates={season_year}&seasontype={stype}&week={wk}"
            res_data = requests.get(schedule_url, timeout=10).json()
            events = res_data.get('events', [])

        for event in events:
            game_id = event['id']
            comp = event['competitions'][0]
            game_time = event['date']
            
            home_comp = next((c for c in comp['competitors'] if c['homeAway'] == 'home'), None)
            away_comp = next((c for c in comp['competitors'] if c['homeAway'] == 'away'), None)

            # --- FIREBASE TEAM DISCOVERY ---
            for c in [home_comp, away_comp]:
                if not c: continue
                tid = str(c['team']['id'])
                if tid not in teams_dict:
                    t_name = c['team']['displayName']
                    new_team = {
                        "id": tid,
                        "name": t_name,
                        "slug": slugify(t_name),
                        "abbr": c['team'].get('abbreviation', 'TBD')
                    }
                    db.collection('cfb_teams').document(tid).set(new_team, merge=True)
                    teams_dict[tid] = new_team
                    print(f"🆕 Discovered and saved new FBS team: {t_name}")

            # --- FIREBASE VENUE DISCOVERY ---
            espn_venue = comp.get('venue', {})
            venue_id = str(espn_venue.get('id', ''))
            stadium_info = venues_dict.get(venue_id)
            
            if not stadium_info and venue_id:
                s_name = espn_venue.get('fullName', 'TBD Location')
                s_city = espn_venue.get('address', {}).get('city', '')
                s_state = espn_venue.get('address', {}).get('state', '')
                is_indoor = espn_venue.get('indoor', False)
                
                lat, lon = geocode_venue_multi_stage(s_name, s_city, home_comp['team']['displayName'] if home_comp else "")
                
                stadium_info = {
                    "id": venue_id,
                    "name": s_name,
                    "city": s_city,
                    "state": s_state,
                    "roof": "Dome" if is_indoor else "Open",
                    "surface": "TBD",
                    "lat": lat,
                    "lon": lon
                }
                db.collection('cfb_venues').document(venue_id).set(stadium_info, merge=True)
                venues_dict[venue_id] = stadium_info
                print(f"🏟️ Discovered and geocoded new venue: {s_name} ({lat}, {lon})")

            # --- WEATHER FETCH ---
            is_dome = stadium_info.get('roof') in ["Dome", "Retractable"] if stadium_info else False
            if not is_dome and stadium_info and stadium_info.get('lat', 0.0) != 0.0:
                weather_payload = fetch_game_weather(stadium_info['lat'], stadium_info['lon'], game_time) or {"status": "ok", "temp": 72, "windSpeed": 0, "precip": 0, "hourly": []}
            else:
                weather_payload = {"status": "ok", "temp": 70, "windSpeed": 0, "precip": 0, "hourly": []}

            # --- RANKING FORMATTER ---
            home_rank = home_comp.get('curatedRank', {}).get('current', 99) if home_comp else 99
            away_rank = away_comp.get('curatedRank', {}).get('current', 99) if away_comp else 99
            
            h_name_display = home_comp['team']['displayName'] if home_comp else "TBD"
            a_name_display = away_comp['team']['displayName'] if away_comp else "TBD"
            
            if home_rank and home_rank <= 25: h_name_display = f"#{home_rank} {h_name_display}"
            if away_rank and away_rank <= 25: a_name_display = f"#{away_rank} {a_name_display}"

            games_list.append({
                "game_id": game_id,
                "game_info": event['name'],
                "status": event['status']['type']['state'],
                "clock": event['status']['type'].get('shortDetail', ''),
                "game_time": game_time,
                "week_label": week_label,
                "home_id": str(home_comp['team']['id']) if home_comp else "TBD",
                "away_id": str(away_comp['team']['id']) if away_comp else "TBD",
                "home_team": h_name_display,
                "away_team": a_name_display,
                "stadium": stadium_info,
                "weather": weather_payload
            })
    except Exception as e:
        print(f"❌ Failed to fetch current schedule: {e}")

    return week_label, games_list

# ==========================================
# 6. HTML GENERATORS
# ==========================================
def render_game_card(game, is_single_team=False):
    stadium = game.get('stadium', {})
    is_dome = stadium.get('roof') in ["Dome", "Retractable"]
    w = game.get('weather') or {"status": "too_early", "temp": "--", "windSpeed": 0, "precip": 0}
    is_too_early = w.get('status') == "too_early" or w.get('temp') == "--"
    
    hourly_list = w.get('hourly', [])
    max_pop = max([h.get('precipChance', 0) for h in hourly_list], default=0) if hourly_list else 0
    is_thunderstorm = any(h.get('isThunderstorm', False) for h in hourly_list) if hourly_list else False
    is_snow = any(h.get('isSnow', False) for h in hourly_list) if hourly_list else False

    border_class = ""
    bg_class = "bg-weather-sunny"
    precip_val = w.get('precip', 0)
    wind_val = w.get('windSpeed', 0)
    temp_val = w.get('temp', 70)

    if is_too_early: bg_class = "bg-light"
    elif is_dome: bg_class = "bg-weather-roof"
    elif is_thunderstorm or is_snow or max_pop >= 60 or precip_val >= 0.25 or wind_val >= 20:
        border_class = "border-danger border-3"; bg_class = "bg-weather-storm"
    elif max_pop >= 30 or precip_val > 0 or wind_val >= 15:
        border_class = "border-warning border-3"; bg_class = "bg-weather-rain"
    elif wind_val >= 12 or max_pop >= 15: bg_class = "bg-weather-cloudy"

    badge_text = "TBD"
    badge_style = "bg-light text-dark border"
    status_state = game.get('status', 'pre')

    if status_state == 'pre' and game.get('game_time'):
        dt = datetime.datetime.fromisoformat(game['game_time'].replace('Z', '+00:00')).astimezone(EST_TZ)
        badge_text = dt.strftime("%a %I:%M %p").replace(" 0", " ")
    elif status_state == 'in':
        badge_text = game.get('clock', 'LIVE')
        badge_style = "bg-danger text-white border-danger"
    elif status_state == 'post':
        badge_text = "FINAL"
        badge_style = "bg-secondary text-white border-secondary"

    away_name = game.get('away_team', 'TBD')
    home_name = game.get('home_team', 'TBD')
    away_logo = f"https://a.espncdn.com/i/teamlogos/ncaa/500/{game.get('away_id', '')}.png"
    home_logo = f"https://a.espncdn.com/i/teamlogos/ncaa/500/{game.get('home_id', '')}.png"

    display_rain = "0%" if is_dome else f"{max_pop}%"
    weather_emoji_line = f"Roof Closed 🌡️{temp_val}°" if is_dome else f"🌧️{display_rain} 🌡️{temp_val}° 💨{wind_val}mph"
    
    stadium_name = stadium.get('name', 'TBD Location')
    radar_url = f"https://embed.windy.com/embed2.html?lat={stadium.get('lat',0)}&lon={stadium.get('lon',0)}&zoom=11&level=surface&overlay=rain&product=ecmwf"

    weather_section = f"""
        <div class="weather-row row text-center align-items-center mt-2 mx-0">
            <div class="col-4 border-end px-1"><div class="fw-bold">{temp_val}°F</div><div class="small text-muted" style="font-size: 0.7rem;">Temp</div></div>
            <div class="col-4 border-end px-1"><div class="fw-bold text-primary">{display_rain}</div><div class="small text-muted" style="font-size: 0.7rem;">Rain</div></div>
            <div class="col-4 px-1"><div class="fw-bold">{wind_val} <span style="font-size:0.7em">mph</span></div><div class="small text-muted" style="font-size: 0.7rem;">Wind</div></div>
        </div>
        <div class="mt-2 mb-2">
            <button class="btn btn-sm btn-outline-primary w-100 py-1 fw-bold" onclick="showRadar('{radar_url}', '{stadium_name}')">🗺️ View Live Radar Map</button>
        </div>
    """

    col_class = "w-100 mb-3" if is_single_team else "col-md-6 col-lg-4 col-xl-3 mb-3 px-1"
    show_ribbon = "none" if is_single_team else "block"
    show_full = "block" if is_single_team else "none"
    
    return f"""
    <div class="{col_class}" id="game-{game['game_id']}">
        <div class="card game-card shadow-sm {border_class} {bg_class}" style="overflow: hidden;">
            
            <div class="ribbon-view p-2 position-relative" onclick="toggleSingleCard(event, '{game['game_id']}')" style="cursor: pointer; display: {show_ribbon};">
                <div class="d-flex align-items-center mb-1">
                    <span class="badge {badge_style} flex-shrink-0 px-2 py-1" style="font-size: 0.65rem;">{badge_text}</span>
                    <div class="fw-bold text-dark text-center flex-grow-1 ms-2" style="font-size: 0.75rem;">{weather_emoji_line}</div>
                </div>
                <div class="d-flex align-items-center mt-1" style="gap: 4px;">
                    <img src="{away_logo}" style="width: 16px; height: 16px; object-fit: contain;">
                    <span class="fw-bold text-dark lh-1" style="font-size: 0.75rem;">{away_name}</span>
                    <span class="fw-bold text-muted lh-1" style="font-size: 0.7rem;">@</span>
                    <img src="{home_logo}" style="width: 16px; height: 16px; object-fit: contain;">
                    <span class="fw-bold text-dark lh-1" style="font-size: 0.75rem;">{home_name}</span>
                </div>
            </div>

            <div class="full-card-view" onclick="toggleSingleCard(event, '{game['game_id']}')" style="cursor: pointer; display: {show_full};">
                <div class="card-body px-2 pt-2 pb-2"> 
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <span class="badge {badge_style}">{badge_text}</span>
                        <span class="stadium-name text-truncate text-end flex-grow-1 ms-2">{stadium_name}</span>
                    </div>
                    <div class="d-flex justify-content-between align-items-center px-1 mb-1">
                        <div class="d-flex align-items-center text-truncate" style="width: 45%; min-width: 0;"> 
                            <img src="{away_logo}" class="me-2" style="width: 24px; height: 24px; object-fit: contain;">
                            <div class="fw-bold lh-sm text-dark text-truncate" style="font-size: 0.95rem;">{away_name}</div>
                        </div>
                        <div class="text-center text-muted fw-bold" style="width: 10%; font-size: 0.8rem;">@</div>
                        <div class="d-flex align-items-center justify-content-end text-truncate" style="width: 45%; min-width: 0;"> 
                            <img src="{home_logo}" class="me-2" style="width: 24px; height: 24px; object-fit: contain;">
                            <div class="fw-bold lh-sm text-dark text-truncate text-end" style="font-size: 0.95rem;">{home_name}</div>
                        </div>
                    </div>
                    {weather_section}
                </div>
            </div>

        </div>
    </div>
    """

def render_bye_card(team_name):
    return f"""
    <div class="w-100 mb-3">
        <div class="card p-4 text-center border rounded bg-white shadow-sm">
            <h5 class="fw-bold text-dark mb-2">🏈 Bye Week / Off Week</h5>
            <p class="text-muted mb-0" style="font-size: 0.9rem;">
                The <b>{team_name}</b> do not have a game scheduled for this week.
            </p>
        </div>
    </div>
    """

# ==========================================
# 7. HTML MASTER TEMPLATES
# ==========================================
MAIN_SITE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{week_label} CFB Weather & Stadium Conditions</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/css/select2.min.css" rel="stylesheet" />
    <style>
        body {{ background-color: #f8f9fa; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }} 
        .main-container {{ max-width: 1200px; margin: 30px auto; padding: 0 15px; }}
        .game-card {{ border: 1px solid #dee2e6; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); background: white; overflow: hidden; }}
        .stadium-name {{ color: #6c757d; font-size: 0.85rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }}
        
        /* Select2 Styling Overrides */
        .select2-container .select2-selection--single {{
            height: 38px; border: 1px solid #334155; background-color: #1e293b; color: #fff;
        }}
        .select2-container--default .select2-selection--single .select2-selection__rendered {{
            color: #adb5bd; line-height: 38px; font-weight: bold;
        }}
        .select2-container--default .select2-selection--single .select2-selection__arrow {{
            height: 36px;
        }}

        @keyframes weather-flow {{ 0% {{ background-position: 0% 50%; }} 50% {{ background-position: 100% 50%; }} 100% {{ background-position: 0% 50%; }} }}
        .bg-weather-sunny {{ background: linear-gradient(-45deg, #e3f2fd, #e1f5fe, #f1f8e9); background-size: 300% 300%; animation: weather-flow 15s ease infinite; }}
        .bg-weather-cloudy {{ background: linear-gradient(-45deg, #f5f5f5, #e0e0e0, #eeeeee); background-size: 300% 300%; animation: weather-flow 20s ease infinite; }}
        .bg-weather-rain {{ background: linear-gradient(180deg, #e3f2fd, #cfd8dc, #eceff1); background-size: 200% 200%; animation: weather-flow 8s ease infinite; }}
        .bg-weather-storm {{ background: linear-gradient(-45deg, #e1bee7, #cfd8dc, #e0e0e0); background-size: 300% 300%; animation: weather-flow 10s ease infinite; }}
        .bg-weather-roof {{ background-color: #ffffff; }}
    </style>
</head>
<body>
    <nav class="navbar shadow-sm py-2 mb-0 sticky-top" style="background-color: #0f172a;">
        <div class="container d-flex justify-content-between align-items-center flex-wrap gap-2">
            <a href="/" class="navbar-brand text-white fw-bold m-0" style="font-style: italic; font-size: 1.6rem;">
                Weather <span style="color: #5ac8fa;">CFB</span>
            </a>
            <div class="d-flex align-items-center gap-2" style="min-width: 250px;">
                <select id="team-nav-select" class="form-select form-select-sm fw-bold shadow-sm">
                    <option></option>
                    {select_options}
                </select>
            </div>
        </div>
    </nav>

    <div class="main-container">
        <div class="text-center mb-2">
            <h1 class="fw-bold h2 mb-1">Live College Football Weather</h1>
            <div class="fw-bold text-secondary mb-3" style="font-size: 0.9rem; text-transform: uppercase;">
                📅 {week_label}
            </div>
            <button class="btn btn-sm shadow-sm fw-bold px-4 py-1 border border-secondary bg-white" onclick="toggleAllWeatherCards()">
                <span id="expand-toggle-icon">▼</span> <span id="expand-toggle-text">Expand All Cards</span>
            </button>
        </div>
        
        <div id="games-container" class="row">
            {cards_content}
        </div>
    </div>

    <!-- RADAR MODAL -->
    <div class="modal fade" id="radarModal" tabindex="-1">
        <div class="modal-dialog modal-lg modal-dialog-centered">
            <div class="modal-content shadow">
                <div class="modal-header bg-dark text-white border-0 py-2">
                    <h5 class="modal-title fw-bold">Live Weather Radar</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body p-0 bg-light" style="height: 60vh;">
                    <iframe id="radarFrame" src="" class="w-100 h-100 border-0"></iframe>
                </div>
            </div>
        </div>
    </div>

    <script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/js/select2.min.js"></script>
    <script>
    let globalScoreboardMode = true;

    $(document).ready(function() {{
        $('#team-nav-select').select2({{
            placeholder: "🔍 Search FBS Teams...",
            width: '100%'
        }}).on('change', function() {{
            if(this.value) window.location.href = this.value;
        }});
    }});

    function toggleSingleCard(e, gameId) {{
        if (e && e.target.closest('a, button, input, label, select')) return; 
        const card = document.getElementById(`game-${{gameId}}`);
        if (!card) return;
        const ribbon = card.querySelector('.ribbon-view');
        const full = card.querySelector('.full-card-view');
        
        ribbon.style.display = ribbon.style.display === 'none' ? 'block' : 'none';
        full.style.display = full.style.display === 'none' ? 'block' : 'none';
    }}

    function toggleAllWeatherCards() {{
        globalScoreboardMode = !globalScoreboardMode;
        $('#expand-toggle-text').text(globalScoreboardMode ? 'Expand All Cards' : 'Collapse All Cards');
        $('#expand-toggle-icon').text(globalScoreboardMode ? '▼' : '▲');
        
        document.querySelectorAll('.game-card').forEach(card => {{
            card.querySelector('.ribbon-view').style.display = globalScoreboardMode ? 'block' : 'none';
            card.querySelector('.full-card-view').style.display = globalScoreboardMode ? 'none' : 'block';
        }});
    }}

    function showRadar(url, venueName) {{
        document.querySelector('#radarModal .modal-title').innerText = `Radar: ${{venueName}}`;
        document.getElementById('radarFrame').src = url;
        bootstrap.Modal.getOrCreateInstance(document.getElementById('radarModal')).show();
    }}
    </script>
</body>
</html>
"""

TEAM_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_title}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/css/select2.min.css" rel="stylesheet" />
    <style>
        body {{ background-color: #f8f9fa; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }} 
        .main-container {{ max-width: 520px; margin: 30px auto; padding: 0 15px; }}
        .game-card {{ border: 1px solid #dee2e6; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); background: white; overflow: hidden; }}
        
        .select2-container .select2-selection--single {{ height: 38px; border: 1px solid #334155; background-color: #1e293b; color: #fff; }}
        .select2-container--default .select2-selection--single .select2-selection__rendered {{ color: #adb5bd; line-height: 38px; font-weight: bold; }}
        .select2-container--default .select2-selection--single .select2-selection__arrow {{ height: 36px; }}
        
        @keyframes weather-flow {{ 0% {{ background-position: 0% 50%; }} 50% {{ background-position: 100% 50%; }} 100% {{ background-position: 0% 50%; }} }}
        .bg-weather-sunny {{ background: linear-gradient(-45deg, #e3f2fd, #e1f5fe, #f1f8e9); background-size: 300% 300%; animation: weather-flow 15s ease infinite; }}
        .bg-weather-cloudy {{ background: linear-gradient(-45deg, #f5f5f5, #e0e0e0, #eeeeee); background-size: 300% 300%; animation: weather-flow 20s ease infinite; }}
        .bg-weather-rain {{ background: linear-gradient(180deg, #e3f2fd, #cfd8dc, #eceff1); background-size: 200% 200%; animation: weather-flow 8s ease infinite; }}
        .bg-weather-storm {{ background: linear-gradient(-45deg, #e1bee7, #cfd8dc, #e0e0e0); background-size: 300% 300%; animation: weather-flow 10s ease infinite; }}
        .bg-weather-roof {{ background-color: #ffffff; }}
    </style>
</head>
<body>
    <nav class="navbar shadow-sm py-2 mb-0 sticky-top" style="background-color: #0f172a;">
        <div class="container d-flex justify-content-between align-items-center flex-wrap gap-2">
            <a href="/" class="navbar-brand text-white fw-bold m-0" style="font-style: italic; font-size: 1.6rem;">
                Weather <span style="color: #5ac8fa;">CFB</span>
            </a>
            <div class="d-flex align-items-center gap-2" style="min-width: 250px;">
                <select id="team-nav-select" class="form-select form-select-sm fw-bold">
                    <option></option>
                    {select_options}
                </select>
            </div>
        </div>
    </nav>
    
    <div class="main-container">
        <div class="text-center mt-3 mb-3">
            <h1 class="h4 fw-bold text-dark mb-1">{team_name} Weather Forecast</h1>
        </div>
        <div id="team-weather-container">
            {team_card_content}
        </div>
    </div>
    
    <!-- RADAR MODAL -->
    <div class="modal fade" id="radarModal" tabindex="-1">
        <div class="modal-dialog modal-lg modal-dialog-centered">
            <div class="modal-content shadow">
                <div class="modal-header bg-dark text-white border-0 py-2">
                    <h5 class="modal-title fw-bold">Live Weather Radar</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body p-0 bg-light" style="height: 60vh;">
                    <iframe id="radarFrame" src="" class="w-100 h-100 border-0"></iframe>
                </div>
            </div>
        </div>
    </div>
    
    <script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/js/select2.min.js"></script>
    <script>
        $(document).ready(function() {{
            $('#team-nav-select').select2({{
                placeholder: "🔍 Search FBS Teams...", width: '100%'
            }}).val(`/team_pages/{team_slug}/`).trigger('change.select2');
            
            $('#team-nav-select').on('change', function() {{
                if(this.value && this.value !== `/team_pages/{team_slug}/`) window.location.href = this.value;
            }});
        }});
        
        function toggleSingleCard(e, gameId) {{
            if (e && e.target.closest('a, button, input, label, select')) return; 
            const card = document.getElementById(`game-${{gameId}}`);
            if (!card) return;
            const ribbon = card.querySelector('.ribbon-view');
            const full = card.querySelector('.full-card-view');
            
            ribbon.style.display = ribbon.style.display === 'none' ? 'block' : 'none';
            full.style.display = full.style.display === 'none' ? 'block' : 'none';
        }}

        function showRadar(url, venueName) {{
            document.querySelector('#radarModal .modal-title').innerText = `Radar: ${{venueName}}`;
            document.getElementById('radarFrame').src = url;
            bootstrap.Modal.getOrCreateInstance(document.getElementById('radarModal')).show();
        }}
    </script>
</body>
</html>
"""

# ==========================================
# 8. MAIN CONTROLLER PIPELINE
# ==========================================
def main():
    now_utc = datetime.datetime.now(timezone.utc)
    print(f"🎬 Starting CFB (FBS) Static Site Generator ({now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')})...")

    # Load from Firestore
    print("🔥 Loading Teams and Venues from Firestore...")
    teams_dict = {doc.id: doc.to_dict() for doc in db.collection('cfb_teams').stream()}
    venues_dict = {doc.id: doc.to_dict() for doc in db.collection('cfb_venues').stream()}

    # Fetch Schedule & Geocode New Entrants
    week_label, games = get_current_cfb_schedule(venues_dict, teams_dict)
    print(f"🏈 Processed {len(games)} games for {week_label}.")

    # Build Dropdown Select2 Options from Firestore Master List
    sorted_teams = sorted(teams_dict.values(), key=lambda x: x["name"])
    select_options = "\n".join([f'<option value="/team_pages/{t["slug"]}/">{t["name"]}</option>' for t in sorted_teams])

    changed_urls = []

    # Format Main Page Games Grouped By Date
    if games:
        games_by_date = {}
        for g in games:
            dt = datetime.datetime.fromisoformat(g['game_time'].replace('Z', '+00:00')).astimezone(EST_TZ)
            date_str = dt.strftime("%A, %B %d, %Y").replace(" 0", " ")
            if date_str not in games_by_date: games_by_date[date_str] = []
            games_by_date[date_str].append(g)
            
        cards_content = ""
        for date_str, daily_games in games_by_date.items():
            cards_content += f'<div class="col-12 mt-4 mb-2"><h3 class="h5 fw-bold text-dark border-bottom pb-2">{date_str}</h3></div>'
            for g in daily_games:
                cards_content += render_game_card(g, is_single_team=False)
    else:
        cards_content = f'<div class="col-12 text-center py-5"><div class="alert alert-light border shadow-sm"><h5>No Games Scheduled for {week_label}</h5></div></div>'

    main_html = MAIN_SITE_TEMPLATE.format(week_label=week_label, select_options=select_options, cards_content=cards_content)
    if write_if_changed(MAIN_INDEX_FILE, main_html):
        changed_urls.append("https://weathercfb.com/")

    # Generate All Team Pages stored in Firestore
    for team_id, team in teams_dict.items():
        team_name = team["name"]
        team_slug = team["slug"]

        target_game = None
        for g in games:
            if g['home_id'] == team_id or g['away_id'] == team_id:
                target_game = g
                break

        if target_game:
            card_markup = render_game_card(target_game, is_single_team=True)
            page_title = f"{team_name} Weather Forecast | Live Game Radar"
        else:
            card_markup = render_bye_card(team_name)
            page_title = f"{team_name} Game Weather | CFB Weather"

        team_html = TEAM_PAGE_TEMPLATE.format(
            page_title=page_title,
            team_name=team_name,
            team_slug=team_slug,
            select_options=select_options,
            team_card_content=card_markup
        )

        team_dir = os.path.join(TEAM_PAGES_DIR, team_slug)
        os.makedirs(team_dir, exist_ok=True)
        
        output_filepath = os.path.join(team_dir, "index.html")
        if write_if_changed(output_filepath, team_html):
            changed_urls.append(f"https://weathercfb.com/team_pages/{team_slug}/")

    print(f"🚀 HTML parsing complete. {len(changed_urls)} pages required updates.")
    print("🎉 CFB generation pipeline complete!")

if __name__ == "__main__":
    main()
