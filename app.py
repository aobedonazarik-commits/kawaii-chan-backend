import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS


# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

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
)

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

# URL твоего Mini App.
# Пока оставляем GitHub Pages.
MINI_APP_URL = os.environ.get(
    "MINI_APP_URL",
    "https://aobedonazarik-commits.github.io/kawaii-chan-mini-app/"
).strip()

MAX_MEDIA = 100

# Пока обычный Telegram Bot API.
MAX_VIDEO_SIZE = 50 * 1024 * 1024

# Максимум страниц в одном альбоме.
MAX_ALBUM_ITEMS = 200


TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
)


app = Flask(__name__)
CORS(app)

db_lock = threading.Lock()


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_db():

    with db_lock:

        conn = get_db()

        # -------------------------------------------------
        # Обычные посты
        # -------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_ids TEXT DEFAULT '[]',
                text TEXT DEFAULT '',
                media_json TEXT DEFAULT '[]',
                post_type TEXT DEFAULT 'text',
                created_at TEXT NOT NULL
            )
        """)

        # -------------------------------------------------
        # Отложенные посты
        # -------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT DEFAULT '',
                run_at TEXT NOT NULL,
                media_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                published_at TEXT,
                error TEXT
            )
        """)

        # -------------------------------------------------
        # НОВОЕ: альбомы
        # -------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS albums (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT DEFAULT '',
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                published_message_id INTEGER,
                status TEXT DEFAULT 'draft'
            )
        """)

        # -------------------------------------------------
        # НОВОЕ: страницы альбомов
        # -------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS album_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                storage_message_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(album_id)
                    REFERENCES albums(id)
                    ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_album_items_album_position
            ON album_items(album_id, position)
        """)

        conn.commit()
        conn.close()


init_db()


# =========================================================
# ВРЕМЯ
# =========================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


# =========================================================
# TELEGRAM API
# =========================================================

def telegram(method, payload=None, files=None):

    if not BOT_TOKEN:
        return {
            "ok": False,
            "error": "BOT_TOKEN не настроен."
        }

    url = (
        f"https://api.telegram.org/bot"
        f"{BOT_TOKEN}/{method}"
    )

    try:

        response = requests.post(
            url,
            data=payload,
            files=files,
            timeout=120
        )

        try:
            return response.json()

        except Exception:
            return {
                "ok": False,
                "error":
                    f"Telegram вернул HTTP "
                    f"{response.status_code}"
            }

    except requests.RequestException as exc:

        return {
            "ok": False,
            "error":
                f"Ошибка соединения с Telegram: {exc}"
        }


# =========================================================
# JSON
# =========================================================

def safe_json(value, default):

    try:
        return json.loads(value)

    except Exception:
        return default


# =========================================================
# ВРЕМЕННЫЕ ФАЙЛЫ
# =========================================================

def save_temp_file(file):

    if not file or not file.filename:
        return None

    filename = os.path.basename(
        file.filename
    )

    timestamp = str(
        int(time.time() * 1000000)
    )

    path = (
        UPLOAD_DIR /
        f"{timestamp}_{filename}"
    )

    file.save(path)

    return path


def cleanup_files(paths):

    for path in paths:

        try:
            Path(path).unlink(
                missing_ok=True
            )

        except Exception:
            pass


# =========================================================
# STORAGE — ФОТО
# =========================================================

def save_photo_to_storage(path):

    with open(path, "rb") as photo:

        result = telegram(
            "sendPhoto",
            payload={
                "chat_id": STORAGE_CHAT_ID,
                "disable_notification": "true"
            },
            files={
                "photo": (
                    Path(path).name,
                    photo,
                    "application/octet-stream"
                )
            }
        )

    if not result.get("ok"):

        raise RuntimeError(
            result.get(
                "description",
                "Telegram не принял фото."
            )
        )

    message = result["result"]

    photos = message.get(
        "photo",
        []
    )

    if not photos:

        raise RuntimeError(
            "Telegram не вернул "
            "file_id фотографии."
        )

    file_id = photos[-1]["file_id"]

    return {
        "type": "photo",
        "file_id": file_id,
        "storage_message_id":
            message.get("message_id")
    }


# =========================================================
# STORAGE — ВИДЕО
# =========================================================

