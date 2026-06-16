from dataclasses import asdict, dataclass
from typing import List, Optional, Protocol


@dataclass
class TrackQuery:
    query: str
    source_id: Optional[str] = None


@dataclass
class ResolvedTrack:
    id: str
    title: str
    artist: str
    album: str = ""
    cover: Optional[str] = None
    duration: int = 0
    uri: Optional[str] = None
    hires: bool = False

    def to_dict(self):
        return asdict(self)


@dataclass
class PlaylistRef:
    id: str
    name: str
    tracks_count: int = 0

    def to_dict(self):
        return asdict(self)


@dataclass
class ServiceProfile:
    authorized: bool
    service: str
    display_name: Optional[str] = None
    id: Optional[str] = None
    detail: Optional[str] = None
    extra: Optional[dict] = None

    def to_dict(self):
        data = asdict(self)
        if self.extra:
            data.update(self.extra)
        data.pop("extra", None)
        return data


@dataclass
class TransferRequest:
    destination: str
    track_ids: List[str]
    name: Optional[str] = None
    playlist_id: Optional[str] = None
    order: str = "original"


@dataclass
class TransferResult:
    status: str
    playlist_id: str
    count: int = 0
    is_new: bool = False
    detail: Optional[str] = None
    account: Optional[dict] = None

    def to_dict(self):
        return asdict(self)


class MusicServiceAdapter(Protocol):
    service_id: str

    def auth_status(self, session: dict) -> ServiceProfile:
        ...

    def list_playlists(self, session: dict) -> List[PlaylistRef]:
        ...

    def read_tracks(self, session: dict, payload: dict) -> dict:
        ...

    def search_track(self, session: dict, query: str) -> Optional[ResolvedTrack]:
        ...

    def create_playlist(self, session: dict, name: str) -> str:
        ...

    def add_tracks(self, session: dict, playlist_id: str, track_ids: List[str]) -> bool:
        ...


SERVICE_CATALOG = [
    {
        "id": "manual",
        "name": "Список вручную",
        "short_name": "Вручную",
        "roles": ["source"],
        "enabled": True,
        "auth_required": False,
        "status": "ready",
        "note": "Вставка списка или TXT-файл.",
    },
    {
        "id": "yandex",
        "name": "Яндекс.Музыка",
        "short_name": "Яндекс",
        "roles": ["source"],
        "enabled": True,
        "auth_required": False,
        "status": "ready",
        "note": "Публичные ссылки работают без входа; «Мне нравится» требует вход.",
    },
    {
        "id": "vk",
        "name": "VK Музыка",
        "short_name": "VK",
        "roles": ["source"],
        "enabled": False,
        "auth_required": False,
        "status": "planned",
        "note": "Пока отложено: используйте ручную вставку списка.",
    },
    {
        "id": "qobuz",
        "name": "Qobuz",
        "short_name": "Qobuz",
        "roles": ["destination"],
        "enabled": True,
        "auth_required": True,
        "status": "ready",
        "note": "Создание и пополнение плейлистов.",
    },
    {
        "id": "spotify",
        "name": "Spotify",
        "short_name": "Spotify",
        "roles": ["destination"],
        "enabled": True,
        "auth_required": True,
        "status": "beta",
        "note": "OAuth, поиск треков, создание и пополнение плейлистов.",
    },
    {
        "id": "deezer",
        "name": "Deezer",
        "short_name": "Deezer",
        "roles": ["destination"],
        "enabled": False,
        "auth_required": True,
        "status": "planned",
        "note": "Будет добавлен после Spotify.",
    },
    {
        "id": "apple",
        "name": "Apple Music",
        "short_name": "Apple",
        "roles": ["destination"],
        "enabled": False,
        "auth_required": True,
        "status": "planned",
        "note": "Нужны Apple Developer credentials и MusicKit flow.",
    },
]
