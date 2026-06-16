import os
import time
import json
import logging
import re
import requests
import asyncio
import threading
import subprocess
import sys
import secrets
import sqlite3
import hashlib
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from yandex_music import Client as YandexMusicClient

# Импортируем оригинальные классы и переменные из main.py
from main import QobuzDirect

app = FastAPI(title="Qobuz Playlist Importer")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qobuz_web")

load_dotenv(override=True)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_COOKIE_NAME = "qsync_sid"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 90
SESSION_COOKIE_SECURE = os.getenv("QSYNC_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}
DB_FILE = os.getenv("QSYNC_DB_PATH", "qobuzsync.db")
LOGIN_PROFILE_ROOT = os.getenv("QSYNC_LOGIN_PROFILE_DIR") or os.path.join(APP_DIR, ".qobuz_login_profiles")
BROWSER_LOGIN_ENABLED = os.getenv("QSYNC_BROWSER_LOGIN_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SERVER_DEFAULT_APP_ID = os.getenv("QOBUZ_APP_ID") or "30650571"
SERVER_DEFAULT_APP_SECRET = os.getenv("QOBUZ_APP_SECRET") or "5929d2b8b9354226a0a73d327f918991"
db_lock = threading.Lock()
QOBUZ_APP_ID_CANDIDATES = [
    SERVER_DEFAULT_APP_ID,
    "798273057",     # Android
    "950096963",     # Web-player
    "579939560",
    "100000000",
    "306000000",
    "274246104",
]
qobuz_web_app_ids_cache = {"expires_at": 0, "app_ids": []}

def init_db():
    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    qobuz_token TEXT,
                    qobuz_app_id TEXT,
                    qobuz_app_secret TEXT,
                    qobuz_working_app_id TEXT,
                    yandex_token TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            conn.commit()

def db_execute(query, params=(), fetchone=False):
    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(query, params)
            row = cur.fetchone() if fetchone else None
            conn.commit()
            return dict(row) if row else None

def create_session() -> str:
    session_id = secrets.token_urlsafe(32)
    now = int(time.time())
    db_execute(
        """
        INSERT INTO sessions (
            id, qobuz_token, qobuz_app_id, qobuz_app_secret,
            qobuz_working_app_id, yandex_token, created_at, updated_at
        ) VALUES (?, '', ?, ?, NULL, NULL, ?, ?)
        """,
        (session_id, SERVER_DEFAULT_APP_ID, SERVER_DEFAULT_APP_SECRET, now, now),
    )
    return session_id

def get_session(session_id: str) -> Optional[dict]:
    if not session_id:
        return None
    return db_execute("SELECT * FROM sessions WHERE id = ?", (session_id,), fetchone=True)

def set_session_cookie(response: Response, session_id: str):
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )

def get_or_create_session(request: Request, response: Response) -> dict:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session = get_session(session_id)
    if not session:
        session_id = create_session()
        session = get_session(session_id)
        set_session_cookie(response, session_id)
    return session

def get_websocket_session(websocket: WebSocket) -> dict:
    session_id = websocket.cookies.get(SESSION_COOKIE_NAME)
    session = get_session(session_id)
    if not session:
        raise WebSocketDisconnect(code=4401)
    return session

def update_session_values(session_id: str, updates: dict):
    if not updates:
        return
    allowed = {
        "qobuz_token",
        "qobuz_app_id",
        "qobuz_app_secret",
        "qobuz_working_app_id",
        "yandex_token",
    }
    fields = [key for key in updates.keys() if key in allowed]
    if not fields:
        return
    assignments = ", ".join(f"{field} = ?" for field in fields)
    values = [updates[field] for field in fields]
    values.extend([int(time.time()), session_id])
    db_execute(f"UPDATE sessions SET {assignments}, updated_at = ? WHERE id = ?", values)

def make_qobuz_client(session: dict) -> QobuzDirect:
    app_id = session.get("qobuz_working_app_id") or session.get("qobuz_app_id") or SERVER_DEFAULT_APP_ID
    app_secret = session.get("qobuz_app_secret") or SERVER_DEFAULT_APP_SECRET
    return QobuzDirect(session.get("qobuz_token") or "", app_id, app_secret)

init_db()

def unique_values(values):
    seen = set()
    return [value for value in values if value and not (value in seen or seen.add(value))]

def get_qobuz_web_app_ids():
    now = time.time()
    if qobuz_web_app_ids_cache["expires_at"] > now:
        return qobuz_web_app_ids_cache["app_ids"]

    app_ids = []
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        page = requests.get("https://play.qobuz.com/login", headers=headers, timeout=(5, 15))
        page.raise_for_status()
        script_urls = re.findall(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', page.text)

        for script_url in script_urls[:10]:
            script = requests.get(urljoin(page.url, script_url), headers=headers, timeout=(5, 15))
            if script.status_code != 200:
                continue
            app_ids.extend(re.findall(r'["\']app_?id["\']\s*[:=]\s*["\']?(\d{6,})', script.text, flags=re.IGNORECASE))
            app_ids.extend(re.findall(r'appId\s*[:=]\s*["\']?(\d{6,})', script.text))
            app_ids.extend(re.findall(r'app_id[=:/%22]+(\d{6,})', script.text, flags=re.IGNORECASE))

        app_ids = unique_values(app_ids)
        if app_ids:
            logger.info("Найдены app_id Qobuz web-player: %s", ", ".join(app_ids[:5]))
    except Exception as exc:
        logger.warning("Не удалось обновить app_id Qobuz web-player: %s", exc)

    qobuz_web_app_ids_cache["app_ids"] = app_ids
    qobuz_web_app_ids_cache["expires_at"] = now + 3600
    return app_ids

def get_qobuz_profile(cl: QobuzDirect, preferred_app_ids=None):
    known_app_ids = list(preferred_app_ids or []) + get_qobuz_web_app_ids() + QOBUZ_APP_ID_CANDIDATES
    # Убираем дубликаты с сохранением порядка
    known_app_ids = unique_values(known_app_ids)
    
    for app_id in known_app_ids:
        try:
            data = cl._request("user/get", current_app_id=app_id)
            if isinstance(data, dict) and 'display_name' in data:
                cl.app_id = app_id  # Запоминаем рабочий ID
                return {
                    "authorized": True,
                    "display_name": data["display_name"],
                    "id": data["id"],
                    "app_id": app_id
                }
        except Exception as e:
            logger.error(f"Ошибка проверки app_id {app_id}: {e}")
    return {"authorized": False}

def ensure_qobuz_authorized(session: dict):
    qobuz_client = make_qobuz_client(session)
    profile = get_qobuz_profile(qobuz_client, [
        session.get("qobuz_working_app_id"),
        session.get("qobuz_app_id"),
    ])
    if not profile.get("authorized"):
        raise HTTPException(
            status_code=401,
            detail="Qobuz аккаунт не выбран. Вставьте token своего Qobuz аккаунта и сохраните настройки.",
        )
    if profile.get("app_id") != session.get("qobuz_working_app_id"):
        update_session_values(session["id"], {"qobuz_working_app_id": profile["app_id"]})
    return qobuz_client, profile

CACHE_FILE = "search_cache.json"
MATCH_CONCURRENCY = int(os.getenv("MATCH_CONCURRENCY", "5"))
cache_lock = threading.Lock()
search_cache = {}
thread_local = threading.local()

def load_search_cache():
    global search_cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                search_cache = json.load(f)
            logger.info(f"Загружен кэш сопоставления: {len(search_cache)} треков.")
        except Exception as e:
            logger.error(f"Не удалось загрузить search_cache.json: {e}")
            search_cache = {}

def save_search_cache():
    try:
        with cache_lock:
            cache_snapshot = dict(search_cache)

        temp_file = f"{CACHE_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(cache_snapshot, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, CACHE_FILE)
    except Exception as e:
        logger.error(f"Не удалось сохранить search_cache.json: {e}")

# Загружаем кэш
load_search_cache()

def get_thread_qobuz_client(session: dict) -> QobuzDirect:
    app_id_to_use = session.get("qobuz_working_app_id") or session.get("qobuz_app_id") or SERVER_DEFAULT_APP_ID
    config_key = (
        session.get("qobuz_token") or "",
        app_id_to_use,
        session.get("qobuz_app_secret") or SERVER_DEFAULT_APP_SECRET,
    )
    cached_key = getattr(thread_local, "qobuz_config_key", None)
    cached_client = getattr(thread_local, "qobuz_client", None)

    if cached_client is None or cached_key != config_key:
        cached_client = QobuzDirect(*config_key)
        thread_local.qobuz_client = cached_client
        thread_local.qobuz_config_key = config_key

    return cached_client

def search_track_rich_thread(query: str, session: dict) -> Optional[dict]:
    return search_track_rich(get_thread_qobuz_client(session), query)

def search_track_rich(cl: QobuzDirect, query: str) -> Optional[dict]:
    query_key = query.lower().strip()
    with cache_lock:
        if query_key in search_cache:
            return search_cache[query_key]

    method = "catalog/search"
    timestamp = str(int(time.time()))
    params = {
        "query": query,
        "type": "tracks",
        "limit": 1,
        "request_ts": timestamp
    }
    params["request_sig"] = cl._generate_signature(method, params, timestamp)
    try:
        data = cl._request(method, params)
        track_info = None
        if isinstance(data, dict) and 'tracks' in data and data['tracks']['items']:
            track = data['tracks']['items'][0]
            cover = None
            if 'album' in track and 'image' in track['album'] and track['album']['image']:
                cover = track['album']['image'].get('small') or track['album']['image'].get('thumbnail')
            
            track_info = {
                "id": track["id"],
                "title": track["title"],
                "artist": track["performer"]["name"],
                "album": track["album"]["title"] if "album" in track else "",
                "cover": cover,
                "duration": track.get("duration", 0),
                "hires": track.get("hires", False) or track.get("maximum_bit_depth", 16) > 16
            }
        
        with cache_lock:
            search_cache[query_key] = track_info
            
        return track_info
    except Exception as e:
        logger.error(f"Ошибка поиска для '{query}': {e}")
    return None

def search_tracks_rich_multi(cl: QobuzDirect, query: str, limit: int = 6) -> List[dict]:
    method = "catalog/search"
    timestamp = str(int(time.time()))
    params = {
        "query": query,
        "type": "tracks",
        "limit": limit,
        "request_ts": timestamp
    }
    params["request_sig"] = cl._generate_signature(method, params, timestamp)
    results = []
    try:
        data = cl._request(method, params)
        if 'tracks' in data and data['tracks']['items']:
            for track in data['tracks']['items']:
                cover = None
                if 'album' in track and 'image' in track['album'] and track['album']['image']:
                    cover = track['album']['image'].get('small') or track['album']['image'].get('thumbnail')
                
                results.append({
                    "id": track["id"],
                    "title": track["title"],
                    "artist": track["performer"]["name"],
                    "album": track["album"]["title"] if "album" in track else "",
                    "cover": cover,
                    "duration": track.get("duration", 0),
                    "hires": track.get("hires", False) or track.get("maximum_bit_depth", 16) > 16
                })
    except Exception as e:
        logger.error(f"Ошибка мультипоиска для '{query}': {e}")
    return results

# Pydantic модели
class ConfigData(BaseModel):
    token: str
    app_id: str
    app_secret: str

class QobuzLoginData(BaseModel):
    email: str
    password: str
    app_id: Optional[str] = None
    app_secret: Optional[str] = None

class PlaylistData(BaseModel):
    name: Optional[str] = None
    playlist_id: Optional[str] = None
    track_ids: List[int]

class SingleSearchQuery(BaseModel):
    query: str

class UrlParseRequest(BaseModel):
    url: str

# Эндпоинты

@app.get("/")
async def get_index(request: Request):
    file_response = FileResponse("index.html")
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not get_session(session_id):
        session_id = create_session()
        set_session_cookie(file_response, session_id)
    return file_response

@app.get("/style.css")
async def get_css():
    return FileResponse("style.css", media_type="text/css")

def get_yandex_profile(token: str = ""):
    if not token:
        return {"authorized": False}
    try:
        ya_client = YandexMusicClient(token).init()
        return {
            "authorized": True,
            "display_name": ya_client.me.account.display_name or ya_client.me.account.login,
            "uid": ya_client.me.account.uid
        }
    except Exception as e:
        logger.error(f"Failed to fetch Yandex profile: {e}")
        return {"authorized": False}

@app.get("/api/config")
async def get_config(request: Request, response: Response):
    session = get_or_create_session(request, response)
    qobuz_client = make_qobuz_client(session)
    profile = get_qobuz_profile(qobuz_client, [
        session.get("qobuz_working_app_id"),
        session.get("qobuz_app_id"),
    ])
    if profile.get("authorized") and profile.get("app_id") != session.get("qobuz_working_app_id"):
        update_session_values(session["id"], {"qobuz_working_app_id": profile["app_id"]})
    yandex_profile = get_yandex_profile(session.get("yandex_token") or "")
    return {
        "has_qobuz_token": bool(session.get("qobuz_token")),
        "app_id": session.get("qobuz_app_id") or SERVER_DEFAULT_APP_ID,
        "browser_login_enabled": BROWSER_LOGIN_ENABLED,
        "profile": profile,
        "yandex_profile": yandex_profile
    }

@app.post("/api/config")
async def save_config(data: ConfigData, request: Request, response: Response):
    session = get_or_create_session(request, response)
    try:
        app_id = data.app_id.strip() or SERVER_DEFAULT_APP_ID
        token = data.token.strip() or session.get("qobuz_token") or ""
        app_secret = data.app_secret.strip() or session.get("qobuz_app_secret") or SERVER_DEFAULT_APP_SECRET
        update_session_values(session["id"], {
            "qobuz_token": token,
            "qobuz_app_id": app_id,
            "qobuz_app_secret": app_secret,
            "qobuz_working_app_id": None,
        })
        session = get_session(session["id"])
        qobuz_client = make_qobuz_client(session)
        profile = get_qobuz_profile(qobuz_client, [app_id])
        if profile.get("authorized"):
            update_session_values(session["id"], {"qobuz_working_app_id": profile["app_id"]})
        return {"status": "success", "profile": profile}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/auth/test")
async def test_auth(request: Request, response: Response):
    session = get_or_create_session(request, response)
    qobuz_client = make_qobuz_client(session)
    profile = get_qobuz_profile(qobuz_client, [
        session.get("qobuz_working_app_id"),
        session.get("qobuz_app_id"),
    ])
    return profile

@app.post("/api/auth/qobuz-logout")
async def qobuz_logout(request: Request, response: Response):
    session = get_or_create_session(request, response)
    update_session_values(session["id"], {
        "qobuz_token": "",
        "qobuz_working_app_id": None,
    })
    return {"status": "success"}

@app.post("/api/auth/qobuz-login")
async def qobuz_password_login(data: QobuzLoginData, request: Request, response: Response):
    session = get_or_create_session(request, response)
    email = data.email.strip()
    password = data.password

    if not email or not password:
        raise HTTPException(status_code=400, detail="Введите email и пароль Qobuz")

    preferred_app_id = (data.app_id or session.get("qobuz_app_id") or SERVER_DEFAULT_APP_ID).strip()
    app_secret = (data.app_secret or session.get("qobuz_app_secret") or SERVER_DEFAULT_APP_SECRET).strip()
    app_ids = unique_values([preferred_app_id] + get_qobuz_web_app_ids() + QOBUZ_APP_ID_CANDIDATES)
    password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
    last_error = None

    for app_id in app_ids:
        qobuz_client = QobuzDirect("", app_id, app_secret)
        login_result = await asyncio.to_thread(qobuz_client.login, email, password_hash, app_id)
        if isinstance(login_result, dict) and login_result.get("user_auth_token"):
            update_session_values(session["id"], {
                "qobuz_token": login_result["user_auth_token"],
                "qobuz_app_id": app_id,
                "qobuz_app_secret": app_secret,
                "qobuz_working_app_id": app_id,
            })
            session = get_session(session["id"])
            profile_client = make_qobuz_client(session)
            profile = get_qobuz_profile(profile_client, [app_id])
            if profile.get("authorized"):
                update_session_values(session["id"], {"qobuz_working_app_id": profile["app_id"]})
                return {"status": "success", "profile": profile}

            update_session_values(session["id"], {"qobuz_token": "", "qobuz_working_app_id": None})
            raise HTTPException(status_code=401, detail="Qobuz вернул token, но профиль не удалось проверить")

        if isinstance(login_result, dict):
            last_error = login_result.get("message") or login_result.get("detail") or login_result.get("status")

    raise HTTPException(status_code=401, detail=last_error or "Не удалось войти в Qobuz. Проверьте email и пароль.")

@app.post("/api/auth/browser-login")
async def browser_login(request: Request, response: Response):
    if not BROWSER_LOGIN_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Браузерный вход Qobuz отключен на этом сервере. Вставьте Qobuz token вручную.",
        )

    session = get_or_create_session(request, response)
    logger.info("Запуск автоматического перехвата токена через браузер...")

    def run_capture_process():
        script_path = os.path.join(APP_DIR, "qobuz_browser_login.py")
        profile_dir = os.path.join(LOGIN_PROFILE_ROOT, session["id"])
        try:
            os.makedirs(profile_dir, mode=0o700, exist_ok=True)
            completed = subprocess.run(
                [sys.executable, script_path, profile_dir],
                cwd=APP_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=135,
            )
        except OSError as exc:
            return {
                "status": "error",
                "error": f"Нет доступа к директории профилей браузера: {LOGIN_PROFILE_ROOT}. Проверьте владельца и права. {exc}",
            }
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "error": "Окно входа было открыто слишком долго. Попробуйте еще раз и войдите в Qobuz в течение 2 минут.",
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

        output = (completed.stdout or "").strip()
        if not output:
            return {
                "status": "error",
                "error": (completed.stderr or "Процесс авторизации завершился без ответа.").strip(),
            }

        try:
            return json.loads(output.splitlines()[-1])
        except json.JSONDecodeError:
            logger.error("Некорректный ответ процесса авторизации Qobuz: %s", output)
            return {"status": "error", "error": output}

    auth_result = await asyncio.to_thread(run_capture_process)

    if auth_result.get("status") == "success" and auth_result.get("token"):
        token = auth_result["token"]
        save_app_id = auth_result.get("app_id") or session.get("qobuz_app_id") or SERVER_DEFAULT_APP_ID
        
        update_session_values(session["id"], {
            "qobuz_token": token,
            "qobuz_app_id": save_app_id,
            "qobuz_app_secret": session.get("qobuz_app_secret") or SERVER_DEFAULT_APP_SECRET,
            "qobuz_working_app_id": None,
        })
        session = get_session(session["id"])
        qobuz_client = make_qobuz_client(session)
        profile = get_qobuz_profile(qobuz_client, [save_app_id])
        if profile.get("authorized"):
            update_session_values(session["id"], {"qobuz_working_app_id": profile["app_id"]})
        return {"status": "success", "profile": profile}

    detail = auth_result.get("error") or "Не удалось войти в аккаунт или перехватить токен."
    raise HTTPException(status_code=400, detail=detail)

@app.post("/api/tracks/parse")
async def parse_tracks(
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None)
):
    track_names = []
    if file:
        content = await file.read()
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            lines = content.decode("cp1251", errors="ignore").splitlines()
        track_names = [line.strip() for line in lines if line.strip()]
    elif text:
        track_names = [line.strip() for line in text.splitlines() if line.strip()]
        
    return {"tracks": track_names}

