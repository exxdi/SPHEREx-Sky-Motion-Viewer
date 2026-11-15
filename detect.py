"""
Автоматический поиск движущихся и переменных объектов в выровненных кадрах SPHEREx.

Вход: кадры на общей сетке (север вверх), у каждого: эпоха, MJD, детектор, длина волны.
Идея:
  1. Сравниваем только кадры с ОДИНАКОВОЙ длиной волны (|Δλ| < 0.03 мкм, тот же детектор):
     иначе разница спектра звезды выглядела бы как «изменение».
  2. Каждая разность «новая эпоха − старая» переводится в единицы шума (σ, через MAD),
     затем медиана по всем парам: космические лучи и горячие пиксели (есть в одном кадре)
     выпадают, шум падает как ~1/√N. Потом сглаживание по размеру звезды (PSF-фильтр).
  3. Пятна > kσ: светлое = «появилось», тёмное = «исчезло».
     Пара «исчезло тут + появилось рядом» похожей силы = движущийся объект.
     Остатки от неподвижных ярких звёзд (чуть разный PSF в эпохах) отсеиваем по доле потока.
     Одиночные пятна = переменные (ярче/тусклее).
  4. Внутри одной эпохи ищем быстрые объекты (астероиды): кадр минус медиана соседних кадров
     той же эпохи; ≥3 обнаружений на прямой с постоянной скоростью = трек.
"""
import math

import numpy as np
from scipy import ndimage as ndi

PIX_ARCSEC = 6.15                # пиксель SPHEREx
MATCH_DLAM = 0.03                # мкм: «та же длина волны»
SMOOTH_SIGMA_PX = 1.0            # PSF-фильтр
MIN_AREA_PX = 2                  # одиночный пиксель после медианы — не объект
EDGE_PX = 2                      # у края данных не ищем (артефакты)
MIN_SEP_PX = 0.7                 # меньше — это остаток PSF неподвижной звезды
MAX_PM_ARCSEC_YR = 30.0          # быстрейшие звёзды ~10″/год; запас для неизвестных
MOVE_FLUX_FRAC = 0.35            # доля потока источника, которая должна «переехать»
VAR_FLUX_FRAC = 0.15             # изменение блеска ≥15% — переменная
FAST_SIGMA = 6.0                 # порог для быстрых объектов внутри эпохи
FAST_MIN_DETS = 5                # мінімум точок у треку (менше — випадкові збіги «гарячих» пікселів)
FAST_TOL_PX = 1.5                # допуск на прямую
FAST_HIT_FRAC = 0.6              # трек видно щонайменше в 60% кадрів, де він мав бути
COMMON_FRAC = 0.8                # піксель рахується, лише якщо його покривають ≥80% кадрів групи
APER_PX = 2.0                    # апертура для движения (соседние положения не перекрываются)
APER_VAR_PX = 3.0                # апертура для переменности: почти весь поток при любом PSF


