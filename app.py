import os
import time
import json
import logging
import re
import base64
import hmac
import requests
import asyncio
import threading
import subprocess
import sys
import secrets
import sqlite3
import hashlib
import html
from urllib.parse import urlencode, urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from yandex_music import Client as YandexMusicClient

# Импортируем оригинальные классы и переменные из main.py
from main import QobuzDirect
from music_services import PlaylistRef, ResolvedTrack, SERVICE_CATALOG, ServiceProfile, TransferResult

app = FastAPI(title="Qobuz Playlist Importer")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qobuz_web")

load_dotenv(override=False)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_COOKIE_NAME = "qsync_sid"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 90
SESSION_COOKIE_SECURE = os.getenv("QSYNC_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}
DB_FILE = os.getenv("QSYNC_DB_PATH", "qobuzsync.db")
LOGIN_PROFILE_ROOT = os.getenv("QSYNC_LOGIN_PROFILE_DIR") or os.path.join(APP_DIR, ".qobuz_login_profiles")
BROWSER_LOGIN_ENABLED = os.getenv("QSYNC_BROWSER_LOGIN_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
SERVER_DEFAULT_APP_ID = os.getenv("QOBUZ_APP_ID") or "30650571"
SERVER_DEFAULT_APP_SECRET = os.getenv("QOBUZ_APP_SECRET") or ""
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "").strip()
SPOTIFY_SCOPES = "user-read-private user-read-email playlist-modify-public playlist-modify-private playlist-read-private"
SECRET_KEY_FILE = os.getenv("QSYNC_SECRET_KEY_FILE")
if not SECRET_KEY_FILE:
    db_dir = os.path.dirname(os.path.abspath(DB_FILE)) or APP_DIR
    SECRET_KEY_FILE = os.path.join(db_dir, ".qsync_secret")
MAX_UPLOAD_BYTES = int(os.getenv("QSYNC_MAX_UPLOAD_BYTES", str(1024 * 1024)))
MAX_TRACKS_PER_REQUEST = int(os.getenv("QSYNC_MAX_TRACKS", "2000"))
MAX_TRACK_NAME_LENGTH = int(os.getenv("QSYNC_MAX_TRACK_NAME_LENGTH", "300"))
MAX_PLAYLIST_NAME_LENGTH = int(os.getenv("QSYNC_MAX_PLAYLIST_NAME_LENGTH", "120"))
MAX_SEARCH_QUERY_LENGTH = int(os.getenv("QSYNC_MAX_SEARCH_QUERY_LENGTH", "300"))
MAX_YANDEX_URL_LENGTH = int(os.getenv("QSYNC_MAX_YANDEX_URL_LENGTH", "2048"))
SEARCH_CACHE_POSITIVE_TTL_SECONDS = int(os.getenv("QSYNC_SEARCH_CACHE_TTL", str(60 * 60 * 24 * 30)))
SEARCH_CACHE_NEGATIVE_TTL_SECONDS = int(os.getenv("QSYNC_SEARCH_NEGATIVE_CACHE_TTL", str(60 * 60)))
db_lock = threading.Lock()
rate_lock = threading.Lock()
rate_buckets = {}
SENSITIVE_SESSION_FIELDS = {"qobuz_token", "qobuz_app_secret", "yandex_token"}
SECRET_PREFIX = "enc:v1:"
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

def get_secret_key_material() -> bytes:
    env_key = os.getenv("QSYNC_SECRET_KEY")
    if env_key:
        return env_key.encode("utf-8")

    key_dir = os.path.dirname(os.path.abspath(SECRET_KEY_FILE))
    os.makedirs(key_dir, mode=0o700, exist_ok=True)
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "rb") as f:
            return f.read().strip()

    key = secrets.token_urlsafe(48).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(SECRET_KEY_FILE, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.write(b"\n")
    finally:
        try:
            os.chmod(SECRET_KEY_FILE, 0o600)
        except OSError:
            pass
    logger.warning(
        "QSYNC_SECRET_KEY is not set; generated local encryption key file at %s",
        SECRET_KEY_FILE,
    )
    return key

SECRET_KEY = hashlib.sha256(get_secret_key_material()).digest()

def make_hmac(message: bytes) -> bytes:
    return hmac.new(SECRET_KEY, message, hashlib.sha256).digest()

def xor_stream(data: bytes, nonce: bytes) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < len(data):
        block = make_hmac(b"stream:" + nonce + counter.to_bytes(4, "big"))
        output.extend(block)
        counter += 1
    return bytes(value ^ output[idx] for idx, value in enumerate(data))

def encrypt_secret(value):
    if value is None or value == "":
        return value
    if isinstance(value, str) and value.startswith(SECRET_PREFIX):
        return value
    raw = str(value).encode("utf-8")
    nonce = secrets.token_bytes(16)
    ciphertext = xor_stream(raw, nonce)
    tag = make_hmac(b"mac:" + nonce + ciphertext)
    payload = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")
    return SECRET_PREFIX + payload

def decrypt_secret(value):
    if value is None or value == "" or not isinstance(value, str):
        return value
    if not value.startswith(SECRET_PREFIX):
        return value
    try:
        payload = base64.urlsafe_b64decode(value[len(SECRET_PREFIX):].encode("ascii"))
        nonce, tag, ciphertext = payload[:16], payload[16:48], payload[48:]
        expected = make_hmac(b"mac:" + nonce + ciphertext)
        if not hmac.compare_digest(tag, expected):
            logger.error("Encrypted session value failed integrity check")
            return ""
        return xor_stream(ciphertext, nonce).decode("utf-8")
    except Exception as exc:
        logger.error("Failed to decrypt session value: %s", exc)
        return ""

def decrypt_session_row(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return row
    row = dict(row)
    for field in SENSITIVE_SESSION_FIELDS:
        row[field] = decrypt_secret(row.get(field))
    return row

def init_db():
    db_dir = os.path.dirname(os.path.abspath(DB_FILE))
    if db_dir:
        os.makedirs(db_dir, mode=0o700, exist_ok=True)
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS service_connections (
                    session_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    credentials TEXT,
                    profile_json TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (session_id, service)
                )
            """)
            conn.commit()
    cleanup_expired_sessions()

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
        (session_id, SERVER_DEFAULT_APP_ID, encrypt_secret(SERVER_DEFAULT_APP_SECRET), now, now),
    )
    return session_id

def delete_session(session_id: str):
    if session_id:
        db_execute("DELETE FROM service_connections WHERE session_id = ?", (session_id,))
        db_execute("DELETE FROM sessions WHERE id = ?", (session_id,))

def cleanup_expired_sessions():
    cutoff = int(time.time()) - SESSION_TTL_SECONDS
    db_execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
    db_execute("DELETE FROM service_connections WHERE session_id NOT IN (SELECT id FROM sessions)")

def get_session(session_id: str) -> Optional[dict]:
    if not session_id:
        return None
    row = db_execute("SELECT * FROM sessions WHERE id = ?", (session_id,), fetchone=True)
    if not row:
        return None
    if int(row.get("updated_at") or 0) < int(time.time()) - SESSION_TTL_SECONDS:
        delete_session(session_id)
        return None
    decrypted = decrypt_session_row(row)
    plaintext_updates = {
        field: decrypted.get(field)
        for field in SENSITIVE_SESSION_FIELDS
        if row.get(field) and not str(row.get(field)).startswith(SECRET_PREFIX)
    }
    if plaintext_updates:
        update_session_values(session_id, plaintext_updates)
    return decrypted

def encode_credentials(credentials: dict) -> str:
    return encrypt_secret(json.dumps(credentials or {}, ensure_ascii=False))

def decode_credentials(value: Optional[str]) -> dict:
    if not value:
        return {}
    raw = decrypt_secret(value)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.error("Failed to decode service credentials JSON")
        return {}

def normalize_profile(profile: Optional[dict]) -> dict:
    return profile if isinstance(profile, dict) else {}

def upsert_service_connection(session_id: str, service: str, credentials: dict, profile: Optional[dict] = None):
    if not session_id or not service:
        return
    now = int(time.time())
    existing = db_execute(
        "SELECT created_at FROM service_connections WHERE session_id = ? AND service = ?",
        (session_id, service),
        fetchone=True,
    )
    created_at = int(existing["created_at"]) if existing else now
    db_execute(
        """
        INSERT OR REPLACE INTO service_connections (
            session_id, service, credentials, profile_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            service,
            encode_credentials(credentials),
            json.dumps(normalize_profile(profile), ensure_ascii=False),
            created_at,
            now,
        ),
    )

def delete_service_connection(session_id: str, service: str):
    db_execute(
        "DELETE FROM service_connections WHERE session_id = ? AND service = ?",
        (session_id, service),
    )

def get_service_connection(session_id: str, service: str) -> Optional[dict]:
    row = db_execute(
        "SELECT * FROM service_connections WHERE session_id = ? AND service = ?",
        (session_id, service),
        fetchone=True,
    )
    if not row:
        return None
    try:
        profile = json.loads(row.get("profile_json") or "{}")
    except json.JSONDecodeError:
        profile = {}
    return {
        "session_id": row["session_id"],
        "service": row["service"],
        "credentials": decode_credentials(row.get("credentials")),
        "profile": normalize_profile(profile),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

def legacy_qobuz_credentials(session: dict) -> dict:
    return {
        "token": session.get("qobuz_token") or "",
        "app_id": session.get("qobuz_app_id") or SERVER_DEFAULT_APP_ID,
        "app_secret": session.get("qobuz_app_secret") or SERVER_DEFAULT_APP_SECRET,
        "working_app_id": session.get("qobuz_working_app_id"),
    }

def get_service_credentials(session: dict, service: str) -> dict:
    connection = get_service_connection(session["id"], service) if session and session.get("id") else None
    if connection:
        return connection["credentials"]
    if service == "qobuz":
        return legacy_qobuz_credentials(session)
    if service == "yandex":
        return {"token": session.get("yandex_token") or ""}
    return {}

def migrate_legacy_session_connections(session: Optional[dict]):
    if not session or not session.get("id"):
        return
    session_id = session["id"]
    if session.get("qobuz_token") and not get_service_connection(session_id, "qobuz"):
        upsert_service_connection(session_id, "qobuz", legacy_qobuz_credentials(session), {})
    if session.get("yandex_token") and not get_service_connection(session_id, "yandex"):
        upsert_service_connection(session_id, "yandex", {"token": session.get("yandex_token")}, {})

def sync_legacy_connections_from_session(session_id: str):
    row = db_execute("SELECT * FROM sessions WHERE id = ?", (session_id,), fetchone=True)
    session = decrypt_session_row(row)
    if not session:
        return
    if session.get("qobuz_token"):
        upsert_service_connection(session_id, "qobuz", legacy_qobuz_credentials(session), {})
    else:
        delete_service_connection(session_id, "qobuz")
    if session.get("yandex_token"):
        upsert_service_connection(session_id, "yandex", {"token": session.get("yandex_token")}, {})
    else:
        delete_service_connection(session_id, "yandex")

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
    migrate_legacy_session_connections(session)
    return session

def get_websocket_session(websocket: WebSocket) -> dict:
    session_id = websocket.cookies.get(SESSION_COOKIE_NAME)
    session = get_session(session_id)
    if not session:
        raise WebSocketDisconnect(code=4401)
    return session

RATE_LIMIT_RULES = {
    "qobuz_login": (5, 600),
    "browser_login": (1, 600),
    "config_save": (20, 300),
    "parse_tracks": (30, 300),
    "parse_url": (15, 300),
    "manual_search": (120, 300),
    "playlist_read": (60, 300),
    "playlist_write": (20, 300),
    "match_ws": (10, 300),
    "yandex_auth_ws": (5, 600),
    "yandex_liked": (15, 300),
    "service_connect": (20, 300),
    "import_source": (30, 300),
    "match_http": (10, 300),
}

def client_host_from_request(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"

def client_host_from_websocket(websocket: WebSocket) -> str:
    forwarded_for = websocket.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return websocket.client.host if websocket.client else "unknown"

def check_rate_limit(scope: str, identity: str):
    limit, window = RATE_LIMIT_RULES[scope]
    now = time.monotonic()
    key = (scope, identity)
    with rate_lock:
        bucket = [ts for ts in rate_buckets.get(key, []) if now - ts < window]
        if len(bucket) >= limit:
            retry_after = max(1, int(window - (now - bucket[0])))
            raise HTTPException(
                status_code=429,
                detail=f"Слишком много запросов. Попробуйте через {retry_after} сек.",
            )
        bucket.append(now)
        rate_buckets[key] = bucket

def check_ws_rate_limit(scope: str, identity: str):
    try:
        check_rate_limit(scope, identity)
    except HTTPException as exc:
        return exc.detail
    return None

def rate_identity(request: Request, session: Optional[dict] = None) -> str:
    if session and session.get("id"):
        return f"sid:{session['id']}"
    cookie_sid = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_sid:
        return f"sid:{cookie_sid}"
    return f"ip:{client_host_from_request(request)}"

def ws_rate_identity(websocket: WebSocket, session: Optional[dict] = None) -> str:
    if session and session.get("id"):
        return f"sid:{session['id']}"
    cookie_sid = websocket.cookies.get(SESSION_COOKIE_NAME)
    if cookie_sid:
        return f"sid:{cookie_sid}"
    return f"ip:{client_host_from_websocket(websocket)}"

def enforce_http_rate_limit(scope: str, request: Request, session: Optional[dict] = None):
    check_rate_limit(scope, rate_identity(request, session))

def normalize_track_names(lines: List[str]) -> List[str]:
    track_names = []
    for line in lines:
        track = str(line).strip()
        if not track:
            continue
        if len(track) > MAX_TRACK_NAME_LENGTH:
            raise ValueError(f"Название трека длиннее {MAX_TRACK_NAME_LENGTH} символов")
        track_names.append(track)
        if len(track_names) > MAX_TRACKS_PER_REQUEST:
            raise ValueError(f"Слишком много треков. Максимум: {MAX_TRACKS_PER_REQUEST}")
    return track_names

def yandex_cover_url(track) -> Optional[str]:
    cover_uri = getattr(track, "cover_uri", None)
    if not cover_uri:
        albums = getattr(track, "albums", None) or []
        if albums:
            cover_uri = getattr(albums[0], "cover_uri", None) or getattr(albums[0], "og_image", None)
    if not cover_uri:
        cover_uri = getattr(track, "og_image", None)
    if not cover_uri:
        return None
    cover_uri = str(cover_uri).replace("%%", "200x200")
    if cover_uri.startswith("//"):
        return f"https:{cover_uri}"
    if cover_uri.startswith("http://"):
        return "https://" + cover_uri[len("http://"):]
    if cover_uri.startswith("https://"):
        return cover_uri
    return f"https://{cover_uri}"

def yandex_track_item(track) -> dict:
    artists = ", ".join([artist.name for artist in getattr(track, "artists", []) if getattr(artist, "name", None)])
    title = getattr(track, "title", "") or ""
    albums = getattr(track, "albums", None) or []
    album = getattr(albums[0], "title", "") if albums else ""
    query = f"{artists} - {title}" if artists else title
    return {
        "query": query,
        "title": title,
        "artist": artists,
        "album": album,
        "cover": yandex_cover_url(track),
    }

def normalize_track_items(items, fallback_tracks: Optional[List[str]] = None) -> List[dict]:
    source_items = items or [{"query": query} for query in (fallback_tracks or [])]
    normalized = []
    for item in source_items:
        if not isinstance(item, dict):
            item = {"query": str(item)}
        query = (item.get("query") or "").strip()
        if not query:
            title = (item.get("title") or "").strip()
            artist = (item.get("artist") or "").strip()
            query = f"{artist} - {title}" if artist and title else title or artist
        if not query:
            continue
        query = normalize_track_names([query])[0]
        normalized.append({
            "query": query,
            "title": (item.get("title") or "").strip(),
            "artist": (item.get("artist") or "").strip(),
            "album": (item.get("album") or "").strip(),
            "cover": item.get("cover"),
        })
        if len(normalized) > MAX_TRACKS_PER_REQUEST:
            raise ValueError(f"Слишком много треков. Максимум: {MAX_TRACKS_PER_REQUEST}")
    return normalized

def validate_query_text(query: str) -> str:
    query = (query or "").strip()
    if not query:
        raise ValueError("Пустой поисковый запрос")
    if len(query) > MAX_SEARCH_QUERY_LENGTH:
        raise ValueError(f"Поисковый запрос длиннее {MAX_SEARCH_QUERY_LENGTH} символов")
    return query

def validate_playlist_name(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    name = name.strip()
    if len(name) > MAX_PLAYLIST_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Название плейлиста длиннее {MAX_PLAYLIST_NAME_LENGTH} символов",
        )
    return name

def normalize_destination_track_id(service: str, track_id) -> Optional[str]:
    value = str(track_id or "").strip()
    if not value:
        return None
    if service == "qobuz":
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return None
        return str(numeric) if numeric > 0 else None
    if service == "spotify":
        prefix = "spotify:track:"
        if value.startswith(prefix):
            return value[len(prefix):]
        return value
    return value

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
    values = [
        encrypt_secret(updates[field]) if field in SENSITIVE_SESSION_FIELDS else updates[field]
        for field in fields
    ]
    values.extend([int(time.time()), session_id])
    db_execute(f"UPDATE sessions SET {assignments}, updated_at = ? WHERE id = ?", values)
    if {"qobuz_token", "qobuz_app_id", "qobuz_app_secret", "qobuz_working_app_id", "yandex_token"} & set(fields):
        sync_legacy_connections_from_session(session_id)

def make_qobuz_client(session: dict) -> QobuzDirect:
    credentials = get_service_credentials(session, "qobuz")
    app_id = credentials.get("working_app_id") or credentials.get("app_id") or SERVER_DEFAULT_APP_ID
    app_secret = credentials.get("app_secret") or SERVER_DEFAULT_APP_SECRET
    return QobuzDirect(credentials.get("token") or "", app_id, app_secret)

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
    if not cl.auth_token:
        return {"authorized": False}

    known_app_ids = list(preferred_app_ids or []) + get_qobuz_web_app_ids() + QOBUZ_APP_ID_CANDIDATES
    # Убираем дубликаты с сохранением порядка
    known_app_ids = unique_values(known_app_ids)
    
    for app_id in known_app_ids:
        try:
            data = cl._request("user/get", current_app_id=app_id, quiet_errors=True)
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
    credentials = get_service_credentials(session, "qobuz")
    profile = get_qobuz_profile(qobuz_client, [
        credentials.get("working_app_id"),
        credentials.get("app_id"),
    ])
    if not profile.get("authorized"):
        raise HTTPException(
            status_code=401,
            detail="Qobuz аккаунт не выбран. Вставьте token своего Qobuz аккаунта и сохраните настройки.",
        )
    if profile.get("app_id") != credentials.get("working_app_id"):
        update_session_values(session["id"], {"qobuz_working_app_id": profile["app_id"]})
        credentials["working_app_id"] = profile["app_id"]
    upsert_service_connection(session["id"], "qobuz", credentials, profile)
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
    credentials = get_service_credentials(session, "qobuz")
    app_id_to_use = credentials.get("working_app_id") or credentials.get("app_id") or SERVER_DEFAULT_APP_ID
    config_key = (
        credentials.get("token") or "",
        app_id_to_use,
        credentials.get("app_secret") or SERVER_DEFAULT_APP_SECRET,
    )
    cached_key = getattr(thread_local, "qobuz_config_key", None)
    cached_client = getattr(thread_local, "qobuz_client", None)

    if cached_client is None or cached_key != config_key:
        cached_client = QobuzDirect(*config_key)
        thread_local.qobuz_client = cached_client
        thread_local.qobuz_config_key = config_key

    return cached_client

def qobuz_cache_scope(session: dict) -> str:
    credentials = get_service_credentials(session, "qobuz")
    app_id = credentials.get("working_app_id") or credentials.get("app_id") or SERVER_DEFAULT_APP_ID
    token = credentials.get("token") or ""
    return hashlib.sha256(f"{app_id}:{token}".encode("utf-8")).hexdigest()

def search_cache_key(query: str, session: dict) -> str:
    return f"{qobuz_cache_scope(session)}:{query.lower().strip()}"

def get_cached_search_result(cache_key: str):
    now = int(time.time())
    with cache_lock:
        entry = search_cache.get(cache_key)
        if not isinstance(entry, dict) or "cached_at" not in entry:
            return None, False
        result = entry.get("result")
        ttl = SEARCH_CACHE_NEGATIVE_TTL_SECONDS if result is None else SEARCH_CACHE_POSITIVE_TTL_SECONDS
        if now - int(entry.get("cached_at") or 0) > ttl:
            search_cache.pop(cache_key, None)
            return None, False
        return result, True

def set_cached_search_result(cache_key: str, result):
    with cache_lock:
        search_cache[cache_key] = {
            "cached_at": int(time.time()),
            "result": result,
        }

def search_track_rich_thread(query: str, session: dict) -> Optional[dict]:
    return search_track_rich(get_thread_qobuz_client(session), query, session)

def search_track_rich(cl: QobuzDirect, query: str, session: dict) -> Optional[dict]:
    query_key = search_cache_key(query, session)
    cached_result, found = get_cached_search_result(query_key)
    if found:
        return cached_result

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
        
        set_cached_search_result(query_key, track_info)
            
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

def service_catalog_with_runtime_status():
    services = []
    for service in SERVICE_CATALOG:
        item = dict(service)
        if item["id"] == "spotify" and not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
            item["enabled"] = False
            item["status"] = "needs_config"
            item["note"] = "Нужно задать SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET в .env."
        services.append(item)
    return services

def service_meta(service_id: str) -> Optional[dict]:
    return next((item for item in service_catalog_with_runtime_status() if item["id"] == service_id), None)

def ensure_service_enabled(service_id: str, role: Optional[str] = None):
    meta = service_meta(service_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Сервис не найден")
    if role and role not in meta.get("roles", []):
        raise HTTPException(status_code=400, detail=f"Сервис {meta['name']} не поддерживает выбранную роль")
    if not meta.get("enabled"):
        raise HTTPException(status_code=400, detail=meta.get("note") or "Сервис пока недоступен")
    return meta

class ManualSourceAdapter:
    service_id = "manual"

    def auth_status(self, session: dict) -> ServiceProfile:
        return ServiceProfile(True, self.service_id, display_name="Ручной список", id="manual")

    def read_tracks(self, session: dict, payload: dict) -> dict:
        raw_tracks = payload.get("tracks")
        if raw_tracks is not None:
            if not isinstance(raw_tracks, list):
                raise ValueError("Некорректный список треков")
            tracks = normalize_track_names(raw_tracks)
        else:
            text = payload.get("text") or ""
            if len(text.encode("utf-8")) > MAX_UPLOAD_BYTES:
                raise ValueError(f"Текст слишком большой. Максимум: {MAX_UPLOAD_BYTES} байт")
            tracks = normalize_track_names(text.splitlines())
        return {"tracks": tracks, "playlist_name": payload.get("playlist_name") or "Импортированный список"}

class YandexAdapter:
    service_id = "yandex"

    def auth_status(self, session: dict) -> ServiceProfile:
        token = get_service_credentials(session, self.service_id).get("token") or ""
        profile = get_yandex_profile(token)
        return ServiceProfile(
            bool(profile.get("authorized")),
            self.service_id,
            display_name=profile.get("display_name"),
            id=str(profile.get("uid")) if profile.get("uid") else None,
            extra=profile,
        )

    def connect(self, session: dict, payload: dict) -> dict:
        token = (payload.get("token") or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="Передайте Yandex token или используйте device-code вход")
        profile = get_yandex_profile(token)
        if not profile.get("authorized"):
            raise HTTPException(status_code=401, detail="Yandex token не прошел проверку")
        update_session_values(session["id"], {"yandex_token": token})
        upsert_service_connection(session["id"], self.service_id, {"token": token}, profile)
        return {"status": "success", "profile": profile}

    def read_tracks(self, session: dict, payload: dict) -> dict:
        input_type = payload.get("input_type") or "url"
        if input_type == "liked":
            tracks, playlist_name, track_items = read_yandex_liked_tracks(session)
            return {"tracks": tracks, "playlist_name": playlist_name, "track_items": track_items}
        if input_type == "url":
            url = payload.get("url") or ""
            if len(url) > MAX_YANDEX_URL_LENGTH:
                raise ValueError(f"Ссылка длиннее {MAX_YANDEX_URL_LENGTH} символов")
            tracks, playlist_name, track_items = parse_yandex_music_url(url, session, include_items=True)
            return {"tracks": normalize_track_names(tracks), "playlist_name": playlist_name, "track_items": track_items}
        return ManualSourceAdapter().read_tracks(session, payload)

class QobuzAdapter:
    service_id = "qobuz"

    def auth_status(self, session: dict) -> ServiceProfile:
        credentials = get_service_credentials(session, self.service_id)
        if not credentials.get("token"):
            return ServiceProfile(False, self.service_id)
        qobuz_client = make_qobuz_client(session)
        profile = get_qobuz_profile(qobuz_client, [
            credentials.get("working_app_id"),
            credentials.get("app_id"),
        ])
        if profile.get("authorized"):
            credentials["working_app_id"] = profile.get("app_id")
            upsert_service_connection(session["id"], self.service_id, credentials, profile)
            update_session_values(session["id"], {"qobuz_working_app_id": profile.get("app_id")})
        return ServiceProfile(
            bool(profile.get("authorized")),
            self.service_id,
            display_name=profile.get("display_name"),
            id=str(profile.get("id")) if profile.get("id") else None,
            extra=profile,
        )

    def connect(self, session: dict, payload: dict) -> dict:
        method = payload.get("method") or "token"
        app_id = (payload.get("app_id") or get_service_credentials(session, self.service_id).get("app_id") or SERVER_DEFAULT_APP_ID).strip()
        app_secret = (payload.get("app_secret") or get_service_credentials(session, self.service_id).get("app_secret") or SERVER_DEFAULT_APP_SECRET).strip()

        if method == "password":
            email = (payload.get("email") or "").strip()
            password = payload.get("password") or ""
            if not email or not password or len(email) > 254 or len(password) > 256:
                raise HTTPException(status_code=400, detail="Введите email и пароль Qobuz")
            password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
            app_ids = unique_values([app_id] + get_qobuz_web_app_ids() + QOBUZ_APP_ID_CANDIDATES)
            last_error = None
            for candidate_app_id in app_ids:
                qobuz_client = QobuzDirect("", candidate_app_id, app_secret)
                login_result = qobuz_client.login(email, password_hash, candidate_app_id, True)
                if isinstance(login_result, dict) and login_result.get("user_auth_token"):
                    token = login_result["user_auth_token"]
                    credentials = {
                        "token": token,
                        "app_id": candidate_app_id,
                        "app_secret": app_secret,
                        "working_app_id": candidate_app_id,
                    }
                    update_session_values(session["id"], {
                        "qobuz_token": token,
                        "qobuz_app_id": candidate_app_id,
                        "qobuz_app_secret": app_secret,
                        "qobuz_working_app_id": candidate_app_id,
                    })
                    profile = self.auth_status(get_session(session["id"])).to_dict()
                    upsert_service_connection(session["id"], self.service_id, credentials, profile)
                    return {"status": "success", "profile": profile}
                if isinstance(login_result, dict):
                    last_error = login_result.get("message") or login_result.get("detail") or login_result.get("status")
            raise HTTPException(status_code=401, detail=last_error or "Не удалось войти в Qobuz")

        token = (payload.get("token") or get_service_credentials(session, self.service_id).get("token") or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="Передайте Qobuz token")
        credentials = {
            "token": token,
            "app_id": app_id,
            "app_secret": app_secret,
            "working_app_id": None,
        }
        update_session_values(session["id"], {
            "qobuz_token": token,
            "qobuz_app_id": app_id,
            "qobuz_app_secret": app_secret,
            "qobuz_working_app_id": None,
        })
        upsert_service_connection(session["id"], self.service_id, credentials, {})
        profile = self.auth_status(get_session(session["id"])).to_dict()
        return {"status": "success", "profile": profile}

    def list_playlists(self, session: dict) -> List[PlaylistRef]:
        qobuz_client, _profile = ensure_qobuz_authorized(session)
        res = qobuz_client._request("playlist/getUserPlaylists", {"limit": 100})
        items = []
        playlist_items = (res.get("playlists", {}) or {}).get("items", []) if isinstance(res, dict) else []
        for item in playlist_items:
            items.append(PlaylistRef(
                id=str(item.get("id")),
                name=item.get("name") or "Без названия",
                tracks_count=int(item.get("tracks_count") or item.get("tracks", {}).get("total") or 0),
            ))
        return items

    def list_playlist_track_ids(self, session: dict, playlist_id: str) -> List[str]:
        qobuz_client, _profile = ensure_qobuz_authorized(session)
        return [str(track_id) for track_id in get_playlist_track_ids(qobuz_client, playlist_id)]

    def search_track(self, session: dict, query: str) -> Optional[ResolvedTrack]:
        result = search_track_rich(make_qobuz_client(session), query, session)
        if not result:
            return None
        return ResolvedTrack(
            id=str(result["id"]),
            title=result.get("title") or "",
            artist=result.get("artist") or "",
            album=result.get("album") or "",
            cover=result.get("cover"),
            duration=int(result.get("duration") or 0),
            hires=bool(result.get("hires")),
        )

    def search_tracks(self, session: dict, query: str, limit: int = 6) -> List[ResolvedTrack]:
        return [
            ResolvedTrack(
                id=str(item["id"]),
                title=item.get("title") or "",
                artist=item.get("artist") or "",
                album=item.get("album") or "",
                cover=item.get("cover"),
                duration=int(item.get("duration") or 0),
                hires=bool(item.get("hires")),
            )
            for item in search_tracks_rich_multi(make_qobuz_client(session), query, limit)
        ]

    def create_playlist(self, session: dict, name: str) -> str:
        qobuz_client, _profile = ensure_qobuz_authorized(session)
        playlist_id = qobuz_client.create_playlist(name)
        if not playlist_id:
            raise HTTPException(status_code=500, detail="Не удалось создать плейлист в Qobuz")
        return str(playlist_id)

    def add_tracks(self, session: dict, playlist_id: str, track_ids: List[str]) -> bool:
        qobuz_client, _profile = ensure_qobuz_authorized(session)
        success = True
        for i in range(0, len(track_ids), 100):
            chunk = [int(track_id) for track_id in track_ids[i:i+100]]
            if not qobuz_client.add_tracks_to_playlist(playlist_id, chunk):
                success = False
        return success

class SpotifyAdapter:
    service_id = "spotify"
    auth_base = "https://accounts.spotify.com"
    api_base = "https://api.spotify.com/v1"

    def configured(self) -> bool:
        return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)

    def redirect_uri(self, request: Optional[Request] = None) -> str:
        if SPOTIFY_REDIRECT_URI:
            return SPOTIFY_REDIRECT_URI
        if request:
            return str(request.url_for("spotify_callback"))
        return ""

    def make_state(self, session_id: str) -> str:
        nonce = secrets.token_urlsafe(16)
        payload = f"{session_id}.{nonce}"
        sig = base64.urlsafe_b64encode(make_hmac(payload.encode("utf-8"))[:16]).decode("ascii").rstrip("=")
        return f"{payload}.{sig}"

    def verify_state(self, state: str, session_id: str) -> bool:
        try:
            state_session_id, nonce, sig = state.split(".", 2)
        except ValueError:
            return False
        if state_session_id != session_id:
            return False
        payload = f"{state_session_id}.{nonce}"
        expected = base64.urlsafe_b64encode(make_hmac(payload.encode("utf-8"))[:16]).decode("ascii").rstrip("=")
        return hmac.compare_digest(sig, expected)

    def auth_url(self, session: dict, request: Request) -> str:
        if not self.configured():
            raise HTTPException(status_code=400, detail="Spotify не настроен: задайте SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET")
        params = {
            "client_id": SPOTIFY_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": self.redirect_uri(request),
            "scope": SPOTIFY_SCOPES,
            "state": self.make_state(session["id"]),
        }
        return f"{self.auth_base}/authorize?{urlencode(params)}"

    def response_error_detail(self, response: requests.Response, fallback: str) -> str:
        message = ""
        try:
            data = response.json()
            error = data.get("error")
            if isinstance(error, dict):
                message = error.get("message") or ""
            elif isinstance(error, str):
                message = data.get("error_description") or error
            if not message:
                message = data.get("message") or data.get("error_description") or ""
        except ValueError:
            message = response.text.strip()[:300]
        return f"{fallback}: HTTP {response.status_code}" + (f" - {message}" if message else "")

    def exchange_code(self, session: dict, code: str, request: Request) -> dict:
        if not self.configured():
            raise HTTPException(status_code=400, detail="Spotify не настроен")
        response = requests.post(
            f"{self.auth_base}/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri(request),
            },
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            timeout=(5, 20),
        )
        if response.status_code != 200:
            logger.warning("Spotify token exchange failed: status=%s body=%s", response.status_code, response.text[:500])
            raise HTTPException(status_code=400, detail=self.response_error_detail(response, "Spotify token exchange failed"))
        token_data = response.json()
        credentials = {
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": int(time.time()) + int(token_data.get("expires_in") or 3600) - 60,
            "token_type": token_data.get("token_type") or "Bearer",
        }
        profile = self.fetch_profile(credentials)
        upsert_service_connection(session["id"], self.service_id, credentials, profile)
        return profile

    def refresh_credentials(self, session: dict, credentials: dict) -> dict:
        if not credentials.get("refresh_token"):
            raise HTTPException(status_code=401, detail="Spotify refresh token отсутствует")
        if int(credentials.get("expires_at") or 0) > int(time.time()):
            return credentials
        response = requests.post(
            f"{self.auth_base}/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": credentials["refresh_token"],
            },
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            timeout=(5, 20),
        )
        if response.status_code != 200:
            logger.warning("Spotify token refresh failed: status=%s body=%s", response.status_code, response.text[:500])
            raise HTTPException(status_code=401, detail=self.response_error_detail(response, "Не удалось обновить Spotify token"))
        token_data = response.json()
        credentials["access_token"] = token_data.get("access_token")
        credentials["expires_at"] = int(time.time()) + int(token_data.get("expires_in") or 3600) - 60
        if token_data.get("refresh_token"):
            credentials["refresh_token"] = token_data["refresh_token"]
        connection = get_service_connection(session["id"], self.service_id) or {}
        upsert_service_connection(session["id"], self.service_id, credentials, connection.get("profile", {}))
        return credentials

    def request(self, session: dict, method: str, path: str, **kwargs):
        credentials = get_service_credentials(session, self.service_id)
        if not credentials.get("access_token"):
            raise HTTPException(status_code=401, detail="Spotify аккаунт не подключен")
        credentials = self.refresh_credentials(session, credentials)
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {credentials['access_token']}"
        response = requests.request(method, f"{self.api_base}{path}", headers=headers, timeout=(5, 25), **kwargs)
        if response.status_code == 204:
            return {}
        if response.status_code >= 400:
            logger.warning("Spotify API request failed: method=%s path=%s status=%s body=%s", method, path, response.status_code, response.text[:500])
            raise HTTPException(status_code=response.status_code, detail=self.response_error_detail(response, "Spotify API error"))
        return response.json()

    def fetch_profile(self, credentials: dict) -> dict:
        response = requests.get(
            f"{self.api_base}/me",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
            timeout=(5, 20),
        )
        if response.status_code != 200:
            logger.warning("Spotify profile request failed: status=%s body=%s", response.status_code, response.text[:500])
            raise HTTPException(status_code=response.status_code, detail=self.response_error_detail(response, "Не удалось получить профиль Spotify"))
        data = response.json()
        return {
            "authorized": True,
            "service": self.service_id,
            "display_name": data.get("display_name") or data.get("id"),
            "id": data.get("id"),
        }

    def auth_status(self, session: dict) -> ServiceProfile:
        connection = get_service_connection(session["id"], self.service_id)
        if not connection or not connection["credentials"].get("access_token"):
            return ServiceProfile(False, self.service_id)
        try:
            credentials = self.refresh_credentials(session, connection["credentials"])
            profile = self.fetch_profile(credentials)
            upsert_service_connection(session["id"], self.service_id, credentials, profile)
            return ServiceProfile(True, self.service_id, profile.get("display_name"), profile.get("id"), extra=profile)
        except Exception as exc:
            return ServiceProfile(False, self.service_id, detail=str(exc))

    def connect(self, session: dict, payload: dict, request: Request) -> dict:
        return {"status": "auth_required", "authorization_url": self.auth_url(session, request)}

    def list_playlists(self, session: dict) -> List[PlaylistRef]:
        playlists = []
        path = "/me/playlists?limit=50"
        while path:
            data = self.request(session, "GET", path)
            for item in data.get("items", []):
                playlists.append(PlaylistRef(
                    id=item.get("id"),
                    name=item.get("name") or "Без названия",
                    tracks_count=int((item.get("tracks") or {}).get("total") or 0),
                ))
            next_url = data.get("next")
            path = next_url.replace(self.api_base, "") if next_url else None
        return playlists

    def list_playlist_track_ids(self, session: dict, playlist_id: str) -> List[str]:
        track_ids = []
        path = f"/playlists/{playlist_id}/tracks"
        params = {
            "limit": 100,
            "fields": "next,items(track(id,uri))",
        }
        while path:
            data = self.request(session, "GET", path, params=params)
            params = None
            for item in data.get("items", []):
                track = item.get("track") or {}
                track_id = normalize_destination_track_id(self.service_id, track.get("id") or track.get("uri"))
                if track_id:
                    track_ids.append(track_id)
            next_url = data.get("next")
            path = next_url.replace(self.api_base, "") if next_url else None
        return track_ids

    def search_track(self, session: dict, query: str) -> Optional[ResolvedTrack]:
        data = self.request(session, "GET", "/search", params={"q": query, "type": "track", "limit": 1})
        items = ((data.get("tracks") or {}).get("items") or [])
        if not items:
            return None
        return self.spotify_track_to_resolved(items[0])

    def search_tracks(self, session: dict, query: str, limit: int = 6) -> List[ResolvedTrack]:
        data = self.request(session, "GET", "/search", params={"q": query, "type": "track", "limit": limit})
        return [self.spotify_track_to_resolved(item) for item in ((data.get("tracks") or {}).get("items") or [])]

    def spotify_track_to_resolved(self, track: dict) -> ResolvedTrack:
        album = track.get("album") or {}
        images = album.get("images") or []
        artists = ", ".join(artist.get("name") or "" for artist in track.get("artists", []) if artist.get("name"))
        return ResolvedTrack(
            id=track.get("id"),
            uri=track.get("uri"),
            title=track.get("name") or "",
            artist=artists,
            album=album.get("name") or "",
            cover=images[-1].get("url") if images else None,
            duration=int((track.get("duration_ms") or 0) / 1000),
        )

    def create_playlist(self, session: dict, name: str) -> str:
        profile = self.auth_status(session)
        if not profile.authorized or not profile.id:
            raise HTTPException(status_code=401, detail="Spotify аккаунт не подключен")
        data = self.request(
            session,
            "POST",
            f"/users/{profile.id}/playlists",
            json={"name": name, "public": False},
        )
        return data.get("id")

    def add_tracks(self, session: dict, playlist_id: str, track_ids: List[str]) -> bool:
        uris = [track_id if str(track_id).startswith("spotify:track:") else f"spotify:track:{track_id}" for track_id in track_ids]
        for i in range(0, len(uris), 100):
            self.request(session, "POST", f"/playlists/{playlist_id}/tracks", json={"uris": uris[i:i+100]})
        return True

SERVICE_ADAPTERS = {
    "manual": ManualSourceAdapter(),
    "yandex": YandexAdapter(),
    "qobuz": QobuzAdapter(),
    "spotify": SpotifyAdapter(),
}

def get_adapter(service_id: str):
    adapter = SERVICE_ADAPTERS.get(service_id)
    if not adapter:
        ensure_service_enabled(service_id)
        raise HTTPException(status_code=400, detail="Сервис пока не реализован")
    return adapter

def read_yandex_liked_tracks(session: dict):
    yandex_token = get_service_credentials(session, "yandex").get("token") or session.get("yandex_token") or ""
    if not yandex_token:
        raise ValueError("Для импорта 'Мне нравится' необходимо авторизоваться в Яндекс.Музыке.")
    yandex_client = YandexMusicClient(yandex_token).init()
    tracks_list = yandex_client.users_likes_tracks()
    if not tracks_list or not tracks_list.tracks_ids:
        return [], "Мне нравится", []
    track_items = []
    for i in range(0, len(tracks_list.tracks_ids), 100):
        chunk_ids = tracks_list.tracks_ids[i:i+100]
        tracks = yandex_client.tracks(chunk_ids)
        for track in tracks:
            if not track:
                continue
            track_items.append(yandex_track_item(track))
    track_items = normalize_track_items(track_items)
    return [item["query"] for item in track_items], "Мне нравится", track_items

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
    destination: Optional[str] = "qobuz"

class UrlParseRequest(BaseModel):
    url: str

class ServiceConnectRequest(BaseModel):
    method: Optional[str] = None
    token: Optional[str] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None

class ImportSourceRequest(BaseModel):
    source: str
    input_type: Optional[str] = "manual"
    url: Optional[str] = None
    text: Optional[str] = None
    tracks: Optional[List[str]] = None
    playlist_name: Optional[str] = None

class MatchRequest(BaseModel):
    destination: str = "qobuz"
    tracks: List[str]

class PlaylistMissingRequest(BaseModel):
    playlist_id: str
    track_ids: List[str]

class TransferData(BaseModel):
    destination: str = "qobuz"
    name: Optional[str] = None
    playlist_id: Optional[str] = None
    track_ids: List[str]
    order: Optional[str] = "original"

# Эндпоинты

async def match_tracks_for_destination(session: dict, destination: str, tracks: List[str]):
    ensure_service_enabled(destination, "destination")
    adapter = get_adapter(destination)
    if not hasattr(adapter, "search_track"):
        raise HTTPException(status_code=400, detail="Сервис назначения не поддерживает поиск треков")
    queries = normalize_track_names(tracks)
    if not queries:
        return {"total": 0, "matched": 0, "matches": []}
    concurrency = max(1, min(MATCH_CONCURRENCY, len(queries)))
    sem = asyncio.Semaphore(concurrency)

    async def worker(idx, query):
        async with sem:
            track = await asyncio.to_thread(adapter.search_track, session, query)
            return {
                "index": idx,
                "original": query,
                "status": "found" if track else "not_found",
                "match": track.to_dict() if track else None,
            }

    results = await asyncio.gather(*[worker(idx, query) for idx, query in enumerate(queries)])
    if destination == "qobuz":
        await asyncio.to_thread(save_search_cache)
    return {
        "total": len(queries),
        "matched": sum(1 for item in results if item["match"]),
        "matches": sorted(results, key=lambda item: item["index"]),
    }

def transfer_tracks_sync(session: dict, data: TransferData) -> dict:
    ensure_service_enabled(data.destination, "destination")
    adapter = get_adapter(data.destination)
    track_ids = [str(track_id).strip() for track_id in data.track_ids if str(track_id).strip()]
    if not track_ids:
        raise HTTPException(status_code=400, detail="Список ID треков пуст")
    if len(track_ids) > MAX_TRACKS_PER_REQUEST:
        raise HTTPException(status_code=400, detail=f"Слишком много треков. Максимум: {MAX_TRACKS_PER_REQUEST}")
    if data.destination == "qobuz":
        try:
            track_ids = [str(int(track_id)) for track_id in track_ids if int(track_id) > 0]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Список ID треков содержит некорректные значения")
    if data.order == "reverse":
        track_ids = list(reversed(track_ids))

    playlist_name = validate_playlist_name(data.name)
    playlist_id = (data.playlist_id or "").strip()
    is_new = False
    if not playlist_id:
        if not playlist_name:
            raise HTTPException(status_code=400, detail="Название плейлиста не указано")
        playlist_id = adapter.create_playlist(session, playlist_name)
        is_new = True
    elif hasattr(adapter, "list_playlist_track_ids"):
        existing_ids = {
            normalized
            for normalized in (
                normalize_destination_track_id(data.destination, track_id)
                for track_id in adapter.list_playlist_track_ids(session, playlist_id)
            )
            if normalized
        }
        track_ids = [
            track_id
            for track_id in track_ids
            if normalize_destination_track_id(data.destination, track_id) not in existing_ids
        ]
        if not track_ids:
            account = adapter.auth_status(session).to_dict()
            return TransferResult(
                status="success",
                playlist_id=playlist_id,
                count=0,
                is_new=False,
                detail="Все треки уже есть в плейлисте (новых треков нет)",
                account=account,
            ).to_dict()

    success = adapter.add_tracks(session, playlist_id, track_ids)
    account = adapter.auth_status(session).to_dict()
    if success:
        return TransferResult(
            status="success",
            playlist_id=playlist_id,
            count=len(track_ids),
            is_new=is_new,
            account=account,
        ).to_dict()
    return TransferResult(
        status="partial_success",
        playlist_id=playlist_id,
        count=len(track_ids),
        is_new=is_new,
        detail="Часть треков не удалось добавить",
        account=account,
    ).to_dict()

def playlist_missing_tracks_sync(session: dict, service: str, data: PlaylistMissingRequest) -> dict:
    ensure_service_enabled(service, "destination")
    adapter = get_adapter(service)
    if not hasattr(adapter, "list_playlist_track_ids"):
        raise HTTPException(status_code=400, detail="Сервис пока не поддерживает проверку недостающих треков")
    playlist_id = (data.playlist_id or "").strip()
    if not playlist_id:
        raise HTTPException(status_code=400, detail="Выберите плейлист назначения")
    if len(data.track_ids) > MAX_TRACKS_PER_REQUEST:
        raise HTTPException(status_code=400, detail=f"Слишком много треков. Максимум: {MAX_TRACKS_PER_REQUEST}")

    requested_ids = []
    for track_id in data.track_ids:
        normalized = normalize_destination_track_id(service, track_id)
        if normalized:
            requested_ids.append(normalized)
    if not requested_ids:
        raise HTTPException(status_code=400, detail="Список ID треков пуст")

    playlist_ids = adapter.list_playlist_track_ids(session, playlist_id)
    existing_ids = {
        normalized
        for normalized in (normalize_destination_track_id(service, track_id) for track_id in playlist_ids)
        if normalized
    }
    missing_ids = [track_id for track_id in requested_ids if track_id not in existing_ids]
    existing_requested_ids = [track_id for track_id in requested_ids if track_id in existing_ids]
    return {
        "playlist_id": playlist_id,
        "checked_count": len(requested_ids),
        "playlist_track_count": len(existing_ids),
        "missing_count": len(missing_ids),
        "existing_count": len(existing_requested_ids),
        "missing_track_ids": missing_ids,
        "existing_track_ids": existing_requested_ids,
    }

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

@app.get("/favicon.svg")
async def get_favicon_svg():
    return FileResponse("favicon.svg", media_type="image/svg+xml")

@app.get("/favicon.ico")
async def get_favicon_ico():
    return FileResponse("favicon.svg", media_type="image/svg+xml")

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

@app.get("/api/services")
async def get_services():
    return {"services": service_catalog_with_runtime_status()}

@app.get("/api/connections")
async def get_connections(request: Request, response: Response):
    session = get_or_create_session(request, response)
    result = {}
    for meta in service_catalog_with_runtime_status():
        service_id = meta["id"]
        adapter = SERVICE_ADAPTERS.get(service_id)
        if adapter and meta.get("enabled"):
            try:
                profile = await asyncio.to_thread(adapter.auth_status, session)
                result[service_id] = profile.to_dict()
            except Exception as exc:
                result[service_id] = {"authorized": False, "service": service_id, "detail": str(exc)}
        else:
            result[service_id] = {
                "authorized": service_id == "manual",
                "service": service_id,
                "detail": meta.get("note"),
            }
    return {"connections": result}

@app.post("/api/connections/{service}/connect")
async def connect_service(service: str, data: ServiceConnectRequest, request: Request, response: Response):
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("service_connect", request, session)
    ensure_service_enabled(service)
    adapter = get_adapter(service)
    if service == "spotify":
        return adapter.connect(session, data.dict(), request)
    if not hasattr(adapter, "connect"):
        raise HTTPException(status_code=400, detail="Для этого сервиса пока нет универсального входа")
    return await asyncio.to_thread(adapter.connect, session, data.dict())

def spotify_callback_page(title: str, message: str, success: bool = False) -> HTMLResponse:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    accent = "#22d3ee" if success else "#fb7185"
    return HTMLResponse(f"""
<!doctype html>
<html lang="ru">
<head><meta charset="utf-8"><title>{safe_title}</title></head>
<body style="font-family: sans-serif; background: #0b1020; color: white; display: grid; place-items: center; min-height: 100vh;">
  <main style="text-align: center; max-width: 560px; padding: 24px;">
    <h1 style="margin-bottom: 12px; color: {accent};">{safe_title}</h1>
    <p style="line-height: 1.5;">{safe_message}</p>
    <a href="/" style="color: #22d3ee;">Вернуться к переносу</a>
    {"<script>setTimeout(() => location.href = '/', 900);</script>" if success else ""}
  </main>
</body>
</html>
""", status_code=200 if success else 400)

@app.get("/api/connections/spotify/callback", name="spotify_callback")
async def spotify_callback(
    request: Request,
    response: Response,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    session = get_or_create_session(request, response)
    adapter = SERVICE_ADAPTERS["spotify"]
    if error:
        return spotify_callback_page("Spotify не подключен", f"Spotify OAuth error: {error}")
    if not code or not state or not adapter.verify_state(state, session["id"]):
        return spotify_callback_page("Spotify не подключен", "Некорректный Spotify OAuth callback. Попробуйте начать вход заново.")
    try:
        profile = await asyncio.to_thread(adapter.exchange_code, session, code, request)
    except HTTPException as exc:
        logger.warning("Spotify callback failed: %s", exc.detail)
        return spotify_callback_page("Spotify не подключен", str(exc.detail))
    display_name = profile.get("display_name") or "Spotify"
    return spotify_callback_page("Spotify подключен", f"Аккаунт: {display_name}", success=True)

@app.post("/api/connections/{service}/disconnect")
async def disconnect_service(service: str, request: Request, response: Response):
    session = get_or_create_session(request, response)
    delete_service_connection(session["id"], service)
    if service == "qobuz":
        update_session_values(session["id"], {"qobuz_token": "", "qobuz_working_app_id": None})
    elif service == "yandex":
        update_session_values(session["id"], {"yandex_token": None})
    return {"status": "success"}

@app.post("/api/import/source")
async def import_source(data: ImportSourceRequest, request: Request, response: Response):
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("import_source", request, session)
    ensure_service_enabled(data.source, "source")
    adapter = get_adapter(data.source)
    try:
        result = await asyncio.to_thread(adapter.read_tracks, session, data.dict())
        result["tracks"] = normalize_track_names(result.get("tracks") or [])
        result["track_items"] = normalize_track_items(result.get("track_items"), result["tracks"])
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/match")
async def match_tracks(data: MatchRequest, request: Request, response: Response):
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("match_http", request, session)
    return await match_tracks_for_destination(session, data.destination, data.tracks)

@app.get("/api/destinations/{service}/playlists")
async def list_destination_playlists(service: str, request: Request, response: Response):
    session = get_or_create_session(request, response)
    ensure_service_enabled(service, "destination")
    adapter = get_adapter(service)
    if not hasattr(adapter, "list_playlists"):
        raise HTTPException(status_code=400, detail="Сервис не поддерживает список плейлистов")
    playlists = await asyncio.to_thread(adapter.list_playlists, session)
    return {"playlists": [playlist.to_dict() for playlist in playlists]}

@app.post("/api/destinations/{service}/playlists/missing")
async def playlist_missing_tracks(service: str, data: PlaylistMissingRequest, request: Request, response: Response):
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("playlist_read", request, session)
    return await asyncio.to_thread(playlist_missing_tracks_sync, session, service, data)

@app.post("/api/transfer")
async def transfer_tracks(data: TransferData, request: Request, response: Response):
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("playlist_write", request, session)
    return await asyncio.to_thread(transfer_tracks_sync, session, data)

@app.get("/api/config")
async def get_config(request: Request, response: Response):
    session = get_or_create_session(request, response)
    qobuz_credentials = get_service_credentials(session, "qobuz")
    qobuz_client = make_qobuz_client(session)
    profile = await asyncio.to_thread(get_qobuz_profile, qobuz_client, [
        qobuz_credentials.get("working_app_id"),
        qobuz_credentials.get("app_id"),
    ])
    if profile.get("authorized") and profile.get("app_id") != qobuz_credentials.get("working_app_id"):
        update_session_values(session["id"], {"qobuz_working_app_id": profile["app_id"]})
    yandex_token = get_service_credentials(session, "yandex").get("token") or session.get("yandex_token") or ""
    yandex_profile = await asyncio.to_thread(get_yandex_profile, yandex_token)
    return {
        "has_qobuz_token": bool(qobuz_credentials.get("token")),
        "app_id": qobuz_credentials.get("app_id") or SERVER_DEFAULT_APP_ID,
        "browser_login_enabled": BROWSER_LOGIN_ENABLED,
        "profile": profile,
        "yandex_profile": yandex_profile
    }

@app.post("/api/config")
async def save_config(data: ConfigData, request: Request, response: Response):
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("config_save", request, session)
    try:
        qobuz_credentials = get_service_credentials(session, "qobuz")
        app_id = data.app_id.strip() or SERVER_DEFAULT_APP_ID
        token = data.token.strip() or qobuz_credentials.get("token") or ""
        app_secret = data.app_secret.strip() or qobuz_credentials.get("app_secret") or SERVER_DEFAULT_APP_SECRET
        update_session_values(session["id"], {
            "qobuz_token": token,
            "qobuz_app_id": app_id,
            "qobuz_app_secret": app_secret,
            "qobuz_working_app_id": None,
        })
        session = get_session(session["id"])
        qobuz_client = make_qobuz_client(session)
        profile = await asyncio.to_thread(get_qobuz_profile, qobuz_client, [app_id])
        if profile.get("authorized"):
            update_session_values(session["id"], {"qobuz_working_app_id": profile["app_id"]})
        return {"status": "success", "profile": profile}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/auth/test")
async def test_auth(request: Request, response: Response):
    session = get_or_create_session(request, response)
    qobuz_client = make_qobuz_client(session)
    profile = await asyncio.to_thread(get_qobuz_profile, qobuz_client, [
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

@app.post("/api/session/delete")
async def delete_current_session(request: Request, response: Response):
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        delete_session(session_id)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )
    return {"status": "success"}

@app.post("/api/auth/qobuz-login")
async def qobuz_password_login(data: QobuzLoginData, request: Request, response: Response):
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("qobuz_login", request, session)
    email = data.email.strip()
    password = data.password

    if not email or not password or len(email) > 254 or len(password) > 256:
        raise HTTPException(status_code=400, detail="Введите email и пароль Qobuz")

    preferred_app_id = (data.app_id or session.get("qobuz_app_id") or SERVER_DEFAULT_APP_ID).strip()
    app_secret = (data.app_secret or session.get("qobuz_app_secret") or SERVER_DEFAULT_APP_SECRET).strip()
    app_ids = unique_values([preferred_app_id] + get_qobuz_web_app_ids() + QOBUZ_APP_ID_CANDIDATES)
    password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
    last_error = None

    for app_id in app_ids:
        qobuz_client = QobuzDirect("", app_id, app_secret)
        login_result = await asyncio.to_thread(qobuz_client.login, email, password_hash, app_id, True)
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
    enforce_http_rate_limit("browser_login", request, session)
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
    request: Request,
    response: Response,
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None)
):
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("parse_tracks", request, session)
    try:
        track_names = []
        if file:
            content = await file.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Файл слишком большой. Максимум: {MAX_UPLOAD_BYTES} байт",
                )
            try:
                lines = content.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                lines = content.decode("cp1251", errors="ignore").splitlines()
            track_names = normalize_track_names(lines)
        elif text:
            if len(text.encode("utf-8")) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Текст слишком большой. Максимум: {MAX_UPLOAD_BYTES} байт",
                )
            track_names = normalize_track_names(text.splitlines())
        return {"tracks": track_names}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