def parse_yandex_music_url(url: str, session: dict):
    # Clean up the URL (remove query parameters)
    clean_url = url.split("?")[0].strip().rstrip("/")
    if clean_url.startswith(("music.yandex.ru/", "www.music.yandex.ru/")):
        clean_url = f"https://{clean_url}"
    parsed = urlparse(clean_url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or host not in {"music.yandex.ru", "www.music.yandex.ru"}:
        raise ValueError("Поддерживаются только ссылки music.yandex.ru")
    
    # Initialize client (guest or token)
    yandex_token = session.get("yandex_token") or ""
    try:
        if yandex_token:
            yandex_client = YandexMusicClient(yandex_token).init()
        else:
            yandex_client = YandexMusicClient().init()
    except Exception as e:
        logger.error(f"Failed to initialize Yandex Music Client: {e}")
        raise ValueError(f"Не удалось подключиться к API Яндекс.Музыки: {e}")
    
    track_names = []
    playlist_name_suggestion = "Импортировано из Яндекс.Музыки"
    
    # 1. Match Album + Track: https://music.yandex.ru/album/123/track/456
    album_track_match = re.search(r'/album/(\d+)/track/(\d+)', clean_url)
    if album_track_match:
        track_id = album_track_match.group(2)
        try:
            tracks = yandex_client.tracks([track_id])
            if tracks:
                track = tracks[0]
                artists = ", ".join([a.name for a in track.artists])
                track_names.append(f"{artists} - {track.title}")
                playlist_name_suggestion = f"{artists} - {track.title}"
            else:
                raise ValueError("Трек не найден в базе данных Яндекс.Музыки")
        except Exception as e:
            logger.error(f"Error fetching track {track_id}: {e}")
            raise ValueError(f"Ошибка при загрузке трека: {e}")
        return track_names, playlist_name_suggestion

    # 2. Match Album: https://music.yandex.ru/album/123
    album_match = re.search(r'/album/(\d+)', clean_url)
    if album_match:
        album_id = album_match.group(1)
        try:
            album = yandex_client.albums_with_tracks(album_id)
            if not album:
                raise ValueError("Альбом не найден")
            
            playlist_name_suggestion = f"{album.title}"
            if album.artists:
                playlist_name_suggestion = f"{album.artists[0].name} - {album.title}"
            
            for volume in album.volumes:
                for track in volume:
                    artists = ", ".join([a.name for a in track.artists])
                    track_names.append(f"{artists} - {track.title}")
        except Exception as e:
            logger.error(f"Error fetching album {album_id}: {e}")
            raise ValueError(f"Ошибка при загрузке альбома: {e}")
        return track_names, playlist_name_suggestion

    # 3. Match Liked Playlist: https://music.yandex.ru/users/([^/]+)/(tracks|liked|playlists/likes|playlists/3)
    liked_match = re.search(r'/users/([^/]+)/(tracks|liked|playlists/likes|playlists/3)', clean_url)
    if liked_match:
        if not yandex_token:
            raise ValueError("Для импорта плейлиста 'Мне нравится' необходимо авторизоваться в Яндекс.Музыке в панели настроек.")
        try:
            playlist_name_suggestion = "Мне нравится"
            tracks_list = yandex_client.users_likes_tracks()
            if not tracks_list or not tracks_list.tracks_ids:
                raise ValueError("Список любимых треков пуст или недоступен.")
            
            # Fetch track details in chunks
            for i in range(0, len(tracks_list.tracks_ids), 100):
                chunk_ids = tracks_list.tracks_ids[i:i+100]
                tracks = yandex_client.tracks(chunk_ids)
                for track in tracks:
                    if not track:
                        continue
                    artists = ", ".join([a.name for a in track.artists])
                    track_names.append(f"{artists} - {track.title}")
        except Exception as e:
            logger.error(f"Error fetching liked tracks: {e}")
            raise ValueError(f"Ошибка при загрузке 'Мне нравится': {e}")
        return track_names, playlist_name_suggestion

    # 4. Match Playlist:
    # - https://music.yandex.ru/users/([^/]+)/playlists/(\d+)
    # - https://music.yandex.ru/playlists/lk.([a-zA-Z0-9\-]+)
    playlist_match = re.search(r'/users/([^/]+)/playlists/(\d+)', clean_url)
    lk_match = re.search(r'/playlists/lk\.([a-zA-Z0-9\-]+)', clean_url)
    if playlist_match or lk_match:
        uid = None
        kind = None
        
        if playlist_match:
            username = playlist_match.group(1)
            kind = playlist_match.group(2)
            if username.isdigit():
                uid = int(username)
        
        # Fetch page HTML if we need to resolve uid/kind
        if not uid or not kind:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
            }
            try:
                res = requests.get(clean_url, headers=headers, timeout=10)
                if res.status_code == 200:
                    html = res.text
                    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
                    for s in scripts:
                        if "window.__STATE_PATCHES__" in s:
                            pushes = re.findall(r'\.push\(\s*(\[.*?\])\s*\)', s, re.DOTALL)
                            for push_str in pushes:
                                try:
                                    patches = json.loads(push_str)
                                    for p in patches:
                                        path = p.get("path", "")
                                        value = p.get("value")
                                        if path == "/playlist/meta" and isinstance(value, dict):
                                            uid = value.get("uid")
                                            kind = value.get("kind")
                                            playlist_name_suggestion = value.get("title", playlist_name_suggestion)
                                            break
                                except:
                                    pass
                    if not uid:
                        # Try raw regex fallback in HTML
                        uid_match = re.search(r'"uid":\s*(\d+)', res.text)
                        kind_match = re.search(r'"kind":\s*(\d+)', res.text)
                        if uid_match and kind_match:
                            uid = int(uid_match.group(1))
                            kind = int(kind_match.group(1))
            except Exception as e:
                logger.error(f"Error scraping playlist HTML: {e}")
                raise ValueError(f"Не удалось получить доступ к веб-странице плейлиста: {e}")
                
        if not uid or not kind:
            raise ValueError("Не удалось извлечь ID владельца или плейлиста. Убедитесь, что это публичный плейлист.")
            
        try:
            playlist = yandex_client.users_playlists(kind, uid)
            if not playlist:
                raise ValueError("Плейлист не найден")
            playlist_name_suggestion = playlist.title or playlist_name_suggestion
            tracks = playlist.fetch_tracks()
            for item in tracks:
                track = item.track
                if not track:
                    continue
                artists = ", ".join([a.name for a in track.artists])
                track_names.append(f"{artists} - {track.title}")
        except Exception as e:
            logger.error(f"Error fetching playlist kind={kind} uid={uid}: {e}")
            raise ValueError(f"Ошибка при загрузке плейлиста: {e}. Возможно, плейлист является приватным.")
            
        return track_names, playlist_name_suggestion

    # 4. Match Artist: https://music.yandex.ru/artist/123
    artist_match = re.search(r'/artist/(\d+)', clean_url)
    if artist_match:
        artist_id = artist_match.group(1)
        try:
            info = yandex_client.artists_brief_info(artist_id)
            if not info or not info.artist:
                raise ValueError("Исполнитель не найден")
            playlist_name_suggestion = f"Лучшее: {info.artist.name}"
            if info.popular_tracks:
                for track in info.popular_tracks:
                    artists = ", ".join([a.name for a in track.artists])
                    track_names.append(f"{artists} - {track.title}")
        except Exception as e:
            logger.error(f"Error fetching artist {artist_id}: {e}")
            raise ValueError(f"Ошибка при загрузке исполнителя: {e}")
        return track_names, playlist_name_suggestion

    raise ValueError("Неподдерживаемый формат ссылки Яндекс.Музыки. Пожалуйста, укажите ссылку на плейлист, альбом, трек или исполнителя.")

