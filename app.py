import os
import json
import time
import uuid
import sqlite3
import mimetypes
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHANNEL_USERNAME = os.getenv(
    "CHANNEL_USERNAME",
    "@ahegao_hetai_hub"
).strip()

# Наш существующий закрытый Storage-канал
STORAGE_CHAT_ID = os.getenv(
    "STORAGE_CHAT_ID",
    "-1003992513200"
).strip()

DB_PATH = os.getenv(
    "DB_PATH",
    "kawaii.db"
)

UPLOAD_DIR = os.getenv(
    "UPLOAD_DIR",
    "/tmp/kawaii_uploads"
)

PORT = int(os.getenv("PORT", "10000"))

MAX_MEDIA = 100
MAX_MEDIA_PER_ALBUM = 10

SCHEDULER_INTERVAL = 20

MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

# Telegram Bot API sendVideo currently allows files up to 50 MB.
MAX_VIDEO_SIZE = 50 * 1024 * 1024


# ============================================================
# APP
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

os.makedirs(UPLOAD_DIR, exist_ok=True)


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.Lock()


def get_db():
    connection = sqlite3.connect(
        DB_PATH,
        check_same_thread=False
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_db():
    with db_lock:
        db = get_db()

        cursor = db.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_ids TEXT NOT NULL,
                text TEXT,
                media_count INTEGER DEFAULT 0,
                photo_count INTEGER DEFAULT 0,
                video_count INTEGER DEFAULT 0,
                post_type TEXT DEFAULT 'text',
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scheduled (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                run_at TEXT NOT NULL,
                media_json TEXT,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at TEXT NOT NULL
            )
        """)

        db.commit()

        # Мягкая миграция старой таблицы scheduled
        columns = db.execute(
            "PRAGMA table_info(scheduled)"
        ).fetchall()

        column_names = {
            column["name"]
            for column in columns
        }

        if "media_json" not in column_names:
            db.execute("""
                ALTER TABLE scheduled
                ADD COLUMN media_json TEXT
            """)

        # Мягкая миграция старой таблицы posts
        post_columns = db.execute(
            "PRAGMA table_info(posts)"
        ).fetchall()

        post_column_names = {
            column["name"]
            for column in post_columns
        }

        if "media_count" not in post_column_names:
            db.execute("""
                ALTER TABLE posts
                ADD COLUMN media_count INTEGER DEFAULT 0
            """)

        if "video_count" not in post_column_names:
            db.execute("""
                ALTER TABLE posts
                ADD COLUMN video_count INTEGER DEFAULT 0
            """)

        db.commit()
        db.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def parse_iso_datetime(value):
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except Exception:
        return None


def telegram_api(
    method,
    data=None,
    files=None,
    timeout=120
):
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не найден в настройках Render."
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    try:
        response = requests.post(
            url,
            data=data,
            files=files,
            timeout=timeout
        )
    except requests.RequestException as error:
        raise RuntimeError(
            f"Ошибка соединения с Telegram: {error}"
        )

    try:
        payload = response.json()
    except Exception:
        raise RuntimeError(
            "Telegram вернул некорректный ответ: "
            + response.text[:500]
        )

    if not payload.get("ok"):
        raise RuntimeError(
            payload.get(
                "description",
                "Неизвестная ошибка Telegram API."
            )
        )

    return payload.get("result")


def cleanup_files(paths):
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


def detect_media_type(path):
    mime_type = (
        mimetypes.guess_type(path)[0]
        or ""
    ).lower()

    if mime_type.startswith("video/"):
        return "video"

    return "photo"


def validate_text(text):
    if len(text) > MAX_TEXT_LENGTH:
        raise RuntimeError(
            "Текст слишком длинный. "
            "Максимум — 4096 символов."
        )


# ============================================================
# STORAGE CHANNEL
# ============================================================

def storage_chat_id():
    if not STORAGE_CHAT_ID:
        raise RuntimeError(
            "STORAGE_CHAT_ID не настроен в Render."
        )

    return STORAGE_CHAT_ID


def save_photo_to_storage(path):
    filename = os.path.basename(path)

    mime_type = (
        mimetypes.guess_type(filename)[0]
        or "image/jpeg"
    )

    with open(path, "rb") as media_file:

        result = telegram_api(
            "sendPhoto",
            data={
                "chat_id": storage_chat_id()
            },
            files={
                "photo": (
                    filename,
                    media_file,
                    mime_type
                )
            }
        )

    photo = result.get("photo") or []

    if not photo:
        raise RuntimeError(
            "Telegram не вернул информацию о сохранённом фото."
        )

    # Самый большой размер фото
    best = photo[-1]

    return {
        "type": "photo",
        "file_id": best["file_id"],
        "storage_message_id": result["message_id"]
    }


def save_video_to_storage(path):
    filename = os.path.basename(path)

    file_size = os.path.getsize(path)

    if file_size > MAX_VIDEO_SIZE:
        raise RuntimeError(
            "Видео слишком большое. "
            "Максимальный размер — 50 МБ."
        )

    mime_type = (
        mimetypes.guess_type(filename)[0]
        or "video/mp4"
    )

    with open(path, "rb") as media_file:

        result = telegram_api(
            "sendVideo",
            data={
                "chat_id": storage_chat_id(),
                "supports_streaming": "true"
            },
            files={
                "video": (
                    filename,
                    media_file,
                    mime_type
                )
            },
            timeout=180
        )

    file_id = (
        result.get("video", {})
        .get("file_id")
    )

    if not file_id:
        raise RuntimeError(
            "Telegram не вернул file_id видео."
        )

    return {
        "type": "video",
        "file_id": file_id,
        "storage_message_id": result["message_id"]
    }


def save_media_to_storage(path):
    media_type = detect_media_type(path)

    if media_type == "video":
        return save_video_to_storage(path)

    return save_photo_to_storage(path)


def save_media_files_to_storage(paths):
    result = []

    for path in paths:

        if not os.path.exists(path):
            raise RuntimeError(
                f"Файл не найден: {os.path.basename(path)}"
            )

        saved = save_media_to_storage(path)

        result.append(saved)

    return result


# ============================================================
# TELEGRAM PUBLISHING
# ============================================================

def publish_text(text):
    text = (text or "").strip()

    if not text:
        raise RuntimeError(
            "Текст публикации пустой."
        )

    validate_text(text)

    result = telegram_api(
        "sendMessage",
        data={
            "chat_id": CHANNEL_USERNAME,
            "text": text
        }
    )

    return [
        result["message_id"]
    ]


def publish_media_group(
    media_items,
    caption=""
):
    if not media_items:
        return []

    if len(media_items) > MAX_MEDIA_PER_ALBUM:
        raise RuntimeError(
            "В одной группе Telegram может быть "
            "максимум 10 медиа."
        )

    media = []

    for index, item in enumerate(media_items):

        media_type = item.get("type")
        file_id = item.get("file_id")

        if not file_id:
            raise RuntimeError(
                "У медиа отсутствует file_id."
            )

        if media_type == "video":
            telegram_type = "video"
        else:
            telegram_type = "photo"

        entry = {
            "type": telegram_type,
            "media": file_id
        }

        if index == 0 and caption:
            entry["caption"] = (
                caption[:MAX_CAPTION_LENGTH]
            )

        media.append(entry)

    result = telegram_api(
        "sendMediaGroup",
        data={
            "chat_id": CHANNEL_USERNAME,
            "media": json.dumps(
                media,
                ensure_ascii=False
            )
        },
        timeout=180
    )

    message_ids = []

    for message in result:

        if "message_id" in message:
            message_ids.append(
                message["message_id"]
            )

    return message_ids


def publish_stored_media(
    media_items,
    text=""
):
    if not media_items:
        return publish_text(text)

    if len(media_items) > MAX_MEDIA:
        raise RuntimeError(
            f"Максимум за одну публикацию: "
            f"{MAX_MEDIA} медиа."
        )

    validate_text(text or "")

    groups = []

    for index in range(
        0,
        len(media_items),
        MAX_MEDIA_PER_ALBUM
    ):
        groups.append(
            media_items[
                index:index + MAX_MEDIA_PER_ALBUM
            ]
        )

    message_ids = []

    first_caption = (
        text[:MAX_CAPTION_LENGTH]
        if text
        else ""
    )

    for group_index, group in enumerate(groups):

        caption = ""

        if group_index == 0:
            caption = first_caption

        ids = publish_media_group(
            group,
            caption
        )

        message_ids.extend(ids)

        if group_index < len(groups) - 1:
            time.sleep(1)

    # Если текст длиннее лимита подписи —
    # оставшуюся часть отправляем отдельным сообщением.
    if len(text) > MAX_CAPTION_LENGTH:

        remainder = text[
            MAX_CAPTION_LENGTH:
        ]

        while remainder:

            part = remainder[
                :MAX_TEXT_LENGTH
            ]

            result = telegram_api(
                "sendMessage",
                data={
                    "chat_id": CHANNEL_USERNAME,
                    "text": part
                }
            )

            message_ids.append(
                result["message_id"]
            )

            remainder = remainder[
                MAX_TEXT_LENGTH:
            ]

    return message_ids


# ============================================================
# POST HISTORY
# ============================================================

def save_post_record(
    message_ids,
    text,
    media_count,
    photo_count,
    video_count,
    post_type
):
    with db_lock:

        db = get_db()

        db.execute(
            """
            INSERT INTO posts
            (
                telegram_message_ids,
                text,
                media_count,
                photo_count,
                video_count,
                post_type,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                json.dumps(
                    message_ids
                ),
                text,
                media_count,
                photo_count,
                video_count,
                post_type,
                now_iso()
            )
        )

        db.commit()
        db.close()


