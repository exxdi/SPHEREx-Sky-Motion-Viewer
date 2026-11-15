"""
SPHEREx Sky Motion Viewer на Flask.

Уся логіка (пошук в IRSA, кадри, Find, каталоги, пакет даних) — та сама, з api.py.
Цей файл лише бере кожен маршрут /api/... з api.py і реєструє його у Flask, тож
сайт і API працюють однаково, хоч через FastAPI (python api.py), хоч через Flask.

    python flask_app.py                     # http://127.0.0.1:5050/
    python flask_app.py --host 0.0.0.0      # відкрити для інших комп'ютерів у мережі
    python flask_app.py --port 8000

Для хостингу з WSGI (PythonAnywhere, gunicorn, waitress):  flask_app:app
"""
import argparse
import gzip
import inspect
import json
import math
import re
import typing

from flask import Flask, Response as FlaskResponse, request, send_file

import api

app = Flask(__name__, static_folder=str(api._STATIC), static_url_path="")


# ------------------------------------------------------------ conversions --
class _BadParam(Exception):
    pass


def _plain(o):
    """numpy-числа та інше -> JSON. NaN/inf -> null (як у браузера немає NaN у JSON)."""
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def _clean(o):
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _json(obj, status=200):
    body = json.dumps(_clean(obj), default=_plain, ensure_ascii=False).encode()
    headers = {}
    if len(body) > 1000 and "gzip" in request.headers.get("Accept-Encoding", ""):
        body, headers["Content-Encoding"] = gzip.compress(body, 5), "gzip"   # як GZipMiddleware
    return FlaskResponse(body, status, mimetype="application/json", headers=headers)


def _field(default):
    """FastAPI Query(...)/Body(...) -> (значення за замовчуванням, regex або None, чи це Body)."""
    if type(default).__name__ in ("FieldInfo", "QueryInfo", "BodyInfo", "Query", "Body"):
        pattern = getattr(default, "pattern", None)
        for m in getattr(default, "metadata", []) or []:
            pattern = pattern or getattr(m, "pattern", None)
        dflt = getattr(default, "default", None)
        if repr(dflt) in ("Ellipsis", "PydanticUndefined"):
            dflt = inspect.Parameter.empty
        return dflt, pattern, type(default).__name__ in ("BodyInfo", "Body")
    return default, None, False


def _convert(name, raw, kind):
    if kind is None or raw is None:
        return raw
    args = typing.get_args(kind)
    if args:                                         # int | None, Optional[float] ...
        kind = next((a for a in args if a is not type(None)), str)
    try:
        if kind is bool:
            low = raw.lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off"):
                return False
            raise ValueError
        if kind in (int, float):
            v = kind(raw)
            if kind is float and not math.isfinite(v):
                raise ValueError
            return v
        if kind in (dict, list):
            return json.loads(raw)
        return raw
    except (ValueError, TypeError) as exc:
        raise _BadParam(f"parameter '{name}' must be {getattr(kind, '__name__', kind)}, got '{raw}'") from exc


def _call(fn, path_args):
    hints = typing.get_type_hints(fn)
    kwargs = {}
    for name, p in inspect.signature(fn).parameters.items():
        default, pattern, is_body = _field(p.default)
        kind = hints.get(name)
        if is_body or (kind is dict and request.method == "POST"):
            body = request.get_json(silent=True)
            if body is None:
                raise _BadParam("expected a JSON body")
            kwargs[name] = body
            continue
        raw = path_args.get(name, request.args.get(name))
        if raw is None:
            if default is inspect.Parameter.empty:
                raise _BadParam(f"missing parameter '{name}'")
            kwargs[name] = default
            continue
        if pattern and not re.fullmatch(pattern, raw):
            raise _BadParam(f"parameter '{name}' must match {pattern}, got '{raw}'")
        kwargs[name] = _convert(name, raw, kind)
    return fn(**kwargs)


def _reply(out):
    if isinstance(out, (dict, list)):
        return _json(out)
    if isinstance(out, tuple) and len(out) == 2 and out[0] == "file":           # заглушка тестів
        return send_file(str(out[1]))
    if hasattr(out, "path") and not hasattr(out, "body"):                     # FileResponse
        resp = send_file(str(out.path), mimetype=getattr(out, "media_type", None),
                         as_attachment=bool(getattr(out, "filename", None)),
                         download_name=getattr(out, "filename", None))
        return resp
    if hasattr(out, "body"):                                                  # Response(bytes, ...)
        headers = {k: v for k, v in dict(getattr(out, "headers", {}) or {}).items()
                   if k.lower() not in ("content-length", "content-type")}
        return FlaskResponse(out.body, getattr(out, "status_code", 200),
                             mimetype=getattr(out, "media_type", None), headers=headers)
    return _json(out)


# -------------------------------------------------------------- routing --
def _routes():
    """(метод, шлях, функція) з api.app — працює і з FastAPI, і з тестовою заглушкою."""
    routes = api.app.routes
    if isinstance(routes, dict):
        for key, fn in routes.items():
            method, path = key.split(" ", 1) if key.startswith("POST ") else ("GET", key)
            yield method, path, fn
        return
    for r in routes:
        fn, path = getattr(r, "endpoint", None), getattr(r, "path", "")
        if fn is None or not path.startswith("/api/"):
            continue
        for method in sorted(getattr(r, "methods", None) or {"GET"}):
            if method in ("GET", "POST"):
                yield method, path, fn


def _make_view(fn):
    def view(**path_args):
        try:
            return _reply(_call(fn, path_args))
        except api.HTTPException as exc:
            resp = _json({"detail": exc.detail}, exc.status_code)
            for k, v in (getattr(exc, "headers", None) or {}).items():
                resp.headers[k] = v
            return resp
        except _BadParam as exc:
            return _json({"detail": str(exc)}, 422)
    view.__name__ = f"{fn.__name__}_{id(fn)}"
    return view


for _method, _path, _fn in _routes():
    rule = re.sub(r"\{(\w+)\}", r"<\1>", _path)            # /api/frame/{frame_id}.png -> <frame_id>
    app.add_url_rule(rule, endpoint=f"{_method} {_path}", view_func=_make_view(_fn), methods=[_method])


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.after_request
def _headers(resp):
    resp.headers.setdefault("Access-Control-Allow-Origin", "*")               # як CORSMiddleware
    if not request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"   # старий app.js не змішується з новим index.html
    return resp


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SPHEREx Sky Motion Viewer (Flask)")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = open to other computers in the network")
    ap.add_argument("--port", type=int, default=5050)
    a = ap.parse_args()
    print(f"Open http://{'127.0.0.1' if a.host == '0.0.0.0' else a.host}:{a.port}/ in the browser (Ctrl+C to stop)")
    try:
        try:
            from waitress import serve                 # якщо встановлено: надійніший сервер для Windows
        except ImportError:
            app.run(host=a.host, port=a.port, threaded=True)
        else:
            serve(app, host=a.host, port=a.port, threads=16)
    except KeyboardInterrupt:
        pass
    print("Server stopped.")
