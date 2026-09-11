from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import requests

app = Flask(__name__)

# Разрешаем Mini App обращаться к Backend
CORS(app)

# Данные из переменных Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_USERNAME = "@ahegao_hetai_hub"


@app.route("/")
def home():
    return "Kawaii Chan Backend работает!"


@app.route("/test")
def test():
    return jsonify({
        "success": True,
        "message": "Backend работает!"
    })


@app.route("/webapp", methods=["POST"])
def webapp():
    data = request.json

    return jsonify({
        "success": True,
        "message": "Mini App успешно связался с Backend!",
        "received": data
    })


# Публикация текста в Telegram-канал
@app.route("/publish", methods=["POST"])
def publish():
    data = request.json or {}

    text = data.get("text", "").strip()

    if not text:
        return jsonify({
            "success": False,
            "message": "Текст поста пустой."
        }), 400

    if not BOT_TOKEN:
        return jsonify({
            "success": False,
            "message": "BOT_TOKEN не найден в настройках Backend."
        }), 500

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHANNEL_USERNAME,
        "text": text
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=20
        )

        result = response.json()

        if result.get("ok"):
            return jsonify({
                "success": True,
                "message": "Пост успешно опубликован!",
                "telegram": result
            })

        return jsonify({
            "success": False,
            "message": "Telegram не смог опубликовать пост.",
            "telegram": result
        }), 400

    except Exception as error:
        return jsonify({
            "success": False,
            "message": "Ошибка соединения с Telegram.",
            "error": str(error)
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
