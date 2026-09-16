# ============================================================
# KAWAII_CHAN BACKEND
# APP.PY — ЧАСТЬ 1/4
# ============================================================

import os
import json
import sqlite3
import threading
import time
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from flask import (
    Flask,
    request,
    jsonify,
    Response,
    redirect,
    send_file
)

from flask_cors import CORS


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    ""
).strip()


CHANNEL_USERNAME = os.environ.get(
    "CHANNEL_USERNAME",
    "@ahegao_hetai_hub"
).strip()


STORAGE_CHAT_ID = os.environ.get(
    "STORAGE_CHAT_ID",
    "-1003992513200"
).strip()


DB_PATH = os.environ.get(
    "DB_PATH",
    "kawaii_chan.db"
).strip()


UPLOAD_DIR = Path(
    os.environ.get(
        "UPLOAD_DIR",
        "/tmp/kawaii_uploads"
    )
)


UPLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)


MINI_APP_URL = os.environ.get(
    "MINI_APP_URL",
    "https://aobedonazarik-commits.github.io/kawaii-chan-mini-app/"
).strip().rstrip("/")


BACKEND_URL = os.environ.get(
    "BACKEND_URL",
    "https://kawaii-chan-backend.onrender.com"
).strip().rstrip("/")


# ============================================================
# ЛИМИТЫ
# ============================================================

MAX_MEDIA = 100

# Максимальное количество страниц
# одного большого альбома.
MAX_ALBUM_ITEMS = 200

# Максимальный размер обычного видео.
MAX_VIDEO_SIZE = 50 * 1024 * 1024

# Максимальный размер одного файла альбома.
MAX_ALBUM_FILE_SIZE = 20 * 1024 * 1024

# Telegram sendMediaGroup принимает максимум
# 10 элементов за один запрос.
TELEGRAM_MEDIA_GROUP_SIZE = 10

# Небольшая пауза между большими группами.
TELEGRAM_GROUP_DELAY = 0.7


TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


CORS(
    app,
    resources={
        r"/*": {
            "origins": "*"
        }
    }
)


# ============================================================
# БЛОКИРОВКА БАЗЫ
# ============================================================

db_lock = threading.Lock()


# ============================================================
# ОБЩИЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def now_iso():
    """
    Возвращает текущее время UTC
    в ISO-формате.
    """

    return datetime.now(
        timezone.utc
    ).isoformat()


def safe_json(value):
    """
    Безопасное преобразование значения
    в JSON-строку.
    """

    try:
        return json.dumps(
            value,
            ensure_ascii=False
        )

    except Exception:
        return "{}"


def parse_json(value, default=None):
    """
    Безопасно читает JSON.
    """

    if default is None:
        default = {}

    if not value:
        return default

    try:
        return json.loads(value)

    except Exception:
        return default


def get_int(value, default=0):
    """
    Безопасно преобразует значение в int.
    """

    try:
        return int(value)

    except Exception:
        return default


def get_float(value, default=0.0):
    """
    Безопасно преобразует значение в float.
    """

    try:
        return float(value)

    except Exception:
        return default


# ============================================================
# SQLITE
# ============================================================

def get_db():
    """
    Открывает соединение с SQLite.

    Каждый запрос получает своё соединение.
    """

    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )

    connection.row_factory = sqlite3.Row

    return connection


def db_execute(
    query,
    params=(),
    fetchone=False,
    fetchall=False,
    commit=False
):
    """
    Универсальная функция работы с SQLite.
    """

    with db_lock:

        connection = get_db()

        try:

            cursor = connection.cursor()

            cursor.execute(
                query,
                params
            )


            if commit:

                connection.commit()


            if fetchone:

                row = cursor.fetchone()

                return (
                    dict(row)
                    if row
                    else None
                )


            if fetchall:

                rows = cursor.fetchall()

                return [
                    dict(row)
                    for row in rows
                ]


            return cursor.lastrowid

        finally:

            connection.close()


# ============================================================
# СОЗДАНИЕ БАЗЫ
# ============================================================

def init_db():

    with db_lock:

        connection = get_db()

        try:

            cursor = connection.cursor()


            # ------------------------------------------------
            # ОБЫЧНЫЕ ПОСТЫ
            # ------------------------------------------------

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS posts (

                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    text TEXT DEFAULT '',

                    media_json TEXT DEFAULT '[]',

                    telegram_message_id INTEGER,

                    created_at TEXT NOT NULL

                )
            """)


            # ------------------------------------------------
            # ОТЛОЖЕННЫЕ ПОСТЫ
            # ------------------------------------------------

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_posts (

                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    text TEXT DEFAULT '',

                    media_json TEXT DEFAULT '[]',

                    scheduled_at TEXT NOT NULL,

                    status TEXT DEFAULT 'pending',

                    created_at TEXT NOT NULL,

                    published_at TEXT

                )
            """)


            # ------------------------------------------------
            # АЛЬБОМЫ
            # ------------------------------------------------

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS albums (

                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    title TEXT NOT NULL,

                    description TEXT DEFAULT '',

                    created_at TEXT NOT NULL,

                    updated_at TEXT NOT NULL,

                    published_message_id INTEGER,

                    status TEXT DEFAULT 'draft'

                )
            """)


            # ------------------------------------------------
            # СТРАНИЦЫ АЛЬБОМОВ
            # ------------------------------------------------

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS album_items (

                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    album_id INTEGER NOT NULL,

                    position INTEGER NOT NULL,

                    type TEXT NOT NULL,

                    file_id TEXT,

                    storage_message_id INTEGER,

                    created_at TEXT NOT NULL,

                    FOREIGN KEY(album_id)
                        REFERENCES albums(id)
                        ON DELETE CASCADE

                )
            """)


            # ------------------------------------------------
            # ДОПОЛНИТЕЛЬНЫЕ ПОЛЯ
            # ------------------------------------------------

            cursor.execute("""
                PRAGMA table_info(album_items)
            """)

            columns = {
                row[1]
                for row in cursor.fetchall()
            }


            if "storage_kind" not in columns:

                cursor.execute("""
                    ALTER TABLE album_items
                    ADD COLUMN storage_kind TEXT
                """)


            if "mime_type" not in columns:

                cursor.execute("""
                    ALTER TABLE album_items
                    ADD COLUMN mime_type TEXT
                """)


            if "file_name" not in columns:

                cursor.execute("""
                    ALTER TABLE album_items
                    ADD COLUMN file_name TEXT
                """)


            if "file_size" not in columns:

                cursor.execute("""
                    ALTER TABLE album_items
                    ADD COLUMN file_size INTEGER
                """)


            # ------------------------------------------------
            # ИНДЕКСЫ
            # ------------------------------------------------

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_album_items_album_position
                ON album_items(album_id, position)
            """)


            cursor.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_scheduled_posts_status_time
                ON scheduled_posts(status, scheduled_at)
            """)


            connection.commit()


        finally:

            connection.close()


# Инициализация базы при запуске.
init_db()


# ============================================================
# TELEGRAM API
# ============================================================

def telegram(
    method,
    payload=None,
    files=None,
    timeout=120
):
    """
    Универсальный запрос к Telegram Bot API.
    """

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN не настроен"
        )


    url = (
        f"{TELEGRAM_API}/{method}"
    )


    payload = payload or {}


    try:

        if files:

            response = requests.post(
                url,
                data=payload,
                files=files,
                timeout=timeout
            )

        else:

            response = requests.post(
                url,
                json=payload,
                timeout=timeout
            )


    except requests.RequestException as error:

        raise RuntimeError(
            f"Ошибка соединения с Telegram: {error}"
        )


    try:

        data = response.json()

    except Exception:

        raise RuntimeError(
            f"Telegram вернул некорректный ответ "
            f"(HTTP {response.status_code})"
        )


    if not data.get("ok"):

        description = data.get(
            "description",
            "Неизвестная ошибка Telegram"
        )


        error_code = data.get(
            "error_code",
            response.status_code
        )


        raise RuntimeError(
            f"Telegram API error "
            f"{error_code}: {description}"
        )


    return data.get(
        "result"
    )


# ============================================================
# РАБОТА С ВРЕМЕННЫМИ ФАЙЛАМИ
# ============================================================

def save_temp_file(
    uploaded_file
):
    """
    Сохраняет загруженный Flask-файл
    во временную директорию.
    """

    if not uploaded_file:
        return None


    original_name = (
        uploaded_file.filename
        or "upload"
    )


    safe_name = (
        Path(original_name)
        .name
        .replace(
            " ",
            "_"
        )
    )


    timestamp = (
        int(time.time() * 1000)
    )


    filename = (
        f"{timestamp}_"
        f"{safe_name}"
    )


    path = (
        UPLOAD_DIR /
        filename
    )


    uploaded_file.save(
        path
    )


    return path


def cleanup_files(
    paths
):
    """
    Удаляет временные файлы.
    """

    for path in paths or []:

        try:

            Path(path).unlink(
                missing_ok=True
            )

        except Exception:

            pass


# ============================================================
# ТИП ФАЙЛА
# ============================================================

def detect_file_type(
    filename="",
    mime_type=""
):
    """
    Определяет photo/video по MIME
    или расширению.
    """

    mime_type = (
        mime_type or ""
    ).lower()


    if mime_type.startswith(
        "video/"
    ):

        return "video"


    if mime_type.startswith(
        "image/"
    ):

        return "photo"


    guessed_type, _ = (
        mimetypes.guess_type(
            filename or ""
        )
    )


    if guessed_type:

        if guessed_type.startswith(
            "video/"
        ):

            return "video"


        if guessed_type.startswith(
            "image/"
        ):

            return "photo"


    extension = (
        Path(
            filename or ""
        ).suffix.lower()
    )


    video_extensions = {
        ".mp4",
        ".mov",
        ".m4v",
        ".webm",
        ".avi",
        ".mkv"
    }


    if extension in video_extensions:

        return "video"


    return "photo"


# ============================================================
# ПОЛУЧЕНИЕ ЗАГРУЖЕННЫХ ФАЙЛОВ
# ============================================================

def get_uploaded_media():
    """
    Получает все файлы из multipart/form-data.

    Поддерживает повторяющееся поле files,
    которое используется Mini App
    для нескольких страниц альбома.
    """

    files = request.files.getlist(
        "files"
    )


    # Совместимость со старым frontend,
    # если он отправляет media вместо files.

    if not files:

        files = request.files.getlist(
            "media"
        )


    return [
        file
        for file in files
        if file and file.filename
    ]


# ============================================================
# СОХРАНЕНИЕ ФАЙЛА В TELEGRAM STORAGE
# ============================================================

def save_photo_to_storage(
    file_path,
    filename=None
):
    """
    Загружает изображение
    в закрытый Telegram Storage Channel.
    """

    path = Path(file_path)


    with path.open(
        "rb"
    ) as file_handle:

        result = telegram(
            "sendPhoto",
            payload={
                "chat_id": STORAGE_CHAT_ID
            },
            files={
                "photo": (
                    filename or path.name,
                    file_handle,
                    "image/jpeg"
                )
            }
        )


    photo_sizes = result.get("photo", [])


    if not photo_sizes:

        raise RuntimeError(
            "Telegram не вернул photo после загрузки"
        )


    photo = photo_sizes[-1]


    return {
        "type": "photo",
        "file_id": photo.get("file_id"),
        "storage_message_id": result.get("message_id"),
        "mime_type": "image/jpeg",
        "file_name": filename or path.name
    }


# ============================================================
# СОХРАНЕНИЕ ВИДЕО В TELEGRAM STORAGE
# ============================================================

