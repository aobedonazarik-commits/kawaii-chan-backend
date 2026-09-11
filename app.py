from flask import Flask, request, jsonify

app = Flask(__name__)


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
        "received": data
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