@app.post("/api/tracks/parse-url")
def parse_tracks_url(data: UrlParseRequest, request: Request, response: Response):
    session = get_or_create_session(request, response)
    try:
        tracks, playlist_name = parse_yandex_music_url(data.url, session)
        return {"tracks": tracks, "playlist_name": playlist_name}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Internal error parsing URL {data.url}: {e}")
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка сервера: {str(e)}")

@app.post("/api/search/single")
async def search_single(data: SingleSearchQuery, request: Request, response: Response):
    session = get_or_create_session(request, response)
    results = search_tracks_rich_multi(make_qobuz_client(session), data.query)
    return {"results": results}

@app.get("/api/qobuz/playlists")
async def get_qobuz_playlists(request: Request, response: Response):
    session = get_or_create_session(request, response)
    try:
        qobuz_client, _profile = ensure_qobuz_authorized(session)
            
        params = {"limit": 100}
        res = qobuz_client._request("playlist/getUserPlaylists", params)
        if "playlists" in res and "items" in res["playlists"]:
            items = []
            for item in res["playlists"]["items"]:
                items.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "tracks_count": item.get("tracks_count")
                })
            return {"playlists": items}
        return {"playlists": []}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching user playlists: {e}")
        raise HTTPException(status_code=500, detail=f"Не удалось получить список плейлистов: {str(e)}")

