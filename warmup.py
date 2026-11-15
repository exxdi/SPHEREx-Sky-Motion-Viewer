"""
Прогрев кэша перед демо: заранее скачивает и обрабатывает кадры 5 тестовых объектов,
чтобы на показе всё открывалось мгновенно. Сервер для этого запускать не нужно.

    python warmup.py                 # как кнопки Examples (поле 0.1°, их детекторы)
    python warmup.py --det all       # дополнительно все детекторы D1-D6 (дольше, ~1 ГБ)
    python warmup.py --fov 0.25      # другое поле зрения

Можно запускать параллельно с сервером: кэш общий, файлы защищены от одновременной записи.
"""
import argparse
import re
import time
from concurrent.futures import ThreadPoolExecutor

import api

# Ровно те строки, что стоят в кнопках Examples (static/index.html) -> те же ключи кэша.
EXAMPLES = [
    ("Barnard's Star", "Barnard's Star", "D2"),       # name -> position for 2026 (fast proper motion)
    ("Epsilon Indi B", "Epsilon Indi B", "D6"),
    ("WISE 0855-0714", "08h55m10.831s -07d14m42.53s", "D6"),
    ("RW Aurigae", "RW Aur", "D2"),
    ("R Draconis", "R Dra", "D2"),
]


def resolve_like_site(text):
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)[\s,]+([+-]?\d+(?:\.\d+)?)\s*$", text)
    if m:                                    # как app.js: числа не идут на сервер
        return float(m.group(1)), float(m.group(2))
    r = api.resolve(text)
    return r["ra"], r["dec"]


def warm(name, text, det, fov):
    t0 = time.time()
    ra, dec = resolve_like_site(text)
    frames = [f for f in api.search_frames(ra, dec) if det == "all" or f["det"] == det]
    frames = api._interleave_by_epoch(frames)[:api.MAX_FRAMES]
    print(f"{name:16s} {det:>3s}: {len(frames)} frames ...", flush=True)
    done = ok = 0
    with ThreadPoolExecutor(api.WORKERS) as pool:
        for res in pool.map(lambda f: api._safe(api.process_frame, f, ra, dec, fov), frames):
            done += 1
            ok += "error" not in res
            if done % 10 == 0 or done == len(frames):
                print(f"    {done}/{len(frames)} processed, {ok} ok", flush=True)
    try:
        g = api.api_gaia(ra=ra, dec=dec, fov=fov, date=None, gmax=16.0, pm_min=100.0, limit=400)
        gaia = f"Gaia {g['n']} stars via {g['source']}"
    except Exception as exc:                                      # noqa: BLE001
        gaia = f"Gaia failed: {getattr(exc, 'detail', exc)}"
    print(f"    done in {time.time() - t0:.0f} s · {gaia}\n", flush=True)


def main():
    p = argparse.ArgumentParser(description="Pre-download the five challenge targets")
    p.add_argument("--fov", type=float, default=0.1)
    p.add_argument("--det", default=None, help="also warm this detector for every target (e.g. all)")
    a = p.parse_args()
    for name, text, det in EXAMPLES:
        warm(name, text, det, a.fov)
        if a.det and a.det != det:
            warm(name, text, a.det, a.fov)
    print("Cache is warm. Start the server and open the examples.")


if __name__ == "__main__":
    main()