def save_video_to_storage(path):

    size = Path(path).stat().st_size

    if size > MAX_VIDEO_SIZE:

        raise RuntimeError(
            "Видео больше 50 МБ."
        )

    with open(path, "rb") as video:

        result = telegram(
            "sendVideo",
            payload={
                "chat_id": STORAGE_CHAT_ID,
                "supports_streaming": "true",
                "disable_notification": "true"
            },
            files={
                "video": (
                    Path(path).name,
                    video,
                    "video/mp4"
                )
            }
        )

    if not result.get("ok"):

        raise RuntimeError(
            result.get(
                "description",
                "Telegram не принял видео."
            )
        )

    message = result["result"]

    video_data = message.get(
        "video"
    )

    if not video_data:

        raise RuntimeError(
            "Telegram не вернул "
            "file_id видео."
        )

    return {
        "type": "video",
        "file_id":
            video_data["file_id"],
        "storage_message_id":
            message.get("message_id")
    }


# =========================================================
# STORAGE — ОБЩАЯ ФУНКЦИЯ
# =========================================================

def save_media_to_storage(
    path,
    media_type
):

    if media_type == "video":
        return save_video_to_storage(path)

    return save_photo_to_storage(path)


# =========================================================
# ПОЛУЧЕНИЕ ЗАГРУЖЕННЫХ ФАЙЛОВ
# =========================================================

def get_uploaded_media():

    files = []

    files.extend(
        request.files.getlist("photos")
    )

    files.extend(
        request.files.getlist("media")
    )

    unique = []
    seen = set()

    for item in files:

        marker = id(item)

        if marker not in seen:

            seen.add(marker)
            unique.append(item)

    return unique


# =========================================================
# ЗАГРУЗКА МЕДИА В STORAGE
# =========================================================

def upload_request_media_to_storage():

    incoming = get_uploaded_media()

    if len(incoming) > MAX_MEDIA:

        raise RuntimeError(
            f"Можно загрузить максимум "
            f"{MAX_MEDIA} файлов."
        )

    temp_paths = []
    stored_media = []

    try:

        for file in incoming:

            content_type = (
                file.content_type or ""
            ).lower()

            if content_type.startswith(
                "video/"
            ):

                media_type = "video"

            elif content_type.startswith(
                "image/"
            ):

                media_type = "photo"

            else:

                raise RuntimeError(
                    "Неподдерживаемый тип файла: "
                    f"{content_type or 'неизвестный'}"
                )

            path = save_temp_file(file)

            if not path:
                continue

            temp_paths.append(path)

            stored = save_media_to_storage(
                path,
                media_type
            )

            stored_media.append(
                stored
            )

        return stored_media

    finally:

        cleanup_files(
            temp_paths
        )


# =========================================================
# ПУБЛИКАЦИЯ MEDIA GROUP
# =========================================================

def publish_media_group(
    media,
    caption=""
):

    telegram_media = []

    for index, item in enumerate(media):

        media_type = item["type"]

        if media_type == "photo":

            obj = {
                "type": "photo",
                "media": item["file_id"]
            }

        elif media_type == "video":

            obj = {
                "type": "video",
                "media": item["file_id"],
                "supports_streaming": True
            }

        else:

            continue

        if index == 0 and caption:

            obj["caption"] = caption
            obj["parse_mode"] = "HTML"

        telegram_media.append(
            obj
        )

    if not telegram_media:

        return {
            "ok": False,
            "description":
                "Нет поддерживаемых медиа."
        }

    return telegram(
        "sendMediaGroup",
        payload={
            "chat_id":
                CHANNEL_USERNAME,

            "media":
                json.dumps(
                    telegram_media,
                    ensure_ascii=False
                )
        }
    )


# =========================================================
# ПУБЛИКАЦИЯ СОХРАНЕННОГО МЕДИА
# =========================================================

