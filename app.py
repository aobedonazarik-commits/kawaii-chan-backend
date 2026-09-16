import os
import json
import sqlite3
import threading
import time
import mimetypes

from datetime import datetime, timezone
from pathlib import Path

import requests

from flask import (
    Flask,
    request,
    jsonify,
    Response,
    send_file
)

from flask_cors import CORS


# ============================================================
# CONFIG
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

MINI_APP_URL = os.environ.get(
    "MINI_APP_URL",
    "https://aobedonazarik-commits.github.io/kawaii-chan-mini-app/"
).strip().rstrip("/")

BACKEND_URL = os.environ.get(
    "BACKEND_URL",
    "https://kawaii-chan-backend.onrender.com"
).strip().rstrip("/")


MAX_MEDIA = 100
MAX_ALBUM_ITEMS = 200

MAX_VIDEO_SIZE = 50 * 1024 * 1024
MAX_ALBUM_FILE_SIZE = 20 * 1024 * 1024

TELEGRAM_MEDIA_GROUP_SIZE = 10
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
# DATABASE
# ============================================================

db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def db_execute(
    query,
    params=(),
    fetch=False,
    fetch_one=False,
    commit=False
):
    with db_lock:
        conn = get_db()

        try:
            cursor = conn.execute(
                query,
                params
            )

            if commit:
                conn.commit()

            if fetch_one:
                row = cursor.fetchone()

                if row is None:
                    return None

                return dict(row)

            if fetch:
                return [
                    dict(row)
                    for row in cursor.fetchall()
                ]

            return cursor.lastrowid

        finally:
            conn.close()


def init_db():
    with db_lock:
        conn = get_db()

        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT,
                    media_json TEXT,
                    created_at TEXT
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    caption TEXT,
                    media_json TEXT,
                    scheduled_at TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT,
                    published_at TEXT
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS albums (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    status TEXT DEFAULT 'draft',
                    created_at TEXT,
                    published_at TEXT,
                    published_message_id INTEGER
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS album_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    album_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    type TEXT DEFAULT 'photo',
                    storage_kind TEXT DEFAULT 'photo',
                    file_id TEXT,
                    storage_message_id INTEGER,
                    mime_type TEXT,
                    file_name TEXT,
                    file_size INTEGER DEFAULT 0,
                    FOREIGN KEY(album_id)
                        REFERENCES albums(id)
                        ON DELETE CASCADE
                )
                """
            )

            # ------------------------------------------------
            # Migration for older databases.
            # ------------------------------------------------

            def ensure_column(
                table_name,
                column_name,
                definition
            ):
                columns = conn.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()

                existing = {
                    row["name"]
                    for row in columns
                }

                if column_name not in existing:
                    conn.execute(
                        f"""
                        ALTER TABLE {table_name}
                        ADD COLUMN {column_name}
                        {definition}
                        """
                    )

            ensure_column(
                "posts",
                "text",
                "TEXT"
            )

            ensure_column(
                "posts",
                "media_json",
                "TEXT"
            )

            ensure_column(
                "posts",
                "created_at",
                "TEXT"
            )

            ensure_column(
                "album_items",
                "storage_kind",
                "TEXT DEFAULT 'photo'"
            )

            ensure_column(
                "album_items",
                "mime_type",
                "TEXT"
            )

            ensure_column(
                "album_items",
                "file_name",
                "TEXT"
            )

            ensure_column(
                "album_items",
                "file_size",
                "INTEGER DEFAULT 0"
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_album_items_album
                ON album_items(album_id, position)
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_scheduled_status_time
                ON scheduled_posts(status, scheduled_at)
                """
            )

            conn.commit()

        finally:
            conn.close()


init_db()


# ============================================================
# TELEGRAM API
# ============================================================

def telegram(
    method,
    data=None,
    files=None,
    timeout=120
):
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не установлен"
        )

    url = f"{TELEGRAM_API}/{method}"

    response = requests.post(
        url,
        data=data or {},
        files=files,
        timeout=timeout
    )

    try:
        result = response.json()
    except Exception:
        raise RuntimeError(
            f"Telegram вернул не JSON: "
            f"{response.text[:500]}"
        )

    if not result.get("ok"):
        raise RuntimeError(
            result.get(
                "description",
                f"Telegram API error: {method}"
            )
        )

    return result


# ============================================================
# FILE HELPERS
# ============================================================

def save_temp_file(uploaded_file):
    UPLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    original_name = (
        uploaded_file.filename
        or "upload"
    )

    safe_name = Path(
        original_name
    ).name

    timestamp = str(
        int(time.time() * 1000000)
    )

    path = UPLOAD_DIR / (
        f"{timestamp}_{safe_name}"
    )

    uploaded_file.save(
        str(path)
    )

    return path


def cleanup_files(paths):
    for path in paths:
        try:
            Path(path).unlink(
                missing_ok=True
            )
        except Exception:
            pass


def detect_file_type(
    filename,
    mime_type
):
    mime = (
        mime_type
        or mimetypes.guess_type(filename)[0]
        or ""
    ).lower()

    if mime.startswith("video/"):
        return "video"

    return "photo"


def get_uploaded_media():
    files = request.files.getlist(
        "files"
    )

    if not files:
        files = request.files.getlist(
            "media"
        )

    return [
        item
        for item in files
        if item is not None
        and item.filename
    ]


# ============================================================
# STORAGE CHANNEL
# ============================================================

def save_photo_to_storage(
    path,
    filename=None,
    mime_type=None
):
    with open(path, "rb") as photo_file:
        result = telegram(
            "sendPhoto",
            data={
                "chat_id": STORAGE_CHAT_ID
            },
            files={
                "photo": (
                    filename or Path(path).name,
                    photo_file,
                    mime_type or "image/jpeg"
                )
            }
        )

    message = result["result"]

    photo_sizes = message.get(
        "photo",
        []
    )

    if not photo_sizes:
        raise RuntimeError(
            "Telegram не вернул сохранённую фотографию"
        )

    photo = photo_sizes[-1]

    return {
        "type": "photo",
        "storage_kind": "photo",
        "file_id": photo["file_id"],
        "storage_message_id": message["message_id"],
        "mime_type": mime_type or "image/jpeg",
        "file_name": filename or Path(path).name,
        "file_size": Path(path).stat().st_size
    }


def save_video_to_storage(
    path,
    filename=None,
    mime_type=None
):
    file_size = Path(path).stat().st_size

    if file_size > MAX_VIDEO_SIZE:
        raise RuntimeError(
            f"Видео слишком большое. "
            f"Максимум "
            f"{MAX_VIDEO_SIZE // (1024 * 1024)} MB."
        )

    with open(path, "rb") as video_file:
        result = telegram(
            "sendVideo",
            data={
                "chat_id": STORAGE_CHAT_ID
            },
            files={
                "video": (
                    filename or Path(path).name,
                    video_file,
                    mime_type or "video/mp4"
                )
            }
        )

    message = result["result"]

    video = message.get("video")

    if not video:
        raise RuntimeError(
            "Telegram не вернул сохранённое видео"
        )

    return {
        "type": "video",
        "storage_kind": "video",
        "file_id": video["file_id"],
        "storage_message_id": message["message_id"],
        "mime_type": mime_type or "video/mp4",
        "file_name": filename or Path(path).name,
        "file_size": file_size
    }


def save_media_to_storage(
    path,
    filename=None,
    mime_type=None
):
    file_type = detect_file_type(
        filename or Path(path).name,
        mime_type
    )

    if file_type == "video":
        return save_video_to_storage(
            path,
            filename=filename,
            mime_type=mime_type
        )

    return save_photo_to_storage(
        path,
        filename=filename,
        mime_type=mime_type
    )


def upload_request_media_to_storage(
    files,
    caption=""
):
    stored_items = []
    temporary_paths = []

    try:
        for uploaded_file in files:
            if not uploaded_file:
                continue

            path = save_temp_file(
                uploaded_file
            )

            temporary_paths.append(
                path
            )

            filename = (
                uploaded_file.filename
                or Path(path).name
            )

            mime_type = (
                uploaded_file.mimetype
                or mimetypes.guess_type(
                    filename
                )[0]
                or ""
            )

            stored = save_media_to_storage(
                path,
                filename=filename,
                mime_type=mime_type
            )

            stored_items.append(
                stored
            )

        return stored_items

    finally:
        cleanup_files(
            temporary_paths
        )


# ============================================================
# MEDIA NORMALIZATION
# ============================================================

def normalize_media_item(item):
    if not isinstance(item, dict):
        return None

    file_id = item.get(
        "file_id"
    )

    if not file_id:
        return None

    return {
        "type": item.get(
            "type",
            item.get(
                "storage_kind",
                "photo"
            )
        ),
        "storage_kind": item.get(
            "storage_kind",
            item.get(
                "type",
                "photo"
            )
        ),
        "file_id": file_id,
        "storage_message_id": item.get(
            "storage_message_id"
        ),
        "mime_type": item.get(
            "mime_type",
            ""
        ),
        "file_name": item.get(
            "file_name",
            ""
        ),
        "file_size": item.get(
            "file_size",
            0
        )
    }


# ============================================================
# PUBLISH MEDIA
# ============================================================

def send_single_media(
    media,
    caption=""
):
    item = normalize_media_item(
        media
    )

    if not item:
        raise RuntimeError(
            "Некорректный media item"
        )

    media_type = item["type"]
    file_id = item["file_id"]

    if media_type == "video":
        result = telegram(
            "sendVideo",
            data={
                "chat_id": CHANNEL_USERNAME,
                "video": file_id,
                "caption": caption or ""
            }
        )

    else:
        result = telegram(
            "sendPhoto",
            data={
                "chat_id": CHANNEL_USERNAME,
                "photo": file_id,
                "caption": caption or ""
            }
        )

    return result["result"]


def publish_media_group(
    media_items,
    caption=""
):
    if not media_items:
        return []

    if len(media_items) > TELEGRAM_MEDIA_GROUP_SIZE:
        raise RuntimeError(
            "Telegram media group не может "
            "содержать больше 10 элементов"
        )

    media = []

    for index, item in enumerate(
        media_items
    ):
        normalized = normalize_media_item(
            item
        )

        if not normalized:
            continue

        entry = {
            "type": normalized["type"],
            "media": normalized["file_id"]
        }

        if index == 0 and caption:
            entry["caption"] = caption

        media.append(
            entry
        )

    if not media:
        return []

    result = telegram(
        "sendMediaGroup",
        data={
            "chat_id": CHANNEL_USERNAME,
            "media": json.dumps(
                media,
                ensure_ascii=False
            )
        }
    )

    return result["result"]


def publish_stored_media(
    media_items,
    caption=""
):
    normalized = []

    for item in media_items:
        value = normalize_media_item(
            item
        )

        if value:
            normalized.append(
                value
            )

    if not normalized:
        if caption:
            result = telegram(
                "sendMessage",
                data={
                    "chat_id": CHANNEL_USERNAME,
                    "text": caption
                }
            )

            return [
                result["result"]
            ]

        return []

    results = []

    # --------------------------------------------------------
    # Один файл.
    # --------------------------------------------------------

    if len(normalized) == 1:
        results.append(
            send_single_media(
                normalized[0],
                caption=caption
            )
        )

        return results

    # --------------------------------------------------------
    # Обычные посты разбиваем на Telegram-группы по 10.
    # --------------------------------------------------------

    for start in range(
        0,
        len(normalized),
        TELEGRAM_MEDIA_GROUP_SIZE
    ):
        chunk = normalized[
            start:start + TELEGRAM_MEDIA_GROUP_SIZE
        ]

        chunk_caption = (
            caption
            if start == 0
            else ""
        )

        results.extend(
            publish_media_group(
                chunk,
                caption=chunk_caption
            )
        )

        if (
            start + TELEGRAM_MEDIA_GROUP_SIZE
            < len(normalized)
        ):
            time.sleep(
                TELEGRAM_GROUP_DELAY
            )

    return results


# ============================================================
# POST HISTORY
# ============================================================

def save_post_history(
    text,
    media_items
):
    return db_execute(
        """
        INSERT INTO posts (
            text,
            media_json,
            created_at
        )
        VALUES (?, ?, ?)
        """,
        (
            text or "",
            json.dumps(
                media_items,
                ensure_ascii=False
            ),
            datetime.utcnow().isoformat()
        ),
        commit=True
    )


# ============================================================
# BASIC ROUTES
# ============================================================

@app.get("/")
def index():
    return "Kawaii Chan Backend работает!"


@app.get("/test")
def test():
    return jsonify({
        "success": True,
        "message": "Backend работает"
    })