def get_playlist_track_ids(cl: QobuzDirect, playlist_id: str) -> List[int]:
    all_track_ids = []
    offset = 0
    limit = 500
    while True:
        try:
            params = {
                "playlist_id": playlist_id,
                "extra": "tracks",
                "limit": limit,
                "offset": offset
            }
            res = cl._request("playlist/get", params)
            if "tracks" in res and "items" in res["tracks"]:
                items = res["tracks"]["items"]
                if not items:
                    break
                for t in items:
                    tid = t.get("id")
                    if tid:
                        all_track_ids.append(int(tid))
                if len(items) < limit:
                    break
                offset += limit
            else:
                break
        except Exception as e:
            logger.error(f"Error fetching tracks for playlist {playlist_id}: {e}")
            break
    return all_track_ids

@app.post("/api/playlist/create")
async def create_playlist(data: PlaylistData, request: Request, response: Response):
    if not data.track_ids:
        raise HTTPException(status_code=400, detail="Список ID треков пуст")
    
    session = get_or_create_session(request, response)
    qobuz_client, profile = ensure_qobuz_authorized(session)
    account = {
        "display_name": profile.get("display_name"),
        "id": profile.get("id"),
    }
    
    playlist_id = data.playlist_id
    if not playlist_id:
        if not data.name:
            raise HTTPException(status_code=400, detail="Название плейлиста не указано")
        playlist_id = qobuz_client.create_playlist(data.name)
        if not playlist_id:
            raise HTTPException(status_code=500, detail="Не удалось создать плейлист в Qobuz")
        is_new = True
    else:
        is_new = False
        # Fetch existing tracks in Qobuz playlist to skip duplicates (Synchronization)
        existing_ids = get_playlist_track_ids(qobuz_client, playlist_id)
        existing_set = set(existing_ids)
        new_track_ids = [tid for tid in data.track_ids if tid not in existing_set]
        
        if not new_track_ids:
            return {
                "status": "success",
                "playlist_id": playlist_id,
                "count": 0,
                "is_new": False,
                "account": account,
                "detail": "Все треки уже есть в плейлисте (новых треков нет)"
            }
            
        data.track_ids = new_track_ids
        
    success = True
    for i in range(0, len(data.track_ids), 100):
        chunk = data.track_ids[i:i+100]
        if not qobuz_client.add_tracks_to_playlist(playlist_id, chunk):
            success = False
            
    if success:
        return {"status": "success", "playlist_id": playlist_id, "count": len(data.track_ids), "is_new": is_new, "account": account}
    else:
        status_name = "partial_success"
        detail = "Часть треков не удалось добавить"
        return {"status": status_name, "playlist_id": playlist_id, "detail": detail, "is_new": is_new, "account": account}

