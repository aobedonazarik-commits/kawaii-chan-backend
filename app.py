import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, request, jsonify, Response, redirect
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

# Для обычных видео оставляем старый лимит.
MAX_VIDEO_SIZE = 50 * 1024 * 1024

# getFile Bot API позволяет скачивать до 20 MB.
# Поэтому страницы альбомов ограничиваем 20 MB.
MAX_ALBUM_FILE_SIZE = 20 * 1024 * 1024

TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
)


app = Flask(__name__)

CORS(
    app,
    resources={
        r"/*": {
            "origins": "*"
        }
    }
)

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


def table_columns(conn, table_name):
    rows = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return {
        row["name"]
        for row in rows
    }


def init_db():

    with db_lock:

        conn = get_db()

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

        # -------------------------------------------------
        # Миграция старых album_items.
        # -------------------------------------------------

        columns = table_columns(
            conn,
            "album_items"
        )

        if "storage_kind" not in columns:

            conn.execute("""
                ALTER TABLE album_items
                ADD COLUMN storage_kind TEXT
                DEFAULT 'photo'
            """)

        if "mime_type" not in columns:

            conn.execute("""
                ALTER TABLE album_items
                ADD COLUMN mime_type TEXT
                DEFAULT 'image/jpeg'
            """)

        if "file_name" not in columns:

            conn.execute("""
                ALTER TABLE album_items
                ADD COLUMN file_name TEXT
                DEFAULT ''
            """)

        if "file_size" not in columns:

            conn.execute("""
                ALTER TABLE album_items
                ADD COLUMN file_size INTEGER
                DEFAULT 0
            """)

        conn.commit()
        conn.close()


init_db()


# =========================================================
# HELPERS
# =========================================================

def now_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def safe_json(value, default):

    try:
        return json.loads(value)
    except Exception:
        return default


def telegram(
    method,
    payload=None,
    files=None
):

    if not BOT_TOKEN:

        return {
            "ok": False,
            "description":
                "BOT_TOKEN не настроен."
        }

    url = (
        f"{TELEGRAM_API}/{method}"
    )

    try:

        response = requests.post(
            url,
            data=payload,
            files=files,
            timeout=180
        )

        try:
            return response.json()

        except Exception:

            return {
                "ok": False,
                "description":
                    f"Telegram HTTP "
                    f"{response.status_code}"
            }

    except requests.RequestException as exc:

        return {
            "ok": False,
            "description":
                f"Ошибка соединения "
                f"с Telegram: {exc}"
        }


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
# ОБЫЧНЫЕ ПОСТЫ
# =========================================================

def save_photo_to_storage(path):

    with open(path, "rb") as photo:

        result = telegram(
            "sendPhoto",
            payload={
                "chat_id":
                    STORAGE_CHAT_ID,
                "disable_notification":
                    "true"
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
            "Telegram не вернул file_id фотографии."
        )

    return {
        "type": "photo",
        "file_id":
            photos[-1]["file_id"],
        "storage_message_id":
            message.get("message_id")
    }


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
                "chat_id":
                    STORAGE_CHAT_ID,
                "supports_streaming":
                    "true",
                "disable_notification":
                    "true"
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
            "Telegram не вернул file_id видео."
        )

    return {
        "type": "video",
        "file_id":
            video_data["file_id"],
        "storage_message_id":
            message.get("message_id")
    }


def save_media_to_storage(
    path,
    media_type
):

    if media_type == "video":

        return save_video_to_storage(
            path
        )

    return save_photo_to_storage(
        path
    )


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
                    + (
                        content_type
                        or "неизвестный"
                    )
                )

            path = save_temp_file(
                file
            )

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