def save_video_to_storage(
    file_path,
    filename=None,
    mime_type=None
):
    """
    Загружает видео в закрытый Telegram Storage Channel.
    """

    path = Path(file_path)


    file_size = path.stat().st_size


    if file_size > MAX_VIDEO_SIZE:

        raise RuntimeError(
            "Видео слишком большое. "
            f"Максимум: {MAX_VIDEO_SIZE // (1024 * 1024)} MB"
        )


    content_type = (
        mime_type
        or mimetypes.guess_type(
            path.name
        )[0]
        or "video/mp4"
    )


    with path.open(
        "rb"
    ) as file_handle:

        result = telegram(
            "sendVideo",
            payload={
                "chat_id": STORAGE_CHAT_ID
            },
            files={
                "video": (
                    filename or path.name,
                    file_handle,
                    content_type
                )
            }
        )


    return {
        "type": "video",
        "file_id": result.get("video", {}).get(
            "file_id"
        ),
        "storage_message_id": result.get(
            "message_id"
        ),
        "mime_type": content_type,
        "file_name": filename or path.name,
        "file_size": file_size
}# ============================================================
# APP.PY — ЧАСТЬ 2/4
# TELEGRAM STORAGE / MEDIA / ОБЫЧНЫЕ ПОСТЫ
# ============================================================


# ============================================================
# СОХРАНЕНИЕ ЛЮБОГО MEDIA-ФАЙЛА
# ============================================================

def save_media_to_storage(
    file_path,
    filename=None,
    mime_type=None
):
    """
    Автоматически определяет тип файла
    и отправляет его в Telegram Storage.
    """

    file_type = detect_file_type(
        filename or Path(file_path).name,
        mime_type or ""
    )


    if file_type == "video":

        return save_video_to_storage(
            file_path,
            filename=filename,
            mime_type=mime_type
        )


    return save_photo_to_storage(
        file_path,
        filename=filename
    )


# ============================================================
# ЗАГРУЗКА REQUEST MEDIA В STORAGE
# ============================================================

def upload_request_media_to_storage(
    uploaded_files
):
    """
    Сохраняет список загруженных Flask-файлов
    в закрытый Telegram Storage Channel.

    Возвращает список объектов media.
    """

    stored = []

    temporary_paths = []


    try:

        for uploaded_file in uploaded_files:

            path = save_temp_file(
                uploaded_file
            )


            if not path:
                continue


            temporary_paths.append(
                path
            )


            filename = (
                uploaded_file.filename
                or path.name
            )


            mime_type = (
                uploaded_file.mimetype
                or ""
            )


            file_size = path.stat().st_size


            file_type = detect_file_type(
                filename,
                mime_type
            )


            if file_type == "video":

                if file_size > MAX_VIDEO_SIZE:

                    raise RuntimeError(
                        f"Видео {filename} "
                        f"слишком большое. "
                        f"Максимум "
                        f"{MAX_VIDEO_SIZE // (1024 * 1024)} MB."
                    )


            stored_file = (
                save_media_to_storage(
                    path,
                    filename=filename,
                    mime_type=mime_type
                )
            )


            stored_file["file_size"] = (
                file_size
            )


            stored_file["mime_type"] = (
                mime_type
                or stored_file.get(
                    "mime_type"
                )
                or ""
            )


            stored_file["file_name"] = (
                filename
            )


            stored.append(
                stored_file
            )


        return stored


    finally:

        cleanup_files(
            temporary_paths
        )


# ============================================================
# СОХРАНЕНИЕ ИСТОРИИ ПОСТА
# ============================================================