def parse_yandex_music_url(url: str, session: dict, include_items: bool = False):
    # Clean up the URL (remove query parameters)
    clean_url = url.split("?")[0].strip().rstrip("/")
    if clean_url.startswith(("music.yandex.ru/", "www.music.yandex.ru/")):
        clean_url = f"https://{clean_url}"
    parsed = urlparse(clean_url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or host not in {"music.yandex.ru", "www.music.yandex.ru"}:
        raise ValueError("Поддерживаются только ссылки music.yandex.ru")
    
    # Initialize client (guest or token)
    yandex_token = get_service_credentials(session, "yandex").get("token") or session.get("yandex_token") or ""
    try:
        if yandex_token:
            yandex_client = YandexMusicClient(yandex_token).init()
        else:
            yandex_client = YandexMusicClient().init()
    except Exception as e:
        logger.error(f"Failed to initialize Yandex Music Client: {e}")
        raise ValueError(f"Не удалось подключиться к API Яндекс.Музыки: {e}")
    
    track_names = []
    track_items = []
    playlist_name_suggestion = "Импортировано из Яндекс.Музыки"

    def add_yandex_track(track):
        item = yandex_track_item(track)
        if item.get("query"):
            track_names.append(item["query"])
            track_items.append(item)

    def finish_yandex_parse():
        normalized_names = normalize_track_names(track_names)
        normalized_items = normalize_track_items(track_items, normalized_names)
        if include_items:
            return normalized_names, playlist_name_suggestion, normalized_items
        return normalized_names, playlist_name_suggestion
    
    # 1. Match Album + Track: https://music.yandex.ru/album/123/track/456
    album_track_match = re.search(r'/album/(\d+)/track/(\d+)', clean_url)
    if album_track_match:
        track_id = album_track_match.group(2)
        try:
            tracks = yandex_client.tracks([track_id])
            if tracks:
                track = tracks[0]
                add_yandex_track(track)
                playlist_name_suggestion = track_items[-1]["query"] if track_items else playlist_name_suggestion
            else:
                raise ValueError("Трек не найден в базе данных Яндекс.Музыки")
        except Exception as e:
            logger.error(f"Error fetching track {track_id}: {e}")
            raise ValueError(f"Ошибка при загрузке трека: {e}")
        return finish_yandex_parse()

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
                    add_yandex_track(track)
        except Exception as e:
            logger.error(f"Error fetching album {album_id}: {e}")
            raise ValueError(f"Ошибка при загрузке альбома: {e}")
        return finish_yandex_parse()

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
                    add_yandex_track(track)
        except Exception as e:
            logger.error(f"Error fetching liked tracks: {e}")
            raise ValueError(f"Ошибка при загрузке 'Мне нравится': {e}")
        return finish_yandex_parse()

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
                add_yandex_track(track)
        except Exception as e:
            logger.error(f"Error fetching playlist kind={kind} uid={uid}: {e}")
            raise ValueError(f"Ошибка при загрузке плейлиста: {e}. Возможно, плейлист является приватным.")
            
        return finish_yandex_parse()

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
                    add_yandex_track(track)
        except Exception as e:
            logger.error(f"Error fetching artist {artist_id}: {e}")
            raise ValueError(f"Ошибка при загрузке исполнителя: {e}")
        return finish_yandex_parse()

    raise ValueError("Неподдерживаемый формат ссылки Яндекс.Музыки. Пожалуйста, укажите ссылку на плейлист, альбом, трек или исполнителя.")

@app.post("/api/tracks/parse-url")
def parse_tracks_url(data: UrlParseRequest, request: Request, response: Response):
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("parse_url", request, session)
    try:
        if len(data.url or "") > MAX_YANDEX_URL_LENGTH:
            raise ValueError(f"Ссылка длиннее {MAX_YANDEX_URL_LENGTH} символов")
        tracks, playlist_name = parse_yandex_music_url(data.url, session)
        tracks = normalize_track_names(tracks)
        return {"tracks": tracks, "playlist_name": playlist_name}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Internal error parsing URL {data.url}: {e}")
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка сервера: {str(e)}")

@app.post("/api/search/single")
async def search_single(data: SingleSearchQuery, request: Request, response: Response):
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("manual_search", request, session)
    try:
        query = validate_query_text(data.query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    destination = data.destination or "qobuz"
    ensure_service_enabled(destination, "destination")
    adapter = get_adapter(destination)
    if hasattr(adapter, "search_tracks"):
        results = await asyncio.to_thread(adapter.search_tracks, session, query, 6)
    else:
        result = await asyncio.to_thread(adapter.search_track, session, query)
        results = [result] if result else []
    return {"results": [track.to_dict() for track in results]}

@app.get("/api/qobuz/playlists")
async def get_qobuz_playlists(request: Request, response: Response):
    session = get_or_create_session(request, response)
    try:
        playlists = await asyncio.to_thread(SERVICE_ADAPTERS["qobuz"].list_playlists, session)
        return {"playlists": [playlist.to_dict() for playlist in playlists]}
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
    session = get_or_create_session(request, response)
    enforce_http_rate_limit("playlist_write", request, session)
    if not data.track_ids:
        raise HTTPException(status_code=400, detail="Список ID треков пуст")
    if len(data.track_ids) > MAX_TRACKS_PER_REQUEST:
        raise HTTPException(status_code=400, detail=f"Слишком много треков. Максимум: {MAX_TRACKS_PER_REQUEST}")

    try:
        track_ids = [int(track_id) for track_id in data.track_ids if int(track_id) > 0]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Список ID треков содержит некорректные значения")
    if not track_ids:
        raise HTTPException(status_code=400, detail="Список ID треков пуст")

    playlist_name = validate_playlist_name(data.name)
    playlist_id_input = (data.playlist_id or "").strip()
    if playlist_id_input and len(playlist_id_input) > 80:
        raise HTTPException(status_code=400, detail="Некорректный ID плейлиста")

    def create_playlist_sync():
        qobuz_client, profile = ensure_qobuz_authorized(session)
        account = {
            "display_name": profile.get("display_name"),
            "id": profile.get("id"),
        }

        playlist_id = playlist_id_input
        import_track_ids = list(track_ids)
        if not playlist_id:
            if not playlist_name:
                raise HTTPException(status_code=400, detail="Название плейлиста не указано")
            playlist_id = qobuz_client.create_playlist(playlist_name)
            if not playlist_id:
                raise HTTPException(status_code=500, detail="Не удалось создать плейлист в Qobuz")
            is_new = True
        else:
            is_new = False
            existing_ids = get_playlist_track_ids(qobuz_client, playlist_id)
            existing_set = set(existing_ids)
            import_track_ids = [tid for tid in import_track_ids if tid not in existing_set]

            if not import_track_ids:
                return {
                    "status": "success",
                    "playlist_id": playlist_id,
                    "count": 0,
                    "is_new": False,
                    "account": account,
                    "detail": "Все треки уже есть в плейлисте (новых треков нет)"
                }

        success = True
        for i in range(0, len(import_track_ids), 100):
            chunk = import_track_ids[i:i+100]
            if not qobuz_client.add_tracks_to_playlist(playlist_id, chunk):
                success = False

        if success:
            return {
                "status": "success",
                "playlist_id": playlist_id,
                "count": len(import_track_ids),
                "is_new": is_new,
                "account": account,
            }
        status_name = "partial_success"
        detail = "Часть треков не удалось добавить"
        return {"status": status_name, "playlist_id": playlist_id, "detail": detail, "is_new": is_new, "account": account}

    return await asyncio.to_thread(create_playlist_sync)

@app.websocket("/api/ws/match")
async def websocket_match(websocket: WebSocket):
    await websocket.accept()
    try:
        session = get_websocket_session(websocket)
        rate_error = check_ws_rate_limit("match_ws", ws_rate_identity(websocket, session))
        if rate_error:
            await websocket.send_json({"type": "error", "message": rate_error})
            await websocket.close(code=4408)
            return
        try:
            data = await websocket.receive_text()
            req = json.loads(data)
            raw_tracks = req.get("tracks", [])
            if not isinstance(raw_tracks, list):
                raise ValueError("Некорректный список треков")
            queries = normalize_track_names(raw_tracks)
            destination = req.get("destination") or "qobuz"
            ensure_service_enabled(destination, "destination")
            adapter = get_adapter(destination)
        except HTTPException as exc:
            await websocket.send_json({"type": "error", "message": exc.detail})
            await websocket.close(code=1003)
            return
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close(code=1003)
            return
        
        total = len(queries)
        if total == 0:
            await websocket.send_json({"type": "done", "total": 0, "matched": 0})
            return
        matched_count = 0
        processed_count = 0
        
        # Ограничиваем параллельность запросов к API Qobuz
        concurrency = max(1, min(MATCH_CONCURRENCY, total or 1))
        sem = asyncio.Semaphore(concurrency)
        
        async def worker(idx, query):
            async with sem:
                track = await asyncio.to_thread(adapter.search_track, session, query)
                track_info = track.to_dict() if track else None
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
            if processed_count and destination == "qobuz":
                await asyncio.to_thread(save_search_cache)
            raise ex
            
        await websocket.send_json({
            "type": "done",
            "total": total,
            "matched": matched_count
        })
        if processed_count and destination == "qobuz":
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
    enforce_http_rate_limit("yandex_liked", request, session)
    try:
        track_names, playlist_name, track_items = read_yandex_liked_tracks(session)
        return {"tracks": track_names, "playlist_name": playlist_name, "track_items": track_items}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    rate_error = check_ws_rate_limit("yandex_auth_ws", ws_rate_identity(websocket, session))
    if rate_error:
        await websocket.send_json({"type": "error", "message": rate_error})
        await websocket.close(code=4408)
        return
    loop = asyncio.get_running_loop()
    event_queue = asyncio.Queue()
    cancel_auth = threading.Event()

    def run_auth():
        def send_event(payload):
            loop.call_soon_threadsafe(event_queue.put_nowait, payload)

        try:
            cl = YandexMusicClient()
            
            def on_code(code_obj):
                logger.info("Получен device-code Яндекс.Музыки: %s", code_obj.user_code)
                send_event({
                    "type": "code",
                    "verification_url": code_obj.verification_url,
                    "user_code": code_obj.user_code
                })
                
            token = cl.device_auth(on_code=on_code, should_cancel=cancel_auth.is_set)
            send_event({"type": "token", "token": token.access_token})
        except Exception as e:
            logger.error(f"Error in device_auth: {e}")
            send_event({"type": "error", "message": str(e)})

    loop.run_in_executor(thread_pool_executor, run_auth)

    try:
        while True:
            event = await asyncio.wait_for(event_queue.get(), timeout=900)
            if event.get("type") == "token":
                token = event["token"]
                try:
                    update_session_values(session["id"], {"yandex_token": token})

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
                return

            await websocket.send_json(event)
            if event.get("type") == "error":
                return
    except asyncio.TimeoutError:
        cancel_auth.set()
        await websocket.send_json({"type": "error", "message": "Истекло время ожидания авторизации Яндекс.Музыки"})
    except WebSocketDisconnect:
        cancel_auth.set()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