def publish_stored_post(
    text,
    media_items
):
    text = (text or "").strip()

    if not text and not media_items:
        raise RuntimeError(
            "Нельзя опубликовать пустой пост."
        )

    if media_items:

        message_ids = publish_stored_media(
            media_items,
            text
        )

        photo_count = sum(
            1
            for item in media_items
            if item.get("type") == "photo"
        )

        video_count = sum(
            1
            for item in media_items
            if item.get("type") == "video"
        )

        if video_count and photo_count:
            post_type = "mixed"
        elif video_count:
            post_type = "video"
        else:
            post_type = "photo"

        save_post_record(
            message_ids,
            text,
            len(media_items),
            photo_count,
            video_count,
            post_type
        )

        return message_ids

    message_ids = publish_text(text)

    save_post_record(
        message_ids,
        text,
        0,
        0,
        0,
        "text"
    )

    return message_ids


# ============================================================
# FILE UPLOADS
# ============================================================

def save_uploaded_media(files):

    saved_paths = []

    if len(files) > MAX_MEDIA:
        raise RuntimeError(
            f"Можно выбрать максимум "
            f"{MAX_MEDIA} файлов."
        )

    for file in files:

        if not file:
            continue

        original_name = (
            file.filename
            or "media"
        )

        extension = os.path.splitext(
            original_name
        )[1].lower()

        if not extension:
            extension = ".jpg"

        unique_name = (
            f"{uuid.uuid4().hex}"
            f"{extension}"
        )

        path = os.path.join(
            UPLOAD_DIR,
            unique_name
        )

        file.save(path)

        saved_paths.append(path)

    return saved_paths


