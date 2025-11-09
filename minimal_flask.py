from flask import Flask
app = Flask(__name__)
@app.route("/ping")
def p():
    return "pong"
app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