def publish_stored_media(
    media,
    text=""
):

    if not media:

        result = telegram(
            "sendMessage",
            payload={
                "chat_id":
                    CHANNEL_USERNAME,

                "text":
                    text,

                "parse_mode":
                    "HTML"
            }
        )

        if not result.get("ok"):

            raise RuntimeError(
                result.get(
                    "description",
                    "Не удалось отправить текст."
                )
            )

        return [
            result["result"]["message_id"]
        ]

    message_ids = []

    groups = [
        media[i:i + 10]
        for i in range(
            0,
            len(media),
            10
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

        result = publish_media_group(
            group,
            caption
        )

        if not result.get("ok"):

            raise RuntimeError(
                result.get(
                    "description",
                    "Не удалось "
                    "опубликовать медиа."
                )
            )

        for message in result["result"]:

            message_ids.append(
                message["message_id"]
            )

        if group_index < len(groups) - 1:

            time.sleep(0.5)

    return message_ids


# =========================================================
# ИСТОРИЯ ПОСТОВ
# =========================================================

def save_post_history(
    message_ids,
    text,
    media
):

    post_type = (
        "media"
        if media
        else "text"
    )

    with db_lock:

        conn = get_db()

        conn.execute(
            """
            INSERT INTO posts
            (
                telegram_message_ids,
                text,
                media_json,
                post_type,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                json.dumps(
                    message_ids
                ),

                text,

                json.dumps(
                    media,
                    ensure_ascii=False
                ),

                post_type,

                now_iso()
            )
        )

        conn.commit()
        conn.close()


# =========================================================
# ГЛАВНАЯ
# =========================================================

@app.get("/")
def home():

    return jsonify({
        "ok": True,
        "service":
            "Kawaii Chan Backend",
        "status":
            "online",
        "channel":
            CHANNEL_USERNAME,
        "storage_chat_id":
            STORAGE_CHAT_ID,
        "mini_app_url":
            MINI_APP_URL,
        "time":
            now_iso()
    })


# =========================================================
# TEST BOT
# =========================================================

@app.get("/test")
def test():

    result = telegram(
        "getMe"
    )

    if not result.get("ok"):

        return jsonify({
            "ok": False,
            "error":
                result.get(
                    "description",
                    "Bot API error"
                )
        }), 500

    return jsonify({
        "ok": True,
        "bot":
            result["result"],
        "channel":
            CHANNEL_USERNAME,
        "storage_chat_id":
            STORAGE_CHAT_ID
    })


# =========================================================
# STORAGE TEST
# =========================================================

@app.get("/storage-test")
def storage_test():

    result = telegram(
        "sendMessage",
        payload={
            "chat_id":
                STORAGE_CHAT_ID,

            "text":
                "Kawaii Chan Storage test"
        }
    )

    if not result.get("ok"):

        return jsonify({
            "ok": False,
            "error":
                result.get(
                    "description",
                    "Storage error"
                )
        }), 500

    return jsonify({
        "ok": True,
        "message":
            "Storage-канал работает.",
        "message_id":
            result["result"]["message_id"],
        "storage_chat_id":
            STORAGE_CHAT_ID
    })


# =========================================================
# WEBAPP TEST
# =========================================================

@app.post("/webapp")
def webapp():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    return jsonify({
        "ok": True,
        "received": data
    })


# =========================================================
# ОБЫЧНАЯ ПУБЛИКАЦИЯ
# =========================================================

@app.post("/publish")
def publish():

    text = request.form.get(
        "text",
        ""
    ).strip()

    if request.is_json:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        text = str(
            data.get(
                "text",
                ""
            )
        ).strip()

    try:

        if request.is_json:

            media = []

        else:

            media = (
                upload_request_media_to_storage()
            )

        if not text and not media:

            return jsonify({
                "ok": False,
                "error":
                    "Нужен текст "
                    "или хотя бы один файл."
            }), 400

        message_ids = (
            publish_stored_media(
                media,
                text
            )
        )

        save_post_history(
            message_ids,
            text,
            media
        )

        return jsonify({
            "ok": True,
            "message_ids":
                message_ids,
            "media_count":
                len(media),
            "photo_count":
                sum(
                    1
                    for x in media
                    if x["type"] == "photo"
                ),
            "video_count":
                sum(
                    1
                    for x in media
                    if x["type"] == "video"
                )
        })

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error":
                str(exc)
        }), 500


# =========================================================
# ОТЛОЖЕННЫЙ ПОСТ
# =========================================================

@app.post("/schedule")
def schedule():

    text = request.form.get(
        "text",
        ""
    ).strip()

    run_at = request.form.get(
        "run_at",
        ""
    ).strip()

    if not run_at:

        return jsonify({
            "ok": False,
            "error":
                "Не указаны дата и время."
        }), 400

    try:

        datetime.fromisoformat(
            run_at.replace(
                "Z",
                "+00:00"
            )
        )

    except Exception:

        return jsonify({
            "ok": False,
            "error":
                "Неверный формат даты."
        }), 400

    try:

        media = (
            upload_request_media_to_storage()
        )

        if not text and not media:

            return jsonify({
                "ok": False,
                "error":
                              "Нужен текст "
                    "или хотя бы один файл."
            }), 400

        with db_lock:

            conn = get_db()

            cursor = conn.execute(
                """
                INSERT INTO scheduled_posts
                (
                    text,
                    run_at,
                    media_json,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (
                    text,

                    run_at,

                    json.dumps(
                        media,
                        ensure_ascii=False
                    ),

                    now_iso()
                )
            )

            job_id = cursor.lastrowid

            conn.commit()
            conn.close()

        return jsonify({
            "ok": True,
            "id":
                job_id,
            "run_at":
                run_at,
            "media_count":
                len(media),
            "photo_count":
                sum(
                    1
                    for x in media
                    if x["type"] == "photo"
                ),
            "video_count":
                sum(
                    1
                    for x in media
                    if x["type"] == "video"
                )
        })

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error":
                str(exc)
        }), 500


# =========================================================
# СПИСОК ОТЛОЖЕННЫХ
# =========================================================

@app.get("/scheduled")
def scheduled():

    with db_lock:

        conn = get_db()

        rows = conn.execute(
            """
            SELECT *
            FROM scheduled_posts
            ORDER BY run_at ASC
            """
        ).fetchall()

        conn.close()

    items = []

    for row in rows:

        media = safe_json(
            row["media_json"],
            []
        )

        items.append({

            "id":
                row["id"],

            "text":
                row["text"],

            "run_at":
                row["run_at"],

            "status":
                row["status"],

            "created_at":
                row["created_at"],

            "published_at":
                row["published_at"],

            "error":
                row["error"],

            "media_count":
                len(media),

            "photo_count":
                sum(
                    1
                    for x in media
                    if x.get("type") == "photo"
                ),

            "video_count":
                sum(
                    1
                    for x in media
                    if x.get("type") == "video"
                )
        })

    return jsonify({
        "ok": True,
        "items": items
    })

# =========================================================
# ОТМЕНА ОТЛОЖЕННОГО
# =========================================================

@app.delete(
    "/scheduled/<int:post_id>"
)
def cancel_scheduled(post_id):

    with db_lock:

        conn = get_db()

        row = conn.execute(
            """
            SELECT *
            FROM scheduled_posts
            WHERE id = ?
            """,
            (post_id,)
        ).fetchone()

        if not row:

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "Пост не найден."
            }), 404

        if row["status"] != "pending":

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "Этот пост уже обработан."
            }), 400

        conn.execute(
            """
            UPDATE scheduled_posts
            SET status = 'cancelled'
            WHERE id = ?
            """,
            (post_id,)
        )

        conn.commit()
        conn.close()

    return jsonify({
        "ok": True
    })


