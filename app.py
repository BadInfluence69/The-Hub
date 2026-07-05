#watch/tubi_100059630
import os
import sys
import urllib.parse
import re
import json
import sqlite3
import requests
import yt_dlp
import subprocess
import random
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, Response, stream_with_context, redirect, url_for, make_response, send_file
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# ====== CONFIGURATION ======
YOUTUBE_API_KEY = "AQ.Ab8RN6Io4N8iqqmin7fzLz4I82hTrhXzVhKLajFL0c0k7lLX8g" 
LISTENING_PORT = 5002
COMPUTER_IP = "192.168.0.142"

# ====== LOCAL MEDIA LIBRARY PATHS ======
LOCAL_MEDIA_BASE = r"E:\Media"
MEDIA_FOLDERS = {
    "movies": os.path.join(LOCAL_MEDIA_BASE, "Movies"),
    "tv_shows": os.path.join(LOCAL_MEDIA_BASE, "TV"),
    "porn": os.path.join(LOCAL_MEDIA_BASE, "Porn")
}

app.secret_key = os.urandom(24)

RECENT_SEARCH_HISTORY = []
MAX_HISTORY_KEYWORDS = 15
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "hub.db")

YTDLP_PATH = "yt-dlp"
_local_ytdlp = os.path.join(BASE_DIR, "yt-dlp.exe" if os.name == "nt" else "yt-dlp")
if not subprocess.run(["where" if os.name == "nt" else "which", "yt-dlp"],
                      capture_output=True).returncode == 0:
    if os.path.exists(_local_ytdlp):
        YTDLP_PATH = _local_ytdlp

FFMPEG_PATH = os.path.join(BASE_DIR, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
if not os.path.exists(FFMPEG_PATH):
    FFMPEG_PATH = "ffmpeg"

FFPROBE_PATH = os.path.join(BASE_DIR, "ffprobe.exe" if os.name == "nt" else "ffprobe")
if not os.path.exists(FFPROBE_PATH):
    FFPROBE_PATH = "ffprobe"

def search_tubi(query):
    url = f"https://api.tubitv.com/v3/search?q={query}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data and data.get("contents"):
                return data
            print("Tubi api.tubitv.com returned no contents, trying website fallback")
    except Exception as e:
        print(f"Tubi search error (api.tubitv.com): {e}")

    # Fallback: some networks block/can't resolve api.tubitv.com specifically
    # (DNS filtering, firewall rules on that subdomain, etc.) even though the
    # main tubitv.com domain is reachable fine - get_tubi_stream() proves that
    # by successfully hitting tubitv.com/movies/<id> via yt-dlp. So instead of
    # the JSON API, scrape tubitv.com's own search results page and pull the
    # results out of the page's embedded state JSON.
    try:
        return _search_tubi_via_website(query, headers)
    except Exception as e:
        print(f"Tubi search error (tubitv.com fallback): {e}")
        return None


def _search_tubi_via_website(query, headers):
    """
    Fallback search that hits tubitv.com's own search page directly instead
    of api.tubitv.com. Tubi's site is a Next.js app, so the page ships its
    data as JSON inside a <script id="__NEXT_DATA__"> tag rather than us
    having to parse rendered HTML. Returns data shaped like the api.tubitv.com
    response ({"contents": [...]}) so callers don't need to care which path
    was used.

    NOTE: this depends on the internal shape of Tubi's page-state JSON, which
    isn't a documented contract and can shift if Tubi changes their site. If
    this stops finding results, check the console output below - it prints
    the top-level keys it found so you can see what moved.
    """
    url = f"https://tubitv.com/search/{urllib.parse.quote(query)}"
    resp = requests.get(url, headers=headers, timeout=8)
    if resp.status_code != 200:
        print(f"Tubi website fallback: search page returned {resp.status_code}")
        return None

    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        resp.text,
        re.DOTALL
    )
    if not match:
        print("Tubi website fallback: couldn't find __NEXT_DATA__ block in page")

        # Diagnostics: dump the raw page and list every script tag's id/type
        # so we can see what Tubi's site is actually shipping instead of
        # guessing blind. Check hub_tubi_debug.html and the console output.
        try:
            debug_path = os.path.join(BASE_DIR, "hub_tubi_debug.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(resp.text)
            print(f"Tubi website fallback: saved raw page to {debug_path}")
        except Exception as write_err:
            print(f"Tubi website fallback: couldn't save debug HTML: {write_err}")

        script_tags = re.findall(r'<script([^>]*)>', resp.text)
        interesting = [s for s in script_tags if 'id=' in s or 'type="application/json"' in s]
        print(f"Tubi website fallback: found {len(script_tags)} <script> tags, "
              f"{len(interesting)} with an id or json type attribute:")
        for s in interesting[:20]:
            print(f"    <script{s}>")

        return None

    try:
        next_data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        print(f"Tubi website fallback: __NEXT_DATA__ wasn't valid JSON: {e}")
        return None

    page_props = next_data.get("props", {}).get("pageProps", {})

    # Tubi's site has moved this around before, so try a few known shapes
    # instead of assuming just one.
    candidates = (
        page_props.get("results")
        or page_props.get("searchResults")
        or page_props.get("contents")
        or page_props.get("initialState", {}).get("search", {}).get("results")
        or []
    )

    contents = [item for item in candidates if isinstance(item, dict) and item.get("id")]

    if not contents:
        print(f"Tubi website fallback: no results found under pageProps keys {list(page_props.keys())}")
        return None

    return {"contents": contents}

def get_tubi_stream(video_id):
    # Constructing the standard web URL using the ID to feed into yt-dlp
    tubi_url = f"https://tubitv.com/movies/{video_id}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'best',
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(tubi_url, download=False)
            return info.get('url')
        except Exception as e:
            print(f"Error extracting Tubi stream: {e}")
            return None


def search_tubi_videos(query):
    """
    Runs a Tubi search and normalizes the results into the same
    dict shape that search_youtube() returns, so both sources can be
    merged into a single results list and rendered by the same
    video card / watch page. Tubi ids are stored with a 'tubi_'
    prefix so downstream code (get_video, watch, like/dislike, etc.)
    can tell them apart from YouTube ids.
    """
    if not query:
        return []

    tubi_results = search_tubi(query)
    if not tubi_results or "contents" not in tubi_results:
        return []

    results = []
    for item in tubi_results["contents"]:
        raw_id = item.get("id")
        if not raw_id:
            continue

        prefixed_id = f"tubi_{raw_id}"
        title = item.get("title", "Unknown Tubi Title")
        thumb_list = item.get("images", {}).get("thumbnail", [])
        thumbnail_url = thumb_list[0] if thumb_list else ""
        description = item.get("description", "No description available.")

        upsert_video(prefixed_id, title, "Tubi TV", thumbnail_url, description)
        results.append({
            "id": prefixed_id,
            "title": title,
            "channel": "Tubi TV",
            "thumbnail": thumbnail_url,
            "description": description,
            "likes_count": 0,
            "dislikes_count": 0
        })

    return results


# ====== TUBI CATALOG (DIRECT ID ENTRY) ======
# Instead of scraping/searching Tubi's catalog, this now just takes a Tubi
# video ID (or a full URL copied from Tubi's own address bar) and sends the
# user straight to /watch/tubi_<id>, which already knows how to resolve and
# stream that ID via get_tubi_stream(). No search, no scraping - just the
# same thing that would happen if you typed the URL in yourself.
def extract_tubi_id(raw_input):
    """
    Accepts either:
      - a bare numeric Tubi ID, e.g. "100059630"
      - a full Tubi URL copied from the address bar, e.g.
        "https://tubitv.com/movies/100059630/some-title" or
        "https://tubitv.com/tv-shows/100059630/some-show"
    Returns the numeric ID string, or None if nothing usable was found.
    """
    if not raw_input:
        return None

    raw_input = raw_input.strip()

    # Bare ID already
    if raw_input.isdigit():
        return raw_input

    # Pull the ID out of a tubitv.com URL path (movies/<id>, tv-shows/<id>, series/<id>, etc.)
    match = re.search(r'tubitv\.com/(?:movies|tv-shows|series|videos)/(\d+)', raw_input)
    if match:
        return match.group(1)

    # Last resort: grab the first run of digits long enough to plausibly be a Tubi ID
    match = re.search(r'(\d{5,})', raw_input)
    if match:
        return match.group(1)

    return None


# ====== DATABASE SETUP ======
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            video_id    TEXT PRIMARY KEY,
            title       TEXT,
            channel     TEXT,
            thumbnail   TEXT,
            description TEXT,
            likes_count INTEGER DEFAULT 0,
            dislikes_count INTEGER DEFAULT 0
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id     TEXT,
            username     TEXT,
            comment_text TEXT,
            timestamp    TEXT
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            video_id TEXT NOT NULL,
            watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            title TEXT,
            suggestion TEXT,
            upvotes INTEGER DEFAULT 0,
            timestamp TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_id INTEGER,
            username TEXT,
            comment_text TEXT,
            timestamp TEXT,
            FOREIGN KEY(feedback_id) REFERENCES feedback(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback_votes (
            user_id INTEGER,
            feedback_id INTEGER,
            PRIMARY KEY (user_id, feedback_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_name TEXT UNIQUE NOT NULL
        )
    """)
    
    cur.execute("PRAGMA table_info(videos)")
    columns = [column[1] for column in cur.fetchall()]
    if "dislikes_count" not in columns:
        cur.execute("ALTER TABLE videos ADD COLUMN dislikes_count INTEGER DEFAULT 0")
    
    cur.execute("SELECT COUNT(*) FROM subscriptions")
    if cur.fetchone()[0] == 0:
        channels_list = [
            "BADINFLUNCEYT","Selena Gomez","King of the Hill", "SomeOrdinaryGamers", "Sam and Colby", "Clownfish TV", "JRE Clips", "Polecat324",
            "Lon.TV", "The Stevie Richards Show", "The Mirandalorian", "TechLinked", "Kid Rock",
            "Disney Kids", "Bay Area Buggs", "Evanescence", "Theo Von Clips", "Chibi Reviews",
            "CrapgamerReviews", "MAWK3", "Sinnon Nightcore", "Fox News", "ReviewTechUSA2",
            "John Wolfe", "MODDED WARFARE", "ღ NightcoreGalaxy ღ", "Olive Badger", "amc+",
            "GrumpsGarage", "WWE", "More Perfect Union", "Lionsgate Movies", "TmarTn2",
            "Ian Bagg", "Alexandra Kay", "JayzTwoCents", "Rotten Tomatoes TV", "LiveNOW from FOX",
            "HISTORY", "GQ", "Paramount Plus", "Khalid", "Razzlekhan", "The WAN Show",
            "The Ramsey Show Highlights", "WWE on USA", "Janie Ippolito", "Brian Christopher Slots",
            "Blind to Billionaire", "Zee Cinema", "Elfawwaaz Channel", "Joey Diaz", "Aria",
            "Freedom News Now", "ZackScottGames", "GameStop", "Stephen Gardner", "Danny Jones Clips",
            "Vara Dark - Dark Titan Media", "HBO Max", "Peacock", "Specialty Motor Cars", "SyrebralVibes",
            "Rachel Platten", "Linus Tech Tips", "STARZ", "boogie2988", "Disney Channel Animation",
            "Disney Channel", "FX Networks", "Deep Humor", "Jonesy", "Ed Sheeran", "Jeff Favignano",
            "Bass Boosted", "Unreal Engine", "ScreenRant", "EricTheCarGuy", "LockPickingLawyer",
            "Todd Wyatt", "Hollow", "ALF", "NR B17", "Chevy Dude", "Ford Boss Me - Auto / Politics / Family",
            "Flix For Free", "Warner Bros. Classics", "MGM+", "YNW Melly", "718Kipkila", "Detective Kinjaz",
            "Antenna Man", "RecessOwner", "Music Parody Fun", "StreamFab Editors", "TM", "TVnr", "LennyBarn",
            "Chris Dean Starkey", "SpeakerKnockerzOfficial", "YouTube TV", "Music"
        ]
        for channel in channels_list:
            cur.execute("INSERT OR IGNORE INTO subscriptions (channel_name) VALUES (?)", (channel,))
    
    con.commit()
    con.close()



def upsert_video(video_id, title, channel, thumbnail, description):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO videos (video_id, title, channel, thumbnail, description, likes_count, dislikes_count)
        VALUES (?, ?, ?, ?, ?, 0, 0)
        ON CONFLICT(video_id) DO UPDATE SET
            title=excluded.title,
            channel=excluded.channel,
            thumbnail=excluded.thumbnail,
            description=excluded.description
    """, (video_id, title, channel, thumbnail, description))
    con.commit()
    con.close()


def get_video(video_id):
    if video_id.startswith("local_"):
        local_items = scan_local_media_library()
        item = next((v for v in local_items if v["video_id"] == video_id), None)
        if item:
            return {
                "video_id": item["video_id"],
                "title": item["title"],
                "channel": item["channel"],
                "thumbnail": item["thumbnail"],
                "description": item["description"],
                "likes_count": 0,
                "dislikes_count": 0
            }
        return None

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,))
    row = cur.fetchone()
    con.close()
    return dict(row) if row else None