@app.websocket("/api/ws/match")
async def websocket_match(websocket: WebSocket):
    await websocket.accept()
    try:
        session = get_websocket_session(websocket)
        data = await websocket.receive_text()
        req = json.loads(data)
        queries = req.get("tracks", [])
        
        total = len(queries)
        matched_count = 0
        processed_count = 0
        
        # Ограничиваем параллельность запросов к API Qobuz
        concurrency = max(1, min(MATCH_CONCURRENCY, total or 1))
        sem = asyncio.Semaphore(concurrency)
        
        async def worker(idx, query):
            async with sem:
                # Выполняем блокирующий поиск в отдельном потоке
                track_info = await asyncio.to_thread(search_track_rich_thread, query, session)
                return idx, query, track_info
                
        # Создаем все задачи параллельно
        tasks = [asyncio.create_task(worker(idx, q)) for idx, q in enumerate(queries)]
        
        try:
            # Отправляем результаты по мере завершения, чтобы один медленный трек не держал весь прогресс
            for task in asyncio.as_completed(tasks):
                idx, query, track_info = await task
                processed_count += 1
                if track_info:
                    matched_count += 1
                    status = "found"
                else:
                    status = "not_found"
                    
                await websocket.send_json({
                    "type": "progress",
                    "index": idx,
                    "processed": processed_count,
                    "query": query,
                    "status": status,
                    "match": track_info
                })
        except Exception as ex:
            # Отменяем все незавершенные задачи при обрыве соединения или ошибке
            for task in tasks:
                if not task.done():
                    task.cancel()
            if processed_count:
                await asyncio.to_thread(save_search_cache)
            raise ex
            
        await websocket.send_json({
            "type": "done",
            "total": total,
            "matched": matched_count
        })
        if processed_count:
            await asyncio.to_thread(save_search_cache)
    except WebSocketDisconnect:
        logger.info("Пользователь отключился от WebSocket сопоставления")
    except Exception as e:
        logger.error(f"Ошибка WebSocket: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass

@app.post("/api/auth/yandex-logout")
def yandex_logout(request: Request, response: Response):
    session = get_or_create_session(request, response)
    update_session_values(session["id"], {"yandex_token": None})
    return {"status": "success"}

@app.post("/api/tracks/yandex-liked")
def parse_yandex_liked(request: Request, response: Response):
    session = get_or_create_session(request, response)
    yandex_token = session.get("yandex_token") or ""
    if not yandex_token:
        raise HTTPException(status_code=400, detail="Для импорта 'Мне нравится' необходимо авторизоваться в Яндекс.Музыке.")
    try:
        yandex_client = YandexMusicClient(yandex_token).init()
        tracks_list = yandex_client.users_likes_tracks()
        if not tracks_list or not tracks_list.tracks_ids:
            return {"tracks": [], "playlist_name": "Мне нравится"}
            
        track_names = []
        for i in range(0, len(tracks_list.tracks_ids), 100):
            chunk_ids = tracks_list.tracks_ids[i:i+100]
            tracks = yandex_client.tracks(chunk_ids)
            for track in tracks:
                if not track:
                    continue
                artists = ", ".join([a.name for a in track.artists])
                track_names.append(f"{artists} - {track.title}")
                
        return {"tracks": track_names, "playlist_name": "Мне нравится"}
    except Exception as e:
        logger.error(f"Failed to fetch liked tracks directly: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка при загрузке треков: {str(e)}")

thread_pool_executor = ThreadPoolExecutor(max_workers=4)

@app.websocket("/api/ws/yandex-auth")
async def websocket_yandex_auth(websocket: WebSocket):
    await websocket.accept()
    try:
        session = get_websocket_session(websocket)
    except WebSocketDisconnect:
        await websocket.send_json({"type": "error", "message": "Сессия не найдена. Обновите страницу и попробуйте снова."})
        return
    loop = asyncio.get_running_loop()
    
    auth_finished = asyncio.Event()
    auth_result = {}

    def run_auth():
        try:
            cl = YandexMusicClient()
            
            def on_code(code_obj):
                async def send_code():
                    try:
                        await websocket.send_json({
                            "type": "code",
                            "verification_url": code_obj.verification_url,
                            "user_code": code_obj.user_code
                        })
                    except Exception as e:
                        logger.error(f"Failed to send code via websocket: {e}")
                asyncio.run_coroutine_threadsafe(send_code(), loop)
                
            token = cl.device_auth(on_code=on_code)
            auth_result["token"] = token.access_token
            auth_result["status"] = "success"
        except Exception as e:
            logger.error(f"Error in device_auth: {e}")
            auth_result["status"] = "error"
            auth_result["error"] = str(e)
        finally:
            loop.call_soon_threadsafe(auth_finished.set)

    # Run device_auth in threadpool
    loop.run_in_executor(thread_pool_executor, run_auth)
    
    # Wait for authentication to finish
    await auth_finished.wait()
    
    if auth_result.get("status") == "success":
        token = auth_result["token"]
        try:
            update_session_values(session["id"], {"yandex_token": token})
            
            # Check user login profile
            ya_client = YandexMusicClient(token).init()
            display_name = ya_client.me.account.display_name or ya_client.me.account.login
            uid = ya_client.me.account.uid
            
            await websocket.send_json({
                "type": "success",
                "display_name": display_name,
                "uid": uid
            })
        except Exception as e:
            await websocket.send_json({
                "type": "error",
                "message": f"Ошибка сохранения токена: {str(e)}"
            })
    else:
        await websocket.send_json({
            "type": "error",
            "message": auth_result.get("error", "Неизвестная ошибка авторизации")
        })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