# =========================================================
# ПЛАНИРОВЩИК
# =========================================================

def process_due_posts():

    current = (
        datetime.now(
            timezone.utc
        )
    )

    with db_lock:

        conn = get_db()

        rows = conn.execute(
            """
            SELECT *
            FROM scheduled_posts
            WHERE status = 'pending'
            ORDER BY run_at ASC
            """
        ).fetchall()

        conn.close()

    for row in rows:

        try:

            run_at = datetime.fromisoformat(
                row["run_at"].replace(
                    "Z",
                    "+00:00"
                )
            )

            if run_at.tzinfo is None:

                run_at = run_at.replace(
                    tzinfo=timezone.utc
                )

            if run_at > current:
                continue

            media = safe_json(
                row["media_json"],
                []
            )

            message_ids = (
                publish_stored_media(
                    media,
                    row["text"] or ""
                )
            )

            save_post_history(
                message_ids,
                row["text"] or "",
                media
            )

            with db_lock:

                conn = get_db()

                conn.execute(
                    """
                    UPDATE scheduled_posts
                    SET
                        status = 'published',
                        published_at = ?,
                        error = NULL
                    WHERE id = ?
                    """,
                    (
                        now_iso(),
                        row["id"]
                    )
                )

                conn.commit()
                conn.close()

        except Exception as exc:

            with db_lock:

                conn = get_db()

                conn.execute(
                    """
                    UPDATE scheduled_posts
                    SET
                        status = 'error',
                        error = ?
                    WHERE id = ?
                    """,
                    (
                        str(exc),
                        row["id"]
                    )
                )

                conn.commit()
                conn.close()


@app.get(
    "/scheduler/tick"
)
def scheduler_tick():

    process_due_posts()

    return jsonify({
        "ok": True,
        "time":
            now_iso()
    })


