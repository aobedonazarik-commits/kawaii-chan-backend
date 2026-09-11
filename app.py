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

DB_PATH = os.getenv(
    "DB_PATH",
    "kawaii.db"
)

UPLOAD_DIR = os.getenv(
    "UPLOAD_DIR",
    "/tmp/kawaii_uploads"
)

PORT = int(os.getenv("PORT", "10000"))

MAX_PHOTOS = 100
MAX_PHOTOS_PER_ALBUM = 10

SCHEDULER_INTERVAL = 20

# Telegram limits
MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024


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
                photo_count INTEGER DEFAULT 0,
                post_type TEXT DEFAULT 'text',
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scheduled (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                run_at TEXT NOT NULL,
                photos_json TEXT,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at TEXT NOT NULL
            )
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


def telegram_api(method, data=None, files=None, timeout=120):
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
            f"Telegram вернул некорректный ответ: "
            f"{response.text[:500]}"
        )

    if not payload.get("ok"):
        raise RuntimeError(
            payload.get(
                "description",
                "Неизвестная ошибка Telegram API."
            )
        )

    return payload.get("result")


def save_post_record(
    message_ids,
    text,
    photo_count,
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
                photo_count,
                post_type,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                json.dumps(message_ids),
                text,
                photo_count,
                post_type,
                now_iso()
            )
        )

        db.commit()
        db.close()


def cleanup_files(paths):
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


# ============================================================
# TELEGRAM PUBLISHING
# ============================================================

def publish_text(text):
    if not text:
        raise RuntimeError(
            "Текст публикации пустой."
        )

    if len(text) > MAX_TEXT_LENGTH:
        raise RuntimeError(
            "Текст слишком длинный. "
            "Максимум — 4096 символов."
        )

    result = telegram_api(
        "sendMessage",
        data={
            "chat_id": CHANNEL_USERNAME,
            "text": text
        }
    )

    message_id = result["message_id"]

    return [message_id]