def publish_media_group(
    media,
    caption=""
):

    telegram_media = []

    for item in media:

        media_type = item["type"]

        if media_type == "photo":

            obj = {
                "type": "photo",
                "media":
                    item["file_id"]
            }

        elif media_type == "video":

            obj = {
                "type": "video",
                "media":
                    item["file_id"],
                "supports_streaming":
                    True
            }

        else:

            continue

        if (
            not telegram_media
            and caption
        ):

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

        if (
            group_index
            < len(groups) - 1
        ):

            time.sleep(0.7)

    return message_ids


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

        conn.execute("""
            INSERT INTO posts
            (
                telegram_message_ids,
                text,
                media_json,
                post_type,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            json.dumps(message_ids),
            text,
            json.dumps(
                media,
                ensure_ascii=False
            ),
            post_type,
            now_iso()
        ))

        conn.commit()
        conn.close()


# =========================================================
# SERVICE
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
            result["result"]["message_id"]
    })


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
# PUBLISH
# =========================================================

@app.post("/publish")
@app.post("/posts")
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

        media = (
            []
            if request.is_json
            else upload_request_media_to_storage()
        )

        if not text and not media:

            return jsonify({
                "ok": False,
                "error":
                    "Нужен текст или хотя бы один файл."
            }), 400

        message_ids = publish_stored_media(
            media,
            text
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
                    1 for x in media
                    if x["type"] == "photo"
                ),
            "video_count":
                sum(
                    1 for x in media
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
# SCHEDULE
# =========================================================

@app.post("/schedule")
@app.post("/scheduled")
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
            if not request.is_json
            else []
        )

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
                    text
                )
            ).strip()

        if not text and not media:

            return jsonify({
                "ok": False,
                "error":
                    "Нужен текст или хотя бы один файл."
            }), 400

        with db_lock:

            conn = get_db()

            cursor = conn.execute("""
                INSERT INTO scheduled_posts
                (
                    text,
                    run_at,
                    media_json,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, 'pending', ?)
            """, (
                text,
                run_at,
                json.dumps(
                    media,
                    ensure_ascii=False
                ),
                now_iso()
            ))

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
                    1 for x in media
                    if x["type"] == "photo"
                ),
            "video_count":
                sum(
                    1 for x in media
                    if x["type"] == "video"
                )
        })

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error":
                str(exc)
        }), 500


@app.get("/scheduled")
def scheduled():

    with db_lock:

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM scheduled_posts
            ORDER BY run_at ASC
        """).fetchall()

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
                    1 for x in media
                    if x.get("type") == "photo"
                ),
            "video_count":
                sum(
                    1 for x in media
                    if x.get("type") == "video"
                )
        })

    return jsonify({
        "ok": True,
        "items":
            items
    })


@app.delete(
    "/scheduled/<int:post_id>"
)
def cancel_scheduled(post_id):

    with db_lock:

        conn = get_db()

        row = conn.execute("""
            SELECT *
            FROM scheduled_posts
            WHERE id = ?
        """, (
            post_id,
        )).fetchone()

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

        conn.execute("""
            UPDATE scheduled_posts
            SET status = 'cancelled'
            WHERE id = ?
        """, (
            post_id,
        ))

        conn.commit()
        conn.close()

    return jsonify({
        "ok": True
    })


# =========================================================
# SCHEDULER
# =========================================================

def process_due_posts():

    current = datetime.now(
        timezone.utc
    )

    with db_lock:

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM scheduled_posts
            WHERE status = 'pending'
            ORDER BY run_at ASC
        """).fetchall()

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

            message_ids = publish_stored_media(
                media,
                row["text"] or ""
            )

            save_post_history(
                message_ids,
                row["text"] or "",
                media
            )

            with db_lock:

                conn = get_db()

                conn.execute("""
                    UPDATE scheduled_posts
                    SET
                        status = 'published',
                        published_at = ?,
                        error = NULL
                    WHERE id = ?
                """, (
                    now_iso(),
                    row["id"]
                ))

                conn.commit()
                conn.close()

        except Exception as exc:

            with db_lock:

                conn = get_db()

                conn.execute("""
                    UPDATE scheduled_posts
                    SET
                        status = 'error',
                        error = ?
                    WHERE id = ?
                """, (
                    str(exc),
                    row["id"]
                ))

                conn.commit()
                conn.close()


@app.get("/scheduler/tick")
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
# POSTS
# =========================================================

@app.get("/posts")
def posts():

    with db_lock:

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM posts
            ORDER BY id DESC
            LIMIT 100
        """).fetchall()

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
                    1 for x in media
                    if x.get("type") == "photo"
                ),
            "video_count":
                sum(
                    1 for x in media
                    if x.get("type") == "video"
                )
        })

    return jsonify({
        "ok": True,
        "items":
            items
    })


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

        row = conn.execute("""
            SELECT *
            FROM posts
            WHERE id = ?
        """, (
            post_id,
        )).fetchone()

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
                        "Telegram не смог изменить пост."
                    )
            }), 500

        conn.execute("""
            UPDATE posts
            SET text = ?
            WHERE id = ?
        """, (
            new_text,
            post_id
        ))

        conn.commit()
        conn.close()

    return jsonify({
        "ok": True
    })