# ============================================================
# ROOT
# ============================================================

@app.route("/", methods=["GET"])
def root():

    return jsonify({
        "ok": True,
        "service": "Kawaii Chan Backend",
        "status": "online",
        "channel": CHANNEL_USERNAME,
        "storage_chat_id": STORAGE_CHAT_ID,
        "time": now_iso()
    })


# ============================================================
# TEST
# ============================================================

@app.route("/test", methods=["GET"])
def test():

    if not BOT_TOKEN:
        return jsonify({
            "ok": False,
            "error": "BOT_TOKEN не найден."
        }), 500

    try:

        result = telegram_api(
            "getMe"
        )

        return jsonify({
            "ok": True,
            "message": "Бэкэнд подключен.",
            "bot": {
                "id": result.get("id"),
                "username": result.get("username"),
                "first_name": result.get("first_name")
            },
            "channel": CHANNEL_USERNAME,
            "storage_chat_id": STORAGE_CHAT_ID
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# STORAGE TEST
# ============================================================

@app.route("/storage-test", methods=["GET"])
def storage_test():

    try:

        result = telegram_api(
            "sendMessage",
            data={
                "chat_id": storage_chat_id(),
                "text": (
                    "Kawaii Chan Storage подключён.\n"
                    "Тест Backend: OK."
                )
            }
        )

        return jsonify({
            "ok": True,
            "message": "Storage-канал работает.",
            "storage_chat_id": STORAGE_CHAT_ID,
            "message_id": result["message_id"]
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# WEBAPP
# ============================================================

@app.route("/webapp", methods=["GET"])
def webapp():

    return jsonify({
        "ok": True,
        "message": (
            "Kawaii Chan Web App backend работает."
        )
    })


# ============================================================
# PUBLISH
# ============================================================

@app.route("/publish", methods=["POST"])
def publish():

    saved_paths = []

    try:

        if request.is_json:

            data = (
                request.get_json(
                    silent=True
                )
                or {}
            )

            text = (
                data.get("text", "")
                or ""
            ).strip()

            message_ids = publish_stored_post(
                text,
                []
            )

            return jsonify({
                "ok": True,
                "message": (
                    "Пост успешно опубликован."
                ),
                "message_ids": message_ids,
                "media_count": 0
            })

        text = (
            request.form.get(
                "text",
                ""
            )
            or ""
        ).strip()

        media_files = request.files.getlist(
            "photos"
        )

        # Также принимаем поле media
        if not media_files:
            media_files = request.files.getlist(
                "media"
            )

        saved_paths = save_uploaded_media(
            media_files
        )

        media_items = (
            save_media_files_to_storage(
                saved_paths
            )
        )

        message_ids = publish_stored_post(
            text,
            media_items
        )

        return jsonify({
            "ok": True,
            "message": (
                "Пост успешно опубликован."
            ),
            "message_ids": message_ids,
            "media_count": len(media_items)
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500

    finally:

        cleanup_files(
            saved_paths
        )


# ============================================================
# SCHEDULE
# ============================================================

@app.route("/schedule", methods=["POST"])
def schedule():

    saved_paths = []

    try:

        if request.is_json:

            data = (
                request.get_json(
                    silent=True
                )
                or {}
            )

            text = (
                data.get("text", "")
                or ""
            ).strip()

            run_at = (
                data.get("run_at", "")
                or ""
            ).strip()

            media_items = (
                data.get("media")
                or []
            )

        else:

            text = (
                request.form.get(
                    "text",
                    ""
                )
                or ""
            ).strip()

            run_at = (
                request.form.get(
                    "run_at",
                    ""
                )
                or ""
            ).strip()

            media_files = request.files.getlist(
                "photos"
            )

            if not media_files:
                media_files = request.files.getlist(
                    "media"
                )

            saved_paths = save_uploaded_media(
                media_files
            )

            media_items = (
                save_media_files_to_storage(
                    saved_paths
                )
            )

        if not text and not media_items:

            return jsonify({
                "ok": False,
                "error": "Пост пустой."
            }), 400

        validate_text(text)

        parsed_time = parse_iso_datetime(
            run_at
        )

        if not parsed_time:

            return jsonify({
                "ok": False,
                "error": (
                    "Неверная дата или время."
                )
            }), 400

        if parsed_time <= now_utc():

            return jsonify({
                "ok": False,
                "error": (
                    "Время публикации должно "
                    "быть в будущем."
                )
            }), 400

        created_at = now_iso()

        with db_lock:

            db = get_db()

            cursor = db.execute(
                """
                INSERT INTO scheduled
                (
                    text,
                    run_at,
                    media_json,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    text,
                    parsed_time.isoformat(),
                    json.dumps(
                        media_items,
                        ensure_ascii=False
                    ),
                    "pending",
                    created_at
                )
            )

            scheduled_id = cursor.lastrowid

            db.commit()
            db.close()

        return jsonify({
            "ok": True,
            "message": (
                "Пост поставлен в очередь."
            ),
            "id": scheduled_id,
            "run_at": parsed_time.isoformat(),
            "media_count": len(media_items)
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500

    finally:

        cleanup_files(
            saved_paths
        )


# ============================================================
# SCHEDULED LIST
# ============================================================

@app.route("/scheduled", methods=["GET"])
def scheduled_list():

    try:

        with db_lock:

            db = get_db()

            rows = db.execute(
                """
                SELECT *
                FROM scheduled
                ORDER BY run_at ASC
                """
            ).fetchall()

            db.close()

        result = []

        for row in rows:

            media = []

            try:
                media = json.loads(
                    row["media_json"]
                    or "[]"
                )
            except Exception:
                media = []

            photo_count = sum(
                1
                for item in media
                if item.get("type") == "photo"
            )

            video_count = sum(
                1
                for item in media
                if item.get("type") == "video"
            )

            result.append({
                "id": row["id"],
                "text": row["text"],
                "run_at": row["run_at"],
                "status": row["status"],
                "error": row["error"],
                "media_count": len(media),
                "photo_count": photo_count,
                "video_count": video_count,
                "created_at": row["created_at"]
            })

        return jsonify({
            "ok": True,
            "items": result
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# CANCEL SCHEDULED
# ============================================================

@app.route(
    "/scheduled/<int:scheduled_id>",
    methods=["DELETE"]
)
def cancel_scheduled(scheduled_id):

    try:

        with db_lock:

            db = get_db()

            row = db.execute(
                """
                SELECT *
                FROM scheduled
                WHERE id = ?
                """,
                (scheduled_id,)
            ).fetchone()

            if not row:

                db.close()

                return jsonify({
                    "ok": False,
                    "error": (
                        "Отложенный пост "
                        "не найден."
                    )
                }), 404

            db.execute(
                """
                DELETE FROM scheduled
                WHERE id = ?
                """,
                (scheduled_id,)
            )

            db.commit()
            db.close()

        return jsonify({
            "ok": True,
            "message": (
                "Отложенный пост отменён."
            )
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# POSTS HISTORY
# ============================================================

@app.route("/posts", methods=["GET"])
def posts():

    try:

        with db_lock:

            db = get_db()

            rows = db.execute(
                """
                SELECT *
                FROM posts
                ORDER BY id DESC
                LIMIT 100
                """
            ).fetchall()

            db.close()

        result = []

        for row in rows:

            try:
                message_ids = json.loads(
                    row["telegram_message_ids"]
                )
            except Exception:
                message_ids = []

            result.append({
                "id": row["id"],
                "text": row["text"],
                "media_count": row["media_count"],
                "photo_count": row["photo_count"],
                "video_count": row["video_count"],
                "post_type": row["post_type"],
                "message_ids": message_ids,
                "created_at": row["created_at"]
            })

        return jsonify({
            "ok": True,
            "items": result
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# GET POST
# ============================================================

@app.route(
    "/posts/<int:post_id>",
    methods=["GET"]
)
def get_post(post_id):

    try:

        with db_lock:

            db = get_db()

            row = db.execute(
                """
                SELECT *
                FROM posts
                WHERE id = ?
                """,
                (post_id,)
            ).fetchone()

            db.close()

        if not row:

            return jsonify({
                "ok": False,
                "error": "Пост не найден."
            }), 404

        return jsonify({
            "ok": True,
            "post": {
                "id": row["id"],
                "text": row["text"],
                "media_count": row["media_count"],
                "photo_count": row["photo_count"],
                "video_count": row["video_count"],
                "post_type": row["post_type"],
                "message_ids": json.loads(
                    row["telegram_message_ids"]
                ),
                "created_at": row["created_at"]
            }
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# EDIT POST
# ============================================================

@app.route(
    "/posts/edit",
    methods=["POST"]
)
def edit_post():

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        post_id = data.get("id")

        new_text = (
            data.get("text", "")
            or ""
        ).strip()

        if not post_id:

            return jsonify({
                "ok": False,
                "error": (
                    "Не указан ID поста."
                )
            }), 400

        if not new_text:

            return jsonify({
                "ok": False,
                "error": (
                    "Новый текст пустой."
                )
            }), 400

        validate_text(new_text)

        with db_lock:

            db = get_db()

            row = db.execute(
                """
                SELECT *
                FROM posts
                WHERE id = ?
                """,
                (post_id,)
            ).fetchone()

            if not row:

                db.close()

                return jsonify({
                    "ok": False,
                    "error": "Пост не найден."
                }), 404

            message_ids = json.loads(
                row["telegram_message_ids"]
            )

            if not message_ids:

                db.close()

                return jsonify({
                    "ok": False,
                    "error": (
                        "Telegram message ID "
                        "не найден."
                    )
                }), 400

            first_message_id = message_ids[0]

            if row["media_count"] > 0:

                telegram_api(
                    "editMessageCaption",
                    data={
                        "chat_id": CHANNEL_USERNAME,
                        "message_id": first_message_id,
                        "caption": (
                            new_text[
                                :MAX_CAPTION_LENGTH
                            ]
                        )
                    }
                )

            else:

                telegram_api(
                    "editMessageText",
                    data={
                        "chat_id": CHANNEL_USERNAME,
                        "message_id": first_message_id,
                        "text": new_text
                    }
                )

            db.execute(
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

            db.commit()
            db.close()

        return jsonify({
            "ok": True,
            "message": "Пост обновлён."
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# DELETE POST
# ============================================================

@app.route(
    "/posts/<int:post_id>",
    methods=["DELETE"]
)
def delete_post(post_id):

    try:

        with db_lock:

            db = get_db()

            row = db.execute(
                """
                SELECT *
                FROM posts
                WHERE id = ?
                """,
                (post_id,)
            ).fetchone()

            if not row:

                db.close()

                return jsonify({
                    "ok": False,
                    "error": "Пост не найден."
                }), 404

            message_ids = json.loads(
                row["telegram_message_ids"]
            )

            errors = []

            for message_id in message_ids:

                try:

                    telegram_api(
                        "deleteMessage",
                        data={
                            "chat_id": CHANNEL_USERNAME,
                            "message_id": message_id
                        }
                    )

                except Exception as error:

                    errors.append(
                        str(error)
                    )

            db.execute(
                """
                DELETE FROM posts
                WHERE id = ?
                """,
                (post_id,)
            )

            db.commit()
            db.close()

        if errors:

            return jsonify({
                "ok": True,
                "message": (
                    "Запись удалена, "
                    "но Telegram не смог "
                    "удалить одно или несколько "
                    "сообщений."
                ),
                "errors": errors
            })

        return jsonify({
            "ok": True,
            "message": "Пост удалён."
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# STATISTICS
# ============================================================

@app.route("/stats", methods=["GET"])
def stats():

    try:

        member_result = telegram_api(
            "getChatMemberCount",
            data={
                "chat_id": CHANNEL_USERNAME
            }
        )

        with db_lock:

            db = get_db()

            posts_count = db.execute(
                """
                SELECT COUNT(*)
                FROM posts
                """
            ).fetchone()[0]

            media_count = db.execute(
                """
                SELECT COALESCE(
                    SUM(media_count),
                    0
                )
                FROM posts
                """
            ).fetchone()[0]

            photo_count = db.execute(
                """
                SELECT COALESCE(
                    SUM(photo_count),
                    0
                )
                FROM posts
                """
            ).fetchone()[0]

            video_count = db.execute(
                """
                SELECT COALESCE(
                    SUM(video_count),
                    0
                )
                FROM posts
                """
            ).fetchone()[0]

            scheduled_count = db.execute(
                """
                SELECT COUNT(*)
                FROM scheduled
                WHERE status = 'pending'
                """
            ).fetchone()[0]

            db.close()

        return jsonify({
            "ok": True,
            "channel": CHANNEL_USERNAME,
            "members": member_result,
            "posts_published_by_app": posts_count,
            "media_published_by_app": media_count,
            "photos_published_by_app": photo_count,
            "videos_published_by_app": video_count,
            "scheduled_pending": scheduled_count
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# SETTINGS
# ============================================================

@app.route("/settings", methods=["GET"])
def settings():

    return jsonify({
        "ok": True,
        "channel": CHANNEL_USERNAME,
        "storage_chat_id": STORAGE_CHAT_ID,
        "backend": "online",
        "scheduler": "active",
        "max_media_per_publication": MAX_MEDIA,
        "max_media_per_album": MAX_MEDIA_PER_ALBUM,
        "max_text_length": MAX_TEXT_LENGTH,
        "max_caption_length": MAX_CAPTION_LENGTH,
        "max_video_size_mb": 50,
        "server_time_utc": now_iso()
    })


# ============================================================
# SCHEDULER
# ============================================================

def process_scheduled_posts_once():

    current_time = now_utc()

    with db_lock:

        db = get_db()

        rows = db.execute(
            """
            SELECT *
            FROM scheduled
            WHERE status = 'pending'
            ORDER BY run_at ASC
            """
        ).fetchall()

        db.close()

    processed = 0

    for row in rows:

        run_at = parse_iso_datetime(
            row["run_at"]
        )

        if not run_at:
            continue

        if run_at > current_time:
            continue

        scheduled_id = row["id"]

        # Захватываем задачу
        with db_lock:

            db = get_db()

            cursor = db.execute(
                """
                UPDATE scheduled
                SET status = 'processing'
                WHERE id = ?
                AND status = 'pending'
                """,
                (scheduled_id,)
            )

            db.commit()

            claimed = (
                cursor.rowcount == 1
            )

            db.close()

        if not claimed:
            continue

        try:

            media_items = []

            try:
                media_items = json.loads(
                    row["media_json"]
                    or "[]"
                )
            except Exception:
                media_items = []

            publish_stored_post(
                row["text"] or "",
                media_items
            )

            with db_lock:

                db = get_db()

                db.execute(
                    """
                    UPDATE scheduled
                    SET status = 'done'
                    WHERE id = ?
                    """,
                    (scheduled_id,)
                )

                db.commit()
                db.close()

            processed += 1

        except Exception as error:

            with db_lock:

                db = get_db()

                db.execute(
                    """
                    UPDATE scheduled
                    SET status = 'error',
                        error = ?
                    WHERE id = ?
                    """,
                    (
                        str(error),
                        scheduled_id
                    )
                )

                db.commit()
                db.close()

    return processed


def process_scheduled_posts():

    while True:

        try:

            process_scheduled_posts_once()

        except Exception:
            # Не позволяем потоку умереть
            pass

        time.sleep(
            SCHEDULER_INTERVAL
        )


# ============================================================
# EXTERNAL SCHEDULER ENDPOINT
# ============================================================

@app.route(
    "/scheduler/tick",
    methods=["GET", "POST"]
)
def scheduler_tick():

    try:

        processed = (
            process_scheduled_posts_once()
        )

        return jsonify({
            "ok": True,
            "processed": processed,
            "time": now_iso()
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# START SCHEDULER
# ============================================================

scheduler_thread = threading.Thread(
    target=process_scheduled_posts,
    daemon=True
)

scheduler_thread.start()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )
