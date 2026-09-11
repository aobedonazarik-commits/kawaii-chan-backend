from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# Разрешаем нашему Mini App обращаться к бэкенду
CORS(app)


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


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