@app.delete(
    "/posts/<int:post_id>"
)
def delete_post(post_id):

    with db_lock:

        conn = get_db()

        row = conn.execute("""
            SELECT *
            FROM posts
            WHERE id = ?
        """, (
            post_id,
        )).fetchone()

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

        conn.execute("""
            DELETE FROM posts
            WHERE id = ?
        """, (
            post_id,
        ))

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
# STATS
# =========================================================

@app.get("/stats")
def stats():

    with db_lock:

        conn = get_db()

        total_posts = conn.execute(
            "SELECT COUNT(*) FROM posts"
        ).fetchone()[0]

        scheduled_total = conn.execute(
            "SELECT COUNT(*) FROM scheduled_posts"
        ).fetchone()[0]

        scheduled_pending = conn.execute("""
            SELECT COUNT(*)
            FROM scheduled_posts
            WHERE status = 'pending'
        """).fetchone()[0]

        scheduled_published = conn.execute("""
            SELECT COUNT(*)
            FROM scheduled_posts
            WHERE status = 'published'
        """).fetchone()[0]

        scheduled_errors = conn.execute("""
            SELECT COUNT(*)
            FROM scheduled_posts
            WHERE status = 'error'
        """).fetchone()[0]

        total_albums = conn.execute(
            "SELECT COUNT(*) FROM albums"
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
# SETTINGS
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
        "max_album_file_size_mb":
            20,
        "mini_app_url":
            MINI_APP_URL
    })


# =========================================================
# ALBUM HELPERS
# =========================================================

def save_album_image_as_document(
    path
):

    size = Path(path).stat().st_size

    if size > MAX_ALBUM_FILE_SIZE:

        raise RuntimeError(
            f"Страница больше "
            f"{MAX_ALBUM_FILE_SIZE // 1024 // 1024} МБ."
        )

    mime = (
        "application/octet-stream"
    )

    original_name = Path(
        path
    ).name

    lower = original_name.lower()

    if lower.endswith(
        (".jpg", ".jpeg")
    ):
        mime = "image/jpeg"

    elif lower.endswith(".png"):
        mime = "image/png"

    elif lower.endswith(".webp"):
        mime = "image/webp"

    elif lower.endswith(".gif"):
        mime = "image/gif"

    elif lower.endswith(".bmp"):
        mime = "image/bmp"

    elif lower.endswith(".avif"):
        mime = "image/avif"

    else:

        content = None

        try:
            with open(
                path,
                "rb"
            ) as f:
                content = f.read(16)

        except Exception:
            pass

        if (
            content
            and content.startswith(b"\xff\xd8\xff")
        ):
            mime = "image/jpeg"

        elif (
            content
            and content.startswith(b"\x89PNG")
        ):
            mime = "image/png"

        elif (
            content
            and content.startswith(b"RIFF")
            and b"WEBP" in content
        ):
            mime = "image/webp"

        else:

            raise RuntimeError(
                "Не удалось определить формат "
                "изображения. Используй JPG, "
                "PNG или WEBP."
            )

    with open(
        path,
        "rb"
    ) as document:

        result = telegram(
            "sendDocument",
            payload={
                "chat_id":
                    STORAGE_CHAT_ID,
                "disable_notification":
                    "true",
                "disable_content_type_detection":
                    "true"
            },
            files={
                "document": (
                    original_name,
                    document,
                    mime
                )
            }
        )

    if not result.get("ok"):

        raise RuntimeError(
            result.get(
                "description",
                "Telegram не принял страницу."
            )
        )

    message = result["result"]

    document_data = message.get(
        "document"
    )

    if not document_data:

        raise RuntimeError(
            "Telegram не вернул document file_id."
        )

    return {
        "type":
            "photo",
        "storage_kind":
            "document",
        "file_id":
            document_data["file_id"],
        "storage_message_id":
            message.get("message_id"),
        "mime_type":
            document_data.get(
                "mime_type",
                mime
            ),
        "file_name":
            document_data.get(
                "file_name",
                original_name
            ),
        "file_size":
            document_data.get(
                "file_size",
                size
            )
    }


