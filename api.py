"""
SPHEREx Sky Motion Viewer — backend API (FastAPI).

Запуск:
    pip install -r requirements.txt
    uvicorn api:app --reload --port 8000
Документация (Swagger) появится на http://localhost:8000/docs

Поток данных:
    сайт -> /api/search  (TAP IRSA: какие кадры есть в точке, эпохи)
         -> /api/frames  (скачать кутауты, перепроецировать на общую сетку, общая шкала яркости)
         -> /api/frame/{id}.png, /api/coadd.png, /api/diff.png  (картинки для плеера)
         -> /api/spectrum (фотометрия во всех кадрах)
         -> /api/gaia     (звёзды Gaia, сдвинутые на дату снимка)
Ничего не хранится навсегда: всё берётся из IRSA / Gaia Archive и кэшируется на диске.
"""

import hashlib
import io
import json
import math
import os
import random
import shutil
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response

import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.time import Time
from astropy.utils.data import conf as data_conf, download_file
from astropy.visualization import AsinhStretch, ImageNormalize
from astropy.wcs import WCS, FITSFixedWarning

import matplotlib
matplotlib.use("Agg")
from matplotlib import image as mpimg                            # noqa: E402  (без pyplot: потокобезопасно)
from reproject import reproject_interp                           # noqa: E402

import detect                                                     # noqa: E402  (поиск объектов)
import results_package                                            # noqa: E402  (пакет результатов)

from astropy import log as astropy_log                          # noqa: E402
astropy_log.setLevel("WARNING")   # прячем шум "Inconsistent SIP distortion..." (он безвреден)
warnings.filterwarnings("ignore", category=FITSFixedWarning)
warnings.filterwarnings("ignore", module="astropy")
data_conf.remote_timeout = 120

# ------------------------------------------------------------------ config --
CACHE = Path(os.environ.get("CACHE_DIR", Path(__file__).parent / "cache"))   # рядом с api.py
GAIA_TABLE = os.environ.get("GAIA_TABLE", "gaiadr3.gaia_source")   # после 2.12.2026 -> DR4
SEARCH_TTL_S = int(os.environ.get("SEARCH_TTL_S", 12 * 3600))      # релизы IRSA еженедельные
IRSA_BASE = "https://irsa.ipac.caltech.edu/"
IRSA_TAP = "https://irsa.ipac.caltech.edu/TAP"
GAIA_TAP = "https://gea.esac.esa.int/tap-server/tap"
PIX_DEG = 6.15 / 3600            # пиксель SPHEREx ~6.15"
EPOCH_GAP_DAYS = 30              # разрыв > 30 дней = новая эпоха
MAX_FOV, MAX_FRAMES = 0.5, 250
WORKERS = int(os.environ.get("WORKERS", 16))      # потоков обработки (выравнивание, PNG)
IRSA_CONCURRENCY = int(os.environ.get("IRSA_CONCURRENCY", 6))   # одновременных скачиваний из IRSA
RETRY_HTTP = {429, 500, 502, 503, 504}            # «перегружен, попробуйте позже»
GAIA_TTL_S = 30 * 24 * 3600                          # каталог Gaia не меняется
# Какие биты FLAGS считать плохими (0 = ничего не маскировать, только считать).
# Многие биты SPHEREx информационные (например, «в пикселе есть источник»),
# поэтому маскировать «всё ненулевое» нельзя — выбрасывались почти все пиксели.
BAD_BITS = int(os.environ.get("SPHEREX_BAD_BITS", "0"), 0)
CACHE_VER = "v3"                 # смена версии = старый кэш кадров игнорируется
CACHE_MAX_GB = float(os.environ.get("CACHE_MAX_GB", 2.0))   # больше -> удаляем самые старые файлы
MIN_FREE_MB = 300                                          # меньше свободного места -> не качаем
DROP_EXT = {"EPSF", "PSF"}       # модель PSF ~3-4 МБ на кадр, для просмотра не нужна
_STATIC = Path(__file__).parent / "static"
for sub in ("search", "fits", "npy", "png", "gaia"):
    (CACHE / sub).mkdir(parents=True, exist_ok=True)

app = FastAPI(title="SPHEREx Sky Motion Viewer API", version="0.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"],
                   allow_headers=["*"])   # открыто: инструмент можно встраивать на другие сайты
app.add_middleware(GZipMiddleware, minimum_size=1000)   # JSON со списками кадров сжимается в 5-10 раз


@app.middleware("http")
async def no_cache_for_site(request, call_next):
    """HTML/CSS/JS сайта не кэшируем: иначе старый app.js смешивается с новым index.html."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response



# ----------------------------------------------------------------- helpers --
def _key(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:20]


def _check(ra: float, dec: float, fov: float = 0.1):
    if not (0 <= ra < 360 and -90 <= dec <= 90):
        raise HTTPException(400, "ra должен быть 0..360, dec -90..90 (градусы)")
    if not (0.01 <= fov <= MAX_FOV):
        raise HTTPException(400, f"fov должен быть 0.01..{MAX_FOV} градуса")


_locks: dict = {}
_locks_guard = threading.Lock()


def _lock(key) -> threading.Lock:
    """Один замок на файл: два запроса не пишут один и тот же файл кэша одновременно."""
    with _locks_guard:
        return _locks.setdefault(str(key), threading.Lock())


def _atomic_write(path: Path, data: bytes):
    """Пишем во временный файл и подменяем: никто не прочитает недописанный файл."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    except OSError:                    # Windows: файл занят читателем -> оставляем старый
        try:
            os.remove(tmp)
        except OSError:
            pass