def publish_one_photo(
    photo_path,
    caption=""
):
    if caption and len(caption) > MAX_CAPTION_LENGTH:
        caption = caption[:MAX_CAPTION_LENGTH]

    filename = os.path.basename(photo_path)

    mime_type = (
        mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    with open(photo_path, "rb") as photo_file:

        files = {
            "photo": (
                filename,
                photo_file,
                mime_type
            )
        }

        data = {
            "chat_id": CHANNEL_USERNAME
        }

        if caption:
            data["caption"] = caption

        result = telegram_api(
            "sendPhoto",
            data=data,
            files=files
        )

    return [result["message_id"]]


def publish_photo_group(
    photo_paths,
    caption=""
):
    if not photo_paths:
        return []

    if len(photo_paths) > MAX_PHOTOS_PER_ALBUM:
        raise RuntimeError(
            "В одном альбоме Telegram может быть "
            "максимум 10 фотографий."
        )

    media = []
    opened_files = []
    files = {}

    try:
        for index, photo_path in enumerate(photo_paths):

            attach_name = f"photo{index}"

            filename = os.path.basename(photo_path)

            mime_type = (
                mimetypes.guess_type(filename)[0]
                or "application/octet-stream"
            )

            file_object = open(
                photo_path,
                "rb"
            )

            opened_files.append(file_object)

            files[attach_name] = (
                filename,
                file_object,
                mime_type
            )

            item = {
                "type": "photo",
                "media": f"attach://{attach_name}"
            }

            # Caption only on the first photo
            if index == 0 and caption:
                item["caption"] = caption[:MAX_CAPTION_LENGTH]

            media.append(item)

        data = {
            "chat_id": CHANNEL_USERNAME,
            "media": json.dumps(
                media,
                ensure_ascii=False
            )
        }

        result = telegram_api(
            "sendMediaGroup",
            data=data,
            files=files
        )

        message_ids = []

        for message in result:
            if "message_id" in message:
                message_ids.append(
                    message["message_id"]
                )

        return message_ids

    finally:
        for file_object in opened_files:
            try:
                file_object.close()
            except Exception:
                pass


def publish_photos(
    photo_paths,
    text=""
):
    if not photo_paths:
        return publish_text(text)

    if len(photo_paths) > MAX_PHOTOS:
        raise RuntimeError(
            f"Слишком много фотографий. "
            f"Максимум за одну публикацию: {MAX_PHOTOS}."
        )

    message_ids = []

    # Telegram albums: maximum 10 photos
    groups = []

    for index in range(
        0,
        len(photo_paths),
        MAX_PHOTOS_PER_ALBUM
    ):
        groups.append(
            photo_paths[
                index:index + MAX_PHOTOS_PER_ALBUM
            ]
        )

    remaining_text = text or ""

    for group_index, group in enumerate(groups):

        caption = ""

        if group_index == 0:
            caption = remaining_text[
                :MAX_CAPTION_LENGTH
            ]

        group_ids = publish_photo_group(
            group,
            caption
        )

        message_ids.extend(group_ids)

        # Wait between Telegram album requests
        if group_index < len(groups) - 1:
            time.sleep(1)

    # If caption was longer than Telegram's
    # photo caption limit, send remaining text
    # as a separate message.
    if len(remaining_text) > MAX_CAPTION_LENGTH:

        remainder = remaining_text[
            MAX_CAPTION_LENGTH:
        ]

        # Telegram text limit
        while remainder:
            part = remainder[:MAX_TEXT_LENGTH]

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


def publish_files(
    text,
    photo_paths
):
    text = (text or "").strip()

    if not text and not photo_paths:
        raise RuntimeError(
            "Нельзя опубликовать пустой пост."
        )

    if photo_paths:
        message_ids = publish_photos(
            photo_paths,
            text
        )

        post_type = "photo"

    else:
        message_ids = publish_text(text)

        post_type = "text"

    save_post_record(
        message_ids,
        text,
        len(photo_paths),
        post_type
    )

    return message_ids


# ============================================================
# FILE UPLOADS
# ============================================================

def save_uploaded_photos(files):
    saved_paths = []

    if len(files) > MAX_PHOTOS:
        raise RuntimeError(
            f"Можно выбрать максимум {MAX_PHOTOS} фотографий."
        )

    for file in files:

        if not file:
            continue

        original_name = (
            file.filename
            or "photo"
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
# ROOT / TEST
# ============================================================

@app.route("/", methods=["GET"])
def root():

    return jsonify({
        "ok": True,
        "service": "Kawaii Chan Backend",
        "status": "online",
        "channel": CHANNEL_USERNAME,
        "time": now_iso()
    })


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
            "channel": CHANNEL_USERNAME
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


@app.route("/webapp", methods=["GET"])
def webapp():

    return jsonify({
        "ok": True,
        "message": "Kawaii Chan Web App backend работает."
    })


# ============================================================
# PUBLISH
# ============================================================

@app.route("/publish", methods=["POST"])
def publish():

    saved_paths = []

    try:

        # ----------------------------------------------------
        # JSON request
        # ----------------------------------------------------

        if request.is_json:

            data = request.get_json(
                silent=True
            ) or {}

            text = (
                data.get("text", "")
                or ""
            ).strip()

            message_ids = publish_files(
                text,
                []
            )

            return jsonify({
                "ok": True,
                "message": "Пост успешно опубликован.",
                "message_ids": message_ids,
                "photo_count": 0
            })

        # ----------------------------------------------------
        # Multipart request
        # ----------------------------------------------------

        text = (
            request.form.get(
                "text",
                ""
            )
            or ""
        ).strip()

        photos = request.files.getlist(
            "photos"
        )

        saved_paths = save_uploaded_photos(
            photos
        )

        message_ids = publish_files(
            text,
            saved_paths
        )

        return jsonify({
            "ok": True,
            "message": "Пост успешно опубликован.",
            "message_ids": message_ids,
            "photo_count": len(saved_paths)
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

            data = request.get_json(
                silent=True
            ) or {}

            text = (
                data.get("text", "")
                or ""
            ).strip()

            run_at = (
                data.get("run_at", "")
                or ""
            ).strip()

            photos = []

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

            photos = request.files.getlist(
                "photos"
            )

        if not text and not photos:
            return jsonify({
                "ok": False,
                "error": "Пост пустой."
            }), 400

        parsed_time = parse_iso_datetime(
            run_at
        )

        if not parsed_time:
            return jsonify({
                "ok": False,
                "error": "Неверная дата или время."
            }), 400

        if parsed_time <= now_utc():
            return jsonify({
                "ok": False,
                "error": "Время публикации должно быть в будущем."
            }), 400

        if photos:
            saved_paths = save_uploaded_photos(
                photos
            )

        created_at = now_iso()

        with db_lock:

            db = get_db()

            cursor = db.execute(
                """
                INSERT INTO scheduled
                (
                    text,
                    run_at,
                    photos_json,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    text,
                    parsed_time.isoformat(),
                    json.dumps(
                        saved_paths,
                        ensure_ascii=False
                    ),
                    "pending",
                    created_at
                )
            )

            scheduled_id = cursor.lastrowid

            db.commit()
            db.close()

        # Important:
        # files must stay on disk until scheduler publishes them.
        saved_paths = []

        return jsonify({
            "ok": True,
            "message": "Пост поставлен в очередь.",
            "id": scheduled_id,
            "run_at": parsed_time.isoformat()
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500

    finally:

        # If scheduling failed, remove uploaded files.
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

            photos = json.loads(
                row["photos_json"]
                or "[]"
            )

            result.append({
                "id": row["id"],
                "text": row["text"],
                "run_at": row["run_at"],
                "status": row["status"],
                "error": row["error"],
                "photo_count": len(photos),
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
def cancel_scheduled(
    scheduled_id
):

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
                    "error": "Отложенный пост не найден."
                }), 404

            photos = json.loads(
                row["photos_json"]
                or "[]"
            )

            db.execute(
                """
                DELETE FROM scheduled
                WHERE id = ?
                """,
                (scheduled_id,)
            )

            db.commit()
            db.close()

        cleanup_files(
            photos
        )

        return jsonify({
            "ok": True,
            "message": "Отложенный пост отменён."
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

            message_ids = json.loads(
                row["telegram_message_ids"]
            )

            result.append({
                "id": row["id"],
                "text": row["text"],
                "photo_count": row["photo_count"],
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
                "photo_count": row["photo_count"],
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

        data = request.get_json(
            silent=True
        ) or {}

        post_id = data.get("id")
        new_text = (
            data.get("text", "")
            or ""
        ).strip()

        if not post_id:
            return jsonify({
                "ok": False,
                "error": "Не указан ID поста."
            }), 400

        if not new_text:
            return jsonify({
                "ok": False,
                "error": "Новый текст пустой."
            }), 400

        if len(new_text) > MAX_TEXT_LENGTH:
            return jsonify({
                "ok": False,
                "error": "Текст превышает 4096 символов."
            }), 400

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
                    "error": "Telegram message ID не найден."
                }), 400

            first_message_id = message_ids[0]

            if row["photo_count"] > 0:

                result = telegram_api(
                    "editMessageCaption",
                    data={
                        "chat_id": CHANNEL_USERNAME,
                        "message_id": first_message_id,
                        "caption": new_text[:MAX_CAPTION_LENGTH]
                    }
                )

            else:

                result = telegram_api(
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
                    "но Telegram не смог удалить "
                    "одно или несколько сообщений."
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

        # Channel members
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

            photos_count = db.execute(
                """
                SELECT COALESCE(
                    SUM(photo_count),
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
            "photos_published_by_app": photos_count,
            "scheduled_pending": scheduled_count,
            "note": (
                "Просмотры и реакции Telegram-канала "
                "не предоставляются этим API напрямую "
                "как полноценная историческая аналитика."
            )
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
        "backend": "online",
        "scheduler": "active",
        "max_photos_per_publication": MAX_PHOTOS,
        "max_photos_per_album": MAX_PHOTOS_PER_ALBUM,
        "max_text_length": MAX_TEXT_LENGTH,
        "max_photo_caption_length": MAX_CAPTION_LENGTH,
        "server_time_utc": now_iso()
    })


# ============================================================
# SCHEDULER
# ============================================================

def process_scheduled_posts():

    while True:

        try:

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

            for row in rows:

                run_at = parse_iso_datetime(
                    row["run_at"]
                )

                if not run_at:
                    continue

                if run_at > current_time:
                    continue

                scheduled_id = row["id"]

                # Lock this scheduled post first
                with db_lock:

                    db = get_db()

                    db.execute(
                        """
                        UPDATE scheduled
                        SET status = 'processing'
                        WHERE id = ?
                        AND status = 'pending'
                        """,
                        (scheduled_id,)
                    )

                    db.commit()
                    db.close()

                try:

                    photo_paths = json.loads(
                        row["photos_json"]
                        or "[]"
                    )

                    message_ids = publish_files(
                        row["text"] or "",
                        photo_paths
                    )

                    cleanup_files(
                        photo_paths
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

        except Exception:
            # Scheduler must never kill itself
            pass

        time.sleep(
            SCHEDULER_INTERVAL
        )


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