def save_album_video(
    path
):

    size = Path(path).stat().st_size

    if size > MAX_ALBUM_FILE_SIZE:

        raise RuntimeError(
            "Видео в альбоме должно быть "
            "не больше 20 МБ."
        )

    with open(
        path,
        "rb"
    ) as video:

        result = telegram(
            "sendVideo",
            payload={
                "chat_id":
                    STORAGE_CHAT_ID,
                "supports_streaming":
                    "true",
                "disable_notification":
                    "true"
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
            "Telegram не вернул video file_id."
        )

    return {
        "type":
            "video",
        "storage_kind":
            "video",
        "file_id":
            video_data["file_id"],
        "storage_message_id":
            message.get("message_id"),
        "mime_type":
            "video/mp4",
        "file_name":
            Path(path).name,
        "file_size":
            video_data.get(
                "file_size",
                size
            )
    }


# =========================================================
# CREATE ALBUM
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
                f"Максимум "
                f"{MAX_ALBUM_ITEMS} страниц."
        }), 400

    if not title:

        title = "Новый альбом"

    temp_paths = []
    stored_media = []

    try:

        # Сохраняем файлы в Telegram.
        # Картинки идут как DOCUMENT,
        # поэтому Telegram не пытается
        # обрабатывать их как sendPhoto.

        for index, file in enumerate(
            incoming,
            start=1
        ):

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
                    f"Страница {index}: "
                    f"неподдерживаемый тип "
                    f"{content_type or 'неизвестный'}."
                )

            path = save_temp_file(
                file
            )

            if not path:

                raise RuntimeError(
                    f"Страница {index}: "
                    f"файл пустой."
                )

            temp_paths.append(path)

            if media_type == "photo":

                stored = (
                    save_album_image_as_document(
                        path
                    )
                )

            else:

                stored = save_album_video(
                    path
                )

            stored_media.append(
                stored
            )

        if not stored_media:

            raise RuntimeError(
                "Не удалось сохранить "
                "страницы альбома."
            )

        current_time = now_iso()

        with db_lock:

            conn = get_db()

            cursor = conn.execute("""
                INSERT INTO albums
                (
                    title,
                    description,
                    created_at,
                    updated_at,
                    status
                )
                VALUES (?, ?, ?, ?, 'draft')
            """, (
                title,
                description,
                current_time,
                current_time
            ))

            album_id = cursor.lastrowid

            for position, item in enumerate(
                stored_media,
                start=1
            ):

                conn.execute("""
                    INSERT INTO album_items
                    (
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
                """, (
                    album_id,
                    position,
                    item["type"],
                    item["file_id"],
                    item.get(
                        "storage_message_id"
                    ),
                    current_time,
                    item.get(
                        "storage_kind",
                        "photo"
                    ),
                    item.get(
                        "mime_type",
                        "image/jpeg"
                    ),
                    item.get(
                        "file_name",
                        ""
                    ),
                    item.get(
                        "file_size",
                        0
                    )
                ))

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
                    1 for x in stored_media
                    if x["type"] == "photo"
                ),
            "video_count":
                sum(
                    1 for x in stored_media
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
# LIST ALBUMS
# =========================================================

@app.get("/albums")
def list_albums():

    with db_lock:

        conn = get_db()

        rows = conn.execute("""
            SELECT
                a.*,
                COUNT(ai.id) AS items_count
            FROM albums a
            LEFT JOIN album_items ai
                ON ai.album_id = a.id
            GROUP BY a.id
            ORDER BY a.id DESC
        """).fetchall()

        conn.close()

    items = []

    for row in rows:

        first_url = None

        if row["items_count"]:

            first_url = (
                f"{BACKEND_URL}/albums/"
                f"{row['id']}/media/1"
            )

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
                row["published_message_id"],
            "status":
                row["status"],
            "items_count":
                row["items_count"],
            "cover_url":
                first_url
        })

    return jsonify({
        "ok": True,
        "items":
            items
    })


# =========================================================
# GET ALBUM
# =========================================================

@app.get(
    "/albums/<int:album_id>"
)
def get_album(album_id):

    with db_lock:

        conn = get_db()

        album = conn.execute("""
            SELECT *
            FROM albums
            WHERE id = ?
        """, (
            album_id,
        )).fetchone()

        if not album:

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "Альбом не найден."
            }), 404

        rows = conn.execute("""
            SELECT
                id,
                position,
                type,
                storage_message_id,
                storage_kind,
                mime_type,
                file_name,
                file_size
            FROM album_items
            WHERE album_id = ?
            ORDER BY position ASC
        """, (
            album_id,
        )).fetchall()

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
                row["storage_message_id"],
            "storage_kind":
                row["storage_kind"],
            "mime_type":
                row["mime_type"],
            "file_name":
                row["file_name"],
            "file_size":
                row["file_size"],
            "url":
                f"{BACKEND_URL}/albums/"
                f"{album_id}/media/"
                f"{row['position']}",
            "download_url":
                f"{BACKEND_URL}/albums/"
                f"{album_id}/download/"
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
# MEDIA
# =========================================================

def get_album_item(
    album_id,
    position
):

    with db_lock:

        conn = get_db()

        item = conn.execute("""
            SELECT *
            FROM album_items
            WHERE album_id = ?
              AND position = ?
        """, (
            album_id,
            position
        )).fetchone()

        conn.close()

    return item


def fetch_telegram_file(
    file_id
):

    result = telegram(
        "getFile",
        payload={
            "file_id":
                file_id
        }
    )

    if not result.get("ok"):

        raise RuntimeError(
            result.get(
                "description",
                "Telegram не смог "
                "подготовить файл."
            )
        )

    file_path = (
        result["result"].get(
            "file_path"
        )
    )

    if not file_path:

        raise RuntimeError(
            "Telegram не вернул "
            "file_path."
        )

    file_url = (
        f"https://api.telegram.org/"
        f"file/bot{BOT_TOKEN}/"
        f"{file_path}"
    )

    response = requests.get(
        file_url,
        timeout=180
    )

    if response.status_code != 200:

        raise RuntimeError(
            "Telegram не отдал файл."
        )

    return response


@app.get(
    "/albums/<int:album_id>/media/<int:position>"
)
def album_media(
    album_id,
    position
):

    item = get_album_item(
        album_id,
        position
    )

    if not item:

        return jsonify({
            "ok": False,
            "error":
                "Страница альбома "
                "не найдена."
        }), 404

    try:

        response = fetch_telegram_file(
            item["file_id"]
        )

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error":
                str(exc)
        }), 500

    content_type = (
        item["mime_type"]
        or (
            "video/mp4"
            if item["type"] == "video"
            else "image/jpeg"
        )
    )

    return Response(
        response.content,
        status=200,
        content_type=content_type,
        headers={
            "Cache-Control":
                "public, max-age=3600",
            "Access-Control-Allow-Origin":
                "*"
        }
    )