def _transient(exc) -> bool:
    """Помилка, яку варто повторити: 429/5xx, «Proxy Error», тайм-аут, обрив з'єднання."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(k in text for k in ("429", "500", "502", "503", "504", "proxy error", "bad gateway",
                                   "service unavailable", "timeout", "timed out", "connection", "temporarily"))


def _retry(fn, *args, tries=3, wait=2.0):
    """Повтор зі зростаючою паузою (2, 4, 6 … с). Постійні помилки (400, синтаксис ADQL) — одразу."""
    for i in range(1, tries + 1):
        try:
            return fn(*args)
        except Exception as exc:                                  # noqa: BLE001
            if i == tries or not _transient(exc):
                raise
            time.sleep(wait * i)


# ------------------------------------------------------------------ search --
def spherex_adql(ra: float, dec: float) -> str:
    """ADQL, которым ищем кадры SPHEREx (тот же текст пишется в пакет данных)."""
    return (f"SELECT a.uri, p.time_bounds_lower, p.energy_bandpassname "
            f"FROM spherex.artifact AS a JOIN spherex.plane AS p ON a.planeid = p.planeid "
            f"WHERE 1 = CONTAINS(POINT('ICRS', {ra}, {dec}), p.poly) ORDER BY p.time_bounds_lower")


def search_cache_info(ra: float, dec: float) -> dict:
    """Когда реально выполнялся поиск в IRSA (время записи кэша) и сколько кадров нашлось."""
    path = CACHE / "search" / f"{_key(round(ra, 4), round(dec, 4))}.json"
    if not path.exists():
        return {"retrieved_utc": None, "result_count": None}
    return {"retrieved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime)),
            "result_count": len(json.loads(path.read_text()))}


IRSA_WAIT_S = float(os.environ.get("IRSA_WAIT_S", 45))   # довше сторінка не чекає; пошук іде далі у фоні
_SEARCH_POOL = ThreadPoolExecutor(4, thread_name_prefix="irsa-search")
_SEARCH_RUNNING: dict = {}                                 # точка -> Future (повторний клік не дублює запит)
_SEARCH_LOCK = threading.Lock()


CAT_WAIT_S = float(os.environ.get("CAT_WAIT_S", 25))     # Gaia / AllWISE: довше пошук не чекає
SKYBOT_BUDGET_S = 30.0                                     # на всі запити SkyBoT одного пошуку
_BG_POOL = ThreadPoolExecutor(8, thread_name_prefix="catalogs")
_BG_RUNNING: dict = {}
_BG_LOCK = threading.Lock()


def _bg(key, fn):
    """Запустити fn у фоні (той самий key — спільний запит, без дублів). Повертає Future."""
    with _BG_LOCK:
        fut = _BG_RUNNING.get(key)
        if fut is None or fut.done():
            fut = _BG_POOL.submit(fn)
            _BG_RUNNING[key] = fut
            fut.add_done_callback(lambda f, k=key: _BG_RUNNING.pop(k, None) if _BG_RUNNING.get(k) is f else None)
    return fut


def _wait(fut, seconds):
    """Результат або виняток. Не встигло — TimeoutError(«ще працює»), а запит іде далі й заповнює кеш."""
    try:
        return fut.result(timeout=max(0.0, seconds))
    except Exception as exc:                                      # noqa: BLE001
        if not fut.done():
            raise TimeoutError("still running") from exc
        raise


def _irsa_search(ra: float, dec: float, path: Path) -> list[dict]:
    """Запит до IRSA TAP (з повторами) -> список кадрів; результат одразу пишеться в кеш."""
    from astroquery.ipac.irsa import Irsa
    adql = spherex_adql(ra, dec)
    tbl = _retry(lambda: Irsa.query_tap(adql).to_table(), tries=4)
    frames, seen = [], set()
    for row in tbl:
        uri = str(row["uri"]).strip()
        url = uri if uri.lower().startswith("http") else IRSA_BASE + uri.lstrip("/")
        band = str(row["energy_bandpassname"])
        if url in seen or not band.startswith("SPHEREx-D"):
            continue
        seen.add(url)
        mjd = float(row["time_bounds_lower"])
        frames.append({"id": _key(url), "url": url, "mjd": mjd,
                       "date": Time(mjd, format="mjd").iso[:19],
                       "det": band.replace("SPHEREx-", "")})
    # эпохи: SPHEREx снимает точку пачкой раз в ~полгода
    epoch, prev = 0, None
    for f in frames:
        if prev is not None and f["mjd"] - prev > EPOCH_GAP_DAYS:
            epoch += 1
        f["epoch"], prev = epoch, f["mjd"]
    _atomic_write(path, json.dumps(frames).encode())
    return frames


def search_frames(ra: float, dec: float, fresh: bool = False) -> list[dict]:
    """Все кадры SPHEREx, покрывающие точку. Кэш на SEARCH_TTL_S.
    fresh=True — спитати IRSA зараз (не частіше разу на хвилину для точки).
    IRSA іноді відповідає хвилинами: сторінка чекає не довше IRSA_WAIT_S, запит при цьому
    не скасовується — доходить у фоні й кладе результат у кеш (наступний клік миттєвий)."""
    path = CACHE / "search" / f"{_key(round(ra, 4), round(dec, 4))}.json"
    age = time.time() - path.stat().st_mtime if path.exists() else None
    if age is not None and (age < 60 or (not fresh and age < SEARCH_TTL_S)):
        return json.loads(path.read_text())

    with _SEARCH_LOCK:
        fut = _SEARCH_RUNNING.get(path.name)
        if fut is None or fut.done():
            fut = _SEARCH_POOL.submit(_irsa_search, ra, dec, path)
            _SEARCH_RUNNING[path.name] = fut
            fut.add_done_callback(lambda f, k=path.name: _SEARCH_RUNNING.pop(k, None) if _SEARCH_RUNNING.get(k) is f else None)
    try:
        return fut.result(timeout=IRSA_WAIT_S)
    except Exception as exc:                                      # noqa: BLE001
        if not fut.done():                    # IRSA ще думає (у Python 3.11+ це той самий TimeoutError)
            if path.exists():
                return json.loads(path.read_text())      # старий список, поки IRSA думає
            raise HTTPException(504, f"NASA's IRSA archive is answering very slowly right now (no reply in "
                                     f"{IRSA_WAIT_S:.0f} s). The search keeps running in the background — "
                                     "press Show the sky again in a minute and it should be instant.") from exc
        if path.exists():                     # IRSA лежить, але цей пошук уже робили: беремо збережений список
            print(f"IRSA search failed ({type(exc).__name__}); using cached frame list from "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(path.stat().st_mtime))} UTC")
            return json.loads(path.read_text())
        code = next((c for c in ("502", "503", "504", "500", "429") if c in str(exc)), None)
        raise HTTPException(503, "NASA's IRSA archive is temporarily unavailable"
                                 f"{f' (HTTP {code} on their side)' if code else ''}; we retried 4 times. "
                                 "Nothing is wrong with your search — try again in a minute.")


def _utc(mjd: float) -> str:
    """MJD -> ISO 8601 UTC (секунди), без astropy."""
    from datetime import datetime, timedelta
    return (datetime(1858, 11, 17) + timedelta(days=float(mjd))).strftime("%Y-%m-%dT%H:%M:%SZ")


def observation_windows(frames: list) -> list:
    """Групи кадрів, знятих підряд (внутрішні «епохи»), назовні — лише як діапазони дат.
    t0/t1 — MJD, t1 не включно (останній кадр + 1 хв), щоб їх можна було прямо передати в t0/t1 API."""
    by = {}
    for f in frames:
        by.setdefault(f["epoch"], []).append(f)
    out = []
    for e in sorted(by):
        ms = [f["mjd"] for f in by[e]]
        dets = {}
        for f in by[e]:
            dets[f["det"]] = dets.get(f["det"], 0) + 1
        out.append({"t0": min(ms), "t1": max(ms) + 1 / 1440, "start_utc": _utc(min(ms)), "end_utc": _utc(max(ms)),
                    "n_frames": len(ms), "by_detector": dets})
    return out


def _window_epochs(ra, dec, t0, t1) -> list:
    """Внутрішні епохи, кадри яких потрапляють у вікно дат."""
    return sorted({f["epoch"] for f in search_frames(ra, dec) if _in_time(f, t0, t1)})


def _frame_by_id(ra, dec, frame_id) -> dict:
    for f in search_frames(ra, dec):
        if f["id"] == frame_id:
            return f
    raise HTTPException(404, "кадр не найден для этой точки")


# ------------------------------------------------------- cutouts & reproject --
# Кадры SPHEREx повёрнуты относительно севера. Квадратный кутаут размера fov после
# поворота «север вверх» не закрывает углы -> чёрные клинья по краям. Берём с запасом √2.
CUTOUT_PAD = 1.45


def _fits_path(url, ra, dec, fov) -> Path:
    return CACHE / "fits" / f"{_key(url, round(ra, 5), round(dec, 5), fov, CUTOUT_PAD)}.fits"


def _cache_files():
    for sub in ("fits", "npy", "png"):
        yield from (p for p in (CACHE / sub).iterdir() if p.is_file())


def _cache_entries():
    """(mtime, size, path) готовых файлов кэша. Файлы, исчезнувшие во время обхода, пропускаем:
    другие потоки в это время дописывают и переименовывают свои .tmp."""
    out = []
    for p in _cache_files():
        if p.name.endswith((".tmp", ".part")):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((st.st_mtime, st.st_size, p))
    return out


def _cache_size() -> int:
    return sum(e[1] for e in _cache_entries())


_last_prune = [0.0]


def prune_cache(force: bool = False):
    """Держим кэш не больше CACHE_MAX_GB: удаляем файлы, к которым дольше всего не обращались.
    Обход диска не чаще раза в минуту (force=True — сразу)."""
    now = time.time()
    if not force and now - _last_prune[0] < 60:
        return
    _last_prune[0] = now
    for p in _cache_files():                       # недописанные файлы старше часа — мусор после сбоя
        if p.name.endswith((".tmp", ".part")):
            try:
                if now - p.stat().st_mtime > 3600:
                    p.unlink()
            except OSError:
                pass
    entries = sorted(_cache_entries(), key=lambda e: e[0])
    total = sum(e[1] for e in entries)
    limit = CACHE_MAX_GB * 1024 ** 3
    removed = False
    for _, size, p in entries:
        if total <= limit:
            break
        try:
            p.unlink()
            total -= size
            removed = True
        except OSError:
            pass
    if removed:
        _load_image.cache_clear()
        _coadd_from.cache_clear()


def _ensure_space():
    if shutil.disk_usage(CACHE).free < MIN_FREE_MB * 1024 ** 2:
        prune_cache(force=True)
        if shutil.disk_usage(CACHE).free < MIN_FREE_MB * 1024 ** 2:
            raise HTTPException(507, f"The disk is almost full (less than {MIN_FREE_MB} MB free). "
                                     "Free some space or delete the 'cache' folder, then try again.")


_IRSA_SEM = threading.BoundedSemaphore(IRSA_CONCURRENCY)


def _download_cutout(url: str) -> str:
    """Не больше IRSA_CONCURRENCY загрузок сразу. На 503/429 (IRSA перегружен) ждём
    2, 4, 8, 16 с (+ случайная добавка, чтобы потоки не стучались одновременно) и пробуем снова."""
    last = None
    for attempt in range(5):
        with _IRSA_SEM:
            try:
                return download_file(url, cache=False, show_progress=False)
            except Exception as exc:                              # noqa: BLE001
                last = exc
                code = getattr(exc, "code", None)
                if code is not None and code not in RETRY_HTTP:
                    raise                                         # 404 и т.п. — повтор не поможет
        time.sleep(min(30, 2 * 2 ** attempt) + random.uniform(0, 1.5))   # ждём вне семафора
    raise last


def fetch_cutout(url: str, ra: float, dec: float, fov: float) -> Path:
    """Кутаут из IRSA. Сохраняем компактно (без модели PSF): ~0.1-0.3 МБ вместо ~4 МБ."""
    path = _fits_path(url, ra, dec, fov)
    if path.exists():
        return path
    with _lock(path):
        if path.exists():                          # пока ждали замок, другой поток уже скачал
            return path
        _ensure_space()
        size = round(fov * CUTOUT_PAD, 4)
        tmp = _download_cutout(f"{url}?center={ra},{dec}d&size={size}")
        part = path.with_name(f"{path.name}.{threading.get_ident()}.part")
        try:
            with fits.open(tmp) as hdul:
                keep = fits.HDUList([h for h in hdul if h.name not in DROP_EXT])
                keep.writeto(part, overwrite=True)
            os.replace(part, path)
        finally:
            for f in (tmp, part):                  # временные файлы не копятся даже при ошибке
                try:
                    os.remove(f)
                except OSError:
                    pass
    return path


def _wavelength_um(hdul, coord) -> float:
    hdr = hdul["IMAGE"].header
    x, y = WCS(hdr).world_to_pixel(coord)
    ww = WCS(hdr, fobj=hdul, key="W")
    ww.sip = None
    lam, _ = ww.all_pix2world(float(x), float(y), 0)
    unit = ww.wcs.cunit[0]
    unit = u.um if unit == u.dimensionless_unscaled else u.Unit(unit)
    return float((float(lam) * unit).to(u.um).value)


def read_frame(path: Path, coord: SkyCoord):
    """IMAGE - ZODI; NaN и пиксели с BAD_BITS заполняются медианой кадра."""
    with fits.open(path) as hdul:
        img = hdul["IMAGE"].data.astype(float)
        if "ZODI" in hdul:
            img = img - hdul["ZODI"].data
        bad = ~np.isfinite(img)
        nflag = 0
        if "FLAGS" in hdul:
            flags = hdul["FLAGS"].data.astype(np.int64)
            nflag = int((flags != 0).sum())
            if BAD_BITS:
                bad |= (flags & BAD_BITS) != 0
        good = ~bad
        img[bad] = float(np.median(img[good])) if good.any() else 0.0
        wcs = WCS(hdul["IMAGE"].header)
        try:
            lam = _wavelength_um(hdul, coord)
        except Exception:                                         # noqa: BLE001
            lam = float("nan")
        version = str(hdul[0].header.get("VERSION", ""))
    return img, wcs, lam, version, nflag


@lru_cache(maxsize=1024)
def _load_image(path_str: str):
    """Кадр в памяти (float32 + WCS): повторные клики по спектру не читают диск."""
    with fits.open(path_str) as hdul:
        img = hdul["IMAGE"].data.astype(np.float32)
        if "ZODI" in hdul:
            img = img - hdul["ZODI"].data.astype(np.float32)
        wcs = WCS(hdul["IMAGE"].header)
    return img, wcs


def target_grid(ra, dec, fov):
    """Общая сетка для всех кадров: TAN, север вверх, восток влево."""
    n = max(8, int(math.ceil(fov / PIX_DEG)))
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [(n + 1) / 2, (n + 1) / 2]
    w.wcs.cdelt = [-PIX_DEG, PIX_DEG]
    return w, (n, n)


def _npy_path(frame_id, ra, dec, fov) -> Path:
    return CACHE / "npy" / f"{_key(frame_id, round(ra, 5), round(dec, 5), fov, CACHE_VER)}.npy"


def process_frame(f: dict, ra, dec, fov) -> dict:
    """Скачать, перепроецировать, сохранить .npy. Возвращает метаданные кадра."""
    npy = _npy_path(f["id"], ra, dec, fov)
    meta_p = npy.with_suffix(".json")
    if npy.exists() and meta_p.exists():
        return json.loads(meta_p.read_text())
    with _lock(npy):
        if npy.exists() and meta_p.exists():
            return json.loads(meta_p.read_text())
        coord = SkyCoord(ra, dec, unit="deg")
        img, wcs, lam, version, nflag = read_frame(fetch_cutout(f["url"], ra, dec, fov), coord)
        out_wcs, shape = target_grid(ra, dec, fov)
        arr, foot = reproject_interp((img, wcs), out_wcs, shape_out=shape)
        arr[foot <= 0] = np.nan                   # вне кадра (край детектора)
        buf = io.BytesIO()
        np.save(buf, arr.astype(np.float32))
        _atomic_write(npy, buf.getvalue())
        meta = {**{k: f[k] for k in ("id", "date", "mjd", "det", "epoch")},
                "lam_um": lam, "version": version, "flagged_px": nflag,
                "coverage": float((foot > 0).mean())}
        center_coverage(meta, ra, dec, fov)       # считаем сразу и кладём в meta
        _atomic_write(meta_p, json.dumps(meta).encode())   # meta последним = кадр готов
    return meta


MIN_COVERAGE = float(os.environ.get("MIN_COVERAGE", 0.7))   # доля поля, закрытая кадром (за замовчуванням)


def _cov(v) -> float:
    """Поріг покриття з налаштувань сайту (0.3…1.0); не задано — значення сервера."""
    return MIN_COVERAGE if v is None else max(0.3, min(float(v), 1.0))
CENTER_BOX = 0.4                 # центральные 40% поля (где объект) должны быть закрыты целиком


def center_coverage(meta: dict, ra, dec, fov) -> float:
    """Какая доля центральной области закрыта кадром. Кадр у края детектора, который
    не накрывает сам объект, даёт в анимации вспышку пустоты — такие не показываем."""
    if "center_cov" in meta:
        return meta["center_cov"]
    arr = np.load(_npy_path(meta["id"], ra, dec, fov))
    n = arr.shape[0]
    a, b = int(n * (0.5 - CENTER_BOX / 2)), int(math.ceil(n * (0.5 + CENTER_BOX / 2)))
    meta["center_cov"] = float(np.isfinite(arr[a:b, a:b]).mean())
    return meta["center_cov"]


def usable(meta: dict, ra, dec, fov, min_coverage: float) -> bool:
    return meta["coverage"] >= min_coverage and center_coverage(meta, ra, dec, fov) >= 0.99


def load_npy(frame_id, ra, dec, fov) -> np.ndarray:
    npy = _npy_path(frame_id, ra, dec, fov)
    if not npy.exists():
        raise HTTPException(409, "сначала вызовите /api/frames для этой точки")
    return np.load(npy)


def _robust(arr):
    """Нормировка кадра: фон -> 0, яркие -> ~1 (чтобы складывать разные λ)."""
    med = np.nanmedian(arr)
    top = np.nanpercentile(arr, 99)
    return (arr - med) / (top - med if top > med else 1.0)


PNG_VER = "p2"                   # p2: пікселі без даних прозорі (раніше — чорні)


def _png_bytes(arr, vmin, vmax, stretch=0.3, cmap="gray") -> bytes:
    """Кадр -> PNG. Де даних немає (край детектора, кадр не покриває ділянку) — прозоро,
    щоб на сайті це було видно як «немає даних», а не як чорне небо."""
    norm = ImageNormalize(vmin=vmin, vmax=vmax, stretch=AsinhStretch(a=stretch), clip=True)
    nodata = ~np.isfinite(np.asarray(arr, dtype=float))
    img = np.clip(np.ma.masked_invalid(norm(np.nan_to_num(arr, nan=vmin))).filled(0), 0, 1)
    rgba = matplotlib.colormaps[cmap](img)
    rgba[..., 3] = np.where(nodata, 0.0, 1.0)
    buf = io.BytesIO()
    mpimg.imsave(buf, rgba, origin="lower", format="png")
    return buf.getvalue()


def _png_response(key: str, make) -> Response:
    """PNG с дисковым кэшем: картинка рендерится один раз, дальше отдаётся файлом."""
    path = CACHE / "png" / f"{key}-{PNG_VER}.png"
    if path.exists():
        data = path.read_bytes()
    else:
        data = make()
        _atomic_write(path, data)
    return Response(data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800, immutable"})


# ---------------------------------------------------------------- endpoints --
# Тестовые объекты задания (J2000). Запасной вариант, если SIMBAD недоступен.
# (RA, Dec at J2000, pmRA·cosDec, pmDec in mas/yr or None). Відповідь миттєва, без SIMBAD.
_BARNARD = (269.452083, 4.693389, -801.551, 10362.394)          # Gaia DR3 proper motion
_EPS_IND_B = (331.043833, -56.782694, 3966.661, -2536.192)      # рух системи ε Indi (Gaia DR3, A)
KNOWN_TARGETS = {
    "barnard's star": _BARNARD, "barnards star": _BARNARD,
    "epsilon indi b": _EPS_IND_B, "eps ind b": _EPS_IND_B,
    "wise 0855-0714": (133.795129, -7.245147, None, None), "wise 0855": (133.795129, -7.245147, None, None),
    "rw aurigae": (76.956417, 30.401417, None, None), "rw aur": (76.956417, 30.401417, None, None),
    "r draconis": (248.1675, 66.755, None, None), "r dra": (248.1675, 66.755, None, None),
}
# Зорі з великим власним рухом за 25 років зсуваються на хвилини дуги (Barnard: ~4′ на північ),
# тож назву переводимо в положення на середину даних SPHEREx, щоб об'єкт був у центрі поля.
TARGET_EPOCH = float(os.environ.get("TARGET_EPOCH", 2026.0))
RESOLVE_WAIT_S = 15.0
_RESOLVE_POOL = ThreadPoolExecutor(2, thread_name_prefix="resolve")


def _move_to_epoch(ra, dec, pmra, pmdec, years):
    if pmra is None or pmdec is None:
        return ra, dec
    dec2 = dec + pmdec * years / 3.6e6
    ra2 = (ra + pmra * years / 3.6e6 / math.cos(math.radians(dec))) % 360
    return ra2, dec2


def _simbad_lookup(name: str) -> tuple:
    """SIMBAD: (ra, dec J2000, pmra, pmdec) — рух може бути None."""
    from astroquery.simbad import Simbad
    sim = Simbad()
    try:
        sim.add_votable_fields("pmra", "pmdec")
    except Exception:                                             # noqa: BLE001
        pass
    t = sim.query_object(name)
    if t is None or len(t) == 0:
        raise LookupError(name)
    cols = {c.lower(): c for c in t.colnames}
    row = t[0]
    try:
        ra, dec = float(row[cols["ra"]]), float(row[cols["dec"]])           # astroquery >= 0.4.8: градуси
    except (KeyError, ValueError, TypeError):
        c = SkyCoord(str(row[cols["ra"]]), str(row[cols["dec"]]), unit=(u.hourangle, u.deg))
        ra, dec = c.ra.deg, c.dec.deg
    def pm(key):
        k = cols.get(key)
        if k is None or np.ma.is_masked(row[k]):
            return None
        v = float(row[k])
        return v if np.isfinite(v) else None
    return ra, dec, pm("pmra"), pm("pmdec")


def _normalize(text: str) -> str:
    for bad, good in (("\u2019", "'"), ("\u2018", "'"), ("\u02bc", "'"), ("\u2212", "-"),
                      ("\u2013", "-"), ("\u2014", "-"), ("\u00a0", " ")):
        text = text.replace(bad, good)
    return " ".join(text.split())


@app.get("/api/resolve")
def resolve(name: str):
    """Имя объекта или строка координат -> RA/Dec в градусах."""
    name = _normalize(name)
    if not name:
        raise HTTPException(400, "пустой запрос")
    c = None
    if any(ch.isdigit() for ch in name) and any(ch in name for ch in "hd:°"):
        try:                                       # 08h55m10.8s -07d14m42s / 08:55:10 -07:14:42
            c = SkyCoord(name, unit=(u.hourangle, u.deg))
        except Exception:                                         # noqa: BLE001
            c = None
    if c is not None:
        return {"name": name, "ra": c.ra.deg, "dec": c.dec.deg, "source": "coordinates"}

    years = TARGET_EPOCH - 2000.0
    def answer(ra0, dec0, pmra, pmdec, source):
        ra1, dec1 = _move_to_epoch(ra0, dec0, pmra, pmdec, years)
        moved = pmra is not None and pmdec is not None
        return {"name": name, "ra": ra1, "dec": dec1, "source": source,
                "ra_j2000": ra0, "dec_j2000": dec0, "pm_mas_yr": [pmra, pmdec] if moved else None,
                "epoch": TARGET_EPOCH if moved else 2000.0}

    known = KNOWN_TARGETS.get(name.lower())
    if known:                                                     # тестові об'єкти: миттєво
        return answer(*known, "built-in")
    cache_p = CACHE / "resolve" / f"{_key(name.lower())}.json"
    if cache_p.exists() and time.time() - cache_p.stat().st_mtime < 30 * 86400:
        return answer(*json.loads(cache_p.read_text()), "SIMBAD (cached)")
    errors = []
    for label, fn in (("SIMBAD", lambda: _simbad_lookup(name)),
                      ("Sesame", lambda: (lambda c_: (c_.ra.deg, c_.dec.deg, None, None))(SkyCoord.from_name(name)))):
        fut = _RESOLVE_POOL.submit(fn)
        try:
            found = fut.result(timeout=RESOLVE_WAIT_S)
        except Exception as exc:                                  # noqa: BLE001
            errors.append(f"{label}: {'no answer in 15 s' if not fut.done() else type(exc).__name__}")
            continue
        _atomic_write(cache_p, json.dumps(list(found)).encode())
        return answer(*found, label)
    raise HTTPException(404, f"Object not found: '{name}'. Check the spelling or enter "
                             f"coordinates like 269.452 4.693 ({'; '.join(errors)})")


@app.get("/api/search")
def api_search(ra: float, dec: float, fresh: bool = False):
    """Список кадров и эпох (быстро, без скачивания картинок). fresh=1 — перевірити IRSA зараз."""
    _check(ra, dec)
    frames = search_frames(ra, dec, fresh=True) if fresh else search_frames(ra, dec)
    info = search_cache_info(ra, dec)
    path = CACHE / "search" / f"{_key(round(ra, 4), round(dec, 4))}.json"
    stale = path.exists() and time.time() - path.stat().st_mtime >= SEARCH_TTL_S
    return {"ra": ra, "dec": dec, "n_frames": len(frames),
            "stale": stale, "retrieved_utc": info["retrieved_utc"],
            "epochs": sorted({f["epoch"] for f in frames}),                  # внутрішнє, для сумісності
            "windows": observation_windows(frames),                           # для інтерфейсу: лише дати
            "frames": [{k: v for k, v in f.items() if k != "url"} for f in frames]}


@app.get("/api/frames")
def api_frames(ra: float, dec: float, fov: float = 0.1,
               det: str = Query("all", pattern="^(all|D[1-6])$"),
               epoch: int | None = None, min_coverage: float = MIN_COVERAGE,
               t0: float | None = None, t1: float | None = None):
    """Готовит кадры для плеера: скачивание + перепроекция + общая шкала яркости.
    epoch=N — только одна эпоха: сайт грузит эпохи по очереди и показывает первую сразу."""
    _check(ra, dec, fov)
    prune_cache()
    _ensure_space()
    frames = [f for f in search_frames(ra, dec)
              if (det == "all" or f["det"] == det) and (epoch is None or f["epoch"] == epoch)
              and _in_time(f, t0, t1)]
    frames = frames[:MAX_FRAMES]
    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(lambda f: _safe(process_frame, f, ra, dec, fov), frames))
    errors = [r["error"] for r in results if "error" in r]
    done = [r for r in results if "error" not in r]
    ok = [m for m in done if usable(m, ra, dec, fov, min_coverage)]   # без обрезков у края
    ok.sort(key=lambda m: m["mjd"])

    # общая шкала яркости по каждому детектору -> кадры не «мигают» от автоконтраста
    scale = {}
    for d in sorted({m["det"] for m in ok}):
        stack = np.concatenate([load_npy(m["id"], ra, dec, fov).ravel()
                                for m in ok if m["det"] == d])
        stack = stack[np.isfinite(stack)]
        if stack.size:
            scale[d] = [float(np.percentile(stack, 1)), float(np.percentile(stack, 99.5))]
    for m in ok:
        m["png"] = f"/api/frame/{m['id']}.png?ra={ra}&dec={dec}&fov={fov}"
    return {"ra": ra, "dec": dec, "fov": fov, "det": det,
            "data_as_of": time.strftime("%Y-%m-%d"),
            "n_requested": len(frames), "n_ok": len(ok),
            "n_failed": len(errors), "n_low_coverage": len(done) - len(ok),
            "errors": sorted(set(errors))[:5], "scale": scale, "frames": ok}


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:                                      # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        print(f"frame failed: {msg}")
        return {"error": msg[:300]}


# ------------------------------------------------------- progressive loading --
# Сайт не ждёт всю эпоху: сервер качает кадры в фоне, сайт раз в секунду забирает готовые.
# Эпохи чередуются (1,2,3,1,2,3...), поэтому движение видно уже по первым кадрам.
_POOL = ThreadPoolExecutor(WORKERS, thread_name_prefix="irsa")   # общий: ограничивает нагрузку на IRSA
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
SCALE_MIN_FRAMES = 4             # шкала яркости детектора фиксируется после 4 готовых кадров
JOB_TTL_S = 3600
JOB_ABANDON_S = 45               # сторінка не питала статус 45 с (закрита / новий пошук) -> зупиняємо


def _interleave_by_epoch(frames):
    queues = {}
    for f in frames:
        queues.setdefault(f["epoch"], []).append(f)
    lists, out = [queues[e] for e in sorted(queues)], []
    while any(lists):
        for q in lists:
            if q:
                out.append(q.pop(0))
    return out


def _freeze_scales(job):
    """Шкала яркости на детектор: считается один раз и дальше не меняется (кадры не мигают)."""
    for d in sorted({m["det"] for m in job["ok"]} - set(job["scale"])):
        ms = [m for m in job["ok"] if m["det"] == d]
        if len(ms) < SCALE_MIN_FRAMES and not job["finished"]:
            continue
        stack = np.concatenate([np.load(_npy_path(m["id"], job["ra"], job["dec"], job["fov"])).ravel()
                                for m in ms])
        stack = stack[np.isfinite(stack)]
        if stack.size:
            job["scale"][d] = [float(np.percentile(stack, 1)), float(np.percentile(stack, 99.5))]


def _job_task(job, f):
    if not job["cancelled"] and time.time() - job["polled"] > JOB_ABANDON_S:
        job["cancelled"] = True                  # ніхто не чекає на ці кадри
    if job["cancelled"]:
        with _JOBS_LOCK:
            job["done"] += 1
            job["finished"] = job["done"] >= job["total"]
        return
    res = _safe(process_frame, f, job["ra"], job["dec"], job["fov"])
    with _JOBS_LOCK:
        job["done"] += 1
        if "error" in res:
            job["errors"].append(res["error"])
        elif not usable(res, job["ra"], job["dec"], job["fov"], job["min_coverage"]):
            job["low"] += 1                       # кадр у края детектора: объект не накрыт
        else:
            res = dict(res)
            res["png"] = f"/api/frame/{res['id']}.png?ra={job['ra']}&dec={job['dec']}&fov={job['fov']}"
            job["ok"].append(res)
        job["finished"] = job["done"] >= job["total"]
        try:
            _freeze_scales(job)
        except Exception as exc:                                  # noqa: BLE001
            print(f"scale failed: {exc}")


@app.get("/api/frames/start")
def frames_start(ra: float, dec: float, fov: float = 0.1,
                 det: str = Query("all", pattern="^(all|D[1-6])$"),
                 min_coverage: float = MIN_COVERAGE, t0: float | None = None, t1: float | None = None,
                 client: str | None = None):
    """Запустить фоновую загрузку кадров (только в окне дат t0..t1, MJD). Ответ сразу: id задачи.
    client — id сторінки: новий пошук з тієї ж сторінки зупиняє її попереднє завантаження."""
    _check(ra, dec, fov)
    if client:
        _cancel_jobs(client=client)
    prune_cache()
    _ensure_space()
    frames = [f for f in search_frames(ra, dec)
              if (det == "all" or f["det"] == det) and _in_time(f, t0, t1)]
    frames = _interleave_by_epoch(frames)[:MAX_FRAMES]
    job_id = _key("job", ra, dec, fov, det, time.time(), threading.get_ident())
    job = {"ra": ra, "dec": dec, "fov": fov, "det": det, "min_coverage": min_coverage,
           "total": len(frames), "done": 0, "ok": [], "errors": [], "low": 0, "scale": {},
           "finished": not frames, "cancelled": False, "created": time.time(), "futures": [],
           "client": client, "polled": time.time()}
    with _JOBS_LOCK:
        for old in [k for k, v in _JOBS.items() if time.time() - v["created"] > JOB_TTL_S]:
            _JOBS.pop(old, None)
        _JOBS[job_id] = job
    job["futures"] = [_POOL.submit(_job_task, job, f) for f in frames]
    return {"job": job_id, "total": len(frames)}


@app.get("/api/frames/status")
def frames_status(job: str, since: int = 0):
    """Готовые кадры начиная с номера since + прогресс. Сайт опрашивает раз в ~1 с."""
    with _JOBS_LOCK:
        j = _JOBS.get(job)
        if j is None:
            raise HTTPException(404, "loading job not found (server restarted?) — search again")
        j["polled"] = time.time()
        return {"cancelled": j["cancelled"],"total": j["total"], "done": j["done"], "ready": len(j["ok"]),
                "frames": [dict(m) for m in j["ok"][since:]], "next": len(j["ok"]),
                "scale": dict(j["scale"]), "finished": j["finished"],
                "n_failed": len(j["errors"]), "n_low_coverage": j["low"],
                "errors": sorted(set(j["errors"]))[:5], "data_as_of": time.strftime("%Y-%m-%d")}


def _cancel_jobs(job: str | None = None, client: str | None = None) -> int:
    with _JOBS_LOCK:
        hit = [j for k, j in _JOBS.items()
               if not j["cancelled"] and ((job and k == job) or (client and j.get("client") == client))]
        for j in hit:
            j["cancelled"] = True
    for j in hit:
        for fu in j["futures"]:
            fu.cancel()                          # ще не початі кадри не качаються взагалі
    return len(hit)


@app.get("/api/frames/cancel")
def frames_cancel(job: str | None = None, client: str | None = None):
    """Новий пошук зупиняє старе завантаження: за id задачі або всі задачі цієї сторінки (client)."""
    return {"ok": True, "cancelled": _cancel_jobs(job or None, client or None)}


@app.get("/api/frame/{frame_id}.png")
def api_frame_png(frame_id: str, ra: float, dec: float, fov: float = 0.1,
                  vmin: float | None = None, vmax: float | None = None,
                  stretch: float = 0.3, cmap: str = "gray", smooth: bool = False):
    key = _key("frame", frame_id, round(ra, 5), round(dec, 5), fov, vmin, vmax,
               stretch, cmap, smooth, CACHE_VER)

    def make():
        arr = load_npy(frame_id, ra, dec, fov)
        if smooth:                                 # подавление шума фильтром размера звезды
            arr = detect.smooth(arr, 0.8)
        lo, hi = (vmin, vmax) if vmin is not None and vmax is not None \
            else np.nanpercentile(arr, [1, 99.5])
        return _png_bytes(arr, lo, hi, stretch, cmap)
    return _png_response(key, make)


def _in_time(f, t0, t1) -> bool:
    """Окно дат пользователя (MJD, t1 не включительно)."""
    return (t0 is None or f["mjd"] >= t0) and (t1 is None or f["mjd"] < t1)


def _coadd_paths(ra, dec, fov, det, epoch=None, lam_min=None, lam_max=None,
                 required: bool = True, t0=None, t1=None, min_cov=None) -> tuple:
    """Готовые хорошие кадры эпохи (не с края детектора), при желании — только в окне λ.
    Кортеж путей = ключ кэша: появились новые кадры -> новый ключ."""
    paths = []
    for f in search_frames(ra, dec):
        if (epoch is not None and f["epoch"] != epoch) or (det != "all" and f["det"] != det) \
                or not _in_time(f, t0, t1):
            continue
        npy = _npy_path(f["id"], ra, dec, fov)
        meta_p = npy.with_suffix(".json")
        if not (npy.exists() and meta_p.exists()):
            continue
        try:
            meta = json.loads(meta_p.read_text())
            if not usable(meta, ra, dec, fov, _cov(min_cov)):
                continue
        except (OSError, ValueError, KeyError):
            continue
        lam = meta.get("lam_um")
        if lam_min is not None or lam_max is not None:
            if lam is None or not np.isfinite(lam):
                continue
            if (lam_min is not None and lam < lam_min) or (lam_max is not None and lam > lam_max):
                continue
        paths.append(str(npy))
    if not paths and required:
        raise HTTPException(404, "no processed frames for these dates / detector / wavelength range yet")
    return tuple(paths)


@lru_cache(maxsize=128)
def _coadd_from(paths: tuple) -> np.ndarray:
    return np.nanmedian(np.stack([_robust(np.load(p)) for p in paths]), axis=0)


def _coadd(ra, dec, fov, det, epoch) -> np.ndarray:
    return _coadd_from(_coadd_paths(ra, dec, fov, det, epoch))


@app.get("/api/coadd.png")
def api_coadd(ra: float, dec: float, fov: float = 0.1, epoch: int | None = None,
              det: str = Query("all", pattern="^(all|D[1-6])$"),
              stretch: float = 0.3, cmap: str = "gray",
              lam_min: float | None = None, lam_max: float | None = None, smooth: bool = False,
              t0: float | None = None, t1: float | None = None, min_cov: float | None = None):
    """Медиана кадрів за діапазоном дат t0..t1 (MJD; або внутрішньої epoch), детектором і вікном λ."""
    paths = _coadd_paths(ra, dec, fov, det, epoch, lam_min, lam_max, t0=t0, t1=t1, min_cov=min_cov)
    key = _key("coadd", paths, stretch, cmap, smooth)

    def make():
        arr = _coadd_from(paths)
        return _png_bytes(detect.smooth(arr, 0.8) if smooth else arr, -0.2, 1.5, stretch, cmap)
    return _png_response(key, make)


@app.get("/api/diff.png")
def api_diff(ra: float, dec: float, fov: float = 0.1, a: int | None = None, b: int | None = None,
             det: str = Query("all", pattern="^(all|D[1-6])$"),
             lam_min: float | None = None, lam_max: float | None = None,
             t0: float | None = None, t1: float | None = None,
             a_t0: float | None = None, a_t1: float | None = None,
             b_t0: float | None = None, b_t1: float | None = None, min_cov: float | None = None):
    """Пізніші дати (b_t0..b_t1) мінус ранні (a_t0..a_t1): нерухоме зникає, рухоме = пара темна/світла.
    a/b — старий спосіб (номери внутрішніх епох)."""
    wa, wb = _two_windows(a, b, t0, t1, a_t0, a_t1, b_t0, b_t1)
    pa = _coadd_paths(ra, dec, fov, det, wa[0], lam_min, lam_max, t0=wa[1], t1=wa[2], min_cov=min_cov)
    pb = _coadd_paths(ra, dec, fov, det, wb[0], lam_min, lam_max, t0=wb[1], t1=wb[2], min_cov=min_cov)

    def make():
        d = _coadd_from(pb) - _coadd_from(pa)
        lim = float(np.nanpercentile(np.abs(d), 99.5)) or 1.0
        return _png_bytes(d, -lim, lim, stretch=1.0, cmap="gray")
    return _png_response(_key("diff", pa, pb), make)


def _two_windows(a, b, t0, t1, a_t0, a_t1, b_t0, b_t1) -> tuple:
    """((epoch|None, t0, t1), (epoch|None, t0, t1)) — два порівнювані діапазони дат."""
    if None not in (a_t0, a_t1, b_t0, b_t1):
        if not (a_t0 < a_t1 and b_t0 < b_t1):
            raise HTTPException(400, "each date range must start before it ends")
        return (None, a_t0, a_t1), (None, b_t0, b_t1)
    if a is None or b is None:
        raise HTTPException(400, "give two date ranges: a_t0, a_t1 (earlier) and b_t0, b_t1 (later)")
    return (a, t0, t1), (b, t0, t1)


def _band(text: str) -> tuple:
    try:
        lo, hi = (float(v) for v in text.split(","))
    except ValueError:
        raise HTTPException(400, f"band must look like 0.75,1.6 (got '{text}')")
    if not (0.5 <= lo < hi <= 5.5):
        raise HTTPException(400, f"band {text} outside 0.5-5.5 µm")
    return lo, hi


@app.get("/api/rgb.png")
def api_rgb(ra: float, dec: float, fov: float = 0.1, epoch: int | None = None,
            b: str = "0.75,1.6", g: str = "1.6,3.2", r: str = "3.2,5.0", stretch: float = 0.3,
            t0: float | None = None, t1: float | None = None, min_cov: float | None = None):
    """Цветная картинка из трёх ИК-диапазонов одной эпохи (синий — короткие λ, красный —
    длинные). Горячие звёзды голубые, холодные коричневые карлики и пыль — красные."""
    bands = [_band(r), _band(g), _band(b)]                    # порядок каналов: R, G, B
    chans = [_coadd_paths(ra, dec, fov, "all", epoch, lo, hi, required=False, t0=t0, t1=t1, min_cov=min_cov)
             for lo, hi in bands]
    if not any(chans):
        raise HTTPException(404, "no processed frames in these wavelength ranges yet "
                                 "(Color needs detector All)")

    def make():
        norm = ImageNormalize(vmin=-0.05, vmax=1.5, stretch=AsinhStretch(a=stretch), clip=True)
        planes = []
        for paths in chans:
            if paths:
                planes.append(np.ma.filled(np.ma.masked_invalid(norm(np.nan_to_num(
                    _coadd_from(paths), nan=-0.05))), 0.0))
            else:
                planes.append(None)
        shape = next(p.shape for p in planes if p is not None)
        rgb = np.dstack([p if p is not None else np.zeros(shape) for p in planes])
        buf = io.BytesIO()
        mpimg.imsave(buf, np.clip(rgb, 0, 1), origin="lower", format="png")
        return buf.getvalue()
    return _png_response(_key("rgb", chans, stretch), make)


# ------------------------------------------------------- automatic detection --
def _usable_frames(ra, dec, fov, det, lam_min=None, lam_max=None, t0=None, t1=None, min_cov=None) -> dict:
    """{эпоха: [{"meta", "path"}]} — готовые хорошие кадры (без краёв детектора)."""
    frames = search_frames(ra, dec)
    out = {}
    for e in sorted({f["epoch"] for f in frames}):
        good = set(_coadd_paths(ra, dec, fov, det, e, lam_min, lam_max, required=False, t0=t0, t1=t1,
                                min_cov=min_cov))
        for f in frames:
            npy = str(_npy_path(f["id"], ra, dec, fov))
            if f["epoch"] == e and npy in good:
                meta = json.loads(Path(npy).with_suffix(".json").read_text())
                out.setdefault(e, []).append({"meta": meta, "path": npy})
    return out


def _load_epochs(groups: dict) -> dict:
    return {e: [{"meta": g["meta"], "arr": np.load(g["path"])} for g in fs] for e, fs in groups.items()}


@lru_cache(maxsize=16)
def _detect_cached(key_paths: tuple, ra, dec, fov, det, sigma, lam_min, lam_max, t0=None, t1=None,
                   min_cov=None, fast_min=detect.FAST_MIN_DETS) -> dict:
    groups = _usable_frames(ra, dec, fov, det, lam_min, lam_max, t0, t1, min_cov)
    epochs = sorted(groups)
    data = _load_epochs(groups)
    t_mid = {e: float(np.mean([f["meta"]["mjd"] for f in data[e]])) for e in epochs}
    cands, pairs_used = [], []
    for i, a in enumerate(epochs):
        for b in epochs[i + 1:]:
            c, maps = detect.compare_epochs(data[a], data[b], t_mid[a], t_mid[b], sigma)
            if maps is not None:
                pairs_used.append({"a": a, "b": b, "matched_frames": maps["n_pairs"]})
            cands += c
    tracks = detect.merge_tracks(cands)
    for e in epochs:
        tracks += [dict(t, epoch=e) for t in detect.fast_movers(data[e], min_dets=fast_min)]
    return {"epochs": epochs, "t_mid": t_mid, "pairs": pairs_used, "tracks": tracks,
            "n_frames": sum(len(v) for v in data.values())}


APP_VERSION = "1.0"


def _cat_match(track, cat, ra, dec, n, tol_px):
    """Источник каталога, который в те же даты стоит в тех же местах (с учётом его движения)."""
    if not cat or not cat.get("stars"):
        return None
    best = None
    for s_ in cat["stars"]:
        pmra, pmdec = s_.get("pmra") or 0.0, s_.get("pmdec") or 0.0
        ok, dmax = True, 0.0
        for t, x, y in track["points"]:
            dt = 2000 + (t - 51544.5) / 365.25 - cat["epoch_jyear"]
            sra = s_["ra"] + pmra * dt / 3.6e6 / math.cos(math.radians(s_["dec"]))
            sdec = s_["dec"] + pmdec * dt / 3.6e6
            gx, gy = detect.sky_to_pix(sra, sdec, ra, dec, n, PIX_DEG)
            d = math.hypot(gx - x, gy - y)
            if d > tol_px:
                ok = False
                break
            dmax = max(dmax, d)
        if ok and (best is None or dmax < best[0]):
            best = (dmax, s_)
    if not best:
        return None
    return {**best[1], "sep_arcsec": round(best[0] * PIX_DEG * 3600, 2)}   # наибольшее расхождение по треку


_SKYBOT_CACHE: dict = {}


def _skybot(ra_, dec_, mjd, radius_arcmin=1.0) -> list:
    """Известные тела Солнечной системы в точке на момент mjd (IMCCE SkyBoT, геоцентр)."""
    key = (round(ra_, 4), round(dec_, 4), round(mjd, 3))
    if key in _SKYBOT_CACHE:
        return _SKYBOT_CACHE[key]
    from astroquery.imcce import Skybot
    try:
        t = Skybot.cone_search(SkyCoord(ra_, dec_, unit="deg"), radius_arcmin * u.arcmin,
                               Time(mjd + 2400000.5, format="jd"), location="500")
    except RuntimeError as exc:
        if "No solar system object" in str(exc):
            _SKYBOT_CACHE[key] = []
            return []
        raise
    val = lambda v: float(getattr(v, "value", v))               # noqa: E731
    out = []
    for r_ in t:
        num = r_["Number"] if "Number" in t.colnames else None
        num = None if num is None or np.ma.is_masked(num) else int(val(num))
        out.append({"name": str(r_["Name"]), "number": num, "ra": val(r_["RA"]), "dec": val(r_["DEC"]),
                    "v": val(r_["V"]) if "V" in t.colnames else None,
                    "dist_arcsec": val(r_["centerdist"]), "posunc_arcsec": val(r_["posunc"])})
    _SKYBOT_CACHE[key] = out
    return out


def _quality_reasons(t, n, n_epochs, sigma, bright_xy) -> list:
    """Почему кандидат может быть ненадёжным (тогда вердикт — inconclusive)."""
    reasons = []
    pts = t["points"]
    if t["kind"] == "moving" and n_epochs >= 3 and t["pairs"] < 2:
        reasons.append("seen in only one comparison of observation dates")
    if t["kind"] == "fast" and len(pts) < 4:
        reasons.append("only 3 detections")
    if t["snr"] < sigma + 2:
        reasons.append(f"weak signal (SNR {t['snr']})")
    if any(min(x, y, n - 1 - x, n - 1 - y) < 4 for _, x, y in pts):
        reasons.append("near the edge of the field")
    if any(math.hypot(x - bx, y - by) < 3 for _, x, y in pts for bx, by in bright_xy):
        reasons.append("next to a bright star (its residuals can mimic a change)")
    return reasons


@app.get("/api/detect")
def api_detect(ra: float, dec: float, fov: float = 0.1,
               det: str = Query("all", pattern="^(all|D[1-6])$"), sigma: float = 5.0,
               gaia: bool = True, lam_min: float | None = None, lam_max: float | None = None,
               t0: float | None = None, t1: float | None = None, min_cov: float | None = None,
               fast_min: int = detect.FAST_MIN_DETS):
    """Автоматический поиск: движущиеся объекты (между эпохами, на одинаковых λ),
    переменные (ярче/тусклее) и быстрые объекты внутри эпохи (астероиды).
    Каждый кандидат: known (найден в Gaia / AllWISE / SkyBoT), unmatched (нигде нет,
    сигнал чистый) или inconclusive (есть причины сомневаться — перечислены)."""
    _check(ra, dec, fov)
    t_start = time.time()
    sigma = max(3.0, min(sigma, 20.0))
    fast_min = max(3, min(int(fast_min), 8))
    groups = _usable_frames(ra, dec, fov, det, lam_min, lam_max, t0, t1, min_cov)
    if len(groups) < 1 or sum(len(v) for v in groups.values()) < 4:
        raise HTTPException(409, "not enough processed frames in this date / wavelength range — "
                                 "load the target first or widen the range")
    key_paths = tuple(sorted(g["path"] for fs in groups.values() for g in fs))
    if gaia:                       # каталоги качаються паралельно з пошуком, а не після нього
        gaia_fut = _bg(("gaia", round(ra, 4), round(dec, 4), fov),
                       lambda: api_gaia(ra=ra, dec=dec, fov=fov, date=None, gmax=21.0, pm_min=0.0, limit=2000))
        wise_fut = _bg(("wise", round(ra, 4), round(dec, 4), fov),
                       lambda: api_wise(ra=ra, dec=dec, fov=fov, wmax=17.0, limit=2000))
    res = _detect_cached(key_paths, ra, dec, fov, det, sigma, lam_min, lam_max, t0, t1, min_cov, fast_min)
    t_detect = time.time()
    n = target_grid(ra, dec, fov)[1][0]

    catalogs = {"gaia": {"table": GAIA_TABLE, "via": None, "error": None},
                "allwise": {"table": "AllWISE (II/328 / allwise_p3as_psd)", "via": None, "error": None},
                "asteroids": {"service": "IMCCE SkyBoT (geocentric)", "queries": 0, "error": None, "log": []}}
    gaia_cat = wise_cat = None
    slow = (f"no answer in {CAT_WAIT_S:.0f} s — still loading in the background; "
            "press Find again in a minute to include it")
    if gaia:
        try:
            gaia_cat = _wait(gaia_fut, CAT_WAIT_S - (time.time() - t_start))
            catalogs["gaia"].update(via=gaia_cat.get("source"), result_count=len(gaia_cat.get("stars", [])),
                                    retrieved_utc=gaia_cat.get("retrieved_utc"), query=gaia_cat.get("query"))
        except TimeoutError:
            catalogs["gaia"]["error"] = slow
        except HTTPException as exc:
            catalogs["gaia"]["error"] = str(exc.detail)
        except Exception as exc:                                  # noqa: BLE001
            catalogs["gaia"]["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
        try:
            wise_cat = _wait(wise_fut, CAT_WAIT_S - (time.time() - t_start))
            catalogs["allwise"].update(via=wise_cat.get("source"), result_count=len(wise_cat.get("stars", [])),
                                       retrieved_utc=wise_cat.get("retrieved_utc"), query=wise_cat.get("query"))
        except TimeoutError:
            catalogs["allwise"]["error"] = slow
        except HTTPException as exc:
            catalogs["allwise"]["error"] = str(exc.detail)
        except Exception as exc:                                  # noqa: BLE001
            catalogs["allwise"]["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
    bright = []
    if gaia_cat:
        for s_ in gaia_cat["stars"]:
            if s_.get("g") is not None and s_["g"] < 11:
                bright.append(detect.sky_to_pix(s_["ra"], s_["dec"], ra, dec, n, PIX_DEG))

    out = []
    order = {"moving": 0, "fast": 1, "brightening": 2, "fading": 3}
    tracks = sorted(res["tracks"], key=lambda t: (order[t["kind"]], -t["pairs"], -t["snr"]))
    for i, t in enumerate(tracks, 1):
        pts = [{"mjd": p[0], "x": round(p[1], 2), "y": round(p[2], 2),
                **dict(zip(("ra", "dec"), detect.pix_to_sky(p[1], p[2], ra, dec, n, PIX_DEG)))}
               for p in t["points"]]
        item = {"id": i, "kind": t["kind"], "snr": t["snr"], "support": t["pairs"], "points": pts}
        if t["kind"] == "moving" and len(pts) >= 2:
            dt_yr = (pts[-1]["mjd"] - pts[0]["mjd"]) / 365.25
            dx = (pts[-1]["x"] - pts[0]["x"]) * PIX_DEG * 3600
            dy = (pts[-1]["y"] - pts[0]["y"]) * PIX_DEG * 3600
            item["pm_arcsec_yr"] = round(math.hypot(dx, dy) / dt_yr, 2) if dt_yr > 0 else None
            item["pa_deg"] = round(math.degrees(math.atan2(-dx, dy)) % 360, 1)   # от севера к востоку
        if t["kind"] == "fast":
            item["rate_arcsec_hr"] = round(t["rate_px_per_day"] * PIX_DEG * 3600 / 24, 1)
            item["epoch"] = t.get("epoch")                                    # внутрішнє
            item["window"] = {"start_utc": _utc(pts[0]["mjd"]), "end_utc": _utc(pts[-1]["mjd"])}
        if t.get("change_frac") is not None:
            item["change_pct"] = round(100 * t["change_frac"])

        matches = {"gaia": None, "allwise": None, "asteroid": None}
        checked_ok = False
        if t["kind"] == "fast":
            mid = pts[len(pts) // 2]
            if catalogs["asteroids"]["queries"] < 10:
                q = {"candidate": i, "ra_deg": mid["ra"], "dec_deg": mid["dec"], "mjd": mid["mjd"],
                     "radius_arcmin": 1.0, "location": "500 (geocentre)", "max_sep_arcsec": "15 + posunc",
                     "request_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "status": None, "result_count": None, "error": None}
                catalogs["asteroids"]["log"].append(q)
                try:
                    catalogs["asteroids"]["queries"] += 1
                    left = SKYBOT_BUDGET_S - (time.time() - t_detect)
                    found = _wait(_bg(("skybot", round(mid["ra"], 4), round(mid["dec"], 4), round(mid["mjd"], 3)),
                                      lambda m=mid: _skybot(m["ra"], m["dec"], m["mjd"])), min(12.0, left))
                    q.update(status="SUCCESS", result_count=len(found))
                    near = [b for b in found if b["dist_arcsec"] <= 15 + (b["posunc_arcsec"] or 0)]
                    if near:
                        matches["asteroid"] = min(near, key=lambda b: b["dist_arcsec"])
                    checked_ok = True
                except Exception as exc:                                  # noqa: BLE001
                    catalogs["asteroids"]["error"] = ("no answer in time — still loading in the background"
                                                      if isinstance(exc, TimeoutError)
                                                      else f"{type(exc).__name__}: {str(exc)[:150]}")
                    q.update(status="FAILED", error=catalogs["asteroids"]["error"])
        else:
            g = _cat_match(t, gaia_cat, ra, dec, n, 1.5)
            if g:
                matches["gaia"] = {"source_id": g["source_id"], "g": g["g"], "pmra": g["pmra"], "pmdec": g["pmdec"],
                                   "sep_arcsec": g.get("sep_arcsec")}
            w = _cat_match(t, wise_cat, ra, dec, n, 2.0)                    # pm AllWISE менее точны
            if w:
                matches["allwise"] = {"name": w["name"], "w1": w["w1"], "w2": w["w2"],
                                      "pmra": w.get("pmra"), "pmdec": w.get("pmdec"), "sep_arcsec": w.get("sep_arcsec")}
        item["matches"] = matches
        item["gaia"] = matches["gaia"]                                       # совместимость

        reasons = _quality_reasons(t, n, len(res["epochs"]), sigma,
                                   [] if matches["gaia"] else bright)
        if t["kind"] != "fast":
            checked_ok = catalogs["gaia"]["via"] is not None
        if not checked_ok and not any(matches.values()):
            reasons.append("catalog check unavailable")
        if any(matches.values()):
            item["verdict_class"] = "known"
            if matches["asteroid"]:
                b = matches["asteroid"]
                nm = f"({b['number']}) {b['name']}" if b["number"] else b["name"]
                item["verdict"] = f"Known asteroid {nm}" + (f", V={b['v']:.1f}" if b["v"] else "")
            elif t["kind"] in ("brightening", "fading"):
                item["verdict"] = "Known star — brightness changed"
            else:
                item["verdict"] = "Known star (" + ", ".join(k for k in ("gaia", "allwise") if matches[k]).replace(
                    "gaia", "Gaia DR3").replace("allwise", "AllWISE") + ")"
        elif reasons:
            item["verdict_class"] = "inconclusive"
            item["verdict"] = "Inconclusive: " + "; ".join(reasons)
        else:
            item["verdict_class"] = "unmatched"
            item["verdict"] = ("Unmatched — not a known asteroid (SkyBoT)" if t["kind"] == "fast"
                               else "Unmatched — not in Gaia DR3 or AllWISE")
        item["reasons"] = reasons
        out.append(item)

    # provenance: что, откуда и с какими настройками — чтобы результат можно было проверить и повторить
    urls = {f["id"]: f["url"] for f in search_frames(ra, dec)}
    frames = []
    for e, fs in sorted(groups.items()):
        for g_ in fs:
            m = g_["meta"]
            frames.append({"id": m["id"], "epoch": e, "mjd": m["mjd"], "det": m["det"],
                           "lam_um": m.get("lam_um"), "release": m.get("version"), "url": urls.get(m["id"])})
    provenance = {
        "tool": "SPHEREx Sky Motion Viewer", "version": APP_VERSION,
        "run_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": {"ra_deg": ra, "dec_deg": dec, "fov_deg": fov, "detector": det,
                   "grid_px": n, "pixel_arcsec": round(PIX_DEG * 3600, 3)},
        "parameters": {"sigma": sigma, "lam_min_um": lam_min, "lam_max_um": lam_max,
                       "date_from_mjd": t0, "date_to_mjd": t1,
                       "match_dlam_um": detect.MATCH_DLAM, "smooth_sigma_px": detect.SMOOTH_SIGMA_PX,
                       "move_flux_frac": detect.MOVE_FLUX_FRAC, "var_flux_frac": detect.VAR_FLUX_FRAC,
                       "fast_sigma": detect.FAST_SIGMA, "max_pm_arcsec_yr": detect.MAX_PM_ARCSEC_YR,
                       "min_frame_coverage": _cov(min_cov), "fast_min_detections": fast_min},
        "spherex_search": {"service": "IRSA TAP", "endpoint": IRSA_TAP, "adql": spherex_adql(ra, dec),
                           **search_cache_info(ra, dec)},
        "cutouts": {"service": "IRSA SPHEREx cutout service", "size_deg": round(fov * CUTOUT_PAD, 4),
                    "url_pattern": "{frame_url}?center={ra},{dec}d&size={size_deg}"},
        "timing_s": {"detection": round(t_detect - t_start, 3), "total": round(time.time() - t_start, 3)},
        "data": {"archive": "NASA/IPAC Infrared Science Archive (IRSA)",
                 "releases": sorted({f["release"] for f in frames if f["release"]}),
                 "n_frames": len(frames), "frames": frames},
        "epoch_pairs": res["pairs"], "catalogs": catalogs,
    }
    win = {e: (min(f["mjd"] for f in fs), max(f["mjd"] for f in fs)) for e, fs in
           ((e, [g_["meta"] for g_ in v]) for e, v in groups.items())}
    date_pairs = [{"earlier": {"start_utc": _utc(win[p_["a"]][0]), "end_utc": _utc(win[p_["a"]][1])},
                   "later": {"start_utc": _utc(win[p_["b"]][0]), "end_utc": _utc(win[p_["b"]][1])}}
                  for p_ in res["pairs"] if p_.get("a") in win and p_.get("b") in win]
    provenance["date_pairs"] = date_pairs
    return {"n_frames": res["n_frames"], "epochs": res["epochs"], "epoch_pairs": res["pairs"],
            "date_pairs": date_pairs,
            "sigma": sigma, "gaia_checked": gaia_cat is not None, "candidates": out,
            "summary": {c: sum(1 for o in out if o["verdict_class"] == c)
                        for c in ("known", "unmatched", "inconclusive")},
            "provenance": provenance}


def _package_context() -> dict:
    """Дані сервера для пакета: версії бібліотек (у processing_log.json)."""
    import platform
    from importlib import metadata
    libs = {"python": platform.python_version()}
    for name in ("numpy", "scipy", "astropy", "astroquery", "reproject", "matplotlib", "fastapi"):
        try:
            libs[name] = metadata.version(name)
        except Exception:                                         # noqa: BLE001
            libs[name] = None
    return {"library_versions": libs}


def _package_response(res: dict) -> Response:
    data, name = results_package.build_package(res, _package_context())
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.get("/api/detect/package.zip")
def api_detect_package(ra: float, dec: float, fov: float = 0.1,
                       det: str = Query("all", pattern="^(all|D[1-6])$"), sigma: float = 5.0,
                       gaia: bool = True, lam_min: float | None = None, lam_max: float | None = None,
                       t0: float | None = None, t1: float | None = None, min_cov: float | None = None,
                       fast_min: int = detect.FAST_MIN_DETS):
    """Те саме, що /api/detect, але повним пакетом даних SOLAR (Data Contract v1.0) у ZIP."""
    return _package_response(api_detect(ra, dec, fov, det, sigma, gaia, lam_min, lam_max, t0, t1, min_cov, fast_min))


@app.post("/api/package")
def api_package(res: dict = Body(...)):
    """Упакувати вже отриманий результат /api/detect (сайт надсилає те, що показав користувачу)."""
    if not isinstance(res.get("candidates"), list) or not isinstance(res.get("provenance"), dict):
        raise HTTPException(422, "expected the JSON returned by /api/detect")
    if len(json.dumps(res)) > 20_000_000:
        raise HTTPException(413, "result too large")
    try:
        return _package_response(res)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"malformed detection result: {exc}") from exc


@app.get("/api/detect/map.png")
def api_detect_map(ra: float, dec: float, fov: float = 0.1, a: int | None = None, b: int | None = None,
                   det: str = Query("all", pattern="^(all|D[1-6])$"),
                   lam_min: float | None = None, lam_max: float | None = None,
                   t0: float | None = None, t1: float | None = None,
                   a_t0: float | None = None, a_t1: float | None = None,
                   b_t0: float | None = None, b_t1: float | None = None, min_cov: float | None = None):
    """Очищена різниця двох діапазонів дат: лише кадри однакової λ, медіана, PSF-фільтр.
    Сіре = шум, біле = з'явилося, чорне = зникло (шкала ±6σ)."""
    wa, wb = _two_windows(a, b, t0, t1, a_t0, a_t1, b_t0, b_t1)
    sel = {}
    for name, (ep, w0, w1) in (("a", wa), ("b", wb)):
        groups = _usable_frames(ra, dec, fov, det, lam_min, lam_max, w0, w1, min_cov)
        sel[name] = [g for e, fs in groups.items() if ep is None or e == ep for g in fs]
    if not sel["a"] or not sel["b"]:
        raise HTTPException(404, "both date ranges must have loaded frames first")
    key = _key("dmap", tuple(sorted(g["path"] for k in ("a", "b") for g in sel[k])),
               tuple(sorted(g["path"] for g in sel["a"])))

    def make():
        data = _load_epochs({"a": sel["a"], "b": sel["b"]})
        pairs = detect.matched_pairs(data["a"], data["b"])
        if len(pairs) < 2:
            raise HTTPException(404, "too few frames at matching wavelengths in these two date ranges")
        S = detect.significance_maps(pairs)["S"]
        return _png_bytes(S, -6.0, 6.0, stretch=1.0, cmap="gray")
    return _png_response(key, make)


@app.get("/api/spectrum")
def api_spectrum(ra: float, dec: float, fov: float = 0.1,
                 x_ra: float | None = None, x_dec: float | None = None, r_px: float = 2.0):
    """Яркость точки (x_ra, x_dec; по умолчанию центр) во всех кадрах -> спектр."""
    _check(ra, dec, fov)
    pt = SkyCoord(x_ra if x_ra is not None else ra, x_dec if x_dec is not None else dec,
                  unit="deg")
    out = []
    win = int(math.ceil(3.5 * r_px)) + 1
    for f in search_frames(ra, dec):
        path = _fits_path(f["url"], ra, dec, fov)
        meta_p = _npy_path(f["id"], ra, dec, fov).with_suffix(".json")
        if not path.exists() or not meta_p.exists():
            continue                       # спектр строим по уже обработанным кадрам
        try:
            lam = json.loads(meta_p.read_text())["lam_um"]   # λ уже посчитана при обработке
            img, wcs = _load_image(str(path))
            x, y = (float(v) for v in wcs.world_to_pixel(pt))
            x0, y0 = int(round(x)), int(round(y))
            ys, xs = slice(max(0, y0 - win), y0 + win + 1), slice(max(0, x0 - win), x0 + win + 1)
            cut = img[ys, xs]
            if cut.size == 0:
                continue                   # точка вне этого кадра
            yy, xx = np.mgrid[ys, xs]
            rr = np.hypot(xx - x, yy - y)
            ap = cut[rr <= r_px]
            bg = cut[(rr > 2 * r_px) & (rr <= 3.5 * r_px)]
            if np.isfinite(ap).sum() and np.isfinite(bg).sum() and np.isfinite(lam):
                out.append({"id": f["id"], "lam_um": lam,
                            "flux": float(np.nanmean(ap) - np.nanmedian(bg)),
                            "date": f["date"], "mjd": f["mjd"], "det": f["det"],
                            "epoch": f["epoch"]})
        except Exception:                                         # noqa: BLE001
            continue
    out.sort(key=lambda p: p["lam_um"])
    return {"unit": "MJy/sr (средн. в апертуре минус фон)", "points": out}


def _ready_fits(ra, dec, fov, frame_id) -> Path:
    f = _frame_by_id(ra, dec, frame_id)
    path = _fits_path(f["url"], ra, dec, fov)
    if not path.exists():
        raise HTTPException(409, "кадр ещё не скачан: сначала /api/frames")
    return path


@app.get("/api/fits/{frame_id}")
def api_fits(frame_id: str, ra: float, dec: float, fov: float = 0.1):
    """Скачать исходный кутаут (MEF: IMAGE, FLAGS, VARIANCE, ZODI, EPSF, WCS-WAVE)."""
    return FileResponse(_ready_fits(ra, dec, fov, frame_id), media_type="application/fits",
                        filename=f"spherex_{frame_id}.fits")


@app.get("/api/flags/{frame_id}.png")
def api_flags_png(frame_id: str, ra: float, dec: float, fov: float = 0.1, bits: int = 0):
    """Прозрачная маска флагов (красным) на общей сетке. bits=0 -> любой ненулевой флаг."""
    path = _ready_fits(ra, dec, fov, frame_id)

    def make():
        with fits.open(path) as hdul:
            if "FLAGS" not in hdul:
                raise HTTPException(404, "в кадре нет расширения FLAGS")
            flags = hdul["FLAGS"].data.astype(np.int64)
            wcs = WCS(hdul["IMAGE"].header)
        mask = ((flags & bits) != 0) if bits else (flags != 0)
        out_wcs, shape = target_grid(ra, dec, fov)
        m, _ = reproject_interp((mask.astype(float), wcs), out_wcs, shape_out=shape,
                                order="nearest-neighbor")
        rgba = np.zeros(shape + (4,), dtype=float)
        on = np.nan_to_num(m) > 0.5
        rgba[on] = [1.0, 0.33, 0.30, 0.8]
        buf = io.BytesIO()
        mpimg.imsave(buf, rgba, origin="lower", format="png")
        return buf.getvalue()
    return _png_response(_key("flags", frame_id, round(ra, 5), round(dec, 5), fov, bits,
                              CUTOUT_PAD), make)


@app.get("/api/flags/{frame_id}.json")
def api_flags_info(frame_id: str, ra: float, dec: float, fov: float = 0.1):
    """Сколько пикселей с каждым битом + подсказки из заголовка FLAGS (что значат биты)."""
    with fits.open(_ready_fits(ra, dec, fov, frame_id)) as hdul:
        if "FLAGS" not in hdul:
            return {"bits": [], "header_hints": []}
        flags = hdul["FLAGS"].data.astype(np.int64)
        hdr = hdul["FLAGS"].header
    counts = [{"bit": b, "value": 1 << b, "pixels": int(((flags >> b) & 1).sum())}
              for b in range(32)]
    hints = [{"key": k, "value": str(hdr[k]), "comment": hdr.comments[k]}
             for k in dict.fromkeys(hdr.keys()) if k not in ("COMMENT", "HISTORY", "")
             if k.startswith(("MP_", "FLAG", "BIT", "HIERARCH")) or "bit" in hdr.comments[k].lower()]
    return {"total_flagged": int((flags != 0).sum()), "pixels": int(flags.size),
            "bits": [c for c in counts if c["pixels"]], "header_hints": hints[:60]}


def _as_float(t, name):
    """Колонка как float с NaN вместо пустых. Нет колонки (сервис её не вернул) -> вся NaN."""
    try:
        col = t[name]
    except (KeyError, IndexError):
        n = len(next(iter(t.values()))) if isinstance(t, dict) else len(t)
        return np.full(n, np.nan)
    return np.ma.filled(np.ma.asarray(col, dtype=float), np.nan)


def gaia_adql(ra, dec, radius, gmax, pm_min, limit) -> str:
    return (f"SELECT TOP {limit} source_id AS sid, ra, dec, pmra, pmdec, phot_g_mean_mag AS g, parallax AS plx "
            f"FROM {GAIA_TABLE} "
            f"WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra}, {dec}, {radius})) "
            f"AND pmra IS NOT NULL AND (phot_g_mean_mag < {gmax} OR pm > {pm_min}) "
            f"ORDER BY phot_g_mean_mag ASC")


def _gaia_from_esa(ra, dec, radius, gmax, pm_min, limit) -> dict:
    """ESA Gaia Archive, синхронный запрос (не создаёт фоновых заданий: лимит 5 заданий)."""
    from astroquery.gaia import Gaia
    adql = gaia_adql(ra, dec, radius, gmax, pm_min, limit)
    t = _retry(lambda: Gaia.launch_job(adql).get_results(), tries=2)
    t.rename_columns(t.colnames, [c.lower() for c in t.colnames])   # регистр зависит от версии
    return {"sid": np.ma.filled(np.ma.asarray(t["sid"]), 0), "ra": _as_float(t, "ra"),
            "dec": _as_float(t, "dec"), "pmra": _as_float(t, "pmra"),
            "pmdec": _as_float(t, "pmdec"), "g": _as_float(t, "g"), "plx": _as_float(t, "plx")}


def _gaia_from_vizier(ra, dec, radius, gmax, pm_min, limit) -> dict:
    """Запасной путь: тот же Gaia DR3 (I/355/gaiadr3) с зеркала VizieR в CDS. Эпоха 2016.0."""
    from astroquery.vizier import Vizier
    cols = ["Source", "RA_ICRS", "DE_ICRS", "pmRA", "pmDE", "Gmag", "PM", "Plx"]
    center = SkyCoord(ra, dec, unit="deg")
    seen, out = set(), {k: [] for k in ("sid", "ra", "dec", "pmra", "pmdec", "g", "plx")}
    for filt in ({"Gmag": f"<{gmax}"}, {"PM": f">{pm_min}"}):     # яркие + все быстрые
        if gmax <= 0 and "Gmag" in filt:
            continue
        v = Vizier(columns=cols, column_filters=filt, row_limit=limit)
        res = v.query_region(center, radius=radius * u.deg, catalog="I/355/gaiadr3")
        if not res:
            continue
        t = res[0]
        src = np.ma.filled(np.ma.asarray(t["Source"]), 0)
        vals = [_as_float(t, c) for c in ("RA_ICRS", "DE_ICRS", "pmRA", "pmDE", "Gmag", "Plx")]
        for i, sid in enumerate(src):
            if int(sid) in seen:
                continue
            seen.add(int(sid))
            out["sid"].append(int(sid))
            for key, arr in zip(("ra", "dec", "pmra", "pmdec", "g", "plx"), vals):
                out[key].append(arr[i])
    return {k: np.asarray(v, dtype=np.int64 if k == "sid" else float) for k, v in out.items()}


@app.get("/api/gaia")
def route_gaia(ra: float, dec: float, fov: float = 0.1, date: str | None = None,
               gmax: float = 16.0, pm_min: float = 100.0, limit: int = 400):
    """Оверлей Gaia для сайту: чекаємо не довше CAT_WAIT_S, далі запит іде у фоні й кешується."""
    try:
        return _wait(_bg(("gaia-ovl", round(ra, 4), round(dec, 4), fov, date, gmax, pm_min, limit),
                         lambda: api_gaia(ra=ra, dec=dec, fov=fov, date=date, gmax=gmax, pm_min=pm_min, limit=limit)),
                     CAT_WAIT_S)
    except TimeoutError as exc:
        raise HTTPException(504, f"Gaia is answering slowly (no reply in {CAT_WAIT_S:.0f} s). It keeps loading "
                                 "in the background — switch the overlay on again in a minute.") from exc


def api_gaia(ra: float, dec: float, fov: float = 0.1, date: str | None = None,
             gmax: float = 16.0, pm_min: float = 100.0, limit: int = 400):
    """Звёзды Gaia в поле, позиции перенесены с 2016.0 на дату снимка.
    Берём то, что SPHEREx реально видит: яркие (G < gmax) + все быстрые (pm > pm_min мас/год)
    любой яркости. Сортировка по яркости, поэтому важные звёзды не теряются в плотных полях."""
    _check(ra, dec, fov)
    limit = max(10, min(limit, 2000))
    when = Time(date) if date else Time(2016.0, format="jyear")
    cache_p = CACHE / "gaia" / f"{_key(GAIA_TABLE, round(ra, 4), round(dec, 4), fov, when.jyear.round(2), gmax, pm_min, limit, 'v3')}.json"
    if cache_p.exists() and time.time() - cache_p.stat().st_mtime < GAIA_TTL_S:
        return json.loads(cache_p.read_text())
    radius = fov / 2 * math.sqrt(2)
    try:
        cols, source = _gaia_from_esa(ra, dec, radius, gmax, pm_min, limit), "ESA Gaia Archive"
    except Exception as esa_exc:                                  # noqa: BLE001
        print(f"Gaia Archive failed ({type(esa_exc).__name__}: {esa_exc}); trying VizieR mirror")
        try:
            cols, source = _gaia_from_vizier(ra, dec, radius, gmax, pm_min, limit), "VizieR (CDS)"
        except Exception as viz_exc:                              # noqa: BLE001
            raise HTTPException(502, f"Gaia is not responding right now (ESA: {type(esa_exc).__name__}; "
                                     f"VizieR: {type(viz_exc).__name__}). The rest of the site works; "
                                     "try the overlay again in a minute.")

    dt_yr = (when - Time(2016.0, format="jyear")).to_value(u.yr)
    ra0, dec0, pmra, pmdec, g, sids = (cols[k] for k in ("ra", "dec", "pmra", "pmdec", "g", "sid"))
    plx = cols.get("plx", np.full(len(sids), np.nan))
    # линейный перенос по собственному движению (pmra уже содержит cos(dec)), мас/год -> град
    dec1 = dec0 + pmdec * dt_yr / 3.6e6
    ra1 = (ra0 + pmra * dt_yr / 3.6e6 / np.cos(np.radians(dec0))) % 360
    stars = [{"source_id": int(s), "ra": float(a), "dec": float(d), "pmra": float(pa),
              "pmdec": float(pd), "g": float(gg) if np.isfinite(gg) else None,
              "plx": float(pp) if np.isfinite(pp) else None}
             for s, a, d, pa, pd, gg, pp in zip(sids, ra1, dec1, pmra, pmdec, g, plx)
             if np.isfinite(a) and np.isfinite(d) and np.isfinite(pa) and np.isfinite(pd)]
    query = ({"service": "ESA Gaia Archive TAP", "endpoint": GAIA_TAP,
              "adql": gaia_adql(ra, dec, radius, gmax, pm_min, limit)} if source.startswith("ESA") else
             {"service": "CDS VizieR (astroquery.vizier)", "endpoint": "VizieR I/355/gaiadr3",
              "cone": {"ra_deg": ra, "dec_deg": dec, "radius_deg": radius}, "filters": {"Gmag_lt": gmax, "PM_gt": pm_min},
              "row_limit": limit})
    res = {"catalog": GAIA_TABLE, "source": source, "epoch": when.iso[:10],
           "epoch_jyear": float(when.jyear), "n": len(stars), "stars": stars,
           "retrieved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "query": query}
    _atomic_write(cache_p, json.dumps(res).encode())
    return res


# ------------------------------------------------------------------ AllWISE --
WISE_EPOCH = 2010.5              # средняя эпоха позиций AllWISE


def _wise_from_vizier(ra, dec, radius, wmax, limit) -> list:
    from astroquery.vizier import Vizier
    v = Vizier(columns=["AllWISE", "RAJ2000", "DEJ2000", "+W1mag", "W2mag", "pmRA", "pmDE"],
               column_filters={"W1mag": f"<{wmax}"}, row_limit=limit)    # "+" = ярким первыми
    res = v.query_region(SkyCoord(ra, dec, unit="deg"), radius=radius * u.deg,
                         catalog="II/328/allwise")
    if not res:
        return []
    t = res[0]
    names = [str(x) for x in t["AllWISE"]]
    return list(zip(names, *(_as_float(t, c) for c in
                             ("RAJ2000", "DEJ2000", "W1mag", "W2mag", "pmRA", "pmDE"))))


def _wise_from_irsa(ra, dec, radius, wmax, limit) -> list:
    from astroquery.ipac.irsa import Irsa
    t = Irsa.query_region(SkyCoord(ra, dec, unit="deg"), catalog="allwise_p3as_psd",
                          spatial="Cone", radius=radius * u.deg,
                          columns="designation,ra,dec,w1mpro,w2mpro,pmra,pmdec")
    t.rename_columns(t.colnames, [c.lower() for c in t.colnames])
    rows = list(zip([str(x) for x in t["designation"]],
                    *(_as_float(t, c) for c in ("ra", "dec", "w1mpro", "w2mpro", "pmra", "pmdec"))))
    rows = [r_ for r_ in rows if np.isfinite(r_[3]) and r_[3] < wmax]
    return sorted(rows, key=lambda r_: r_[3])[:limit]


@app.get("/api/wise")
def route_wise(ra: float, dec: float, fov: float = 0.1, wmax: float = 15.0, limit: int = 400):
    """Оверлей AllWISE для сайту: чекаємо не довше CAT_WAIT_S, далі запит іде у фоні й кешується."""
    try:
        return _wait(_bg(("wise-ovl", round(ra, 4), round(dec, 4), fov, wmax, limit),
                         lambda: api_wise(ra=ra, dec=dec, fov=fov, wmax=wmax, limit=limit)), CAT_WAIT_S)
    except TimeoutError as exc:
        raise HTTPException(504, f"AllWISE is answering slowly (no reply in {CAT_WAIT_S:.0f} s). It keeps loading "
                                 "in the background — switch the overlay on again in a minute.") from exc


def api_wise(ra: float, dec: float, fov: float = 0.1, wmax: float = 15.0, limit: int = 400):
    """Источники AllWISE в поле: имя, W1/W2, собственное движение. Позиции на эпоху 2010.5,
    сайт сам переносит их на дату кадра (как для Gaia)."""
    _check(ra, dec, fov)
    limit = max(10, min(limit, 2000))
    cache_p = CACHE / "gaia" / f"{_key('allwise', round(ra, 4), round(dec, 4), fov, wmax, limit)}.json"
    if cache_p.exists() and time.time() - cache_p.stat().st_mtime < GAIA_TTL_S:
        return json.loads(cache_p.read_text())
    radius = fov / 2 * math.sqrt(2)
    try:
        rows, source = _wise_from_vizier(ra, dec, radius, wmax, limit), "VizieR (CDS)"
    except Exception as viz_exc:                                  # noqa: BLE001
        print(f"VizieR AllWISE failed ({type(viz_exc).__name__}); trying IRSA")
        try:
            rows, source = _wise_from_irsa(ra, dec, radius, wmax, limit), "IRSA"
        except Exception as irsa_exc:                             # noqa: BLE001
            raise HTTPException(502, f"AllWISE is not responding right now (VizieR: "
                                     f"{type(viz_exc).__name__}; IRSA: {type(irsa_exc).__name__}).")
    clean = lambda v: float(v) if np.isfinite(v) else None       # noqa: E731
    stars = []
    for name, a, d, w1, w2, pa, pd in rows:
        if not (np.isfinite(a) and np.isfinite(d)):
            continue
        name = name.strip()
        stars.append({"name": name if name.startswith("AllWISE") else f"AllWISE {name}",
                      "ra": float(a), "dec": float(d), "w1": clean(w1), "w2": clean(w2),
                      "pmra": clean(pa) or 0.0, "pmdec": clean(pd) or 0.0})
    query = ({"service": "CDS VizieR (astroquery.vizier)", "endpoint": "VizieR II/328/allwise"} if source.startswith("VizieR")
             else {"service": "IRSA Gator (astroquery.ipac.irsa)", "endpoint": "allwise_p3as_psd"})
    query.update({"cone": {"ra_deg": ra, "dec_deg": dec, "radius_deg": radius}, "filters": {"W1mag_lt": wmax},
                  "row_limit": limit})
    res = {"catalog": "AllWISE", "source": source, "epoch_jyear": WISE_EPOCH,
           "n": len(stars), "stars": stars,
           "retrieved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "query": query}
    _atomic_write(cache_p, json.dumps(res).encode())
    return res


@app.get("/api/health")
def health(deep: bool = False):
    """deep=1: живая проверка всех внешних сервисов (SIMBAD, IRSA, Gaia)."""
    usage = shutil.disk_usage(CACHE)
    cache_mb = _cache_size() / 1024 ** 2
    out = {"ok": True, "gaia_table": GAIA_TABLE, "search_ttl_h": SEARCH_TTL_S / 3600,
           "min_coverage_default": MIN_COVERAGE, "fast_min_default": detect.FAST_MIN_DETS,
           "workers": WORKERS, "irsa_concurrency": IRSA_CONCURRENCY,
           "cache_dir": str(CACHE.resolve()), "static_dir_found": _STATIC.is_dir(),
           "cache_mb": round(cache_mb, 1), "cache_limit_gb": CACHE_MAX_GB,
           "disk_free_mb": round(usage.free / 1024 ** 2)}
    if not deep:
        return out
    checks = {}

    def run(name, fn):
        t0 = time.time()
        try:
            checks[name] = {"ok": True, "detail": fn(), "seconds": round(time.time() - t0, 1)}
        except Exception as exc:                                  # noqa: BLE001
            checks[name] = {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
                            "seconds": round(time.time() - t0, 1)}
            out["ok"] = False

    run("simbad", lambda: SkyCoord.from_name("Barnard's Star").to_string())

    def irsa():
        from astroquery.ipac.irsa import Irsa
        t = Irsa.query_tap("SELECT TOP 1 a.uri FROM spherex.artifact AS a").to_table()
        return f"{len(t)} row(s)"
    run("irsa_tap", irsa)

    def gaia():
        from astroquery.gaia import Gaia
        t = Gaia.launch_job(f"SELECT TOP 1 source_id FROM {GAIA_TABLE}").get_results()
        return f"{len(t)} row(s)"
    run("gaia", gaia)
    out["checks"] = checks
    return out


# --------------------------------------------------------------- static site --
# Сайт (static/index.html, style.css, app.js) отдаётся тем же сервером:
# http://127.0.0.1:5050/  -> сайт,  /api/... -> данные,  /docs -> документация API.
# Монтируется ПОСЛЕДНИМ, чтобы не перекрывать маршруты /api.
from fastapi.staticfiles import StaticFiles                       # noqa: E402

if _STATIC.is_dir():
    app.mount("/", StaticFiles(directory=_STATIC, html=True), name="site")


if __name__ == "__main__":
    # Запуск однією командою: python api.py  [--port 5050] [--host 127.0.0.1] [--reload]
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(description="SPHEREx Sky Motion Viewer server")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 = open to other computers in the network")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--reload", action="store_true", help="restart automatically when files change")
    a = ap.parse_args()
    print(f"Open http://{'127.0.0.1' if a.host == '0.0.0.0' else a.host}:{a.port}/ in the browser (Ctrl+C to stop)")
    uvicorn.run("api:app", host=a.host, port=a.port, reload=a.reload)