# ------------------------------------------------------------------- basics --
def mad_sigma(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 10:
        return 1.0
    s = 1.4826 * float(np.median(np.abs(x - np.median(x))))
    return s if s > 0 else (float(np.std(x)) or 1.0)


def smooth(arr: np.ndarray, sigma: float = SMOOTH_SIGMA_PX) -> np.ndarray:
    """Гауссово сглаживание с учётом пустых (NaN) областей: края не «затягивает» нулями."""
    bad = ~np.isfinite(arr)
    num = ndi.gaussian_filter(np.where(bad, 0.0, arr), sigma)
    den = ndi.gaussian_filter((~bad).astype(float), sigma)
    out = num / np.maximum(den, 1e-6)
    out[bad | (den < 0.5)] = np.nan
    return out


def _lam(m) -> float:
    v = m.get("lam_um")
    return float(v) if v is not None and np.isfinite(v) else float("nan")


def matched_pairs(frames_a: list, frames_b: list, dlam: float = MATCH_DLAM) -> list:
    """Каждому кадру эпохи B — кадр эпохи A того же детектора с ближайшей λ (если |Δλ|<dlam)."""
    pairs = []
    for fb in frames_b:
        lb, best = _lam(fb["meta"]), None
        for fa in frames_a:
            if fa["meta"]["det"] != fb["meta"]["det"]:
                continue
            d = abs(_lam(fa["meta"]) - lb)
            if np.isfinite(d) and d <= dlam and (best is None or d < best[0]):
                best = (d, fa)
        if best:
            pairs.append((best[1], fb))
    return pairs


def significance_maps(pairs: list) -> dict:
    """Медиана нормированных разностей + опорные изображения обеих эпох в тех же единицах."""
    ds, as_, bs = [], [], []
    for fa, fb in pairs:
        a = fa["arr"] - np.nanmedian(fa["arr"])
        b = fb["arr"] - np.nanmedian(fb["arr"])
        s = mad_sigma(b - a)
        ds.append((b - a) / s)
        as_.append(a / s)
        bs.append(b / s)
    with np.errstate(all="ignore"):
        D = np.nanmedian(np.stack(ds), axis=0)
        A = np.nanmedian(np.stack(as_), axis=0)
        B = np.nanmedian(np.stack(bs), axis=0)
    # край покриття: де піксель бачать лише кілька пар, медіана шумна -> хибні «зміни»
    seen = np.isfinite(np.stack(ds)).sum(0)
    weak = seen < max(2, math.ceil(0.6 * len(ds)))
    D[weak] = A[weak] = B[weak] = np.nan
    Ds, As, Bs = smooth(D), smooth(A), smooth(B)
    noise = mad_sigma(Ds)
    return {"S": Ds / noise, "D": Ds, "A": As, "B": Bs, "Araw": A, "Braw": B,
            "pix_noise": mad_sigma(B - A), "n_pairs": len(pairs)}


def aperture(img: np.ndarray, x: float, y: float, r: float = APER_PX) -> float:
    """Сумма в кружке радиуса r. Поток звезды почти не зависит от ширины PSF,
    в отличие от пика — поэтому изменения блеска меряем так."""
    n0, n1 = img.shape
    y0, y1 = max(0, int(y - r - 1)), min(n0, int(y + r + 2))
    x0, x1 = max(0, int(x - r - 1)), min(n1, int(x + r + 2))
    yy, xx = np.mgrid[y0:y1, x0:x1]
    m = (xx - x) ** 2 + (yy - y) ** 2 <= r * r
    vals = img[y0:y1, x0:x1][m]
    good = np.isfinite(vals)
    if good.mean() < 0.7:
        return float("nan")
    return float(vals[good].sum() / good.mean())


def centroid(img: np.ndarray, x: float, y: float, r: float = 2.0, iters: int = 3) -> tuple:
    """Центр звезды по изображению самой эпохи. На разности двух перекрывающихся звёзд пики
    «расталкиваются» и скорость выходит завышенной — поэтому уточняем так."""
    n0, n1 = img.shape
    for _ in range(iters):
        y0, y1 = max(0, int(y - r - 1)), min(n0, int(y + r + 2))
        x0, x1 = max(0, int(x - r - 1)), min(n1, int(x + r + 2))
        yy, xx = np.mgrid[y0:y1, x0:x1]
        sub = img[y0:y1, x0:x1]
        m = ((xx - x) ** 2 + (yy - y) ** 2 <= r * r) & np.isfinite(sub)
        if m.sum() < 3:
            break
        w = np.clip(sub[m] - np.median(sub[m]), 0, None)
        if w.sum() <= 0:
            break
        nx, ny = float((xx[m] * w).sum() / w.sum()), float((yy[m] * w).sum() / w.sum())
        if math.hypot(nx - x, ny - y) > r:          # уехали к чужой звезде — оставляем как было
            break
        x, y = nx, ny
    return x, y


def flux_change(maps, blob, r: float = APER_PX) -> dict:
    """Поток в апертуре до/после: изменение, его доля и значимость (в σ)."""
    fa = aperture(maps["Araw"], blob["x"], blob["y"], r)
    fb = aperture(maps["Braw"], blob["x"], blob["y"], r)
    npix = math.pi * r ** 2
    noise = maps["pix_noise"] * math.sqrt(npix)
    delta = fb - fa
    return {"fa": fa, "fb": fb, "delta": delta,
            "sig": abs(delta) / noise if noise > 0 else 0.0,
            "rise": delta / fb if fb > 0 else 1.0,          # доля нового потока здесь
            "drop": -delta / fa if fa > 0 else 1.0}         # доля ушедшего потока


def find_blobs(S: np.ndarray, thr: float, sign: int) -> list:
    """Связные области sign·S > thr: центр (взвешенный), пик, площадь. У края данных — пропуск."""
    X = np.nan_to_num(sign * S, nan=0.0)
    edge = ndi.binary_dilation(~np.isfinite(S), iterations=EDGE_PX)
    lab, n = ndi.label(X > thr)
    out = []
    for i, sl in enumerate(ndi.find_objects(lab), start=1):
        if sl is None:
            continue
        region = lab[sl] == i
        if region.sum() < MIN_AREA_PX:
            continue
        sub = np.where(region, X[sl], 0.0)
        py, px = np.unravel_index(int(np.argmax(sub)), sub.shape)
        py, px = py + sl[0].start, px + sl[1].start
        if edge[py, px]:
            continue
        cy, cx = ndi.center_of_mass(sub)
        out.append({"x": float(cx + sl[1].start), "y": float(cy + sl[0].start),
                    "px": int(px), "py": int(py), "snr": float(X[py, px]), "npix": int(region.sum())})
    return out


# ----------------------------------------------------- between two epochs --
def compare_epochs(frames_a, frames_b, t_a, t_b, sigma=5.0) -> tuple:
    """Кандидаты для пары эпох. t_a, t_b — средние MJD эпох."""
    pairs = matched_pairs(frames_a, frames_b)
    if len(pairs) < 2:
        return [], None
    maps = significance_maps(pairs)
    S = maps["S"]
    dt_yr = (t_b - t_a) / 365.25
    max_sep = max(2.5, MAX_PM_ARCSEC_YR * abs(dt_yr) / PIX_ARCSEC + 1.5)

    pos, neg = [], []
    for b in find_blobs(S, sigma, +1):
        fc = flux_change(maps, b)
        if np.isfinite(fc["delta"]) and fc["delta"] > 0 and fc["sig"] >= sigma:
            pos.append(dict(b, frac=min(1.0, fc["rise"]), fc=fc))
    for b in find_blobs(S, sigma, -1):
        fc = flux_change(maps, b)
        if np.isfinite(fc["delta"]) and fc["delta"] < 0 and fc["sig"] >= sigma:
            neg.append(dict(b, frac=min(1.0, fc["drop"]), fc=fc))
    movers_pos = [p for p in pos if p["frac"] >= MOVE_FLUX_FRAC]
    movers_neg = [n for n in neg if n["frac"] >= MOVE_FLUX_FRAC]

    cands, used_pos, used_neg = [], set(), set()
    for i, p in sorted(enumerate(movers_pos), key=lambda e: -e[1]["snr"]):
        best = None
        for j, n in enumerate(movers_neg):
            if j in used_neg:
                continue
            sep = math.hypot(p["x"] - n["x"], p["y"] - n["y"])
            ratio = p["snr"] / n["snr"]
            if MIN_SEP_PX <= sep <= max_sep and 1 / 3 <= ratio <= 3:
                score = min(p["snr"], n["snr"])
                if best is None or score > best[0]:
                    best = (score, j, sep)
        if best:
            used_neg.add(best[1])
            used_pos.add(id(p))
            n = movers_neg[best[1]]
            xa, ya = centroid(maps["A"], n["x"], n["y"])     # где объект был — по эпохе A
            xb, yb = centroid(maps["B"], p["x"], p["y"])     # где стал — по эпохе B
            cands.append({"kind": "moving", "snr": round(best[0], 1),
                          "points": [(t_a, xa, ya), (t_b, xb, yb)],
                          "sep_px": round(math.hypot(xb - xa, yb - ya), 2)})
    def is_source(b, img):
        """Пятно стоит на самом источнике (локальный максимум), а не на крыле соседней звезды."""
        y, x = b["py"], b["px"]
        near = img[max(0, y - 1):y + 2, max(0, x - 1):x + 2]
        wide = img[max(0, y - 3):y + 4, max(0, x - 3):x + 4]
        return np.isfinite(near).any() and np.nanmax(near) >= np.nanmax(wide) - 1e-9

    def variable(b):
        if not is_source(b, maps["B"] if b in pos else maps["A"]):
            return None
        fc = flux_change(maps, b, APER_VAR_PX)
        if not np.isfinite(fc["delta"]) or fc["sig"] < sigma or fc["fa"] <= 0:
            return None
        return fc if abs(fc["delta"]) / max(fc["fa"], fc["fb"]) >= VAR_FLUX_FRAC else None

    for p in pos:
        if id(p) in used_pos:
            continue
        fcv = variable(p)
        if fcv is None or fcv["delta"] <= 0:
            continue
        p = dict(p, fc=fcv)
        fa = p["fc"]["fa"]
        cands.append({"kind": "brightening", "snr": round(p["snr"], 1),
                      "points": [(t_b, p["x"], p["y"])],
                      "change_frac": round(p["fc"]["delta"] / fa, 2) if fa > 0 else None})
    used_ids = {id(movers_neg[j]) for j in used_neg}
    for n in neg:
        if id(n) in used_ids:
            continue
        fcv = variable(n)
        if fcv is None or fcv["delta"] >= 0:
            continue
        n = dict(n, fc=fcv)
        fa = n["fc"]["fa"]
        cands.append({"kind": "fading", "snr": round(n["snr"], 1),
                      "points": [(t_b, n["x"], n["y"])],
                      "change_frac": round(n["fc"]["delta"] / fa, 2) if fa > 0 else None})
    return cands, maps


# ------------------------------------------------- tracks across all epochs --
def _pos_at(points, t):
    pts = sorted(points)
    if len(pts) == 1:
        return pts[0][1], pts[0][2]
    (t0, x0, y0), (t1, x1, y1) = (pts[0], pts[1]) if t <= pts[1][0] else (pts[-2], pts[-1])
    k = (t - t0) / (t1 - t0) if t1 != t0 else 0.0
    return x0 + k * (x1 - x0), y0 + k * (y1 - y0)


def _same_object(t, c, tol_px) -> bool:
    """Одна и та же находка из разных пар эпох: в общей эпохе — в одном месте,
    а если общих эпох нет — совпадает с прогнозом по прямой."""
    tp = {round(p[0], 3): p for p in t["points"]}
    shared = [(tp[round(p[0], 3)], p) for p in c["points"] if round(p[0], 3) in tp]
    if shared:
        return all(math.hypot(a[1] - b[1], a[2] - b[2]) <= tol_px for a, b in shared)
    return all(math.hypot(*(np.subtract(_pos_at(t["points"], tc), (xc, yc)))) <= tol_px
               for tc, xc, yc in c["points"])


def _absorb(target, c):
    target["pairs"] += c.get("pairs", 1)
    target["snr"] = max(target["snr"], c["snr"])
    pts = {}
    for t, x, y in target["points"] + c["points"]:            # одна эпоха из двух пар -> среднее
        pts.setdefault(round(t, 3), []).append((t, x, y))
    target["points"] = sorted((ps[0][0], float(np.mean([p[1] for p in ps])),
                               float(np.mean([p[2] for p in ps]))) for ps in pts.values())


def merge_tracks(cands: list, tol_px: float = 1.5) -> list:
    """Один и тот же объект из разных пар эпох -> один трек; число пар = уверенность."""
    tracks = []
    for c in sorted(cands, key=lambda c: -c["snr"]):
        target = next((t for t in tracks if t["kind"] == c["kind"] and _same_object(t, c, tol_px)), None)
        if target is None:
            tracks.append({**c, "points": list(c["points"]), "pairs": 1})
        else:
            _absorb(target, c)
    merged = True                                              # повторяем, пока что-то склеивается
    while merged:
        merged = False
        for i in range(len(tracks)):
            for j in range(i + 1, len(tracks)):
                if tracks[i]["kind"] == tracks[j]["kind"] and _same_object(tracks[i], tracks[j], tol_px):
                    _absorb(tracks[i], tracks.pop(j))
                    merged = True
                    break
            if merged:
                break
    movers = [t for t in tracks if t["kind"] == "moving"]
    return [t for t in tracks if t["kind"] == "moving" or not any(
        math.hypot(t["points"][0][1] - p[1], t["points"][0][2] - p[2]) <= 2.0
        for m in movers for p in m["points"])]


# ------------------------------------------------- fast movers in one epoch --
def _is_sharp(raw: np.ndarray, px: int, py: int) -> bool:
    """Космический луч — один яркий пиксель, а звезда/астероид размыты оптикой:
    у PSF SPHEREx соседние 8 пикселей вместе ярче центрального."""
    n0, n1 = raw.shape
    y0, y1, x0, x1 = max(0, py - 1), min(n0, py + 2), max(0, px - 1), min(n1, px + 2)
    box = np.nan_to_num(raw[y0:y1, x0:x1])
    centre = raw[py, px]
    if not np.isfinite(centre) or centre <= 0:
        return False
    return (box.sum() - centre) < 0.8 * centre
def _consistent(frames, members, dets, x1, y1, vx, vy, t1) -> bool:
    """Справжній об'єкт видно в більшості кадрів, де він мав опинитися (той самий детектор,
    той самий проміжок часу, є дані). Випадкові збіги шуму — лише в кількох."""
    ts = [dets[m][0] for m in members]
    dets_used = {dets[m][4] for m in members}
    expected = 0
    for f in frames:
        t = f["meta"]["mjd"]
        if f["meta"]["det"] not in dets_used or not (min(ts) - 1e-6 <= t <= max(ts) + 1e-6):
            continue
        valid = f.get("_valid")
        x, y = x1 + vx * (t - t1), y1 + vy * (t - t1)
        px, py = int(round(x)), int(round(y))
        if valid is None or not (0 <= py < valid.shape[0] and 0 <= px < valid.shape[1]) or valid[py, px]:
            expected += 1
    return expected > 0 and len(members) / expected >= FAST_HIT_FRAC


def fast_movers(frames: list, sigma: float = FAST_SIGMA, min_dets: int = FAST_MIN_DETS) -> list:
    """Астероиды/кометы: двигаются заметно за часы-дни, т.е. внутри одной эпохи.
    Для каждого кадра: кадр минус медиана остальных кадров эпохи того же детектора."""
    dets = []
    by_det = {}
    for f in frames:
        by_det.setdefault(f["meta"]["det"], []).append(f)
    for group in by_det.values():
        if len(group) < 4:
            continue
        norm = []
        for f in group:
            a = f["arr"] - np.nanmedian(f["arr"])
            norm.append(a / mad_sigma(a))
        stack = np.stack(norm)
        # лише там, де майже всі кадри мають дані: на межі покриття медіана «інших» стрибає
        common = np.isfinite(stack).sum(0) >= math.ceil(COMMON_FRAC * len(group))
        common = ndi.binary_erosion(common, iterations=EDGE_PX)
        for i, f in enumerate(group):
            with np.errstate(all="ignore"):
                ref = np.nanmedian(np.delete(stack, i, axis=0), axis=0)
            raw = stack[i] - ref
            raw[~common] = np.nan
            f["_valid"] = common & np.isfinite(stack[i])
            R = smooth(raw)
            R = R / mad_sigma(R)
            refs = smooth(ref)
            for b in find_blobs(R, sigma, +1):
                r = refs[b["py"], b["px"]]
                if np.isfinite(r) and r > 0.5 * b["snr"]:      # там и так звезда — не новое
                    continue
                if _is_sharp(raw, b["px"], b["py"]):           # космический луч / горячий пиксель
                    continue
                dets.append((f["meta"]["mjd"], b["x"], b["y"], b["snr"], f["meta"]["det"]))
    dets.sort()
    dets = dets[:200]
    tracks, used = [], set()
    for i in range(len(dets)):
        for j in range(i + 1, len(dets)):
            if i in used or j in used:
                continue
            t1, x1, y1, _, _ = dets[i]
            t2, x2, y2, _, _ = dets[j]
            if t2 - t1 <= 1e-4:
                continue
            vx, vy = (x2 - x1) / (t2 - t1), (y2 - y1) / (t2 - t1)
            if math.hypot(x2 - x1, y2 - y1) < 1.5:              # стоит на месте — не быстрый
                continue
            members = {round(t1, 6): i, round(t2, 6): j}               # не більше однієї точки на кадр
            for k in range(len(dets)):
                if k in (i, j) or k in used:
                    continue
                tk, xk, yk, _, _ = dets[k]
                dk = math.hypot(x1 + vx * (tk - t1) - xk, y1 + vy * (tk - t1) - yk)
                if dk <= FAST_TOL_PX:
                    key = round(tk, 6)
                    if key not in members:
                        members[key] = k
                    else:                                          # дві точки в одному кадрі: ближча
                        m0 = members[key]
                        d0 = math.hypot(x1 + vx * (tk - t1) - dets[m0][1], y1 + vy * (tk - t1) - dets[m0][2])
                        if dk < d0:
                            members[key] = k
            members = list(members.values())
            if len(members) >= min_dets and _consistent(frames, members, dets, x1, y1, vx, vy, t1):
                used.update(members)
                pts = sorted((dets[m][0], dets[m][1], dets[m][2]) for m in members)
                tracks.append({"kind": "fast", "snr": round(min(dets[m][3] for m in members), 1),
                               "points": pts, "pairs": len(members),
                               "rate_px_per_day": round(math.hypot(vx, vy), 2)})
    return tracks


# --------------------------------------------------------------- sky coords --
def pix_to_sky(x, y, ra0, dec0, n, pix_deg):
    """Пиксель общей сетки (0-based, север вверх, восток влево) -> RA, Dec (TAN)."""
    c = (n + 1) / 2
    xi = math.radians(-(x + 1 - c) * pix_deg)
    eta = math.radians((y + 1 - c) * pix_deg)
    d0, a0 = math.radians(dec0), math.radians(ra0)
    den = math.cos(d0) - eta * math.sin(d0)
    ra = a0 + math.atan2(xi, den)
    dec = math.atan((math.sin(d0) + eta * math.cos(d0)) * math.cos(ra - a0) / den)
    return math.degrees(ra) % 360, math.degrees(dec)


def sky_to_pix(ra, dec, ra0, dec0, n, pix_deg):
    a, d, a0, d0 = map(math.radians, (ra, dec, ra0, dec0))
    cosc = math.sin(d0) * math.sin(d) + math.cos(d0) * math.cos(d) * math.cos(a - a0)
    xi = math.cos(d) * math.sin(a - a0) / cosc
    eta = (math.cos(d0) * math.sin(d) - math.sin(d0) * math.cos(d) * math.cos(a - a0)) / cosc
    c = (n + 1) / 2
    return -math.degrees(xi) / pix_deg + c - 1, math.degrees(eta) / pix_deg + c - 1