def scheduler_loop():

    while True:

        try:
            process_due_posts()

        except Exception:
            pass

        time.sleep(20)


# =========================================================
# ИСТОРИЯ ПОСТОВ
# =========================================================

@app.get("/posts")
def posts():

    with db_lock:

        conn = get_db()

        rows = conn.execute(
            """
            SELECT *
            FROM posts
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()

        conn.close()

    items = []

    for row in rows:

        media = safe_json(
            row["media_json"],
            []
        )

        items.append({

            "id":
                row["id"],

            "text":
                row["text"],

            "post_type":
                row["post_type"],

            "created_at":
                row["created_at"],

            "telegram_message_ids":
                safe_json(
                    row[
                        "telegram_message_ids"
                    ],
                    []
                ),

            "media_count":
                len(media),

            "photo_count":
                sum(
                    1
                    for x in media
                    if x.get("type") == "photo"
                ),

            "video_count":
                sum(
                    1
                    for x in media
                    if x.get("type") == "video"
                )
        })

    return jsonify({
        "ok": True,
        "items": items
    })
# =========================================================
# РЕДАКТИРОВАНИЕ ПОСТА
# =========================================================

@app.post("/posts/edit")
def edit_post():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    post_id = data.get(
        "id"
    )

    new_text = str(
        data.get(
            "text",
            ""
        )
    ).strip()

    if not post_id:

        return jsonify({
            "ok": False,
            "error":
                "Не указан ID поста."
        }), 400

    with db_lock:

        conn = get_db()

        row = conn.execute(
            """
            SELECT *
            FROM posts
            WHERE id = ?
            """,
            (post_id,)
        ).fetchone()

        if not row:

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "Пост не найден."
            }), 404

        message_ids = safe_json(
            row["telegram_message_ids"],
            []
        )

        if not message_ids:

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "У поста нет Telegram message_id."
            }), 400

        if row["post_type"] == "text":

            result = telegram(
                "editMessageText",
                payload={
                    "chat_id":
                        CHANNEL_USERNAME,

                    "message_id":
                        message_ids[0],

                    "text":
                        new_text,

                    "parse_mode":
                        "HTML"
                }
            )

        else:

            result = telegram(
                "editMessageCaption",
                payload={
                    "chat_id":
                        CHANNEL_USERNAME,

                    "message_id":
                        message_ids[0],

                    "caption":
                        new_text,

                    "parse_mode":
                        "HTML"
                }
            )

        if not result.get("ok"):

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    result.get(
                        "description",
                        "Telegram не смог "
                        "изменить пост."
                    )
            }), 500

        conn.execute(
            """
            UPDATE posts
            SET text = ?
            WHERE id = ?
            """,
            (
                new_text,
                post_id
            )
        )

        conn.commit()
        conn.close()

    return jsonify({
        "ok": True
    })

# =========================================================
# УДАЛЕНИЕ ПОСТА
# =========================================================

@app.delete(
    "/posts/<int:post_id>"
)
def delete_post(post_id):

    with db_lock:

        conn = get_db()

        row = conn.execute(
            """
            SELECT *
            FROM posts
            WHERE id = ?
            """,
            (post_id,)
        ).fetchone()

        if not row:

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "Пост не найден."
            }), 404

        message_ids = safe_json(
            row["telegram_message_ids"],
            []
        )

        errors = []

        for message_id in message_ids:

            result = telegram(
                "deleteMessage",
                payload={
                    "chat_id":
                        CHANNEL_USERNAME,

                    "message_id":
                        message_id
                }
            )

            if not result.get("ok"):

                errors.append(
                    result.get(
                        "description",
                        "Ошибка удаления"
                    )
                )

        conn.execute(
            """
            DELETE FROM posts
            WHERE id = ?
            """,
            (post_id,)
        )

        conn.commit()
        conn.close()

    if errors:

        return jsonify({
            "ok": False,
            "error":
                "; ".join(errors)
        }), 500

    return jsonify({
        "ok": True
    })
# =========================================================
# СТАТИСТИКА
# =========================================================

@app.get("/stats")
def stats():

    with db_lock:

        conn = get_db()

        total_posts = conn.execute(
            "SELECT COUNT(*) FROM posts"
        ).fetchone()[0]

        scheduled_total = conn.execute(
            """
            SELECT COUNT(*)
            FROM scheduled_posts
            """
        ).fetchone()[0]

        scheduled_pending = conn.execute(
            """
            SELECT COUNT(*)
            FROM scheduled_posts
            WHERE status = 'pending'
            """
        ).fetchone()[0]

        scheduled_published = conn.execute(
            """
            SELECT COUNT(*)
            FROM scheduled_posts
            WHERE status = 'published'
            """
        ).fetchone()[0]

        scheduled_errors = conn.execute(
            """
            SELECT COUNT(*)
            FROM scheduled_posts
            WHERE status = 'error'
            """
        ).fetchone()[0]

        total_albums = conn.execute(
            """
            SELECT COUNT(*)
            FROM albums
            """
        ).fetchone()[0]

        conn.close()

    return jsonify({

        "ok": True,

        "stats": {

            "published_posts":
                total_posts,

            "scheduled_total":
                scheduled_total,

            "scheduled_pending":
                scheduled_pending,

            "scheduled_published":
                scheduled_published,

            "scheduled_errors":
                scheduled_errors,

            "albums":
                total_albums
        }
    })
# =========================================================
# НАСТРОЙКИ
# =========================================================

@app.get("/settings")
def settings():

    return jsonify({

        "ok": True,

        "channel":
            CHANNEL_USERNAME,

        "storage_chat_id":
            STORAGE_CHAT_ID,

        "max_media":
            MAX_MEDIA,

        "max_video_size_mb":
            50,

        "max_album_items":
            MAX_ALBUM_ITEMS,

        "mini_app_url":
            MINI_APP_URL
    })


# =========================================================
# =========================================================
# НОВАЯ СИСТЕМА АЛЬБОМОВ
# =========================================================
# =========================================================


# =========================================================
# СОЗДАНИЕ АЛЬБОМА
# =========================================================

@app.post("/albums")
def create_album():

    title = request.form.get(
        "title",
        ""
    ).strip()

    description = request.form.get(
        "description",
        ""
    ).strip()

    incoming = get_uploaded_media()

    if not incoming:

        return jsonify({
            "ok": False,
            "error":
                "Альбом должен содержать "
                "хотя бы одну страницу."
        }), 400

    if len(incoming) > MAX_ALBUM_ITEMS:

        return jsonify({
            "ok": False,
            "error":
                f"В одном альбоме можно "
                f"сохранить максимум "
                f"{MAX_ALBUM_ITEMS} файлов."
        }), 400

    if not title:

        title = "Новый альбом"

    temp_paths = []
    stored_media = []

    try:

        # -------------------------------------------------
        # Сначала отправляем все страницы
        # в Storage-канал.
        # -------------------------------------------------

        for file in incoming:

            content_type = (
                file.content_type or ""
            ).lower()

            if content_type.startswith(
                "video/"
            ):

                media_type = "video"

            elif content_type.startswith(
                "image/"
            ):

                media_type = "photo"

            else:

                raise RuntimeError(
                    "Неподдерживаемый тип файла: "
                    f"{content_type or 'неизвестный'}"
                )

            path = save_temp_file(file)

            if not path:
                continue

            temp_paths.append(path)

            stored = save_media_to_storage(
                path,
                media_type
            )

            stored_media.append(
                stored
            )

        if not stored_media:

            raise RuntimeError(
                "Не удалось сохранить "
                "страницы альбома."
            )

        # -------------------------------------------------
        # Создаем сам альбом.
        # -------------------------------------------------

        current_time = now_iso()

        with db_lock:

            conn = get_db()

            cursor = conn.execute(
                """
                INSERT INTO albums
                (
                    title,
                    description,
                    created_at,
                    updated_at,
                    status
                )
                VALUES (?, ?, ?, ?, 'draft')
                """,
                (
                    title,
                    description,
                    current_time,
                    current_time
                )
            )

            album_id = cursor.lastrowid

            # -------------------------------------------------
            # Записываем страницы по порядку.
            # -------------------------------------------------

            for position, item in enumerate(
                stored_media,
                start=1
            ):

                conn.execute(
                    """
                    INSERT INTO album_items
                    (
                        album_id,
                        position,
                        type,
                        file_id,
                        storage_message_id,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        album_id,
                        position,
                        item["type"],
                        item["file_id"],
                        item.get(
                            "storage_message_id"
                        ),
                        current_time
                    )
                )

            conn.commit()
            conn.close()

        return jsonify({

            "ok": True,

            "album_id":
                album_id,

            "title":
                title,

            "description":
                description,

            "items_count":
                len(stored_media),

            "photo_count":
                sum(
                    1
                    for x in stored_media
                    if x["type"] == "photo"
                ),

            "video_count":
                sum(
                    1
                    for x in stored_media
                    if x["type"] == "video"
                )
        })

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error":
                str(exc)
        }), 500

    finally:

        cleanup_files(
            temp_paths
        )

# =========================================================
# СПИСОК АЛЬБОМОВ
# =========================================================

@app.get("/albums")
def list_albums():

    with db_lock:

        conn = get_db()

        rows = conn.execute(
            """
            SELECT
                a.*,
                COUNT(ai.id) AS items_count
            FROM albums a
            LEFT JOIN album_items ai
                ON ai.album_id = a.id
            GROUP BY a.id
            ORDER BY a.id DESC
            """
        ).fetchall()

        conn.close()

    items = []

    for row in rows:

        items.append({

            "id":
                row["id"],

            "title":
                row["title"],

            "description":
                row["description"],

            "created_at":
                row["created_at"],

            "updated_at":
                row["updated_at"],

            "published_message_id":
                row[
                    "published_message_id"
                ],

            "status":
                row["status"],

            "items_count":
                row["items_count"]
        })

    return jsonify({
        "ok": True,
        "items": items
    })


# =========================================================
# ПОЛУЧИТЬ АЛЬБОМ
# =========================================================

@app.get(
    "/albums/<int:album_id>"
)
def get_album(album_id):

    with db_lock:

        conn = get_db()

        album = conn.execute(
            """
            SELECT *
            FROM albums
            WHERE id = ?
            """,
            (album_id,)
        ).fetchone()

        if not album:

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "Альбом не найден."
            }), 404

        rows = conn.execute(
            """
            SELECT
                id,
                position,
                type,
                storage_message_id
            FROM album_items
            WHERE album_id = ?
            ORDER BY position ASC
            """,
            (album_id,)
        ).fetchall()

        conn.close()

    items = []

    for row in rows:

        items.append({

            "id":
                row["id"],

            "position":
                row["position"],

            "type":
                row["type"],

            "storage_message_id":
                row[
                    "storage_message_id"
                ],

            # URL, который потом будет
            # использовать наш Mini App.
            "url":
                f"/albums/"
                f"{album_id}/media/"
                f"{row['position']}"
        })

    return jsonify({

        "ok": True,

        "album": {

            "id":
                album["id"],

            "title":
                album["title"],

            "description":
                album["description"],

            "created_at":
                album["created_at"],

            "updated_at":
                album["updated_at"],

            "published_message_id":
                album[
                    "published_message_id"
                ],

            "status":
                album["status"],

            "items_count":
                len(items),

            "items":
                items
        }
    })
    # =========================================================
# ПОЛУЧИТЬ FILE_ID АЛЬБОМА
# =========================================================

@app.get(
    "/albums/<int:album_id>/media/<int:position>"
)
def album_media(album_id, position):

    with db_lock:

        conn = get_db()

        item = conn.execute(
            """
            SELECT *
            FROM album_items
            WHERE album_id = ?
              AND position = ?
            """,
            (
                album_id,
                position
            )
        ).fetchone()

        conn.close()

    if not item:

        return jsonify({
            "ok": False,
            "error":
                "Страница альбома "
                "не найдена."
        }), 404

    # -----------------------------------------------------
    # Получаем file_path у Telegram.
    # -----------------------------------------------------

    result = telegram(
        "getFile",
        payload={
            "file_id":
                item["file_id"]
        }
    )

    if not result.get("ok"):

        return jsonify({
            "ok": False,
            "error":
                result.get(
                    "description",
                    "Telegram не смог "
                    "получить файл."
                )
        }), 500

    file_path = (
        result["result"]
        .get("file_path")
    )

    if not file_path:

        return jsonify({
            "ok": False,
            "error":
                "Telegram не вернул "
                "путь к файлу."
        }), 500

    # -----------------------------------------------------
    # Забираем файл с Telegram.
    # -----------------------------------------------------

    file_url = (
        f"https://api.telegram.org/"
        f"file/bot{BOT_TOKEN}/"
        f"{file_path}"
    )

    try:

        response = requests.get(
            file_url,
            timeout=120
        )

    except requests.RequestException as exc:

        return jsonify({
            "ok": False,
            "error":
                f"Ошибка загрузки файла: {exc}"
        }), 500

    if response.status_code != 200:

        return jsonify({
            "ok": False,
            "error":
                "Telegram не отдал файл."
        }), 500

    content_type = (
        "video/mp4"
        if item["type"] == "video"
        else "image/jpeg"
    )

    return Response(
        response.content,
        status=200,
        content_type=content_type,
        headers={
            "Cache-Control":
                "public, max-age=86400",
            "Access-Control-Allow-Origin":
                "*"
        }
    )


# =========================================================
# УДАЛЕНИЕ АЛЬБОМА
# =========================================================

@app.delete(
    "/albums/<int:album_id>"
)
def delete_album(album_id):

    with db_lock:

        conn = get_db()

        album = conn.execute(
            """
            SELECT *
            FROM albums
            WHERE id = ?
            """,
            (album_id,)
        ).fetchone()

        if not album:

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "Альбом не найден."
            }), 404

        # SQLite foreign_keys не всегда
        # включен автоматически.
        conn.execute(
            """
            DELETE FROM album_items
            WHERE album_id = ?
            """,
            (album_id,)
        )

        conn.execute(
            """
            DELETE FROM albums
            WHERE id = ?
            """,
            (album_id,)
        )

        conn.commit()
        conn.close()

    return jsonify({
        "ok": True
    })


# =========================================================
# ПУБЛИКАЦИЯ АЛЬБОМА В КАНАЛ
# =========================================================

@app.post(
    "/albums/<int:album_id>/publish"
)
def publish_album(album_id):

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    custom_text = str(
        data.get(
            "text",
            ""
        )
    ).strip()

    with db_lock:

        conn = get_db()

        album = conn.execute(
            """
            SELECT *
            FROM albums
            WHERE id = ?
            """,
            (album_id,)
        ).fetchone()

        if not album:

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "Альбом не найден."
            }), 404

        first_item = conn.execute(
            """
            SELECT *
            FROM album_items
            WHERE album_id = ?
            ORDER BY position ASC
            LIMIT 1
            """,
            (album_id,)
        ).fetchone()

        conn.close()

    if not first_item:

        return jsonify({
            "ok": False,
            "error":
                "В альбоме нет страниц."
        }), 400

    # -----------------------------------------------------
    # Ссылка на конкретный альбом.
    #
    # Пока используем обычную HTTPS-ссылку.
    # Позже сделаем красивый полноценный
    # Mini App reader.
    # -----------------------------------------------------

    album_url = (
        f"{MINI_APP_URL}"
        f"?album={album_id}"
    )

    caption_parts = []

    if custom_text:
        caption_parts.append(
            custom_text
        )

    elif album["description"]:
        caption_parts.append(
            album["description"]
        )

    caption_parts.append(
        f"📖 <b>{album['title']}</b>"
    )

    caption = "\n\n".join(
        caption_parts
    )

    # -----------------------------------------------------
    # Отправляем первую страницу
    # -----------------------------------------------------

    result = telegram(
        "sendPhoto"
        if first_item["type"] == "photo"
        else "sendVideo",

        payload={
            "chat_id":
                CHANNEL_USERNAME,

            "caption":
                caption,

            "parse_mode":
                "HTML",

            "disable_notification":
                "false",

            "reply_markup":
                json.dumps({
                    "inline_keyboard": [[
                        {
                            "text":
                                "📖 ОТКРЫТЬ ПОЛНЫЙ АЛЬБОМ",

                            "url":
                                album_url
                        }
                    ]]
                }, ensure_ascii=False)
        }
    )

    if not result.get("ok"):

        return jsonify({
            "ok": False,
            "error":
                result.get(
                    "description",
                    "Не удалось "
                    "опубликовать альбом."
                )
        }), 500

    message_id = (
        result["result"]["message_id"]
    )

    with db_lock:

        conn = get_db()

        conn.execute(
            """
            UPDATE albums
            SET
                published_message_id = ?,
                status = 'published',
                updated_at = ?
            WHERE id = ?
            """,
            (
                message_id,
                now_iso(),
                album_id
            )
        )

        conn.commit()
        conn.close()

    return jsonify({

        "ok": True,

        "album_id":
            album_id,

        "message_id":
            message_id,

        "album_url":
            album_url
    })


# =========================================================
# ЗАПУСК ПЛАНИРОВЩИКА
# =========================================================

def start_scheduler():

    thread = threading.Thread(
        target=scheduler_loop,
        daemon=True
    )

    thread.start()


start_scheduler()
# =========================================================
# ЗАПУСК FLASK
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "10000"
            )
        )
)
