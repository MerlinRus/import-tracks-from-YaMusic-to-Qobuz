import os
import requests
import hashlib
import time
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

def clean_env(key, default=None):
    val = os.getenv(key, default)
    if val:
        return val.strip("'").strip('"').strip()
    return val

# Настройки Qobuz API
# Пытаемся использовать App ID, который чаще всего работает с веб-токенами
APP_ID = clean_env('QOBUZ_APP_ID') or '30650571'
APP_SECRET = clean_env('QOBUZ_APP_SECRET') or '5929d2b8b9354226a0a73d327f918991'
AUTH_TOKEN = clean_env('QOBUZ_TOKEN')
BASE_URL = "https://www.qobuz.com/api.json/0.2/"
REQUEST_TIMEOUT = (5, 30)
REQUEST_RETRIES = 3
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

class QobuzDirect:
    def __init__(self, token, initial_app_id, app_secret):
        self.auth_token = token
        self.app_id = initial_app_id
        self.app_secret = app_secret
        self.session = requests.Session()
        
        # Заголовки как в браузере
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        })

    def _generate_signature(self, method, params, timestamp):
        method_clean = method.replace("/", "")
        keys = sorted(params.keys())
        param_str = "".join([f"{k}{params[k]}" for k in keys if k not in ["request_sig", "app_id"]])
        sig_base = f"{method_clean}{param_str}{timestamp}{self.app_secret}"
        return hashlib.md5(sig_base.encode()).hexdigest()

    def _request(self, method_path, params=None, current_app_id=None, quiet_errors=False):
        """Универсальный метод запроса с ручной сборкой URL"""
        if params is None:
            params = {}
            
        use_app_id = current_app_id or self.app_id
        
        # Строим URL вручную, чтобы app_id был ПЕРВЫМ
        url = f"{BASE_URL}{method_path}?app_id={use_app_id}"
        
        # Добавляем остальные параметры
        for k, v in params.items():
            url += f"&{k}={requests.utils.quote(str(v))}"
        
        # Добавляем токен
        if "user_auth_token" not in url:
            url += f"&user_auth_token={self.auth_token}"

        # Обновляем заголовки для текущего запроса
        headers = {
            "X-User-Auth-Token": self.auth_token,
            "X-App-Id": use_app_id
        }

        response = None
        for attempt in range(1, REQUEST_RETRIES + 1):
            try:
                response = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as e:
                if attempt == REQUEST_RETRIES:
                    if not quiet_errors:
                        print(f"[QOBUZ REQUEST ERROR] {method_path} failed after {attempt} attempts: {e}")
                    return {"status": "error", "message": str(e)}
                time.sleep(0.5 * attempt)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < REQUEST_RETRIES:
                time.sleep(0.5 * attempt)
                continue
            break

        if response is None:
            return {"status": "error", "message": "No response from Qobuz"}

        if response.status_code != 200 and not quiet_errors:
            print(f"[QOBUZ HTTP ERROR] {method_path} returned status {response.status_code}. Response: {response.text}")
        try:
            data = response.json()
            if isinstance(data, dict) and data.get('status') == 'error' and not quiet_errors:
                print(f"[QOBUZ API ERROR] {method_path} error response: {data}")
            return data
        except Exception as e:
            if not quiet_errors:
                print(f"[QOBUZ JSON ERROR] Failed to parse JSON response. Error: {e}. Text: {response.text}")
            return {"status": "error", "message": f"Invalid JSON: {str(e)}"}

    def login(self, email, password_hash, current_app_id=None, quiet_errors=False):
        use_app_id = current_app_id or self.app_id
        url = f"{BASE_URL}user/login"
        timestamp = str(int(time.time()))
        params = {
            "app_id": use_app_id,
            "username": email,
            "password": password_hash,
            "request_ts": timestamp,
        }
        params["request_sig"] = self._generate_signature("user/login", params, timestamp)

        response = None
        for attempt in range(1, REQUEST_RETRIES + 1):
            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as e:
                if attempt == REQUEST_RETRIES:
                    if not quiet_errors:
                        print(f"[QOBUZ REQUEST ERROR] user/login failed after {attempt} attempts: {e}")
                    return {"status": "error", "message": str(e)}
                time.sleep(0.5 * attempt)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < REQUEST_RETRIES:
                time.sleep(0.5 * attempt)
                continue
            break

        if response is None:
            return {"status": "error", "message": "No response from Qobuz"}

        if response.status_code != 200 and not quiet_errors:
            print(f"[QOBUZ HTTP ERROR] user/login returned status {response.status_code}. Response: {response.text}")

        try:
            data = response.json()
            if isinstance(data, dict) and data.get("user_auth_token"):
                self.auth_token = data["user_auth_token"]
                self.app_id = use_app_id
            return data
        except Exception as e:
            if not quiet_errors:
                print(f"[QOBUZ JSON ERROR] Failed to parse login response. Error: {e}. Text: {response.text}")
            return {"status": "error", "message": f"Invalid JSON: {str(e)}"}

    def get_user_info(self):
        """Проверка токена и автоматический подбор App ID"""
        # Список самых популярных App ID
        known_app_ids = [
            self.app_id,     # Тот что из .env
            '950096963',     # Часто используется в веб-плеере
            '798273057',     # Android
            '579939560',     # Альтернативный
            '100000000',     # Старый веб
            '306000000',
            '274246104'
        ]
        
        print("Проверяю токен и подбираю правильный App ID...")
        
        for test_app_id in known_app_ids:
            if not test_app_id: continue
            
            data = self._request("user/get", current_app_id=test_app_id)
            if isinstance(data, dict) and 'display_name' in data:
                print(f"УСПЕХ! Найден рабочий App ID: {test_app_id}")
                self.app_id = test_app_id # Запоминаем рабочий ID
                print(f"Авторизован как: {data['display_name']} (ID: {data['id']})")
                return True
                
        print(f"Ошибка токена: ни один из App ID не подошел. Последний ответ: {data}")
        return False

    def search_track(self, query):
        """Поиск трека"""
        method = "catalog/search"
        timestamp = str(int(time.time()))

        params = {
            "query": query,
            "type": "tracks", # Исправлено с 'track' на 'tracks'
            "limit": 1,
            "request_ts": timestamp
        }
        params["request_sig"] = self._generate_signature(method, params, timestamp)
        
        data = self._request(method, params)
        
        if isinstance(data, dict) and 'tracks' in data and data['tracks']['items']:
            track = data['tracks']['items'][0]
            return track['id'], f"{track['performer']['name']} - {track['title']}"
        
        # Если трек не найден, выводим ответ сервера для отладки
        print(f"\n[ОТЛАДКА ПОИСКА] Ответ сервера для '{query}': {data}")
        return None, None

    def create_playlist(self, name):
        """Создание плейлиста"""
        method = "playlist/create"
        timestamp = str(int(time.time()))
        
        params = {
            "name": name,
            "request_ts": timestamp
        }
        params["request_sig"] = self._generate_signature(method, params, timestamp)
        
        data = self._request(method, params)
        if not isinstance(data, dict) or 'id' not in data:
            print(f"[QOBUZ ERROR] Failed to create playlist. Response: {data}")
        return data.get('id') if isinstance(data, dict) else None

    def add_tracks_to_playlist(self, playlist_id, track_ids):
        """Добавление треков"""
        method = "playlist/addTracks"
        timestamp = str(int(time.time()))
        
        track_ids_str = ",".join(map(str, track_ids))
        params = {
            "playlist_id": playlist_id,
            "track_ids": track_ids_str,
            "request_ts": timestamp
        }
        params["request_sig"] = self._generate_signature(method, params, timestamp)
        
        data = self._request(method, params)
        success = isinstance(data, dict) and 'id' in data and data.get('status') != 'error'
        if not success:
            print(f"[QOBUZ ERROR] Failed to add tracks to playlist. Response: {data}")
        return success