def increment_likes(video_id):
    if video_id.startswith("local_"): return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE videos SET likes_count = likes_count + 1 WHERE video_id = ?", (video_id,))
    con.commit()
    con.close()


def increment_dislikes(video_id):
    if video_id.startswith("local_"): return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE videos SET dislikes_count = dislikes_count + 1 WHERE video_id = ?", (video_id,))
    con.commit()
    con.close()


def add_comment(video_id, username, comment_text):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur.execute(
        "INSERT INTO comments (video_id, username, comment_text, timestamp) VALUES (?, ?, ?, ?)",
        (video_id, username or "Anonymous", comment_text, ts)
    )
    con.commit()
    con.close()


def get_comments(video_id):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        "SELECT * FROM comments WHERE video_id = ? ORDER BY id DESC",
        (video_id,)
    )
    rows = cur.fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_current_user():
    username = request.cookies.get('local_user_session')
    if not username:
        return None
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT id, username FROM users WHERE username = ?", (username,))
    user = cur.fetchone()
    con.close()
    if user:
        return {"id": user[0], "username": user[1]}
    return None


# ====== LOCAL MEDIA SCANNING UTILITY ======
def scan_local_media_library():
    local_videos = []
    VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.ts')
    
    meta_map = {
        "movies": {"label": "Local Movies", "thumb": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?w=500"},
        "tv_shows": {"label": "Local TV Shows", "thumb": "https://images.unsplash.com/photo-1593789198777-f29bc259780e?w=500"},
        "porn": {"label": "Local Porn", "thumb": "https://images.unsplash.com/photo-1593789198777-f29bc259780e?w=500"}
    }
    
    for category_key, target_directory in MEDIA_FOLDERS.items():
        if not os.path.exists(target_directory):
            continue

        for file_name in os.listdir(target_directory):
            full_path = os.path.join(target_directory, file_name)
            if os.path.isfile(full_path) and file_name.lower().endswith(VIDEO_EXTENSIONS):
                clean_title = os.path.splitext(file_name)[0]
                safe_id_token = re.sub(r'[^a-zA-Z0-9]', '_', clean_title)
                unique_video_id = f"local_{category_key}_{safe_id_token}"
                local_videos.append({
                    "video_id": unique_video_id,
                    "id": unique_video_id,
                    "title": clean_title,
                    "channel": meta_map[category_key]["label"],
                    "thumbnail": meta_map[category_key]["thumb"],
                    "description": f"Local asset file found on storage drive at: {file_name}",
                    "file_path": full_path,
                    "category": category_key,
                    "likes_count": 0,
                    "dislikes_count": 0
                })

        for entry in os.listdir(target_directory):
            subfolder_path = os.path.join(target_directory, entry)
            if os.path.isdir(subfolder_path):
                for file_name in os.listdir(subfolder_path):
                    full_path = os.path.join(subfolder_path, file_name)
                    if os.path.isfile(full_path) and file_name.lower().endswith(VIDEO_EXTENSIONS):
                        clean_title = os.path.splitext(file_name)[0]
                        safe_id_token = re.sub(r'[^a-zA-Z0-9]', '_', clean_title)
                        unique_video_id = f"local_{category_key}_{safe_id_token}"
                        local_videos.append({
                            "video_id": unique_video_id,
                            "id": unique_video_id,
                            "title": clean_title,
                            "channel": meta_map[category_key]["label"],
                            "thumbnail": meta_map[category_key]["thumb"],
                            "description": f"Local asset file found on storage drive at: {entry}\\{file_name}",
                            "file_path": full_path,
                            "category": category_key,
                            "likes_count": 0,
                            "dislikes_count": 0
                        })

    return local_videos


# ====== YOUTUBE SEARCH ENGINE ======
def search_youtube(query):
    if not query:
        return []

    if YOUTUBE_API_KEY and YOUTUBE_API_KEY != "AQ.Ab8RN6Io4N8iqqmin7fzLz4I82hTrhXzVhKLajFL0c0k7lLX8g":
        try:
            url = f"https://www.googleapis.com/youtube/v3/search"
            params = {
                "part": "snippet",
                "q": query,
                "maxResults": 25,
                "type": "video",
                "key": YOUTUBE_API_KEY
            }
            response = requests.get(url, params=params, timeout=8)
            if response.status_code == 200:
                data = response.json()
                api_results = []
                for item in data.get("items", []):
                    video_id = item.get("id", {}).get("videoId")
                    if not video_id:
                        continue
                    snippet = item.get("snippet", {})
                    title = snippet.get("title", "Unknown Title")
                    channel = snippet.get("channelTitle", "Unknown Channel")
                    description = snippet.get("description", "No description available.")
                    thumbnails = snippet.get("thumbnails", {})
                    thumbnail_url = (
                        thumbnails.get("high", {}).get("url") or
                        thumbnails.get("medium", {}).get("url") or
                        thumbnails.get("default", {}).get("url") or
                        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
                    )
                    upsert_video(video_id, title, channel, thumbnail_url, description)
                    api_results.append({
                        "id": video_id,
                        "title": title,
                        "channel": channel,
                        "thumbnail": thumbnail_url,
                        "description": description,
                        "likes_count": 0,
                        "dislikes_count": 0
                    })
                return api_results
            else:
                print(f"API Error ({response.status_code}): {response.text}", file=sys.stderr)
        except Exception as e:
            print(f"API Fetch Error: {e}", file=sys.stderr)

    encoded_query = urllib.parse.quote(query)
    url = f"https://www.youtube.com/results?search_query={encoded_query}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    }
    raw_results = []
    try:
        response = requests.get(url, headers=headers, timeout=8)
        if response.status_code == 200:
            match = re.search(r"var ytInitialData\s*=\s*({.*?});", response.text)
            if match:
                data = json.loads(match.group(1))
                try:
                    contents = (
                        data["contents"]["twoColumnSearchResultsRenderer"]
                        ["primaryContents"]["sectionListRenderer"]["contents"]
                    )
                    for content in contents:
                        if "itemSectionRenderer" in content:
                            items = content["itemSectionRenderer"]["contents"]
                            for item in items:
                                if "videoRenderer" in item:
                                    video = item["videoRenderer"]
                                    video_id = video.get("videoId")
                                    if not video_id:
                                        continue
                                    title = video.get("title", {}).get("runs", [{}])[0].get("text", "Unknown Title")
                                    channel = video.get("ownerText", {}).get("runs", [{}])[0].get("text", "Unknown Channel")
                                    thumbnails_list = video.get("thumbnail", {}).get("thumbnails", [])
                                    thumbnail_url = (
                                        thumbnails_list[-1].get("url")
                                        if thumbnails_list
                                        else f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
                                    )
                                    desc_snippets = (
                                        video.get("detailedMetadataSnippets", [{}])[0]
                                        .get("snippetText", {}).get("runs", [])
                                    )
                                    description = (
                                        "".join([s.get("text", "") for s in desc_snippets])
                                        or "No description available."
                                    )
                                    upsert_video(video_id, title, channel, thumbnail_url, description)
                                    raw_results.append({
                                        "id": video_id,
                                        "title": title,
                                        "channel": channel,
                                        "thumbnail": thumbnail_url,
                                        "description": description,
                                        "likes_count": 0,
                                        "dislikes_count": 0
                                    })
                except KeyError:
                    pass
    except Exception as e:
        print(f"Scrape error: {e}", file=sys.stderr)

    return raw_results



def get_home_recommendations(user_id=None):
    fallback_queries = ["fivem server gameplay", "gta 5 rp", "lofi hip hop radio", "gaming highlights", "tech trends"]
    chosen_queries = []

    if user_id:
        try:
            con = sqlite3.connect(DB_PATH)
            cur = con.cursor()
            cur.execute("""
                SELECT v.title FROM watch_history h 
                JOIN videos v ON h.video_id = v.video_id 
                WHERE h.user_id = ? 
                ORDER BY h.watched_at DESC LIMIT 20
            """, (user_id,))
            history_rows = cur.fetchall()
            con.close()

            if history_rows:
                seed_words = []
                for row in history_rows:
                    title = row[0] or ""
                    words = [w.lower() for w in re.findall(r'\b[a-zA-Z]{4,10}\b', title) 
                             if w.lower() not in ["with", "from", "that", "this", "video", "server"]]
                    seed_words.extend(words)

                if seed_words:
                    sampled_seeds = random.sample(list(set(seed_words)), min(3, len(set(seed_words))))
                    chosen_queries.append(" ".join(sampled_seeds))
        except Exception as e:
            print(f"History context retrieval engine failure: {e}", file=sys.stderr)

    chosen_queries.append(random.choice(fallback_queries))
    final_query_string = random.choice(chosen_queries)
    
    return search_youtube(final_query_string)[:100]



# ====== YT-DLP CORE STREAM HELPERS ======
PLAYER_CLIENT_FALLBACKS = ["android", "ios", "web", "roku"]


def _node_available():
    try:
        subprocess.run(["node", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        return True
    except Exception:
        return False



def _build_ytdlp_cmd(player_client, target_url):
    cookie_file_path = os.path.join(BASE_DIR, "cookies.txt")
    cmd = [
        YTDLP_PATH,
        "--ffmpeg-location", FFMPEG_PATH,
        "--extractor-args", f"youtube:player_client={player_client}",
        "--sponsorblock-remove", "sponsor,selfpromo,interaction",
        "--no-check-certificates",
        # Single muxed (video+audio) format - this app proxies one raw URL
        # straight into <video>; it does not merge separate streams with ffmpeg.
        "-f", "best[ext=mp4]/best",
        "--get-url",
        target_url,
    ]
    if os.path.exists(cookie_file_path):
        cmd.extend(["--cookies", cookie_file_path])
    return cmd


def resolve_stream_urls(video_id):
    target = f"https://www.youtube.com/watch?v={video_id}"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    last_error = "Unknown extraction failure"
    for client in PLAYER_CLIENT_FALLBACKS:
        cmd = _build_ytdlp_cmd(client, target)
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
        lines = [l for l in result.stdout.strip().split("\n") if l.startswith("http")] if result.stdout else []

        if result.returncode == 0 and lines:
            return {"video_url": lines[0], "audio_url": lines[1] if len(lines) > 1 else None}

        last_error = (result.stderr or "").strip() or f"yt-dlp ({client} client) returned no usable output"

    raise RuntimeError(last_error)




# ====== STYLING LAYER ======
SHARED_CSS = """
<style>
    body {
        background-color: #121212;
        color: #e0e0e0;
        font-family: Arial, sans-serif;
        margin: 0;
        padding: 0;
    }
    .navbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 24px;
        background: #0d0d0d;
        border-bottom: 1px solid #1f1f1f;
    }
    .hub-logo {
        font-size: 28px;
        letter-spacing: 3px;
        text-decoration: none;
        color: #ff4e4e;
        font-weight: bold;
    }
    .beta-badge {
        background: #b71c1c;
        padding: 4px 12px;
        font-size: 11px;
        font-weight: bold;
        border-radius: 4px;
        color: #fff;
        letter-spacing: 1px;
    }
    .nav-link {
        color: #1e88e5;
        text-decoration: none;
        font-weight: bold;
        font-size: 14px;
        border: 1px solid #1e88e5;
        padding: 6px 14px;
        border-radius: 4px;
    }
    .container {
        max-width: 1200px;
        margin: 0 auto;
        padding: 20px;
    }
    .search-box {
        display: flex;
        gap: 10px;
        margin: 20px auto 40px auto;
        max-width: 750px;
    }
    input[type="text"], textarea {
        flex: 1;
        padding: 14px;
        border: 1px solid #333;
        background: #1e1e1e;
        color: #fff;
        border-radius: 4px;
        font-size: 14px;
        font-family: Arial, sans-serif;
    }
    textarea {
        width: 100%;
        box-sizing: border-box;
        resize: vertical;
    }
    button, .btn {
        padding: 12px 24px;
        background: #1e88e5;
        color: #fff;
        border: none;
        border-radius: 4px;
        cursor: pointer;
        font-size: 14px;
        font-weight: bold;
        text-decoration: none;
        display: inline-block;
    }
    .grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: 25px;
    }
    .split-layout {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 40px;
        margin-top: 20px;
    }
    @media (max-width: 768px) {
        .split-layout { grid-template-columns: 1fr; }
    }
    .card {
        background: #1e1e1e;
        border-radius: 6px;
        overflow: hidden;
        border: 1px solid #2d2d2d;
        text-decoration: none;
        color: inherit;
        display: block;
    }
    .card img {
        width: 100%;
        height: 160px;
        object-fit: cover;
    }
    .card-content {
        padding: 15px;
    }
    .card-title {
        font-weight: bold;
        font-size: 15px;
        margin-bottom: 8px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .back-btn {
        display: inline-block;
        padding: 10px 20px;
        background: #2a2a2a;
        color: white;
        text-decoration: none;
        border-radius: 4px;
        font-weight: bold;
        margin-bottom: 20px;
    }
    .warning-banner {
        background: #3e2723;
        border-left: 5px solid #d84315;
        padding: 15px;
        border-radius: 4px;
        margin: 15px 0;
        font-size: 14px;
        line-height: 1.5;
        color: #ffccbc;
    }
    .meta-panel {
        background: #1e1e1e;
        padding: 20px;
        border-radius: 6px;
        border: 1px solid #2d2d2d;
        margin-top: 15px;
    }
    .comment-card {
        background: #1a1a1a;
        padding: 15px;
        border-radius: 4px;
        margin-bottom: 10px;
        border-left: 4px solid #1e88e5;
    }
    .manifesto-box {
        background: #1e1e1e;
        padding: 30px;
        border-radius: 8px;
        border: 1px solid #2d2d2d;
        line-height: 1.6;
    }
    h2 { color: #ff4e4e; border-bottom: 1px solid #2d2d2d; padding-bottom: 8px; }
    h3 { color: #1e88e5; }
    
    .feedback-card { background: #1e1e1e; padding: 20px; border-radius: 6px; border: 1px solid #2d2d2d; margin-bottom: 20px; }
    .feedback-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2d2d2d; padding-bottom: 10px; margin-bottom: 15px; }
    .vote-badge { background: #333; padding: 6px 12px; border-radius: 20px; font-size: 13px; font-weight: bold; color: #ffb300; border: 1px solid #444; text-decoration: none; }
    .vote-badge:hover { background: #444; border-color: #ffb300; }
    .nested-box { margin-left: 30px; margin-top: 15px; padding-left: 15px; border-left: 2px dashed #444; }
</style>
"""

NAVBAR_HTML = """
<nav class="navbar">
    <a href="/" class="hub-logo">THE HUB</a>
    <div style="display: flex; gap: 15px; align-items: center;">
        <span class="beta-badge">BETA DEVELOPMENT PORTAL</span>
        <a href="/media-library" class="nav-link" style="border-color:#e11d48; color:#e11d48;">LOCAL MEDIA</a>
        <a href="/subscriptions" class="nav-link" style="border-color:#4caf50; color:#4caf50;">MY SUBSCRIPTIONS</a>
        <a href="/tubi-catalog" class="nav-link" style="border-color:#8e44ad; color:#8e44ad;">TUBI CATALOG</a>
        <a href="/feedback" class="nav-link" style="border-color:#ffb300; color:#ffb300;">COMMUNITY FEEDBACK</a>
        <a href="/about" class="nav-link">ABOUT PROJECT</a>
        <a href="/logout" class="nav-link" style="border-color:#ff4e4e; color:#ff4e4e;">SIGN OUT</a>
    </div>
</nav>
"""

GATEKEEPER_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>The Hub Platform Sign-In</title>
    <style>
        body { font-family: 'Segoe UI', Arial, sans-serif; background-color: #0c0c0c; color: #f1f1f1; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
        .auth-card { max-width: 400px; width: 90%; background: #161616; padding: 40px; border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,0.7); border: 1px solid #282828; }
        .logo { font-size: 32px; letter-spacing: 4px; color: #ff4e4e; font-weight: bold; text-align: center; margin-bottom: 5px; }
        .desc { font-size: 13px; color: #888; text-align: center; margin-bottom: 30px; }
        label { font-size: 11px; color: #aaa; text-transform: uppercase; font-weight: bold; letter-spacing: 0.5px; display: block; margin-bottom: 5px; }
        input[type="text"], input[type="password"] { width: 100%; padding: 12px; margin-bottom: 20px; background: #222; border: 1px solid #333; color: #fff; border-radius: 4px; box-sizing: border-box; font-size: 15px; }
        input:focus { border-color: #ff4e4e; outline: none; }
        button { width: 100%; padding: 12px; background: #ff4e4e; border: none; color: white; font-weight: bold; font-size: 15px; border-radius: 4px; cursor: pointer; transition: background 0.2s; }
        button:hover { background: #e03c3c; }
        .switch-mode-btn { background: none; border: none; color: #1e88e5; text-decoration: underline; width: 100%; text-align: center; margin-top: 15px; font-size: 13px; cursor: pointer; }
    </style>
</head>
<body>
<div class="auth-card">
    <div class="logo">THE HUB</div>
    <div class="desc" id="form-desc">Connect to your decoupled proxy tracking index environment.</div>
    
    <form id="auth-form">
        <label>Username</label>
        <input type="text" id="username" autocomplete="off" required>
        
        <label>Password</label>
        <input type="password" id="password" required>
        
        <button type="submit" id="submit-btn">Authorize Access</button>
    </form>
    <button class="switch-mode-btn" id="toggle-btn" onclick="toggleMode()">Create a Local Sandbox Profile</button>
</div>

<script>
    let isLoginMode = true;

    function toggleMode() {
        isLoginMode = !isLoginMode;
        document.getElementById('submit-btn').innerText = isLoginMode ? 'Authorize Access' : 'Compile Profile';
        document.getElementById('toggle-btn').innerText = isLoginMode ? 'Create a Local Sandbox Profile' : 'Return to Account Authorization';
        document.getElementById('form-desc').innerText = isLoginMode ? 'Connect to your decoupled proxy tracking index environment.' : 'Setup a local system-layer account mapping directly inside hub.db.';
    }

    document.getElementById('auth-form').addEventListener('submit', function(e) {
        e.preventDefault();
        const targetUrl = isLoginMode ? '/login' : '/register';
        const params = new URLSearchParams({
            username: document.getElementById('username').value,
            password: document.getElementById('password').value
        });

        fetch(targetUrl, { method: 'POST', body: params })
        .then(async res => {
            const data = await res.json();
            if (res.ok) {
                if(!isLoginMode) {
                    alert(data.message);
                    toggleMode();
                } else {
                    window.location.reload();
                }
            } else {
                alert(data.error || "Authentication structural fault encountered.");
            }
        });
    });
</script>
</body>
</html>
"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>The Hub Platform Core</title>
    """ + SHARED_CSS + """
</head>
<body>
""" + NAVBAR_HTML + """
<div class="container">
    <form action="/search" method="GET" class="search-box">
        <input type="text" name="q" value="{{ original_user_prompt }}" placeholder="Search keywords...">
        <button type="submit">Query Hub Engine</button>
    </form>

    <h2>{% if subscription_view %}Custom Subscriptions Activity Stream{% elif append_mode %}Additional Appended Asset Feeds{% else %}Main Cached Portal Indexes{% endif %}</h2>

    <div class="grid">
        {% for video in videos %}
        <a href="/watch/{{ video.id }}" class="card">
            <img src="{{ video.thumbnail }}" alt="thumbnail">
            <div class="card-content">
                <div class="card-title">{{ video.title }}</div>
                <div style="font-size: 13px; color: #aaa;">{{ video.channel }}</div>
                <div style="font-size: 11px; color: #ffb300; margin-top: 5px;">Upfolk: {{ video.likes_count }} | Downfolk: {{ video.dislikes_count if video.dislikes_count else 0 }}</div>
            </div>
        </a>
        {% endfor %}
    </div>

    <div style="margin: 40px 0; text-align: center;">
        <a href="{% if subscription_view %}/subscriptions{% else %}/load-more?q={{ original_user_prompt }}{% endif %}" class="btn" style="background: #2e7d32; padding: 16px 40px; font-size: 16px; border-radius: 30px; border: 1px solid #388e3c;">
            {% if subscription_view %}Shuffle & Refresh Subs Room{% else %}Load More{% endif %}
        </a>
    </div>
</div>
</body>
</html>
"""

MEDIA_LIBRARY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Local Media Library - The Hub</title>
    """ + SHARED_CSS + """
</head>
<body>
""" + NAVBAR_HTML + """
<div class="container">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
        <h1 style="margin: 0; color: #ff4e4e; letter-spacing: 1px;">Storage Media Index</h1>
        <span style="font-size: 13px; color: #aaa; background: #222; padding: 6px 14px; border-radius: 20px; border: 1px solid #333;">
            Total Indexed: {{ movies|length + tv_shows|length }} items
        </span>
    </div>

    <div class="split-layout">
        <div>
            <h2>Movies Collection ({{ movies|length }})</h2>
            <div style="display: flex; flex-direction: column; gap: 15px; margin-top: 15px;">
                {% for item in movies %}
                <a href="/watch/{{ item.id }}" class="card" style="display: flex; height: 100px;">
                    <img src="{{ item.thumbnail }}" alt="thumb" style="width: 140px; height: 100%; object-fit: cover;">
                    <div class="card-content" style="flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center;">
                        <div class="card-title" style="margin-bottom: 4px;">{{ item.title }}</div>
                        <div style="font-size: 12px; color: #888;">{{ item.channel }}</div>
                    </div>
                </a>
                {% else %}
                <p style="color: #555; font-style: italic;">No movies found inside the path.</p>
                {% endfor %}
            </div>
        </div>

        <div>
            <h2>TV Shows Collection ({{ tv_shows|length }})</h2>
            <div style="display: flex; flex-direction: column; gap: 15px; margin-top: 15px;">
                {% for item in tv_shows %}
                <a href="/watch/{{ item.id }}" class="card" style="display: flex; height: 100px;">
                    <img src="{{ item.thumbnail }}" alt="thumb" style="width: 140px; height: 100%; object-fit: cover;">
                    <div class="card-content" style="flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center;">
                        <div class="card-title" style="margin-bottom: 4px;">{{ item.title }}</div>
                        <div style="font-size: 12px; color: #888;">{{ item.channel }}</div>
                    </div>
                </a>
                {% else %}
                <p style="color: #555; font-style: italic;">No television folders found inside the path.</p>
                {% endfor %}
            </div>
        </div>
    </div>
</div>
</body>
</html>
"""

WATCH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ video.title }} - The Hub</title>
    """ + SHARED_CSS + """
<style>
    video {
        width: 100%;
        aspect-ratio: 16/9;
        background: #000;
        border-radius: 6px;
        border: 1px solid #2d2d2d;
    }
    .action-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 10px;
    }
    .voting-container {
        display: flex;
        gap: 8px;
        align-items: center;
    }
    .like-btn {
        background: #2e7d32;
        color: white;
        font-weight: bold;
        border-radius: 20px;
        padding: 10px 22px;
        border: none;
        cursor: pointer;
        font-size: 14px;
        text-decoration: none;
        display: inline-block;
    }
    .dislike-btn {
        background: #c62828;
        color: white;
        font-weight: bold;
        border-radius: 20px;
        padding: 10px 22px;
        border: none;
        cursor: pointer;
        font-size: 14px;
        text-decoration: none;
        display: inline-block;
    }
    .comment-section {
        margin-top: 40px;
        border-top: 1px solid #333;
        padding-top: 20px;
    }
    .comment-form input, .comment-form textarea {
        width: 100%;
        padding: 12px;
        background: #121212;
        border: 1px solid #444;
        color: white;
        border-radius: 4px;
        margin-bottom: 10px;
        box-sizing: border-box;
        font-family: Arial, sans-serif;
    }
    .submit-btn {
        background: #1e88e5;
    }
    .drm-notice {
        padding: 50px;
        text-align: center;
        background: #1e1e1e;
        border-radius: 6px;
        color: #aaa;
    }
</style>
</head>
<body>
""" + NAVBAR_HTML + """
<div class="container" style="max-width: 900px;">
    <a href="javascript:history.back()" class="back-btn">&#10229; Return to Previous Screen</a>

    {% if stream_link %}
    <video id="hub-player" controls autoplay></video>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script>
        (function() {
            const video = document.getElementById('hub-player');
            const streamSource = {{ stream_link | tojson }};
            const isHls = streamSource.indexOf('.m3u8') !== -1;

            if (isHls && window.Hls && Hls.isSupported()) {
                const hls = new Hls();
                hls.loadSource(streamSource);
                hls.attachMedia(video);
            } else {
                // Direct-playable (mp4, or Safari's native HLS support)
                video.src = streamSource;
            }
        })();
    </script>
    {% else %}
    <div class="drm-notice">
        Video stream could not be loaded directly by the browser proxy player context.
    </div>
    {% endif %}

    <div class="warning-banner">
        <strong>NATIVE SYSTEM NOTICE:</strong> Comments, Upfolks, and Downfolks are transmitted in the background - the player keeps running and the page will not reload.
    </div>

    <div class="meta-panel">
        <div class="action-row">
            <div>
                <h2 style="margin: 0 0 5px 0; color: #e0e0e0;">{{ video.title }}</h2>
                <span style="color: #aaa;">Source Node: {{ video.channel }}</span>
            </div>
            <div class="voting-container">
                <form action="/like/{{ video_id }}" method="POST" style="margin: 0;" class="vote-form" data-endpoint="/like/{{ video_id }}">
                    <button type="submit" class="like-btn">
                        Upfolk (<span id="likes-count">{{ video.likes_count }}</span>)
                    </button>
                </form>
                <form action="/dislike/{{ video_id }}" method="POST" style="margin: 0;" class="vote-form" data-endpoint="/dislike/{{ video_id }}">
                    <button type="submit" class="dislike-btn">
                        Downfolk (<span id="dislikes-count">{{ video.dislikes_count if video.dislikes_count else 0 }}</span>)
                    </button>
                </form>
            </div>
        </div>
        <p style="margin-top: 15px; color: #bbb; font-size: 14px; line-height: 1.6;">{{ video.description }}</p>
    </div>

    <div class="comment-section">
        <h3 id="comments-focus">Local Portal Comments (Bypasses YouTube Infrastructure)</h3>
        <form id="comment-form" action="/comment/{{ video_id }}" method="POST" class="comment-form">
            <textarea id="comment-text-input" name="comment_text" rows="3" placeholder="Leave localized feedback as {{ current_username }}..." required></textarea>
            <button type="submit" class="btn submit-btn">Transmit Local Message</button>
        </form>

        <div id="comments-list" style="margin-top: 25px;">
            {% for comment in comments %}
            <div class="comment-card">
                <strong style="color: #1e88e5;">{{ comment.username }}</strong>
                <span style="font-size: 11px; color: #666; margin-left: 10px;">{{ comment.timestamp }}</span>
                <p style="margin: 8px 0 0 0; color: #ccc;">{{ comment.comment_text }}</p>
            </div>
            {% else %}
            <p id="no-comments-msg" style="color: #666; font-style: italic;">No comments saved to this channel yet.</p>
            {% endfor %}
        </div>
    </div>
</div>
<script>
(function() {
    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // Comment form - submit via fetch, prepend the new comment, no reload.
    const commentForm = document.getElementById('comment-form');
    if (commentForm) {
        commentForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const textarea = document.getElementById('comment-text-input');
            const text = textarea.value.trim();
            if (!text) return;

            fetch(commentForm.action, {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                body: new URLSearchParams({ comment_text: text })
            })
            .then(res => res.json())
            .then(data => {
                if (data.error) { alert(data.error); return; }

                const noCommentsMsg = document.getElementById('no-comments-msg');
                if (noCommentsMsg) noCommentsMsg.remove();

                const list = document.getElementById('comments-list');
                const card = document.createElement('div');
                card.className = 'comment-card';
                card.innerHTML =
                    '<strong style="color: #1e88e5;">' + escapeHtml(data.username) + '</strong>' +
                    '<span style="font-size: 11px; color: #666; margin-left: 10px;">' + escapeHtml(data.timestamp) + '</span>' +
                    '<p style="margin: 8px 0 0 0; color: #ccc;">' + escapeHtml(data.comment_text) + '</p>';
                list.insertBefore(card, list.firstChild);

                textarea.value = '';
            })
            .catch(err => console.error('Comment submit failed:', err));
        });
    }

    // Upfolk / Downfolk - submit via fetch, update the count in place, no reload.
    document.querySelectorAll('.vote-form').forEach(function(form) {
        form.addEventListener('submit', function(e) {
            e.preventDefault();
            fetch(form.dataset.endpoint, {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            })
            .then(res => res.json())
            .then(data => {
                if (data.error) return;
                const likesEl = document.getElementById('likes-count');
                const dislikesEl = document.getElementById('dislikes-count');
                if (likesEl) likesEl.textContent = data.likes_count;
                if (dislikesEl) dislikesEl.textContent = data.dislikes_count;
            })
            .catch(err => console.error('Vote submit failed:', err));
        });
    });
})();
</script>
</body>
</html>
"""

ABOUT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>About The Hub Project</title>
    """ + SHARED_CSS + """
</head>
<body>
""" + NAVBAR_HTML + """
<div class="container" style="max-width: 800px;">
    <a href="/" class="back-btn">&#10229; Return to Dashboard</a>
    <div class="manifesto-box">
        <h2>The Hub Platform Blueprint (Alpha/Beta Stage)</h2>
        <p>
            Welcome to <strong>The Hub</strong>. This application is an active architectural concept built to demonstrate that a high-speed video environment can run completely decoupled from corporate tracking dependencies and broken web layers.
        </p>
        <p>
            Currently, the script acts as an isolated privacy proxy layer, scraping standard public video endpoints and serving raw video packages directly to your terminal without tracking scripts, tracking cookies, or intrusive mid-roll ads.
        </p>
        <h3>Strategic Development Phases:</h3>
        <ul>
            <li>
                <strong>Phase 1 (Current):</strong> Bootstrapping off public catalogs. Video index frames are stored dynamically in a localized SQLite archive database to build independent local structures.
            </li>
            <li>
                <strong>Phase 2 (Independent Social Systems):</strong> Restoring historical infrastructure options removed by corporate tech, such as localized discussion chains, direct text channels, and independent upvotes completely separated from YouTube tracking clusters.
            </li>
            <li>
                <strong>Phase 3 (Decentralized Server Migration):</strong> Relocating to dedicated cloud hardware. To support direct standalone user uploads, the system will feature micro-funding loops with low-cost monthly premium tiers (around $10 to $15) solely to cover raw network hosting fees without selling user telemetry.
            </li>
        </ul>
        <p style="background: #1565c0; padding: 12px; border-radius: 4px; color: white; font-weight: bold; text-align: center; margin-top: 30px;">
            Reclaiming utility. Zero Client Scripts. Built for performance.
        </p>
    </div>
</div>
</body>
</html>
"""

COMMUNITY_FEEDBACK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Community Development Feedback - The Hub</title>
    """ + SHARED_CSS + """
</head>
<body>
""" + NAVBAR_HTML + """
<div class="container" style="max-width: 950px;">
    <h2>Public Platform Roadmap & User Feedback</h2>
    <p style="color: #aaa; margin-bottom: 30px;">Unlike other corporate apps, your suggestions are 100% public. Post feature ideas, vote on enhancements, and collaborate on building a better user experience below.</p>

    <div class="meta-panel" style="margin-bottom: 40px; border-color: #ffb300;">
        <h3 style="color: #ffb300; margin-top: 0;">Submit an Improvement Suggestion</h3>
        <form action="/feedback/submit" method="POST" style="display: flex; flex-direction: column; gap: 12px;">
            <input type="text" name="title" placeholder="Summary Title (e.g., 'Add a Darker Mode Variation')" required>
            <textarea name="suggestion" rows="4" placeholder="Detail your feature concepts, improvements, or layout recommendations..." required></textarea>
            <button type="submit" style="background: #ffb300; color: #111;">Broadcast Idea to Community</button>
        </form>
    </div>

    <h3>Community Discussion Streams</h3>
    {% for post in feedback_posts %}
    <div class="feedback-card">
        <div class="feedback-header">
            <div>
                <h3 style="margin: 0; color: #fff;">{{ post.title }}</h3>
                <span style="font-size: 12px; color: #888;">Proposed by <strong>{{ post.username }}</strong> on {{ post.timestamp }}</span>
            </div>
            <form action="/feedback/vote/{{ post.id }}" method="POST" style="margin: 0;">
                <button type="submit" class="vote-badge">▲ Upvote Concept ({{ post.upvotes }})</button>
            </form>
        </div>
        <p style="color: #ddd; line-height: 1.5; font-size: 15px; white-space: pre-wrap;">{{ post.suggestion }}</p>

        <div class="nested-box">
            <h4 style="margin: 0 0 10px 0; color: #ffb300; font-size: 13px;">Developer Notes & Responses</h4>
            {% for c in post.comments %}
            <div style="background: #252525; padding: 10px; border-radius: 4px; margin-bottom: 8px; border-left: 2px solid #ffb300;">
                <span style="font-size: 12px; color: #aaa;"><strong>{{ c.username }}</strong>:</span>
                <span style="font-size: 12px; color: #eee; margin-left: 5px;">{{ c.comment_text }}</span>
            </div>
            {% endfor %}

            <form action="/feedback/comment/{{ post.id }}" method="POST" style="display: flex; gap: 8px; margin-top: 10px;">
                <input type="text" name="comment_text" placeholder="Add down-stream analysis or update notes..." style="padding: 8px; font-size: 13px;" required>
                <button type="submit" style="padding: 8px 16px; font-size: 13px; background: #333; color: #fff; border: 1px solid #444;">Reply</button>
            </form>
        </div>
    </div>
    {% else %}
    <p style="color: #666; font-style: italic;">No community suggestions have been submitted yet.</p>
    {% endfor %}
</div>
</body>
</html>
"""

SUBSCRIPTIONS_ROOM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>My Subscriptions - The Hub</title>
    """ + SHARED_CSS + """
</head>
<body>
""" + NAVBAR_HTML + """
<div class="container" style="max-width: 900px;">
    <h2>Channel Subscriptions Management (Bypasses Main Feed)</h2>
    <p style="color: #aaa;">Manage the channels indexed directly in your isolated pipeline database. Click any channel block below to instantly generate a custom filtered recommendation stream.</p>
    
    <div class="meta-panel" style="margin-bottom: 30px; border-color: #4caf50;">
        <h3 style="color: #4caf50; margin-top: 0;">Add New Channel Track Node</h3>
        <form action="/subscriptions/add" method="POST" style="display: flex; gap: 10px;">
            <input type="text" name="channel_name" placeholder="Exact Channel Name (e.g., 'Linus Tech Tips')" required>
            <button type="submit" style="background: #4caf50;">Bind to Hub Matrix</button>
        </form>
    </div>

    <div style="display: flex; flex-wrap: wrap; gap: 10px;">
        {% for ch in channels %}
        <div style="background: #1e1e1e; padding: 10px 18px; border-radius: 20px; border: 1px solid #333; display: flex; align-items: center; gap: 12px;">
            <a href="/search?q={{ ch.channel_name | urlencode }}&mode=subs" style="color: #fff; text-decoration: none; font-weight: bold; font-size: 14px;">{{ ch.channel_name }}</a>
            <form action="/subscriptions/delete/{{ ch.id }}" method="POST" style="margin: 0; display: inline;">
                <button type="submit" style="background: none; border: none; color: #ff4e4e; cursor: pointer; padding: 0; font-size: 14px; font-weight: bold;">&times;</button>
            </form>
        </div>
        {% endfor %}
    </div>
</div>
</body>
</html>
"""


TUBI_CATALOG_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tubi Catalog - The Hub</title>
    """ + SHARED_CSS + """
</head>
<body>
""" + NAVBAR_HTML + """
<div class="container">
    <h2>Tubi Direct Player</h2>
    <p style="color: #aaa; margin-top: -10px;">
        Grab the ID (or the whole URL) from Tubi's address bar - e.g. tubitv.com/movies/<b>100059630</b>/some-title -
        paste it below and click Load Video to jump straight to the player.
    </p>

    {% if error %}
    <p style="color: #ff6b6b; font-weight: bold;">{{ error }}</p>
    {% endif %}

    <form method="POST" action="/tubi-catalog" style="display: flex; gap: 10px; flex-wrap: wrap; margin: 20px 0 30px 0;">
        <input type="text" name="tubi_id" placeholder="Tubi ID or tubitv.com URL"
               value="{{ last_input or '' }}"
               style="flex: 1; min-width: 260px; padding: 12px 16px; font-size: 14px; border-radius: 8px;
                      border: 1px solid #333; background: #1e1e1e; color: #fff;">
        <button type="submit" class="btn"
                style="background: #8e44ad; border: 1px solid #a55cd1; color: #fff; padding: 12px 28px;
                       font-size: 14px; border-radius: 8px; cursor: pointer;">
            Load Video
        </button>
    </form>
</div>
</body>
</html>
"""


@app.route("/register", methods=["POST"])
def register():
    data = request.form if request.form else (request.get_json() or {})
    username = data.get("username", "").strip()
    password = data.get("password", "")
    
    if not username or not password:
        return jsonify({"error": "Fields cannot be blank."}), 400
        
    hashed_pw = generate_password_hash(password)
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, hashed_pw))
        con.commit()
        con.close()
        return jsonify({"message": "Profile synced to hub.db! You can now log in."}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "The username you have chosen has already been measured. Please select a unique identifier."}), 400


@app.route("/login", methods=["POST"])
def login():
    data = request.form if request.form else (request.get_json() or {})
    username = data.get("username", "").strip()
    password = data.get("password", "")
    
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    con.close()
        
    if row and check_password_hash(row[0], password):
        resp = make_response(jsonify({"message": "Authorization verified."}))
        resp.set_cookie('local_user_session', username, max_age=60*60*24*365, httponly=True)
        return resp
    
    return jsonify({"error": "Invalid profile identifier credentials."}), 401


@app.route("/logout")
def logout():
    resp = make_response(redirect(url_for('index')))
    resp.delete_cookie('local_user_session')
    return resp


# ====== APPLICATION ROUTES ======
@app.route("/")
def index():
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)
        
    videos = get_home_recommendations(user_id=user["id"])
    for v in videos:
        if v["id"].startswith("local_"):
            continue
        db_row = get_video(v["id"])
        v["likes_count"] = db_row["likes_count"] if db_row else 0
        v["dislikes_count"] = db_row["dislikes_count"] if db_row else 0
    return render_template_string(INDEX_HTML, videos=videos, original_user_prompt="", ai_optimized_notice="", append_mode=False, subscription_view=False)


# Dedicated view engine for sorting through full localized directories
@app.route("/media-library")
def media_library_view():
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)
        
    all_local_assets = scan_local_media_library()
    
    movies_list = [item for item in all_local_assets if item["category"] == "movies"]
    tv_shows_list = [item for item in all_local_assets if item["category"] == "tv_shows"]
    porn_list = [item for item in all_local_assets if item["category"] == "porn"]
    
    return render_template_string(
        MEDIA_LIBRARY_HTML,
        movies=movies_list,
        tv_shows=tv_shows_list,
        porn=porn_list
    )


@app.route("/subscriptions")
def subscriptions_feed():
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)
        
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT channel_name FROM subscriptions ORDER BY RANDOM() LIMIT 3")
    selected_channels = [row[0] for row in cur.fetchall()]
    con.close()
    
    aggregated_videos = []
    if selected_channels:
        for channel in selected_channels:
            videos_from_channel = search_youtube(channel)
            for v in videos_from_channel:
                if channel.lower() in v["channel"].lower() or channel.lower() in v["title"].lower():
                    aggregated_videos.append(v)
            if len(aggregated_videos) >= 40:
                break
                
    if not aggregated_videos:
        for channel in selected_channels:
            aggregated_videos.extend(search_youtube(channel))
            
    for v in aggregated_videos:
        db_row = get_video(v["id"])
        v["likes_count"] = db_row["likes_count"] if db_row else 0
        v["dislikes_count"] = db_row["dislikes_count"] if db_row else 0
        
    return render_template_string(
        INDEX_HTML,
        videos=aggregated_videos[:40],
        original_user_prompt="",
        ai_optimized_notice="",
        append_mode=False,
        subscription_view=True
    )


@app.route("/tubi-catalog", methods=["GET", "POST"])
def tubi_catalog():
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)

    if request.method == "POST":
        raw_input = request.form.get("tubi_id", "").strip()
        parsed_id = extract_tubi_id(raw_input)

        if parsed_id:
            return redirect(url_for("watch", video_id=f"tubi_{parsed_id}"))

        return render_template_string(
            TUBI_CATALOG_HTML,
            error="Couldn't find a valid Tubi ID in that - paste the ID itself or the full tubitv.com URL.",
            last_input=raw_input
        )

    return render_template_string(TUBI_CATALOG_HTML, error=None, last_input="")


@app.route("/search")
def search():
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)
        
    raw_prompt = request.args.get("q", "").strip()
    if not raw_prompt:
        return redirect(url_for("index"))
        
    if raw_prompt not in RECENT_SEARCH_HISTORY:
        RECENT_SEARCH_HISTORY.append(raw_prompt)
        if len(RECENT_SEARCH_HISTORY) > MAX_HISTORY_KEYWORDS:
            RECENT_SEARCH_HISTORY.pop(0)
            
    local_items = scan_local_media_library()
    filtered_local = [v for v in local_items if raw_prompt.lower() in v["title"].lower()]

    videos = filtered_local + search_youtube(raw_prompt) + search_tubi_videos(raw_prompt)
    for v in videos:
        if v["id"].startswith("local_"):
            continue
        db_row = get_video(v["id"])
        v["likes_count"] = db_row["likes_count"] if db_row else 0
        v["dislikes_count"] = db_row["dislikes_count"] if db_row else 0
        
    return render_template_string(
        INDEX_HTML, 
        videos=videos, 
        original_user_prompt=raw_prompt, 
        ai_optimized_notice="", 
        append_mode=False,
        subscription_view=False
    )



@app.route("/api/roku/search")
def api_roku_search():
    query = request.args.get("q", "").strip()
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM videos LIMIT 1")
    sample_row = cursor.fetchone()
    
    available_columns = sample_row.keys() if sample_row else []
    
    id_col = "video_id" if "video_id" in available_columns else ("id" if "id" in available_columns else "ROWID")
    thumb_col = next((c for c in ["thumbnail_url", "thumbnail", "thumb", "image_url", "poster"] if c in available_columns), None)
    channel_col = next((c for c in ["channel", "author", "uploader", "creator", "artist"] if c in available_columns), None)

    sections = []
    used_video_ids = set()

    local_assets = scan_local_media_library()
    local_movies_row = []
    local_tv_row = []
    local_porn_row = []

    if query:
        for item in local_assets:
            if query.lower() in item["title"].lower():
                formatted_item = {
                    "title": item["title"],
                    "url": f"http://{COMPUTER_IP}:{LISTENING_PORT}/api/roku/stream/{item['video_id']}",
                    "streamformat": "mp4",
                    "thumbnail": item["thumbnail"],
                    "author": item["channel"]
                }
                if item["category"] == "movies":
                    local_movies_row.append(formatted_item)
                else:
                    local_tv_row.append(formatted_item)
    else:
        for item in local_assets:
            formatted_item = {
                "title": item["title"],
                "url": f"http://{COMPUTER_IP}:{LISTENING_PORT}/api/roku/stream/{item['video_id']}",
                "streamformat": "mp4",
                "thumbnail": item["thumbnail"],
                "author": item["channel"]
            }
            if item["category"] == "movies":
                local_movies_row.append(formatted_item)
            else:
                local_tv_row.append(formatted_item)

    if local_movies_row:
        sections.append({"row_title": "Local Movies Storage", "items": local_movies_row})
    if local_tv_row:
        sections.append({"row_title": "Local TV Storage", "items": local_tv_row})

    if query:
        if id_col:
            select_fields = f"{id_col} AS safe_id, title"
            if thumb_col: select_fields += f", {thumb_col} AS safe_thumb"
            if channel_col: select_fields += f", {channel_col} AS safe_channel"
            
            where_clause = "WHERE title LIKE ?"
            query_params = [f"%{query}%"]
            if channel_col:
                where_clause += f" OR {channel_col} LIKE ?"
                query_params.append(f"%{query}%")
                
            sql = f"SELECT {select_fields} FROM videos {where_clause} ORDER BY {id_col} DESC LIMIT 100"
            cursor.execute(sql, query_params)
            rows = cursor.fetchall()
            
            if rows:
                search_items = []
                for row in rows:
                    thumb = row["safe_thumb"] if (thumb_col and row["safe_thumb"]) else "https://via.placeholder.com/300x200.png?text=No+Thumbnail"
                    author = row["safe_channel"] if (channel_col and row["safe_channel"]) else "Hub Creator"
                    search_items.append({
                        "title": row["title"] or "Untitled",
                        "url": f"http://{COMPUTER_IP}:{LISTENING_PORT}/api/roku/stream/{row['safe_id']}",
                        "streamformat": "mp4",
                        "thumbnail": thumb,
                        "author": author
                    })
                sections.append({
                    "row_title": f"Cached Database Results for '{query}' ({len(search_items)} items)",
                    "items": search_items
                })
    else:
        if sample_row:
            select_fields = f"{id_col} AS safe_id, title"
            if thumb_col: select_fields += f", {thumb_col} AS safe_thumb"
            if channel_col: select_fields += f", {channel_col} AS safe_channel"


            categories = [
            ("Sports Central", ["sports", "football", "basketball", "nba", "nfl", "soccer", "highlights", "game day", "match highlights", "ufc", "f1"]),
            ("Music Blocks", ["music", "song", "audio", "track", "remix", "playlist", "lofi", "ost", "concert", "album", "music video"]),
            ("Tech & Science", ["tech", "technology", "computer", "pc", "review", "hardware", "gadget", "linux", "science", "space", "engineering", "ai"]),
            ("News & Updates", ["news", "report", "update", "breaking", "politics", "coverage", "documentary", "world news"]),
            ("Free_Movies", ["new action movies", "New Released Action Movie 2026 | Hollywood English Full HD", "free movies", "full movies", "full length movie", "feature film", "free cinema"]),
            ("Comedies & Parodies", ["funny", "comedy", "meme", "parody", "skit", "short", "hilarious", "stand up"]),
            ("Podcasts & Talk Shows", ["podcast", "interview", "discussion", "talk show", "episode", "co-host", "clips"]),
            ("Live Streams & Broadcasts", ["live", "livestream", "stream", "broadcast", "vlog", "irl", "forget about it"]),
            ("Gaming Hub", ["game", "gaming", "gameplay", "playthrough", "nintendo", "xbox", "playstation", "retro", "speedrun", "walkthrough", "lets play"]),
            ("Creativity & DIY", ["diy", "how to make", "crafts", "woodworking", "restoration", "tutorial", "home improvement"]),
            ("Culinary & Food", ["cooking", "recipe", "chef", "street food", "bake", "food review", "gourmet"]),
            ("Wanderlust & Travel", ["travel vlog", "exploration", "tourist guide", "road trip", "nature", "expedition"]),
            ("Health & Fitness", ["workout", "gym", "yoga", "meditation", "exercise", "cardio"]),
            ("Gear & Garage", ["car review", "supercar", "motorcycle", "restoring cars", "test drive", "racing"]),
        ]

            if channel_col:
                try:
                    cursor.execute(f"SELECT DISTINCT {channel_col} FROM videos WHERE {channel_col} IS NOT NULL AND {channel_col} != '' ORDER BY RANDOM() LIMIT 15")
                    db_channels = [r[0] for r in cursor.fetchall()]
                    for chan in db_channels:
                        categories.append((f"More from {chan}", [chan]))
                except Exception:
                    pass

            for row_title, keywords in categories:
                conditions = []
                params = []
                for kw in keywords:
                    conditions.append("title LIKE ?")
                    params.append(f"%{kw}%")
                    if channel_col:
                        conditions.append(f"{channel_col} LIKE ?")
                        params.append(f"%{kw}%")
                
                where_clause = " OR ".join(conditions)
                sql_cat = f"SELECT {select_fields} FROM videos WHERE ({where_clause}) ORDER BY RANDOM() LIMIT 40"
                cursor.execute(sql_cat, params)
                cat_rows = cursor.fetchall()
                
                if cat_rows:
                    items = []
                    for row in cat_rows:
                        if row["safe_id"] in used_video_ids:
                            continue
                        thumb = row["safe_thumb"] if (thumb_col and row["safe_thumb"]) else "https://via.placeholder.com/300x200.png?text=No+Thumbnail"
                        author = row["safe_channel"] if (channel_col and row["safe_channel"]) else "Hub Creator"
                        items.append({
                            "title": row["title"] or "itiem",
                            "url": f"http://{COMPUTER_IP}:{LISTENING_PORT}/api/roku/stream/{row['safe_id']}",
                            "streamformat": "mp4",
                            "thumbnail": thumb,
                            "author": author
                        })
                        used_video_ids.add(row["safe_id"])
                    if items:
                        sections.append({
                            "row_title": row_title,
                            "items": items
                        })

            sql_recent = f"SELECT {select_fields} FROM videos ORDER BY RANDOM() LIMIT 40"
            cursor.execute(sql_recent)
            recent_rows = cursor.fetchall()
            if recent_rows:
                recent_items = []
                for row in recent_rows:
                    if row["safe_id"] in used_video_ids:
                        continue
                    thumb = row["safe_thumb"] if (thumb_col and row["safe_thumb"]) else "https://via.placeholder.com/300x200.png?text=No+Thumbnail"
                    author = row["safe_channel"] if (channel_col and row["safe_channel"]) else "Hub Creator"
                    recent_items.append({
                        "title": row["title"] or "Untitled",
                        "url": f"http://{COMPUTER_IP}:{LISTENING_PORT}/api/roku/stream/{row['safe_id']}",
                        "streamformat": "mp4", "mkv"
                        "thumbnail": thumb,
                        "author": author
                    })
                if recent_items:
                    sections.append({
                        "title": "Recommended For You",
                        "items": recent_items
                    })

    conn.close()
    if not sections:
        sections.append({
            "title": "The Hub Library",
            "items": [{"title": "No media found in database", "url": "", "streamformat": "mp4", "thumbnail": "", "author": "System"}]
        })
    return jsonify(sections)


# ── In-memory stream URL cache ──────────────────────────────────────────────
import time as _time
_STREAM_CACHE: dict = {}
_STREAM_CACHE_TTL = 300

def _get_cached_stream_url(video_id: str) -> str | None:
    entry = _STREAM_CACHE.get(video_id)
    if entry and _time.time() < entry[1]:
        return entry[0]
    return None

def _set_cached_stream_url(video_id: str, url: str):
    _STREAM_CACHE[video_id] = (url, _time.time() + _STREAM_CACHE_TTL)
    now = _time.time()
    stale = [k for k, v in _STREAM_CACHE.items() if now >= v[1]]
    for k in stale:
        del _STREAM_CACHE[k]
# ────────────────────────────────────────────────────────────────────────────

@app.route("/api/roku/stream/<video_id>", methods=["GET"])
def api_roku_stream(video_id):
    print(f"[ROKU] Stream resolve request for video_id={video_id}")

    if video_id.startswith("local_"):
        local_assets = scan_local_media_library()
        local_match = next((v for v in local_assets if v["video_id"] == video_id), None)
        if not local_match or not os.path.exists(local_match["file_path"]):
            return "Local file missing or offline", 404

        file_path = local_match["file_path"]

        # ── MKV: transcode on-the-fly to a fragmented MP4 stream ──────────────
        if file_path.lower().endswith('.mkv'):
            print(f"[ROKU] MKV container detected for {video_id}. Analysing codec.")

            # Detect the video codec inside the MKV with ffprobe
            ffprobe_path = FFPROBE_PATH
            probe_cmd = [
                ffprobe_path, '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name',
                '-of', 'csv=p=0',
                file_path
            ]
            try:
                video_codec = subprocess.check_output(probe_cmd, stderr=subprocess.DEVNULL).decode('utf-8').strip()
                print(f"[ROKU] Detected video codec: {video_codec}")
            except Exception as probe_err:
                video_codec = "hevc"  # safe default: force transcode when probe fails
                print(f"[ROKU] ffprobe failed, forcing h264 transcode to be safe: {probe_err}")

            # x265/HEVC must be transcoded; x264 can be stream-copied directly
            if "265" in video_codec or "hevc" in video_codec.lower():
                print(f"[ROKU] x265/HEVC detected — live CPU transcode to h264.")
                video_flag = ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23']
            else:
                print(f"[ROKU] x264 detected — using zero-CPU stream copy.")
                video_flag = ['-c:v', 'copy']

            ffmpeg_cmd = [
                FFMPEG_PATH,
                '-i', file_path,
            ] + video_flag + [
                '-c:a', 'aac',
                '-b:a', '192k',
                '-ac', '2',
                '-f', 'mp4',
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                'pipe:1'
            ]

            def generate_ffmpeg_stream():
                process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=1024 * 64
                )
                try:
                    while True:
                        data = process.stdout.read(1024 * 64)
                        if not data:
                            break
                        yield data
                except Exception as stream_err:
                    print(f"[ROKU] Stream interrupted: {stream_err}")
                finally:
                    process.kill()
                    process.wait()
                    print(f"[ROKU] FFmpeg pipeline closed for {video_id}")

            resp = Response(generate_ffmpeg_stream(), status=200, mimetype='video/mp4')
            resp.headers['Accept-Ranges'] = 'none'
            return resp
        # ── END MKV block ──────────────────────────────────────────────────────

        # Original MP4 byte-range logic (unchanged)
        file_size = os.path.getsize(file_path)
        byte_range = request.headers.get('Range', None)

        if byte_range:
            match_range = re.search(r'bytes=(\d+)-(\d*)', byte_range)
            start_byte = int(match_range.group(1))
            end_byte = int(match_range.group(2)) if match_range.group(2) else file_size - 1
            length = (end_byte - start_byte) + 1

            def generate_chunks():
                with open(file_path, 'rb') as f:
                    f.seek(start_byte)
                    remaining = length
                    while remaining > 0:
                        chunk_size = min(1024 * 64, remaining)
                        data = f.read(chunk_size)
                        if not data:
                            break
                        yield data
                        remaining -= len(data)

            resp = Response(generate_chunks(), status=206, mimetype='video/mp4')
            resp.headers['Content-Range'] = f'bytes {start_byte}-{end_byte}/{file_size}'
            resp.headers['Accept-Ranges'] = 'bytes'
            resp.headers['Content-Length'] = length
            return resp
        else:
            return send_file(file_path, mimetype='video/mp4')

    cached = _get_cached_stream_url(video_id)
    if cached:
        print(f"[ROKU] Cache hit — redirecting instantly for {video_id}")
        return redirect(cached, code=307)
    try:
        urls = resolve_stream_urls(video_id)
        direct_url = urls["video_url"]
        _set_cached_stream_url(video_id, direct_url)
        print(f"[ROKU] Resolved + cached CDN URL for {video_id}")
        return redirect(direct_url, code=307)
    except Exception as e:
        print(f"[ROKU] Stream resolve failed for {video_id}: {e}", file=sys.stderr)
        return redirect("https://roku.samples.cdn.cloudinary.com/video/mp4/tos_720p.mp4", code=307)


@app.route("/load-more")
def load_more():
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)
        
    raw_prompt = request.args.get("q", "").strip()
    videos = search_youtube(raw_prompt) if raw_prompt else get_home_recommendations(user_id=user["id"])
    for v in videos:
        if v["id"].startswith("local_"):
            continue
        db_row = get_video(v["id"])
        v["likes_count"] = db_row["likes_count"] if db_row else 0
        v["dislikes_count"] = db_row["dislikes_count"] if db_row else 0
    return render_template_string(INDEX_HTML, videos=videos, original_user_prompt=raw_prompt, ai_optimized_notice="", append_mode=True, subscription_view=False)


@app.route("/watch/<video_id>")
def watch(video_id):
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)
        
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT INTO watch_history (user_id, video_id) VALUES (?, ?)", (user["id"], video_id))
    con.commit()
    con.close()

    video = get_video(video_id)
    if not video:
        video = {
            "video_id": video_id,
            "title": "Unknown Title",
            "channel": "Unknown Channel",
            "thumbnail": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
            "description": "No description available.",
            "likes_count": 0,
            "dislikes_count": 0,
        }

    if video_id.startswith("local_"):
        stream_link = f"/api/roku/stream/{video_id}"
    elif video_id.startswith("tubi_"):
        stream_link = None
        try:
            raw_tubi_id = video_id[len("tubi_"):]
            stream_link = get_tubi_stream(raw_tubi_id)
        except Exception as e:
            print(f"Tubi stream resolve error for {video_id}: {e}", file=sys.stderr)
    else:
        stream_link = None
        try:
            urls = resolve_stream_urls(video_id)
            stream_link = urls["video_url"]
        except Exception as e:
            print(f"Stream resolve error for {video_id}: {e}", file=sys.stderr)

    comments = get_comments(video_id)
    return render_template_string(
        WATCH_HTML,
        video=video,
        video_id=video_id,
        stream_link=stream_link,
        comments=comments,
        ai_summary="",
        current_username=user["username"]
    )


def _is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@app.route("/like/<video_id>", methods=["POST"])
def like(video_id):
    user = get_current_user()
    if not user:
        if _is_ajax():
            return jsonify({"error": "Not authenticated"}), 401
        return render_template_string(GATEKEEPER_LOGIN_HTML)
    increment_likes(video_id)
    if _is_ajax():
        row = get_video(video_id)
        return jsonify({
            "likes_count": row["likes_count"] if row else 0,
            "dislikes_count": row["dislikes_count"] if row else 0
        })
    return redirect(url_for("watch", video_id=video_id) + "#comments-focus")


@app.route("/dislike/<video_id>", methods=["POST"])
def dislike(video_id):
    user = get_current_user()
    if not user:
        if _is_ajax():
            return jsonify({"error": "Not authenticated"}), 401
        return render_template_string(GATEKEEPER_LOGIN_HTML)
    increment_dislikes(video_id)
    if _is_ajax():
        row = get_video(video_id)
        return jsonify({
            "likes_count": row["likes_count"] if row else 0,
            "dislikes_count": row["dislikes_count"] if row else 0
        })
    return redirect(url_for("watch", video_id=video_id) + "#comments-focus")


@app.route("/comment/<video_id>", methods=["POST"])
def comment(video_id):
    user = get_current_user()
    if not user:
        if _is_ajax():
            return jsonify({"error": "Not authenticated"}), 401
        return render_template_string(GATEKEEPER_LOGIN_HTML)

    username = user["username"]
    comment_text = request.form.get("comment_text", "").strip()
    if comment_text:
        add_comment(video_id, username, comment_text)

    if _is_ajax():
        if not comment_text:
            return jsonify({"error": "Comment was empty"}), 400
        return jsonify({
            "username": username,
            "comment_text": comment_text,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
    return redirect(url_for("watch", video_id=video_id) + "#comments-focus")


@app.route("/about")
def about():
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)
    return render_template_string(ABOUT_HTML)


# ====== COMMUNITY FEEDBACK ROUTES ======
@app.route("/feedback")
def feedback_dashboard():
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM feedback ORDER BY upvotes DESC, id DESC")
    posts = [dict(row) for row in cur.fetchall()]
    for post in posts:
        cur.execute("SELECT * FROM feedback_comments WHERE feedback_id = ? ORDER BY id ASC", (post["id"],))
        post["comments"] = [dict(r) for r in cur.fetchall()]
    con.close()
    return render_template_string(COMMUNITY_FEEDBACK_HTML, feedback_posts=posts)


@app.route("/feedback/submit", methods=["POST"])
def feedback_submit():
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)

    title = request.form.get("title", "").strip()
    suggestion = request.form.get("suggestion", "").strip()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    if title and suggestion:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(
            "INSERT INTO feedback (user_id, username, title, suggestion, upvotes, timestamp) VALUES (?, ?, ?, ?, 0, ?)",
            (user["id"], user["username"], title, suggestion, ts)
        )
        con.commit()
        con.close()
    return redirect(url_for("feedback_dashboard"))


@app.route("/feedback/vote/<int:feedback_id>", methods=["POST"])
def feedback_vote(feedback_id):
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("INSERT INTO feedback_votes (user_id, feedback_id) VALUES (?, ?)", (user["id"], feedback_id))
        cur.execute("UPDATE feedback SET upvotes = upvotes + 1 WHERE id = ?", (feedback_id,))
        con.commit()
    except sqlite3.IntegrityError:
        pass
    con.close()
    return redirect(url_for("feedback_dashboard"))


@app.route("/feedback/comment/<int:feedback_id>", methods=["POST"])
def feedback_comment(feedback_id):
    user = get_current_user()
    if not user:
        return render_template_string(GATEKEEPER_LOGIN_HTML)

    comment_text = request.form.get("f_comment_text", "").strip()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    if comment_text:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(
            "INSERT INTO feedback_comments (feedback_id, username, comment_text, timestamp) VALUES (?, ?, ?, ?)",
            (feedback_id, user["username"], comment_text, ts)
        )
        con.commit()
        con.close()
    return redirect(url_for("feedback_dashboard"))


@app.route("/api/next-video")
def api_next_video():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
        
    current_id = request.args.get("current", "")
    recommendations = get_home_recommendations(user_id=user["id"])
    next_id = None
    
    if recommendations:
        for i, video in enumerate(recommendations):
            if video["id"] == current_id and i + 1 < len(recommendations):
                next_id = recommendations[i + 1]["id"]
                break
        if not next_id:
            next_id = recommendations[0]["id"]
    return jsonify({"next_id": next_id})


@app.route("/proxy/stream")
def proxy_stream():
    cdn_url = request.args.get("url", "")
    if not cdn_url or not cdn_url.startswith("http"):
        return "Bad URL", 400

    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.youtube.com/",
    }
    if "Range" in request.headers:
        req_headers["Range"] = request.headers["Range"]

    upstream = requests.get(cdn_url, headers=req_headers, stream=True, timeout=15)
    status = upstream.status_code

    resp_headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": "bytes",
    }
    if "Content-Range" in upstream.headers:
        resp_headers["Content-Range"] = upstream.headers["Content-Range"]
    if "Content-Length" in upstream.headers:
        resp_headers["Content-Length"] = upstream.headers["Content-Length"]

    def generate():
        for chunk in upstream.iter_content(chunk_size=1024 * 64):
            if chunk:
                yield chunk
    return Response(stream_with_context(generate()), status=status, headers=resp_headers)

@app.route("/live-stream")
def live_stream_view():
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Live Web Stream</title>
        <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
        <style>
            body { background-color: #0c0c0c; color: #fff; text-align: center; font-family: Arial, sans-serif; padding-top: 50px; }
            .video-wrap { width: 85%; max-width: 1024px; margin: 0 auto; box-shadow: 0 0 30px rgba(0,0,0,0.8); }
            video { width: 100%; display: block; border: 1px solid #333; }
        </style>
    </head>
    <body>
        <h2>Live Platform Feed</h2>
        <div class="video-wrap">
            <video id="videoPlayer" controls autoplay muted></video>
        </div>

        <script>
            const video = document.getElementById('videoPlayer');
            // Points to the stream file managed by our Flask routing below
            const streamSource = '/live-files/stream0.m3u8'; 

            if (Hls.isSupported()) {
                const hls = new Hls();
                hls.loadSource(streamSource);
                hls.attachMedia(video);
                hls.on(Hls.Events.MANIFEST_PARSED, function() {
                    video.play();
                });
            } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                video.src = streamSource;
            }
        </script>
    </body>
    </html>
    """)

@app.route("/live-files/<path:filename>")
def serve_live_hls_files(filename):
    # This targets the 'hls_output' folder in the exact directory where app.py lives
    hls_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hls_output")
    file_path = os.path.join(hls_dir, filename)
    
    if not os.path.exists(file_path):
        return "File not found", 404
        
    if filename.endswith(".m3u8"):
        mimetype = "application/x-mpegURL"
    elif filename.endswith(".ts"):
        mimetype = "video/MP2T"
    else:
        mimetype = "application/octet-stream"
        
    return send_file(file_path, mimetype=mimetype)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=LISTENING_PORT, debug=True)