def save_post_history(
    text,
    media,
    telegram_message_id=None
):
    """
    Сохраняет опубликованный пост
    в локальную SQLite-базу.
    """

    return db_execute(
        """
        INSERT INTO posts (
            text,
            media_json,
            telegram_message_id,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            text or "",
            safe_json(
                media or []
            ),
            telegram_message_id,
            now_iso()
        ),
        commit=True
    )


# ============================================================
# ОТПРАВКА ОДНОГО MEDIA
# ============================================================

def send_single_media(
    media,
    caption=None
):
    """
    Отправляет сохранённый media-файл
    из Telegram Storage в основной канал.
    """

    media_type = (
        media.get("type")
        or "photo"
    )


    file_id = media.get(
        "file_id"
    )


    if not file_id:

        raise RuntimeError(
            "У media отсутствует Telegram file_id"
        )


    caption = (
        caption
        if caption is not None
        else ""
    )


    if media_type == "video":

        return telegram(
            "sendVideo",
            {
                "chat_id": CHANNEL_USERNAME,
                "video": file_id,
                "caption": caption
            }
        )


    return telegram(
        "sendPhoto",
        {
            "chat_id": CHANNEL_USERNAME,
            "photo": file_id,
            "caption": caption
        }
    )


# ============================================================
# ОТПРАВКА MEDIA GROUP
# ============================================================

def publish_media_group(
    media_items,
    caption=None
):
    """
    Отправляет группу из 2–10 media.

    Telegram официально ограничивает
    один sendMediaGroup десятью элементами.

    Поэтому большие списки здесь
    автоматически разбиваются выше.
    """

    if not media_items:

        return []


    if len(media_items) == 1:

        message = send_single_media(
            media_items[0],
            caption=caption
        )

        return [message]


    if len(media_items) > 10:

        raise RuntimeError(
            "publish_media_group получил "
            "больше 10 элементов"
        )


    media_payload = []


    for index, media in enumerate(
        media_items
    ):

        media_type = (
            media.get("type")
            or "photo"
        )


        file_id = media.get(
            "file_id"
        )


        if not file_id:

            raise RuntimeError(
                f"Media #{index + 1} "
                "не содержит file_id"
            )


        item = {
            "type": media_type,
            "media": file_id
        }


        if (
            index == 0
            and caption
        ):

            item["caption"] = caption
            item["parse_mode"] = "HTML"


        media_payload.append(
            item
        )


    result = telegram(
        "sendMediaGroup",
        {
            "chat_id": CHANNEL_USERNAME,
            "media": json.dumps(
                media_payload,
                ensure_ascii=False
            )
        }
    )


    return result or []


# ============================================================
# ПУБЛИКАЦИЯ СОХРАНЁННЫХ MEDIA
# ============================================================

def publish_stored_media(
    media_items,
    text=""
):
    """
    Публикует сохранённые media
    в основной канал.

    ВАЖНО:

    10 элементов Telegram допускает
    только в рамках одной media group.

    Поэтому 20, 50, 100 файлов
    здесь разбиваются на отдельные группы.
    """

    if not media_items:

        if text:

            message = telegram(
                "sendMessage",
                {
                    "chat_id":
                        CHANNEL_USERNAME,

                    "text":
                        text,

                    "parse_mode":
                        "HTML"
                }
            )

            return [message]


        return []


    results = []


    # --------------------------------------------------------
    # Один файл
    # --------------------------------------------------------

    if len(media_items) == 1:

        results.append(
            send_single_media(
                media_items[0],
                caption=text
            )
        )

        return results


    # --------------------------------------------------------
    # Несколько файлов
    # --------------------------------------------------------

    groups = [
        media_items[
            start:start +
            TELEGRAM_MEDIA_GROUP_SIZE
        ]
        for start in range(
            0,
            len(media_items),
            TELEGRAM_MEDIA_GROUP_SIZE
        )
    ]


    for group_index, group in enumerate(
        groups
    ):

        caption = (
            text
            if group_index == 0
            else ""
        )


        group_result = (
            publish_media_group(
                group,
                caption=caption
            )
        )


        results.extend(
            group_result
        )


        if (
            group_index <
            len(groups) - 1
        ):

            time.sleep(
                TELEGRAM_GROUP_DELAY
            )


    return results


# ============================================================
# ФОРМИРОВАНИЕ MEDIA ДЛЯ БД
# ============================================================

def normalize_media_item(
    media
):
    """
    Приводит media к единому JSON-формату.
    """

    return {
        "type": (
            media.get("type")
            or "photo"
        ),

        "file_id": (
            media.get("file_id")
            or ""
        ),

        "storage_message_id": (
            media.get(
                "storage_message_id"
            )
        ),

        "storage_kind": (
            media.get(
                "storage_kind"
            )
            or media.get(
                "type"
            )
            or "photo"
        ),

        "mime_type": (
            media.get(
                "mime_type"
            )
            or ""
        ),

        "file_name": (
            media.get(
                "file_name"
            )
            or ""
        ),

        "file_size": (
            media.get(
                "file_size"
            )
            or 0
        )
    }


# ============================================================
# ROUTE: ГЛАВНАЯ
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def index():

    return (
        "Kawaii Chan Backend работает!"
    )


# ============================================================
# ROUTE: TEST
# ============================================================

@app.route(
    "/test",
    methods=["GET"]
)
def test():

    return jsonify({
        "success": True,
        "message":
            "Kawaii Chan Backend работает"
    })


# ============================================================
# ROUTE: STORAGE TEST
# ============================================================

@app.route(
    "/storage-test",
    methods=["GET"]
)
def storage_test():

    if not BOT_TOKEN:

        return jsonify({
            "success": False,
            "error":
                "BOT_TOKEN не настроен"
        }), 500


    try:

        result = telegram(
            "getChat",
            {
                "chat_id":
                    STORAGE_CHAT_ID
            }
        )


        return jsonify({
            "success": True,
            "chat": result
        })


    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# ============================================================
# ROUTE: WEBAPP
# ============================================================

@app.route(
    "/webapp",
    methods=["GET"]
)
def webapp():

    return redirect(
        MINI_APP_URL
    )


# ============================================================
# ROUTE: ПУБЛИКАЦИЯ ОБЫЧНОГО ПОСТА
# ============================================================

@app.route(
    "/publish",
    methods=["POST"]
)
def publish():

    temporary_media = []


    try:

        text = (
            request.form.get(
                "text",
                ""
            ).strip()
        )


        uploaded_files = (
            get_uploaded_media()
        )


        if (
            not text
            and not uploaded_files
        ):

            return jsonify({
                "success": False,
                "error":
                    "Нет текста или медиа"
            }), 400


        if len(uploaded_files) > MAX_MEDIA:

            return jsonify({
                "success": False,
                "error":
                    f"Слишком много файлов. "
                    f"Максимум {MAX_MEDIA}."
            }), 400


        stored_media = (
            upload_request_media_to_storage(
                uploaded_files
            )
        )


        normalized_media = [
            normalize_media_item(
                item
            )
            for item in stored_media
        ]


        messages = (
            publish_stored_media(
                normalized_media,
                text=text
            )
        )


        first_message_id = None


        if messages:

            first_message_id = (
                messages[0].get(
                    "message_id"
                )
                if isinstance(
                    messages[0],
                    dict
                )
                else None
            )


        save_post_history(
            text=text,
            media=normalized_media,
            telegram_message_id=
                first_message_id
        )


        return jsonify({
            "success": True,
            "message":
                "Пост опубликован",
            "messages_count":
                len(messages),
            "media_count":
                len(normalized_media)
        })


    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


    finally:

        cleanup_files(
            temporary_media
        )


# ============================================================
# ROUTE: СОВМЕСТИМОСТЬ /posts
# ============================================================

@app.route(
    "/posts",
    methods=["POST"]
)
def posts_alias():

    return publish()


# ============================================================
# ПОЛУЧЕНИЕ ПОСТОВ ИЗ БД
# ============================================================

def get_recent_posts(
    limit=50
):

    limit = max(
        1,
        min(
            int(limit),
            500
        )
    )


    return db_execute(
        """
        SELECT
            id,
            text,
            media_json,
            telegram_message_id,
            created_at
        FROM posts
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
        fetchall=True
    )


# ============================================================
# ROUTE: ИСТОРИЯ ПОСТОВ
# ============================================================

@app.route(
    "/posts",
    methods=["GET"]
)
def get_posts():

    try:

        limit = get_int(
            request.args.get(
                "limit",
                50
            ),
            50
        )


        posts = (
            get_recent_posts(
                limit
            )
        )


        for post in posts:

            post["media"] = parse_json(
                post.get(
                    "media_json"
                ),
                []
            )


            post.pop(
                "media_json",
                None
            )


        return jsonify({
            "success": True,
            "posts": posts
        })


    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500# ============================================================