def main():
    if not AUTH_TOKEN:
        print("Пожалуйста, добавьте QOBUZ_TOKEN в файл .env")
        return

    client = QobuzDirect(AUTH_TOKEN, APP_ID, APP_SECRET)
    
    if not client.get_user_info():
        return

    # Чтение файла
    file_path = 'tracks.txt'
    if not os.path.exists(file_path):
        print(f"Файл {file_path} не найден.")
        return

    with open(file_path, 'r', encoding='utf-8') as f:
        track_names = [line.strip() for line in f if line.strip()]

    if not track_names:
        print("Список треков пуст.")
        return

    print(f"\nНачинаю поиск {len(track_names)} треков в Qobuz...")

    found_ids = []
    not_found = []

    for query in track_names:
        track_id, full_name = client.search_track(query)
        if track_id:
            found_ids.append(track_id)
            print(f"OK: {query} -> {full_name}")
        else:
            not_found.append(query)
            print(f"??: {query} (не найден)")

    if not found_ids:
        print("\nНи один трек не найден. Плейлист не будет создан.")
        return

    playlist_name = "Imported from Ya.Music"
    print(f"\nСоздаю плейлист '{playlist_name}'...")
    playlist_id = client.create_playlist(playlist_name)
    
    if playlist_id:
        success = True
        for i in range(0, len(found_ids), 100):
            chunk = found_ids[i:i+100]
            if not client.add_tracks_to_playlist(playlist_id, chunk):
                success = False
        
        if success:
            print(f"\nГОТОВО! Плейлист успешно создан. Добавлено {len(found_ids)} треков.")
        else:
            print("\nПлейлист создан, но возникли проблемы при добавлении некоторых треков.")
    else:
        print("\nНе удалось создать плейлист.")

    if not_found:
        print(f"\nНе найдено ({len(not_found)}):")
        for item in not_found:
            print(f"- {item}")

if __name__ == "__main__":
    main()
