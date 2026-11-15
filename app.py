"""Точка входу для хостингу (SnapDeploy, Render, ...): python app.py
Слухає 0.0.0.0 і порт зі змінної PORT (її задає хостинг), інакше 5050."""
import os

from flask_app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"SPHEREx Sky Motion Viewer on 0.0.0.0:{port}")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=int(os.environ.get("THREADS", 8)))
    except ImportError:
        app.run(host="0.0.0.0", port=port, threaded=True)