# APP.PY — ЧАСТЬ 3/4
# АЛЬБОМЫ / READER / STORAGE / ПУБЛИКАЦИЯ
# ============================================================


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ АЛЬБОМОВ
# ============================================================

def album_row_to_dict(row):
    """
    Преобразует строку albums в обычный dict.
    """

    if not row:
        return None

    return {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "published_message_id":
            row.get("published_message_id"),
        "status":
            row.get("status") or "draft"
    }


def album_item_to_dict(row):
    """
    Преобразует строку album_items
    в объект, который понимает Mini App.
    """

    if not row:
        return None

    item_type = (
        row.get("type")
        or row.get("storage_kind")
        or "photo"
    )

    mime_type = (
        row.get("mime_type")
        or ""
    )

    if (
        not item_type
        and mime_type
    ):
        item_type = detect_file_type(
            row.get("file_name") or "",
            mime_type
        )

    return {
        "id": row.get("id"),
        "album_id": row.get("album_id"),
        "position": row.get("position"),
        "type": item_type,
        "mime_type": mime_type,
        "file_name":
            row.get("file_name") or "",
        "file_size":
            row.get("file_size") or 0,
        "file_id":
            row.get("file_id") or "",
        "storage_message_id":
            row.get("storage_message_id")
    }


def get_album(album_id):
    """
    Получает альбом без страниц.
    """

    return db_execute(
        """
        SELECT
            id,
            title,
            description,
            created_at,
            updated_at,
            published_message_id,
            status
        FROM albums
        WHERE id = ?
        """,
        (album_id,),
        fetchone=True
    )


def get_album_items(album_id):
    """
    Получает все страницы альбома
    в правильном порядке.
    """

    return db_execute(
        """
        SELECT
            id,
            album_id,
            position,
            type,
            file_id,
            storage_message_id,
            created_at,
            storage_kind,
            mime_type,
            file_name,
            file_size
        FROM album_items
        WHERE album_id = ?
        ORDER BY position ASC, id ASC
        """,
        (album_id,),
        fetchall=True
    )


def album_public_data(
    album_row,
    include_items=False
):
    """
    Формирует JSON альбома
    для Mini App.
    """

    if not album_row:
        return None

    album = album_row_to_dict(
        album_row
    )

    item_rows = get_album_items(
        album["id"]
    )

    album["item_count"] = len(
        item_rows
    )

    album["pages"] = len(
        item_rows
    )

    if include_items:

        album["items"] = [
            album_item_to_dict(row)
            for row in item_rows
        ]

    else:

        album["items"] = []

    return album


# ============================================================
# СОЗДАНИЕ АЛЬБОМА
# ============================================================

@app.route(
    "/albums",
    methods=["POST"]
)
def create_album():

    temporary_paths = []


    try:

        title = (
            request.form.get(
                "title",
                ""
            ).strip()
        )


        description = (
            request.form.get(
                "description",
                ""
            ).strip()
        )


        if not title:

            return jsonify({
                "success": False,
                "error":
                    "Введите название альбома"
            }), 400


        uploaded_files = (
            get_uploaded_media()
        )


        if not uploaded_files:

            return jsonify({
                "success": False,
                "error":
                    "Добавьте хотя бы одну страницу"
            }), 400


        if len(uploaded_files) > MAX_ALBUM_ITEMS:

            return jsonify({
                "success": False,
                "error":
                    f"Максимум "
                    f"{MAX_ALBUM_ITEMS} страниц"
            }), 400


        # ----------------------------------------------------
        # Создаём запись альбома СНАЧАЛА.
        # ----------------------------------------------------

        timestamp = now_iso()


        album_id = db_execute(
            """
            INSERT INTO albums (
                title,
                description,
                created_at,
                updated_at,
                status
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                title,
                description,
                timestamp,
                timestamp,
                "draft"
            ),
            commit=True
        )


        try:

            # ------------------------------------------------
            # Каждая страница загружается отдельно.
            #
            # Никаких sendMediaGroup на 21–200 файлов.
            # ------------------------------------------------

            for position, uploaded_file in enumerate(
                uploaded_files,
                start=1
            ):

                if not uploaded_file:
                    continue


                path = save_temp_file(
                    uploaded_file
                )


                if not path:
                    raise RuntimeError(
                        f"Не удалось сохранить "
                        f"страницу #{position}"
                    )


                temporary_paths.append(
                    path
                )


                filename = (
                    uploaded_file.filename
                    or f"page_{position}"
                )


                mime_type = (
                    uploaded_file.mimetype
                    or mimetypes.guess_type(
                        filename
                    )[0]
                    or ""
                )


                file_size = path.stat().st_size


                if file_size <= 0:

                    raise RuntimeError(
                        f"Страница #{position} "
                        "пустая"
                    )


                if file_size <= 0:
                raise RuntimeError(
                    f"Страница #{position} пустая"
                )

                if file_size > MAX_ALBUM_FILE_SIZE:
                raise RuntimeError(
                    f"Страница #{position} "
                    f"слишком большая. "
                    f"Максимум {MAX_ALBUM_FILE_SIZE // (1024 * 1024)} MB."
                )

             file_type = detect_file_type(
                filename,
                mime_type
             )



                # --------------------------------------------
                # Загружаем страницу отдельно.
                # --------------------------------------------

                stored = (
                    save_media_to_storage(
                        path,
                        filename=filename,
                        mime_type=mime_type
                    )
                )


                # --------------------------------------------
                # Сохраняем Telegram file_id.
                # --------------------------------------------

                db_execute(
                    """
                    INSERT INTO album_items (
                        album_id,
                        position,
                        type,
                        file_id,
                        storage_message_id,
                        created_at,
                        storage_kind,
                        mime_type,
                        file_name,
                        file_size
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        album_id,
                        position,
                        file_type,
                        stored.get(
                            "file_id"
                        ),
                        stored.get(
                            "storage_message_id"
                        ),
                        now_iso(),
                        file_type,
                        mime_type,
                        filename,
                        file_size
                    ),
                    commit=True
                )


            # ------------------------------------------------
            # Обновляем время изменения.
            # ------------------------------------------------

            db_execute(
                """
                UPDATE albums
                SET updated_at = ?
                WHERE id = ?
                """,
                (
                    now_iso(),
                    album_id
                ),
                commit=True
            )


        except Exception:

            # Если одна из страниц не загрузилась,
            # удаляем созданный альбом и его записи.

            db_execute(
                """
                DELETE FROM album_items
                WHERE album_id = ?
                """,
                (album_id,),
                commit=True
            )


            db_execute(
                """
                DELETE FROM albums
                WHERE id = ?
                """,
                (album_id,),
                commit=True
            )


            raise


        album = get_album(
            album_id
        )


        return jsonify({
            "success": True,
            "message":
                "Альбом создан",
            "album":
                album_public_data(
                    album,
                    include_items=False
                )
        })


    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


    finally:

        cleanup_files(
            temporary_paths
        )