# =========================================================
# DOWNLOAD
# =========================================================

@app.get(
    "/albums/<int:album_id>/download/<int:position>"
)
def album_download(
    album_id,
    position
):

    item = get_album_item(
        album_id,
        position
    )

    if not item:

        return jsonify({
            "ok": False,
            "error":
                "Страница не найдена."
        }), 404

    try:

        response = fetch_telegram_file(
            item["file_id"]
        )

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error":
                str(exc)
        }), 500

    file_name = (
        item["file_name"]
        or f"page_{position}"
    )

    safe_name = (
        file_name
        .replace('"', "")
        .replace("\r", "")
        .replace("\n", "")
    )

    content_type = (
        item["mime_type"]
        or "application/octet-stream"
    )

    return Response(
        response.content,
        status=200,
        content_type=content_type,
        headers={
            "Content-Disposition":
                f'attachment; filename="{safe_name}"',
            "Cache-Control":
                "no-cache",
            "Access-Control-Allow-Origin":
                "https://web.telegram.org"
        }
    )


# =========================================================
# DELETE ALBUM
# =========================================================

@app.delete(
    "/albums/<int:album_id>"
)
def delete_album(album_id):

    with db_lock:

        conn = get_db()

        album = conn.execute("""
            SELECT *
            FROM albums
            WHERE id = ?
        """, (
            album_id,
        )).fetchone()

        if not album:

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "Альбом не найден."
            }), 404

        conn.execute("""
            DELETE FROM album_items
            WHERE album_id = ?
        """, (
            album_id,
        ))

        conn.execute("""
            DELETE FROM albums
            WHERE id = ?
        """, (
            album_id,
        ))

        conn.commit()
        conn.close()

    return jsonify({
        "ok": True
    })


# =========================================================
# PUBLISH ALBUM
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

        album = conn.execute("""
            SELECT *
            FROM albums
            WHERE id = ?
        """, (
            album_id,
        )).fetchone()

        if not album:

            conn.close()

            return jsonify({
                "ok": False,
                "error":
                    "Альбом не найден."
            }), 404

        count = conn.execute("""
            SELECT COUNT(*)
            FROM album_items
            WHERE album_id = ?
        """, (
            album_id,
        )).fetchone()[0]

        conn.close()

    if count == 0:

        return jsonify({
            "ok": False,
            "error":
                "В альбоме нет страниц."
        }), 400

    # -----------------------------------------------------
    # MAIN MINI APP DEEP LINK
    # -----------------------------------------------------

    album_url = (
        f"https://t.me/"
        f"KawaiiChanAsserBot"
        f"?startapp=album_{album_id}"
    )

    if custom_text:

        caption = custom_text

    else:

        caption = (
            f"📖 <b>{album['title']}</b>\n\n"
            f"{album['description']}\n\n"
            f"📚 Страниц: <b>{count}</b>\n\n"
            f"👉 <a href=\"{album_url}\">"
            f"Открыть полный альбом"
            f"</a>"
        )

    # Если пользователь дал свой текст,
    # ссылку всё равно добавляем.
    if custom_text:

        caption += (
            f"\n\n📚 Страниц: <b>{count}</b>\n"
            f"👉 <a href=\"{album_url}\">"
            f"Читать полный альбом"
            f"</a>"
        )

    result = telegram(
        "sendMessage",
        payload={
            "chat_id":
                CHANNEL_USERNAME,
            "text":
                caption,
            "parse_mode":
                "HTML",
            "disable_web_page_preview":
                "false"
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

        conn.execute("""
            UPDATE albums
            SET
                published_message_id = ?,
                status = 'published',
                updated_at = ?
            WHERE id = ?
        """, (
            message_id,
            now_iso(),
            album_id
        ))

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
# START SCHEDULER
# =========================================================

def start_scheduler():

    thread = threading.Thread(
        target=scheduler_loop,
        daemon=True
    )

    thread.start()


start_scheduler()


# =========================================================
# FLASK
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