@app.get("/storage-test")
def storage_test():
    if not BOT_TOKEN:
        return jsonify({
            "success": False,
            "error": "BOT_TOKEN не установлен"
        }), 500

    try:
        result = telegram(
            "getMe"
        )

        return jsonify({
            "success": True,
            "telegram": result["result"]
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.get("/webapp")
def webapp():
    return jsonify({
        "success": True,
        "mini_app_url": MINI_APP_URL,
        "backend_url": BACKEND_URL
    })


# ============================================================
# NORMAL PUBLISH
# ============================================================

@app.post("/publish")
def publish():
    try:
        text = (
            request.form.get(
                "text",
                ""
            ).strip()
        )

        if not text:
            text = (
                request.form.get(
                    "caption",
                    ""
                ).strip()
            )

        files = get_uploaded_media()

        if len(files) > MAX_MEDIA:
            return jsonify({
                "success": False,
                "error": (
                    f"Можно прикрепить максимум "
                    f"{MAX_MEDIA} файлов"
                )
            }), 400

                media_items = []

        if files:
            media_items = [
                normalize_media_item(
                    uploaded_file
                )
                for uploaded_file in files
            ]


# ============================================================
# ALBUM CREATION
# ============================================================
@app.post("/albums")
def create_album():
    temporary_paths = []
    album_id = None

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
                "error": "Введите название альбома"
            }), 400

        uploaded_files = get_uploaded_media()

        if not uploaded_files:
            return jsonify({
                "success": False,
                "error": "Добавьте хотя бы одну страницу"
            }), 400

        if len(uploaded_files) > MAX_ALBUM_ITEMS:
            return jsonify({
                "success": False,
                "error": (
                    f"Максимум страниц: "
                    f"{MAX_ALBUM_ITEMS}"
                )
            }), 400

        # ----------------------------------------------------
        # Сначала создаём альбом.
        # ----------------------------------------------------

        album_id = db_execute(
            """
            INSERT INTO albums (
                title,
                description,
                status,
                created_at
            )
            VALUES (?, ?, 'draft', ?)
            """,
            (
                title,
                description,
                datetime.utcnow().isoformat()
            ),
            commit=True
        )

        # ----------------------------------------------------
        # Каждую страницу загружаем отдельно в Storage.
        #
        # Это принципиально важно:
        # здесь НЕТ sendMediaGroup.
        #
        # Поэтому 21, 50, 100 или 200 страниц
        # не превращаются в один Telegram media group.
        # ----------------------------------------------------

        for position, uploaded_file in enumerate(
            uploaded_files,
            start=1
        ):

            if not uploaded_file:
                continue

            path = save_temp_file(
                uploaded_file
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
                    f"Страница #{position} пустая"
                )

            if file_size > MAX_ALBUM_FILE_SIZE:
                raise RuntimeError(
                    f"Страница #{position} "
                    f"слишком большая. "
                    f"Максимум "
                    f"{MAX_ALBUM_FILE_SIZE // (1024 * 1024)} MB."
                )

            file_type = detect_file_type(
                filename,
                mime_type
            )

            # ------------------------------------------------
            # Загружаем страницу отдельно.
            # ------------------------------------------------

            stored = save_media_to_storage(
                path,
                filename=filename,
                mime_type=mime_type
            )

            db_execute(
                """
                INSERT INTO album_items (
                    album_id,
                    position,
                    type,
                    storage_kind,
                    file_id,
                    storage_message_id,
                    mime_type,
                    file_name,
                    file_size
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    album_id,
                    position,
                    file_type,
                    stored.get(
                        "storage_kind",
                        file_type
                    ),
                    stored["file_id"],
                    stored.get(
                        "storage_message_id"
                    ),
                    stored.get(
                        "mime_type",
                        mime_type
                    ),
                    stored.get(
                        "file_name",
                        filename
                    ),
                    stored.get(
                        "file_size",
                        file_size
                    )
                ),
                commit=True
            )

        album = album_public_data(
            album_id
        )

        return jsonify({
            "success": True,
            "album": album
        })

    except Exception as e:
        # ----------------------------------------------------
        # Если создание альбома оборвалось,
        # удаляем его записи из базы.
        # ----------------------------------------------------

        if album_id is not None:
            try:
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

            except Exception:
                pass

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    finally:
        cleanup_files(
            temporary_paths
        )


# ============================================================
# ALBUM LIST
# ============================================================

@app.get("/albums")
def get_albums():
    try:
        rows = db_execute(
            """
            SELECT
                id,
                title,
                description,
                status,
                created_at,
                published_at,
                published_message_id
            FROM albums
            ORDER BY id DESC
            """,
            fetch=True
        )

        albums = []

        for row in rows:
            album = album_row_to_dict(
                row
            )

            count_row = db_execute(
                """
                SELECT COUNT(*) AS count
                FROM album_items
                WHERE album_id = ?
                """,
                (row["id"],),
                fetch_one=True
            )

            album["page_count"] = (
                count_row["count"]
            )

            albums.append(
                album
            )

        return jsonify({
            "success": True,
            "albums": albums
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# GET ONE ALBUM
# ============================================================

@app.get("/albums/<int:album_id>")
def get_one_album(album_id):
    try:
        album = album_public_data(
            album_id
        )

        if not album:
            return jsonify({
                "success": False,
                "error": "Альбом не найден"
            }), 404

        return jsonify({
            "success": True,
            "album": album
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# ALBUM PAGE FILE
# ============================================================

@app.get(
    "/albums/<int:album_id>/items/<int:item_id>/file"
)
def album_item_file(
    album_id,
    item_id
):
    try:
        row = db_execute(
            """
            SELECT *
            FROM album_items
            WHERE id = ?
              AND album_id = ?
            """,
            (
                item_id,
                album_id
            ),
            fetch_one=True
        )

        if not row:
            return jsonify({
                "success": False,
                "error": "Страница не найдена"
            }), 404

        file_id = row["file_id"]

        if not file_id:
            return jsonify({
                "success": False,
                "error": "У страницы отсутствует file_id"
            }), 404

        result = telegram(
            "getFile",
            data={
                "file_id": file_id
            }
        )

        file_path = (
            result["result"]["file_path"]
        )

        file_url = (
            f"https://api.telegram.org/file/"
            f"bot{BOT_TOKEN}/{file_path}"
        )

        upstream = requests.get(
            file_url,
            stream=True,
            timeout=120
        )

        upstream.raise_for_status()

        content_type = (
            row["mime_type"]
            or mimetypes.guess_type(
                row["file_name"] or ""
            )[0]
            or "application/octet-stream"
        )

        def generate():
            for chunk in upstream.iter_content(
                chunk_size=1024 * 512
            ):
                if chunk:
                    yield chunk

        headers = {
            "Cache-Control": (
                "public, max-age=86400"
            )
        }

        if row["file_size"]:
            headers["Content-Length"] = str(
                row["file_size"]
            )

        return Response(
            generate(),
            mimetype=content_type,
            headers=headers
        )

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# ALBUM DEEP LINK
# ============================================================

def album_deep_link(album_id):
    return (
        "https://t.me/"
        "KawaiiChanAsserBot"
        f"?startapp=album_{album_id}"
    )


# ============================================================
# PUBLISH ALBUM COVER
# ============================================================

def publish_album_cover(
    album,
    items
):
    if not items:
        raise RuntimeError(
            "В альбоме нет страниц"
        )

    cover = items[0]

    title = album["title"]

    description = (
        album["description"]
        or ""
    )

    page_count = len(items)

    caption_parts = [
        f"📖 {title}"
    ]

    if description:
        caption_parts.append(
            description
        )

    caption_parts.append(
        f"📄 Страниц: {page_count}"
    )

    caption = "\n\n".join(
        caption_parts
    )

    button_url = album_deep_link(
        album["id"]
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "📖 ОТКРЫТЬ АЛЬБОМ",
                    "url": button_url
                }
            ]
        ]
    }

    if cover["type"] == "video":
        result = telegram(
            "sendVideo",
            data={
                "chat_id": CHANNEL_USERNAME,
                "video": cover["file_id"],
                "caption": caption,
                "reply_markup": json.dumps(
                    reply_markup,
                    ensure_ascii=False
                )
            }
        )

    else:
        result = telegram(
            "sendPhoto",
            data={
                "chat_id": CHANNEL_USERNAME,
                "photo": cover["file_id"],
                "caption": caption,
                "reply_markup": json.dumps(
                    reply_markup,
                    ensure_ascii=False
                )
            }
        )

    return result["result"]


# ============================================================
# PUBLISH ALBUM
# ============================================================

@app.post(
    "/albums/<int:album_id>/publish"
)
def publish_album(album_id):
    try:
        album = get_album(
            album_id
        )

        if not album:
            return jsonify({
                "success": False,
                "error": "Альбом не найден"
            }), 404

        items = get_album_items(
            album_id
        )

        if not items:
            return jsonify({
                "success": False,
                "error": "В альбоме нет страниц"
            }), 400

        message = publish_album_cover(
            album,
            items
        )

        message_id = message.get(
            "message_id"
        )

        db_execute(
            """
            UPDATE albums
            SET status = 'published',
                published_at = ?,
                published_message_id = ?
            WHERE id = ?
            """,
            (
                datetime.utcnow().isoformat(),
                message_id,
                album_id
            ),
            commit=True
        )

        return jsonify({
            "success": True,
            "album_id": album_id,
            "message_id": message_id,
            "url": album_deep_link(
                album_id
            )
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# DELETE ALBUM
# ============================================================

@app.delete(
    "/albums/<int:album_id>"
)
def delete_album(album_id):
    try:
        album = get_album(
            album_id
        )

        if not album:
            return jsonify({
                "success": False,
                "error": "Альбом не найден"
            }), 404

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

        return jsonify({
            "success": True,
            "deleted": album_id
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.get(
    "/albums/<int:album_id>/info"
)
def album_info(album_id):
    try:
        album = album_public_data(
            album_id
        )

        if not album:
            return jsonify({
                "success": False,
                "error": "Альбом не найден"
            }), 404

        return jsonify({
            "success": True,
            "album": album
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# SCHEDULE
# ============================================================

def parse_scheduled_at(value):
    if not value:
        raise ValueError(
            "Не указано время публикации"
        )

    value = str(
        value
    ).strip()

    try:
        dt = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        )

        if dt.tzinfo is not None:
            dt = (
                dt.astimezone(
                    timezone.utc
                )
                .replace(
                    tzinfo=None
                )
            )

        return dt

    except Exception:
        raise ValueError(
            "Неверный формат времени"
        )


@app.post("/schedule")
def schedule_post():
    try:
        scheduled_at_raw = (
            request.form.get(
                "scheduled_at",
                ""
            ).strip()
        )

        caption = (
            request.form.get(
                "caption",
                ""
            ).strip()
        )

        text = (
            request.form.get(
                "text",
                ""
            ).strip()
        )

        if not caption and text:
            caption = text

        scheduled_at = parse_scheduled_at(
            scheduled_at_raw
        )

        if scheduled_at <= datetime.utcnow():
            return jsonify({
                "success": False,
                "error": (
                    "Время публикации "
                    "должно быть в будущем"
                )
            }), 400

        files = get_uploaded_media()

        if len(files) > MAX_MEDIA:
            return jsonify({
                "success": False,
                "error": (
                    f"Можно прикрепить максимум "
                    f"{MAX_MEDIA} файлов"
                )
            }), 400

        media_items = []

        if files:
            media_items = (
                upload_request_media_to_storage(
                    files,
                    caption=caption
                )
            )

        post_id = db_execute(
            """
            INSERT INTO scheduled_posts (
                caption,
                media_json,
                scheduled_at,
                status,
                created_at
            )
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (
                caption,
                json.dumps(
                    media_items,
                    ensure_ascii=False
                ),
                scheduled_at.isoformat(),
                datetime.utcnow().isoformat()
            ),
            commit=True
        )

        return jsonify({
            "success": True,
            "id": post_id,
            "scheduled_at": (
                scheduled_at.isoformat()
            ),
            "media_count": len(
                media_items
            )
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


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

        posts = []

        for row in rows:
            try:
                media = json.loads(
                    row["media_json"]
                    or "[]"
                )
            except Exception:
                media = []

            posts.append({
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

        return jsonify({
            "success": True,
            "posts": posts
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.post("/schedule")
def schedule_post():
    try:
        scheduled_at_raw = (
            request.form.get(
                "scheduled_at",
                ""
            ).strip()
        )

        caption = (
            request.form.get(
                "caption",
                ""
            ).strip()
        )

        text = (
            request.form.get(
                "text",
                ""
            ).strip()
        )

        if not caption and text:
            caption = text

        scheduled_at = parse_scheduled_at(
            scheduled_at_raw
        )

        if scheduled_at <= datetime.utcnow():
            return jsonify({
                "success": False,
                "error": (
                    "Время публикации "
                    "должно быть в будущем"
                )
            }), 400

        files = get_uploaded_media()

        if len(files) > MAX_MEDIA:
            return jsonify({
                "success": False,
                "error": (
                    f"Можно прикрепить максимум "
                    f"{MAX_MEDIA} файлов"
                )
            }), 400

        media_items = []

        if files:
            media_items = (
                upload_request_media_to_storage(
                    files,
                    caption=caption
                )
            )

        post_id = db_execute(
            """
            INSERT INTO scheduled_posts (
                caption,
                media_json,
                scheduled_at,
                status,
                created_at
            )
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (
                caption,
                json.dumps(
                    media_items,
                    ensure_ascii=False
                ),
                scheduled_at.isoformat(),
                datetime.utcnow().isoformat()
            ),
            commit=True
        )

        return jsonify({
            "success": True,
            "id": post_id,
            "scheduled_at": (
                scheduled_at.isoformat()
            ),
            "media_count": len(
                media_items
            )
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


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

        posts = []

        for row in rows:
            try:
                media = json.loads(
                    row["media_json"]
                    or "[]"
                )
            except Exception:
                media = []

            posts.append({
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

        return jsonify({
            "success": True,
            "posts": posts
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.delete(
    "/scheduled/<int:scheduled_id>"
)
def delete_scheduled(
    scheduled_id
):
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
            return jsonify({
                "success": False,
                "error": "Отложенный пост не найден"
            }), 404

        if row["status"] == "published":
            return jsonify({
                "success": False,
                "error": (
                    "Опубликованный пост "
                    "нельзя удалить как отложенный"
                )
            }), 400

        db_execute(
            """
            DELETE FROM scheduled_posts
            WHERE id = ?
            """,
            (scheduled_id,),
            commit=True
        )

        return jsonify({
            "success": True,
            "deleted": scheduled_id
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# SCHEDULER
# ============================================================

def claim_due_scheduled_posts():
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
                    claimed.append(
                        dict(row)
                    )

            conn.commit()

            return claimed

        finally:
            conn.close()


def mark_scheduled_published(
    post_id
):
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


def mark_scheduled_failed(
    post_id,
    error_text
):
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
        f"[SCHEDULER] "
        f"Пост #{post_id} не опубликован: "
        f"{error_text}",
        flush=True
    )


def process_scheduled_post(
    row
):
    post_id = row["id"]

    try:
        try:
            media = json.loads(
                row["media_json"]
                or "[]"
            )
        except Exception:
            media = []

        caption = (
            row["caption"]
            or ""
        )

        print(
            f"[SCHEDULER] "
            f"Публикация поста #{post_id}",
            flush=True
        )

        if media:
            publish_stored_media(
                media,
                caption=caption
            )

        elif caption:
            result = telegram(
                "sendMessage",
                data={
                    "chat_id": CHANNEL_USERNAME,
                    "text": caption
                }
            )

            if not result.get("ok"):
                raise RuntimeError(
                    result.get(
                        "description",
                        "Telegram sendMessage error"
                    )
                )

        mark_scheduled_published(
            post_id
        )

        print(
            f"[SCHEDULER] "
            f"Пост #{post_id} опубликован",
            flush=True
        )

    except Exception as e:
        mark_scheduled_failed(
            post_id,
            str(e)
        )


def scheduler_loop():
    print(
        "[SCHEDULER] Планировщик запущен",
        flush=True
    )

    while True:
        try:
            posts = (
                claim_due_scheduled_posts()
            )

            for row in posts:
                process_scheduled_post(
                    row
                )

        except Exception as e:
            print(
                f"[SCHEDULER] Ошибка: {e}",
                flush=True
            )

        time.sleep(5)


_scheduler_started = False

_scheduler_start_lock = (
    threading.Lock()
)


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


# ============================================================
# STATS
# ============================================================

@app.get("/stats")
def get_stats():
    try:
        posts_count = db_execute(
            """
            SELECT COUNT(*) AS count
            FROM posts
            """,
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
            """
            SELECT COUNT(*) AS count
            FROM albums
            """,
            fetch_one=True
        )["count"]

        album_pages = db_execute(
            """
            SELECT COUNT(*) AS count
            FROM album_items
            """,
            fetch_one=True
        )["count"]

        return jsonify({
            "success": True,
            "posts": posts_count,
            "scheduled": scheduled_pending,
            "scheduled_pending": scheduled_pending,
            "scheduled_published": scheduled_published,
            "albums": albums_count,
            "album_pages": album_pages
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# START
# ============================================================

start_scheduler()


if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
        )