# ============================================================
# ПОЛУЧЕНИЕ СПИСКА АЛЬБОМОВ
# ============================================================

@app.route(
    "/albums",
    methods=["GET"]
)
def list_albums():

    try:

        rows = db_execute(
            """
            SELECT
                id,
                title,
                description,
                created_at,
                updated_at,
                published_message_id,
                status
            FROM albums
            ORDER BY id DESC
            """,
            fetchall=True
        )


        albums = []


        for row in rows:

            album = (
                album_public_data(
                    row,
                    include_items=False
                )
            )


            albums.append(
                album
            )


        return jsonify({
            "success": True,
            "albums": albums
        })


    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# ============================================================
# ПОЛУЧЕНИЕ ОДНОГО АЛЬБОМА
# ============================================================

@app.route(
    "/albums/<int:album_id>",
    methods=["GET"]
)
def get_album_route(
    album_id
):

    try:

        album = get_album(
            album_id
        )


        if not album:

            return jsonify({
                "success": False,
                "error":
                    "Альбом не найден"
            }), 404


        data = (
            album_public_data(
                album,
                include_items=True
            )
        )


        # ----------------------------------------------------
        # Для каждой страницы формируем URL reader endpoint.
        # ----------------------------------------------------

        for item in data["items"]:

            item["url"] = (
                f"{BACKEND_URL}"
                f"/albums/"
                f"{album_id}"
                f"/items/"
                f"{item['id']}"
                f"/file"
            )


        return jsonify({
            "success": True,
            "album": data
        })


    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# ============================================================
# ОТДАЧА ФАЙЛА СТРАНИЦЫ READER
# ============================================================

@app.route(
    "/albums/<int:album_id>/items/<int:item_id>/file",
    methods=["GET"]
)
def album_item_file(
    album_id,
    item_id
):

    try:

        item = db_execute(
            """
            SELECT
                id,
                album_id,
                position,
                type,
                file_id,
                storage_message_id,
                mime_type,
                file_name,
                file_size
            FROM album_items
            WHERE id = ?
              AND album_id = ?
            """,
            (
                item_id,
                album_id
            ),
            fetchone=True
        )


        if not item:

            return jsonify({
                "success": False,
                "error":
                    "Страница не найдена"
            }), 404


        file_id = (
            item.get("file_id")
            or ""
        )


        if not file_id:

            return jsonify({
                "success": False,
                "error":
                    "У страницы нет file_id"
            }), 404


        # ----------------------------------------------------
        # Получаем Telegram File.
        # ----------------------------------------------------

        file_info = telegram(
            "getFile",
            {
                "file_id": file_id
            }
        )


        telegram_file_path = (
            file_info.get(
                "file_path"
            )
        )


        if not telegram_file_path:

            raise RuntimeError(
                "Telegram не вернул file_path"
            )


        download_url = (
            f"https://api.telegram.org/"
            f"file/bot{BOT_TOKEN}/"
            f"{telegram_file_path}"
        )


        # ----------------------------------------------------
        # Потоковая загрузка файла.
        # ----------------------------------------------------

        response = requests.get(
            download_url,
            stream=True,
            timeout=120
        )


        response.raise_for_status()


        content_type = (
            item.get(
                "mime_type"
            )
            or mimetypes.guess_type(
                item.get(
                    "file_name"
                ) or ""
            )[0]
            or (
                "video/mp4"
                if item.get("type") == "video"
                else "image/jpeg"
            )
        )


        def generate():

            for chunk in response.iter_content(
                chunk_size=1024 * 256
            ):

                if chunk:

                    yield chunk


        headers = {
            "Cache-Control":
                "public, max-age=86400"
        }


        if item.get("file_size"):

            headers["Content-Length"] = str(
                item.get("file_size")
            )


        return Response(
            generate(),
            mimetype=content_type,
            headers=headers
        )


    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# ============================================================
# СОЗДАНИЕ ССЫЛКИ MAIN MINI APP
# ============================================================

def album_deep_link(
    album_id
):
    """
    Формирует ссылку:

    https://t.me/KawaiiChanAsserBot?startapp=album_123

    Это Main Mini App deep link.
    """

    return (
        "https://t.me/"
        "KawaiiChanAsserBot"
        "?startapp="
        f"album_{album_id}"
    )


# ============================================================
# ПУБЛИКАЦИЯ ОБЛОЖКИ АЛЬБОМА
# ============================================================

def publish_album_cover(
    album_id,
    title,
    description,
    cover_file_id,
    page_count
):
    """
    Публикует только обложку альбома
    в основной канал.

    Все остальные страницы остаются
    в Storage и открываются через Mini App.
    """

    deep_link = album_deep_link(
        album_id
    )


    caption_parts = []


    if title:

        caption_parts.append(
            f"<b>{title}</b>"
        )


    if description:

        caption_parts.append(
            description
        )


    caption_parts.append(
        f"📖 Страниц: {page_count}"
    )


    caption = "\n\n".join(
        caption_parts
    )


    keyboard = {
        "inline_keyboard": [[
            {
                "text":
                    "📖 ОТКРЫТЬ АЛЬБОМ",
                "url":
                    deep_link
            }
        ]]
    }


    result = telegram(
        "sendPhoto",
        {
            "chat_id":
                CHANNEL_USERNAME,

            "photo":
                cover_file_id,

            "caption":
                caption,

            "parse_mode":
                "HTML",

            "reply_markup":
                json.dumps(
                    keyboard,
                    ensure_ascii=False
                )
        }
    )


    return result


# ============================================================
# ROUTE: ПУБЛИКАЦИЯ АЛЬБОМА
# ============================================================

@app.route(
    "/albums/<int:album_id>/publish",
    methods=["POST"]
)
def publish_album(
    album_id
):

    try:

        album = get_album(
            album_id
        )


        if not album:

            return jsonify({
                "success": False,
                "error":
                    "Альбом не найден"
            }), 404


        items = get_album_items(
            album_id
        )


        if not items:

            return jsonify({
                "success": False,
                "error":
                    "В альбоме нет страниц"
            }), 400


        # ----------------------------------------------------
        # Первая страница = обложка.
        # ----------------------------------------------------

        cover = items[0]


        cover_file_id = (
            cover.get("file_id")
            or ""
        )


        if not cover_file_id:

            return jsonify({
                "success": False,
                "error":
                    "У первой страницы "
                    "нет Telegram file_id"
            }), 500


        message = (
            publish_album_cover(
                album_id=album_id,
                title=album.get(
                    "title"
                ) or "",
                description=album.get(
                    "description"
                ) or "",
                cover_file_id=
                    cover_file_id,
                page_count=len(items)
            )
        )


        message_id = (
            message.get(
                "message_id"
            )
            if isinstance(
                message,
                dict
            )
            else None
        )


        db_execute(
            """
            UPDATE albums
            SET
                published_message_id = ?,status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                message_id,
                "published",
                now_iso(),
                album_id
            ),
            commit=True
        )


        return jsonify({
            "success": True,
            "message":
                "Альбом опубликован",
            "album_id":
                album_id,
            "message_id":
                message_id,
            "pages":
                len(items),
            "deep_link":
                album_deep_link(
                    album_id
                )
        })


    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# ============================================================
# ROUTE: УДАЛЕНИЕ АЛЬБОМА
# ============================================================

@app.route(
    "/albums/<int:album_id>",
    methods=["DELETE"]
)
def delete_album(
    album_id
):

    try:

        album = get_album(
            album_id
        )


        if not album:

            return jsonify({
                "success": False,
                "error":
                    "Альбом не найден"
            }), 404


        # ----------------------------------------------------
        # Удаляем страницы.
        # ----------------------------------------------------

        db_execute(
            """
            DELETE FROM album_items
            WHERE album_id = ?
            """,
            (album_id,),
            commit=True
        )


        # ----------------------------------------------------
        # Удаляем сам альбом.
        # ----------------------------------------------------

        db_execute(
            """
            DELETE FROM albums
            WHERE id = ?
            """,
            (album_id,),
            commit=True
        )


        return jsonify({
            "success": True,
            "message":
                "Альбом удалён"
        })


    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# ============================================================
# ROUTE: АЛЬБОМ — ПРОСТАЯ ИНФОРМАЦИЯ
# ============================================================

@app.route(
    "/albums/<int:album_id>/info",
    methods=["GET"]
)
def album_info(
    album_id
):

    try:

        album = get_album(
            album_id
        )


        if not album:

            return jsonify({
                "success": False,
                "error":
                    "Альбом не найден"
            }), 404


        items = get_album_items(
            album_id
        )


        return jsonify({
            "success": True,
            "album": {
                **album_row_to_dict(
                    album
                ),
                "item_count":
                    len(items),
                "pages":
                    len(items)
            }
        })


    except Exception as error:

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500# ===== SCHEDULE =====

def parse_scheduled_at(value):
    if not value:
        raise ValueError("Не указано время публикации")

    value = str(value).strip()

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

        return dt

    except Exception:
        raise ValueError("Неверный формат времени. Используй YYYY-MM-DDTHH:MM")


@app.post("/schedule")
def schedule_post():
    temporary_media = []

    try:
        scheduled_at_raw = request.form.get("scheduled_at", "").strip()
        caption = request.form.get("caption", "").strip()
        text = request.form.get("text", "").strip()

        if not caption and text:
            caption = text

        if not scheduled_at_raw:
            return {"success": False, "error": "Не указано время публикации"}, 400

        scheduled_at = parse_scheduled_at(scheduled_at_raw)

        if scheduled_at <= datetime.utcnow():
            return {
                "success": False,
                "error": "Время публикации должно быть в будущем"
            }, 400

        files = get_uploaded_media()

        if len(files) > MAX_MEDIA:
            return {
                "success": False,
                "error": f"Можно прикрепить максимум {MAX_MEDIA} файлов"
            }, 400

        media_items = []

        if files:
            media_items = upload_request_media_to_storage(
                files,
                caption=caption
            )

        post_id = db_execute(
            """
            INSERT INTO scheduled_posts
            (caption, media_json, scheduled_at, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (
                caption,
                json.dumps(media_items, ensure_ascii=False),
                scheduled_at.isoformat(),
                datetime.utcnow().isoformat()
            ),
            commit=True
        )

        return {
            "success": True,
            "id": post_id,
            "scheduled_at": scheduled_at.isoformat(),
            "media_count": len(media_items)
        }

    except Exception as e:
        cleanup_files(temporary_media)

        return {
            "success": False,
            "error": str(e)
        }, 500


@app.get("/scheduled")
def get_scheduled():
    try:
        rows = db_execute(
            """
            SELECT
                id,
                caption,
                media_json,
                scheduled_at,
                status,
                created_at,
                published_at
            FROM scheduled_posts
            ORDER BY scheduled_at ASC
            """,
            fetch=True
        )

        result = []

        for row in rows:
            media = []

            try:
                media = json.loads(row["media_json"] or "[]")
            except Exception:
                media = []

            result.append({
                "id": row["id"],
                "caption": row["caption"] or "",
                "text": row["caption"] or "",
                "media": media,
                "media_count": len(media),
                "scheduled_at": row["scheduled_at"],
                "status": row["status"],
                "created_at": row["created_at"],
                "published_at": row["published_at"]
            })

        return {
            "success": True,
            "posts": result
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }, 500


@app.delete("/scheduled/<int:scheduled_id>")
def delete_scheduled(scheduled_id):
    try:
        row = db_execute(
            """
            SELECT id, status
            FROM scheduled_posts
            WHERE id = ?
            """,
            (scheduled_id,),
            fetch_one=True
        )

        if not row:
            return {
                "success": False,
                "error": "Отложенный пост не найден"
            }, 404

        if row["status"] == "published":
            return {
                "success": False,
                "error": "Опубликованный пост нельзя удалить как отложенный"
            }, 400

        db_execute(
            "DELETE FROM scheduled_posts WHERE id = ?",
            (scheduled_id,),
            commit=True
        )

        return {
            "success": True,
            "deleted": scheduled_id
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }, 500


# ===== SCHEDULER =====

def claim_due_scheduled_posts():
    """
    Забирает только те посты, которые ещё находятся в pending.
    Это защищает от повторной публикации одного и того же поста.
    """

    now = datetime.utcnow().isoformat()

    with db_lock:
        conn = get_db()

        try:
            rows = conn.execute(
                """
                SELECT
                    id,
                    caption,
                    media_json,
                    scheduled_at,
                    status
                FROM scheduled_posts
                WHERE status = 'pending'
                  AND scheduled_at <= ?
                ORDER BY scheduled_at ASC
                """,
                (now,)
            ).fetchall()

            claimed = []

            for row in rows:
                cursor = conn.execute(
                    """
                    UPDATE scheduled_posts
                    SET status = 'processing'
                    WHERE id = ?
                      AND status = 'pending'
                    """,
                    (row["id"],)
                )

                if cursor.rowcount == 1:
                    claimed.append(dict(row))

            conn.commit()

            return claimed

        finally:
            conn.close()


def mark_scheduled_published(post_id):
    db_execute(
        """
        UPDATE scheduled_posts
        SET status = 'published',
            published_at = ?
        WHERE id = ?
        """,
        (
            datetime.utcnow().isoformat(),
            post_id
        ),
        commit=True
    )


def mark_scheduled_failed(post_id, error_text):
    try:
        db_execute(
            """
            UPDATE scheduled_posts
            SET status = 'failed'
            WHERE id = ?
            """,
            (post_id,),
            commit=True
        )
    except Exception:
        pass

    print(
        f"[SCHEDULER] Пост #{post_id} не опубликован: {error_text}",
        flush=True
    )


def process_scheduled_post(row):
    post_id = row["id"]

    try:
        media = []

        try:
            media = json.loads(row["media_json"] or "[]")
        except Exception:
            media = []

        caption = row["caption"] or ""

        print(
            f"[SCHEDULER] Публикация отложенного поста #{post_id}",
            flush=True
        )

        if media:
            publish_stored_media(
                media,
                caption=caption
            )
        else:
            result = telegram(
                "sendMessage",
                {
                    "chat_id": CHANNEL_USERNAME,
                    "text": caption or " "
                }
            )

            if not result.get("ok"):
                raise RuntimeError(
                    result.get("description", "Telegram sendMessage error")
                )

        mark_scheduled_published(post_id)

        print(
            f"[SCHEDULER] Пост #{post_id} опубликован",
            flush=True
        )

    except Exception as e:
        mark_scheduled_failed(post_id, str(e))


def scheduler_loop():
    print("[SCHEDULER] Планировщик запущен", flush=True)

    while True:
        try:
            posts = claim_due_scheduled_posts()

            for row in posts:
                process_scheduled_post(row)

        except Exception as e:
            print(
                f"[SCHEDULER] Ошибка цикла: {e}",
                flush=True
            )

        time.sleep(5)


# ===== STATS =====

@app.get("/stats")
def get_stats():
    try:
        posts_count = db_execute(
            "SELECT COUNT(*) AS count FROM posts",
            fetch_one=True
        )["count"]

        scheduled_pending = db_execute(
            """
            SELECT COUNT(*) AS count
            FROM scheduled_posts
            WHERE status = 'pending'
            """,
            fetch_one=True
        )["count"]

        scheduled_published = db_execute(
            """
            SELECT COUNT(*) AS count
            FROM scheduled_posts
            WHERE status = 'published'
            """,
            fetch_one=True
        )["count"]

        albums_count = db_execute(
            "SELECT COUNT(*) AS count FROM albums",
            fetch_one=True
        )["count"]

        album_pages = db_execute(
            "SELECT COUNT(*) AS count FROM album_items",
            fetch_one=True
        )["count"]

        return {
            "success": True,
            "posts": posts_count,
            "scheduled": scheduled_pending,
            "scheduled_pending": scheduled_pending,
            "scheduled_published": scheduled_published,
            "albums": albums_count,
            "album_pages": album_pages
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }, 500


# ===== SIMPLE EDIT/DELETE SUPPORT =====

@app.delete("/posts/<int:post_id>")
def delete_post(post_id):
    try:
        row = db_execute(
            """
            SELECT id
            FROM posts
            WHERE id = ?
            """,
            (post_id,),
            fetch_one=True
        )

        if not row:
            return {
                "success": False,
                "error": "Пост не найден"
            }, 404

        db_execute(
            "DELETE FROM posts WHERE id = ?",
            (post_id,),
            commit=True
        )

        return {
            "success": True,
            "deleted": post_id
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }, 500


# ===== START SCHEDULER =====

_scheduler_started = False
_scheduler_start_lock = threading.Lock()


def start_scheduler():
    global _scheduler_started

    with _scheduler_start_lock:
        if _scheduler_started:
            return

        thread = threading.Thread(
            target=scheduler_loop,
            daemon=True,
            name="kawaii-scheduler"
        )

        thread.start()

        _scheduler_started = True


start_scheduler()


# ===== RUN =====

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
