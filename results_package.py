"""SOLAR Candidate Data Package (Data Contract v1.0) — повний пакет результатів пошуку рухомих об'єктів.

Будує ZIP за контрактом із SOLAR_RUN_2026_0001.zip і специфікацією
"Comprehensive Output Data Package Specification":

  <run_id>/
    manifest.json, README.md, checksums.sha256
    metadata/  search_parameters.json, data_sources.json, schema.json, processing_log.json
    results/   candidates.csv, observations.csv, trajectories.csv, crossmatches.csv,
               rejected_detections.csv, candidate_report.json
    verification/ external_queries.json, external_responses.json, observer_ephemeris.csv
    exchange/  candidates.vot, observations.ecsv, jpl_requests.json, README.md
    evidence/  cutout_index.csv, fits/README.md

Принципи контракту, яких тут дотримано:
  * один канонічний набір (observations / candidates / trajectories) -> усі формати експорту;
  * спостереження окремо від ідентифікації (crossmatches не змінюють траєкторію);
  * невідоме = порожнє поле / null, ніколи не вигадане число;
  * ADES не генерується: немає ефемериди SPHEREx і похибок (контракт забороняє "фейкові" ADES);
  * UNMATCHED — не відкриття.
Без зовнішніх залежностей (лише стандартна бібліотека Python).
"""
import csv
import hashlib
import io
import json
import math
import zipfile
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

CONTRACT_NAME = "SOLAR Candidate Data Package"
CONTRACT_VERSION = "1.0"
PACKAGE_VERSION = "1.0.0"          # версія формату пакета (semver)
SCHEMA_VERSION = "1.0.0"
PRODUCER = "SOLAR"
MODEL_VERSION = "solar-linear2d-1.0"
HOURS_PER_YEAR = 365.25 * 24
_MJD0 = datetime(1858, 11, 17)

# --------------------------------------------------------------------------------- schema --
# Єдине джерело правди: з цього словника пишуться CSV, schema.json, VOTable і ECSV.
# (name, type, unit, nullable, description, extra)
S, F, I, B = "string", "float", "integer", "boolean"
CANDIDATE_CLASS = ["MOVING", "UNCERTAIN", "ARTIFACT", "VARIABLE"]
IDENT_STATUS = ["UNCHECKED", "KNOWN_MATCH", "POSSIBLE_MATCH", "UNMATCHED", "INCONCLUSIVE"]
SCI_STATUS = ["PRELIMINARY", "VALIDATED"]
QUALITY_FLAGS = {
    "SINGLE_EPOCH_PAIR": "seen in only one comparison of observation dates (one pair of epochs)",
    "FEW_DETECTIONS": "only 3 detections in a fast-mover track",
    "LOW_SNR": "weak signal",
    "NEAR_EDGE": "near the edge of the field",
    "NEAR_BRIGHT_STAR": "next to a bright star (residuals can mimic a change)",
    "CATALOG_UNAVAILABLE": "catalog check could not be performed",
    "TWO_POINT_TRACK": "track has only two positions; motion fit is exact and has no residuals",
    "OTHER": "see candidate_report.json reasons",
}

TABLES = {
    "results/candidates.csv": {
        "key": ["run_id", "candidate_id"], "ucd_main": True,
        "columns": [
            ("run_id", S, None, False, "Run identifier", {}),
            ("candidate_id", S, None, False, "Candidate identifier, unique within the run", {}),
            ("detection_source", S, None, False, "Data set the candidate was detected in", {"enum": ["SPHEREx"]}),
            ("first_observed_utc", S, None, False, "Time of the first observation (ISO 8601 UTC)", {"format": "date-time"}),
            ("last_observed_utc", S, None, False, "Time of the last observation (ISO 8601 UTC)", {"format": "date-time"}),
            ("n_detections", I, None, False, "Number of observations (rows in observations.csv)", {"minimum": 1}),
            ("n_independent_epochs", I, None, False, "Number of distinct SPHEREx epochs among the observations", {"minimum": 1}),
            ("reference_epoch_utc", S, None, False, "Epoch of ra_ref/dec_ref (mean observation time for fitted tracks)", {"format": "date-time"}),
            ("ra_ref_deg", F, "deg", False, "RA at the reference epoch (ICRS)", {"minimum": 0, "exclusiveMaximum": 360, "ucd": "pos.eq.ra;meta.main"}),
            ("dec_ref_deg", F, "deg", False, "Dec at the reference epoch (ICRS)", {"minimum": -90, "maximum": 90, "ucd": "pos.eq.dec;meta.main"}),
            ("motion_rate_arcsec_h", F, "arcsec/hour", True, "Total angular rate from the linear fit; empty for variables", {"minimum": 0, "ucd": "pos.pm"}),
            ("motion_pa_deg", F, "deg", True, "Direction of motion, north through east", {"minimum": 0, "exclusiveMaximum": 360, "ucd": "pos.posAng"}),
            ("motion_significance", F, None, True, "Significance (sigma) of the motion signal in the cleaned difference / track; empty for variables", {"ucd": "stat.snr"}),
            ("fit_rms_arcsec", F, "arcsec", True, "RMS of linear-fit residuals; empty when fewer than 3 positions", {"minimum": 0}),
            ("detection_score", F, None, True, "Detection S/N reported by the detector", {"ucd": "stat.snr"}),
            ("candidate_class", S, None, False, "MOVING / UNCERTAIN / ARTIFACT per contract; VARIABLE = SOLAR extension for brightness changes", {"enum": CANDIDATE_CLASS}),
            ("identification_status", S, None, False, "Outcome of catalog checks", {"enum": IDENT_STATUS}),
            ("matched_object_id", S, None, True, "Identifier of the matched known object", {"ucd": "meta.id.cross"}),
            ("matched_catalog", S, None, True, "Catalog of the match", {}),
            ("scientific_status", S, None, False, "PRELIMINARY until independently validated", {"enum": SCI_STATUS}),
            ("quality_flags", S, None, True, "Semicolon-separated warning codes (see schema.json quality_flags)", {}),
        ]},
    "results/observations.csv": {
        "key": ["run_id", "candidate_id", "observation_id"],
        "columns": [
            ("run_id", S, None, False, "Run identifier", {}),
            ("candidate_id", S, None, False, "Candidate this observation belongs to", {}),
            ("observation_id", S, None, False, "Observation identifier, unique within the run", {}),
            ("obs_time_utc", S, None, False, "Observation time, ISO 8601 UTC (derived from native_epoch; see native_time_scale)", {"format": "date-time"}),
            ("jd_utc", F, "JD (UTC)", False, "Julian Date = native MJD + 2400000.5", {"ucd": "time.epoch"}),
            ("native_epoch", F, "d", False, "Time as given by the source (MJD)", {}),
            ("native_time_scale", S, None, False, "Meaning and scale of native_epoch", {}),
            ("ra_deg", F, "deg", False, "Measured RA (ICRS, from the SPHEREx pipeline WCS)", {"minimum": 0, "exclusiveMaximum": 360, "ucd": "pos.eq.ra"}),
            ("dec_deg", F, "deg", False, "Measured Dec (ICRS)", {"minimum": -90, "maximum": 90, "ucd": "pos.eq.dec"}),
            ("coordinate_frame", S, None, False, "Celestial frame", {"enum": ["ICRS"]}),
            ("ra_sigma_arcsec", F, "arcsec", True, "RA uncertainty incl. cos(Dec); not estimated in v1 (empty)", {}),
            ("dec_sigma_arcsec", F, "arcsec", True, "Dec uncertainty; not estimated in v1 (empty)", {}),
            ("ra_dec_corr", F, "dimensionless", True, "RA-Dec correlation; not estimated (empty)", {}),
            ("magnitude", F, "mag", True, "Not measured in v1 (empty)", {}),
            ("mag_error", F, "mag", True, "Not measured in v1 (empty)", {}),
            ("band", S, None, True, "SPHEREx detector and wavelength range", {}),
            ("flux", F, None, True, "Not measured in v1 (empty)", {}),
            ("flux_error", F, None, True, "Not measured in v1 (empty)", {}),
            ("flux_unit", S, None, True, "Unit of flux", {}),
            ("snr", F, "dimensionless", True, "Per-observation S/N; not available in v1 (see candidates.detection_score)", {}),
            ("quality_flags", S, None, True, "Semicolon-separated codes", {}),
            ("astrometric_quality", S, None, False, "How the position was measured", {"enum": ["EPOCH_COADD_CENTROID", "SINGLE_FRAME_CENTROID"]}),
            ("observer_id", S, None, False, "Observing platform", {"enum": ["SPHEREx"]}),
            ("observer_type", S, None, False, "Platform type", {"enum": ["SPACECRAFT"]}),
            ("observer_state_id", S, None, True, "Link to verification/observer_ephemeris.csv; empty = not loaded", {}),
            ("image_id", S, None, True, "SPHEREx frame id (single-frame positions only)", {}),
            ("exposure_id", S, None, True, "Not available (empty)", {}),
            ("transit_id", S, None, True, "Not applicable to SPHEREx (empty)", {}),
            ("ccd_id", S, None, True, "SPHEREx detector (D1-D6) for single-frame positions", {}),
            ("archive", S, None, False, "Archive the images came from", {"enum": ["IRSA"]}),
            ("catalog", S, None, True, "Source catalog (empty: measured on images, not taken from a catalog)", {}),
            ("catalog_row_id", S, None, True, "Not applicable (empty)", {}),
            ("source_url", S, None, True, "URL of the SPHEREx image (single-frame positions)", {}),
            ("is_independent_epoch", B, None, False, "First observation of its epoch for this candidate", {}),
            ("used_in_fit", B, None, False, "Used in the trajectory fit", {}),
            ("residual_arcsec", F, "arcsec", True, "Linear-fit residual; empty when the fit has no residual (2 points) or no fit", {}),
        ]},
    "results/trajectories.csv": {
        "key": ["run_id", "candidate_id", "trajectory_id"],
        "columns": [
            ("trajectory_id", S, None, False, "Trajectory model identifier", {}),
            ("run_id", S, None, False, "Run identifier", {}),
            ("candidate_id", S, None, False, "Candidate identifier", {}),
            ("model_type", S, None, False, "Kinematic model (not orbital elements)", {"enum": ["LINEAR_2D", "QUADRATIC_2D", "ORBITAL"]}),
            ("reference_epoch_utc", S, None, False, "Model reference epoch (mean observation time)", {"format": "date-time"}),
            ("ra_ref_deg", F, "deg", False, "Model RA at the reference epoch", {"minimum": 0, "exclusiveMaximum": 360, "ucd": "pos.eq.ra"}),
            ("dec_ref_deg", F, "deg", False, "Model Dec at the reference epoch", {"minimum": -90, "maximum": 90, "ucd": "pos.eq.dec"}),
            ("dra_cosdec_arcsec_h", F, "arcsec/hour", False, "d(RA)/dt * cos(Dec)", {}),
            ("ddec_arcsec_h", F, "arcsec/hour", False, "d(Dec)/dt", {}),
            ("angular_speed_arcsec_h", F, "arcsec/hour", False, "Total angular rate", {"minimum": 0}),
            ("position_angle_deg", F, "deg", False, "Direction of motion, north through east", {"minimum": 0, "exclusiveMaximum": 360}),
            ("ra_acceleration_arcsec_h2", F, "arcsec/hour^2", True, "Not fitted for LINEAR_2D (empty)", {}),
            ("dec_acceleration_arcsec_h2", F, "arcsec/hour^2", True, "Not fitted for LINEAR_2D (empty)", {}),
            ("fit_rms_arcsec", F, "arcsec", True, "RMS of residuals; empty for 2-point tracks", {"minimum": 0}),
            ("reduced_chi2", F, None, True, "Needs per-observation uncertainties; not available in v1 (empty)", {}),
            ("n_inliers", I, None, False, "Observations used in the fit", {"minimum": 2}),
            ("n_outliers", I, None, False, "Observations rejected by the fit", {"minimum": 0}),
            ("fit_algorithm", S, None, False, "Fitting method", {}),
            ("model_version", S, None, False, "Implementation version", {}),
            ("fit_status", S, None, False, "VALID (>=3 points) / LOW_CONFIDENCE (2 points) / FAILED", {"enum": ["VALID", "LOW_CONFIDENCE", "FAILED"]}),
        ]},
    "results/crossmatches.csv": {
        "key": ["run_id", "match_id"],
        "columns": [
            ("match_id", S, None, False, "Match identifier", {}),
            ("run_id", S, None, False, "Run identifier", {}),
            ("candidate_id", S, None, False, "Candidate identifier", {}),
            ("catalog_name", S, None, False, "Catalog or service", {"enum": ["Gaia", "AllWISE", "IMCCE SkyBoT", "JPL", "MPC", "VizieR"]}),
            ("catalog_release", S, None, True, "Release / version", {}),
            ("external_object_id", S, None, False, "Identifier in that catalog", {}),
            ("external_object_name", S, None, True, "Name of the object", {}),
            ("match_epoch_utc", S, None, False, "Time at which positions were compared (last observation)", {"format": "date-time"}),
            ("angular_separation_arcsec", F, "arcsec", True, "Largest separation along the track (catalog position moved to each date)", {"minimum": 0}),
            ("motion_difference_arcsec_h", F, "arcsec/hour", True, "|fitted motion - catalog proper motion|; empty when not comparable", {"minimum": 0}),
            ("match_score", F, None, True, "Not computed in v1 (empty)", {}),
            ("match_status", S, None, False, "MATCH / POSSIBLE / REJECTED", {"enum": ["MATCH", "POSSIBLE", "REJECTED"]}),
            ("verification_method", S, None, False, "How the match was made", {"enum": ["POSITION", "POSITION_MOTION", "EPHEMERIS"]}),
            ("observer_geometry_validated", B, None, False, "False: spacecraft geometry was not applied", {}),
            ("query_id", S, None, False, "Link to verification/external_queries.json", {}),
        ]},
    "results/rejected_detections.csv": {
        "key": ["run_id", "observation_id"],
        "columns": [
            ("run_id", S, None, False, "Run identifier", {}),
            ("observation_id", S, None, False, "Observation identifier", {}),
            ("candidate_id", S, None, False, "Candidate identifier", {}),
            ("rejection_reason", S, None, False, "Why the detection is doubtful", {}),
            ("quality_flags", S, None, True, "Semicolon-separated codes", {}),
            ("rejection_stage", S, None, False, "Pipeline stage", {"enum": ["CANDIDATE_REVIEW"]}),
            ("threshold", S, None, True, "Threshold in force", {}),
        ]},
    "verification/observer_ephemeris.csv": {
        "key": ["observer_state_id"],
        "columns": [
            ("observer_state_id", S, None, False, "State identifier", {}),
            ("observer_id", S, None, False, "Platform", {}),
            ("epoch_utc", S, None, False, "Epoch of the state vector", {"format": "date-time"}),
            ("reference_origin", S, None, False, "Origin of the vector", {}),
            ("reference_frame", S, None, False, "Frame of the vector", {}),
            ("position_unit", S, None, False, "Unit of position", {}),
            ("velocity_unit", S, None, False, "Unit of velocity", {}),
            ("pos_x", F, None, False, "Position x", {}), ("pos_y", F, None, False, "Position y", {}),
            ("pos_z", F, None, False, "Position z", {}), ("vel_x", F, None, False, "Velocity x", {}),
            ("vel_y", F, None, False, "Velocity y", {}), ("vel_z", F, None, False, "Velocity z", {}),
            ("ephemeris_source", S, None, False, "Source of the ephemeris", {}),
            ("validation_status", S, None, False, "Validation status", {}),
        ]},
    "evidence/cutout_index.csv": {
        "key": ["image_id"],
        "columns": [
            ("image_id", S, None, False, "SPHEREx frame id", {}),
            ("observation_id", S, None, True, "Observation measured on this image (single-frame positions); empty = used in an epoch co-add", {}),
            ("fits_uri", S, None, False, "IRSA cutout URL (centre and size used by this run)", {}),
            ("local_path", S, None, True, "Path inside evidence/fits/ when bundled; empty = not bundled", {}),
            ("hdu", S, None, False, "HDU holding the image", {}),
            ("wcs_frame", S, None, False, "WCS celestial frame", {}),
            ("observation_time_utc", S, None, False, "Exposure start (IRSA time_bounds_lower)", {"format": "date-time"}),
            ("band", S, None, False, "Detector and central wavelength", {}),
            ("sha256", S, None, True, "Checksum of the bundled file; empty when not bundled", {}),
            ("rights", S, None, False, "Usage conditions", {}),
        ]},
}

TIME_NOTE = ("MJD from IRSA spherex.plane.time_bounds_lower (exposure start), time scale as published by IRSA, not "
             "converted. Epoch positions use the mean of the exposure-start times of the frames combined.")


# ------------------------------------------------------------------------------- helpers --
def _iso(mjd):
    if mjd is None:
        return None
    t = _MJD0 + timedelta(days=float(mjd))
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _r(v, nd):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(v, nd)


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def _csv(path, rows) -> bytes:
    cols = [c[0] for c in TABLES[path]["columns"]]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for row in rows:
        w.writerow([_cell(row.get(c)) for c in cols])
    return buf.getvalue().encode("utf-8")


def _json(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def _tan_inverse(xi_deg, eta_deg, ra0, dec0):
    """Гномонічна проекція: зсув у площині (град) -> RA/Dec."""
    xi, eta = math.radians(xi_deg), math.radians(eta_deg)
    a0, d0 = math.radians(ra0), math.radians(dec0)
    den = math.cos(d0) - eta * math.sin(d0)
    ra = a0 + math.atan2(xi, den)
    dec = math.atan2(math.sin(d0) + eta * math.cos(d0), math.hypot(xi, den))
    return [round(math.degrees(ra) % 360, 7), round(math.degrees(dec), 7)]


def linear_fit(points):
    """Лінійна модель руху на дотичній площині. points: [{mjd, ra, dec}]. Без похибок -> без chi2."""
    n = len(points)
    if n < 2:
        return None
    ra0 = points[0]["ra"]
    dec0 = sum(p["dec"] for p in points) / n
    cosd = math.cos(math.radians(dec0))
    xs = [(((p["ra"] - ra0 + 180) % 360) - 180) * cosd * 3600 for p in points]    # arcsec, схід = +
    ys = [(p["dec"] - dec0) * 3600 for p in points]
    tref = sum(p["mjd"] for p in points) / n
    ts = [(p["mjd"] - tref) * 24 for p in points]                                     # години
    stt = sum(t * t for t in ts)
    if stt <= 0:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum(t * x for t, x in zip(ts, xs)) / stt
    vy = sum(t * y for t, y in zip(ts, ys)) / stt
    res = [math.hypot(x - (mx + vx * t), y - (my + vy * t)) for t, x, y in zip(ts, xs, ys)]
    rms = math.sqrt(sum(r * r for r in res) / n) if n >= 3 else None
    ra_ref = (ra0 + mx / 3600 / cosd) % 360
    return {"tref_mjd": tref, "ra_ref": ra_ref, "dec_ref": dec0 + my / 3600, "vx": vx, "vy": vy,
            "speed": math.hypot(vx, vy), "pa": math.degrees(math.atan2(vx, vy)) % 360,
            "rms": rms, "residuals": res if n >= 3 else [None] * n, "n": n}


def _flag_codes(reasons, n_points, kind):
    codes = []
    for r in reasons or []:
        r = r.lower()
        code = ("SINGLE_EPOCH_PAIR" if ("one pair" in r or "one comparison" in r) else "FEW_DETECTIONS" if "detections" in r else
                "LOW_SNR" if "weak signal" in r else "NEAR_EDGE" if "edge" in r else
                "NEAR_BRIGHT_STAR" if "bright star" in r else "CATALOG_UNAVAILABLE" if "catalog" in r else "OTHER")
        if code not in codes:
            codes.append(code)
    if kind in ("moving", "fast") and n_points == 2:
        codes.append("TWO_POINT_TRACK")
    return codes


def _epoch_of(mjd, frames):
    if not frames:
        return None
    return min(frames, key=lambda f: abs(f["mjd"] - mjd)).get("epoch")


def _nearest_frame(mjd, frames):
    return min(frames, key=lambda f: abs(f["mjd"] - mjd)) if frames else None


# ------------------------------------------------------------------------------- builder --
def build_package(res: dict, ctx: dict | None = None) -> tuple:
    """res = відповідь /api/detect; ctx = дані сервера (версії бібліотек). Повертає (zip_bytes, file_name)."""
    ctx = ctx or {}
    prov = res.get("provenance") or {}
    tgt = prov.get("target") or {}
    par = prov.get("parameters") or {}
    data = prov.get("data") or {}
    cats = prov.get("catalogs") or {}
    frames = data.get("frames") or []
    created = _now()

    # стабільний ID запуску: залежить від змісту результату, не від часу пакування
    stable = {"candidates": res.get("candidates"), "parameters": par, "target": tgt,
              "frames": [f.get("id") for f in frames], "run_utc": prov.get("run_utc")}
    digest = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
    year = (prov.get("run_utc") or created)[:4]
    run_id = f"SOLAR_RUN_{year}_{digest[:8].upper()}"

    gaia_ok = (cats.get("gaia") or {}).get("via") is not None
    wise_ok = (cats.get("allwise") or {}).get("via") is not None
    gaia_tried = gaia_ok or (cats.get("gaia") or {}).get("error") is not None
    sky_log = (cats.get("asteroids") or {}).get("log") or []

    cand_rows, obs_rows, traj_rows, xm_rows, rej_rows, report = [], [], [], [], [], []
    queries, responses = [], []
    det_label = tgt.get("detector")
    lam_lo, lam_hi = par.get("lam_min_um") or 0.75, par.get("lam_max_um") or 5.0

    # ---- зовнішні запити (verification)
    def add_query(qid, service, endpoint, when, params, status, count, matched=None, error=None):
        queries.append({"query_id": qid, "service": service, "endpoint": endpoint, "request_time_utc": when,
                        "parameters": params, "response_status": status})
        responses.append({"query_id": qid, "response_time_utc": when, "service_version": None,
                          "result_count": count, "matched_objects": matched or [], "errors": [error] if error else []})

    ss = prov.get("spherex_search") or {}
    add_query("Q-SPHEREX-TAP", ss.get("service", "IRSA TAP"), ss.get("endpoint"), ss.get("retrieved_utc"),
              {"adql": ss.get("adql")}, "SUCCESS", ss.get("result_count"))
    for key, qid, label in (("gaia", "Q-GAIA", "Gaia DR3"), ("allwise", "Q-ALLWISE", "AllWISE")):
        c = cats.get(key) or {}
        if c.get("via") is None and c.get("error") is None:
            continue                                        # перевірку каталогом не запускали
        q = c.get("query") or {}
        add_query(qid, q.get("service") or c.get("via") or label, q.get("endpoint"), c.get("retrieved_utc"),
                  {k: v for k, v in q.items() if k not in ("service", "endpoint")} or {"table": c.get("table")},
                  "SUCCESS" if c.get("via") else "FAILED", c.get("result_count"), error=c.get("error"))
    sky_qid = {}
    for k, q in enumerate(sky_log, 1):
        qid = f"Q-SKYBOT-{k:03d}"
        sky_qid[q.get("candidate")] = qid
        add_query(qid, "IMCCE SkyBoT cone search", "astroquery.imcce.Skybot.cone_search", q.get("request_time_utc"),
                  {"ra_deg": q.get("ra_deg"), "dec_deg": q.get("dec_deg"), "epoch_utc": _iso(q.get("mjd")),
                   "radius_arcmin": q.get("radius_arcmin"), "location": q.get("location")},
                  q.get("status") or "FAILED", q.get("result_count"), error=q.get("error"))
    resp_by_id = {r["query_id"]: r for r in responses}

    # ---- кандидати, спостереження, траєкторії, ідентифікації
    for d in res.get("candidates", []):
        cid = f"SPX-C{d['id']:04d}"
        kind = d["kind"]
        pts = d["points"]
        reasons = d.get("reasons") or []
        m = d.get("matches") or {}
        flags = _flag_codes(reasons, len(pts), kind)
        is_track = kind in ("moving", "fast")
        fit = linear_fit(pts) if is_track else None
        epochs = {_epoch_of(p["mjd"], frames) for p in pts}

        # класифікація за контрактом
        if kind in ("brightening", "fading"):
            cls = "VARIABLE"
        else:
            cls = "UNCERTAIN" if d.get("verdict_class") == "inconclusive" else "MOVING"
        checked = (kind == "fast" and d["id"] in sky_qid) or (kind != "fast" and gaia_tried)
        if m.get("asteroid") or m.get("gaia"):
            ident = "KNOWN_MATCH"
        elif m.get("allwise"):
            ident = "POSSIBLE_MATCH"                # близькість до джерела каталогу, не ідентифікація
        elif not checked:
            ident = "UNCHECKED"
        elif d.get("verdict_class") == "unmatched":
            ident = "UNMATCHED"
        else:
            ident = "INCONCLUSIVE"
        if m.get("asteroid"):
            a = m["asteroid"]
            mid_, mcat = (f"({a['number']}) {a['name']}" if a.get("number") else a.get("name")), "IMCCE SkyBoT"
        elif m.get("gaia"):
            mid_, mcat = f"Gaia DR3 {m['gaia']['source_id']}", "Gaia DR3"
        elif m.get("allwise"):
            mid_, mcat = m["allwise"]["name"], "AllWISE"
        else:
            mid_, mcat = None, None

        ref_mjd = fit["tref_mjd"] if fit else pts[-1]["mjd"]
        cand_rows.append({
            "run_id": run_id, "candidate_id": cid, "detection_source": "SPHEREx",
            "first_observed_utc": _iso(pts[0]["mjd"]), "last_observed_utc": _iso(pts[-1]["mjd"]),
            "n_detections": len(pts), "n_independent_epochs": len(epochs - {None}) or 1,
            "reference_epoch_utc": _iso(ref_mjd),
            "ra_ref_deg": _r(fit["ra_ref"] if fit else pts[-1]["ra"], 7),
            "dec_ref_deg": _r(fit["dec_ref"] if fit else pts[-1]["dec"], 7),
            "motion_rate_arcsec_h": _r(fit["speed"], 8) if fit else None,
            "motion_pa_deg": _r(fit["pa"], 2) if fit else None,
            "motion_significance": d.get("snr") if is_track else None,
            "fit_rms_arcsec": _r(fit["rms"], 3) if fit else None,
            "detection_score": d.get("snr"), "candidate_class": cls, "identification_status": ident,
            "matched_object_id": mid_, "matched_catalog": mcat, "scientific_status": "PRELIMINARY",
            "quality_flags": ";".join(flags) or None,
        })

        obs_ids, seen_epochs = [], set()
        for k, p in enumerate(pts):
            oid = f"{cid}-O{k + 1:02d}"
            obs_ids.append(oid)
            single = kind == "fast"
            fr = _nearest_frame(p["mjd"], frames) if single else None
            ep = _epoch_of(p["mjd"], frames)
            first_of_epoch = ep not in seen_epochs
            seen_epochs.add(ep)
            band = (f"SPHEREx {fr['det']} {fr['lam_um']:.4f} um" if fr and fr.get("lam_um") else
                    f"SPHEREx {det_label} {lam_lo}-{lam_hi} um (same-wavelength frame pairs)")
            obs_rows.append({
                "run_id": run_id, "candidate_id": cid, "observation_id": oid, "obs_time_utc": _iso(p["mjd"]),
                "jd_utc": _r(p["mjd"] + 2400000.5, 6), "native_epoch": _r(p["mjd"], 6), "native_time_scale": TIME_NOTE,
                "ra_deg": _r(p["ra"] % 360, 7), "dec_deg": _r(p["dec"], 7), "coordinate_frame": "ICRS",
                "band": band, "quality_flags": None,
                "astrometric_quality": "SINGLE_FRAME_CENTROID" if single else "EPOCH_COADD_CENTROID",
                "observer_id": "SPHEREx", "observer_type": "SPACECRAFT", "observer_state_id": None,
                "image_id": fr["id"] if fr else None, "ccd_id": fr["det"] if fr else None, "archive": "IRSA",
                "source_url": fr.get("url") if fr else None, "is_independent_epoch": first_of_epoch,
                "used_in_fit": bool(fit), "residual_arcsec": _r(fit["residuals"][k], 3) if fit else None,
            })
            if reasons:
                rej_rows.append({"run_id": run_id, "observation_id": oid, "candidate_id": cid,
                                 "rejection_reason": "; ".join(reasons), "quality_flags": ";".join(flags) or None,
                                 "rejection_stage": "CANDIDATE_REVIEW",
                                 "threshold": f"detection sigma = {par.get('sigma')}"})

        if fit:
            traj_rows.append({
                "trajectory_id": f"{cid}-T1", "run_id": run_id, "candidate_id": cid, "model_type": "LINEAR_2D",
                "reference_epoch_utc": _iso(fit["tref_mjd"]), "ra_ref_deg": _r(fit["ra_ref"], 7),
                "dec_ref_deg": _r(fit["dec_ref"], 7), "dra_cosdec_arcsec_h": _r(fit["vx"], 8),
                "ddec_arcsec_h": _r(fit["vy"], 8), "angular_speed_arcsec_h": _r(fit["speed"], 8),
                "position_angle_deg": _r(fit["pa"], 2), "fit_rms_arcsec": _r(fit["rms"], 3),
                "n_inliers": fit["n"], "n_outliers": 0,
                "fit_algorithm": "Unweighted least squares on the tangent plane at the mean position: "
                                 "RA*cos(Dec) and Dec linear in time",
                "model_version": MODEL_VERSION, "fit_status": "VALID" if fit["n"] >= 3 else "LOW_CONFIDENCE",
            })

        # ідентифікації — окремо від спостережень
        k_m = 0
        last_t = _iso(pts[-1]["mjd"])

        def pm_diff(cat):
            if not fit or cat.get("pmra") is None or cat.get("pmdec") is None:
                return None
            return _r(math.hypot(fit["vx"] - cat["pmra"] / 1000 / HOURS_PER_YEAR,
                                 fit["vy"] - cat["pmdec"] / 1000 / HOURS_PER_YEAR), 8)

        for key, name, rel, status, method, qid in (
                ("gaia", "Gaia", "DR3", "MATCH", "POSITION_MOTION", "Q-GAIA"),
                ("allwise", "AllWISE", "AllWISE", "POSSIBLE", "POSITION_MOTION", "Q-ALLWISE"),
                ("asteroid", "IMCCE SkyBoT", "live ephemerides", "MATCH", "EPHEMERIS", sky_qid.get(d["id"]))):
            c = m.get(key)
            if not c:
                continue
            k_m += 1
            if key == "gaia":
                ext_id, ext_name, sep, mdiff = str(c["source_id"]), f"Gaia DR3 {c['source_id']}", c.get("sep_arcsec"), pm_diff(c)
            elif key == "allwise":
                ext_id, ext_name, sep, mdiff = c["name"], c["name"], c.get("sep_arcsec"), pm_diff(c)
            else:
                ext_id = str(c["number"]) if c.get("number") else c.get("name")
                ext_name, sep, mdiff = c.get("name"), c.get("dist_arcsec"), None
            xm_rows.append({"match_id": f"{cid}-M{k_m}", "run_id": run_id, "candidate_id": cid,
                            "catalog_name": name, "catalog_release": rel, "external_object_id": ext_id,
                            "external_object_name": ext_name, "match_epoch_utc": last_t,
                            "angular_separation_arcsec": _r(sep, 3) if sep is not None else None,
                            "motion_difference_arcsec_h": mdiff, "match_score": None, "match_status": status,
                            "verification_method": method, "observer_geometry_validated": False, "query_id": qid})
            if qid in resp_by_id:
                resp_by_id[qid]["matched_objects"].append({"candidate_id": cid, "external_object_id": ext_id})

        report.append({"candidate_id": cid, "ui_number": d["id"], "kind": kind, "candidate_class": cls,
                       "identification_status": ident, "ui_verdict_class": d.get("verdict_class"),
                       "ui_verdict": d.get("verdict"), "reasons": reasons, "quality_flags": flags,
                       "snr": d.get("snr"), "independent_epoch_pairs": d.get("support"),
                       "proper_motion_arcsec_yr": d.get("pm_arcsec_yr"), "rate_arcsec_h": d.get("rate_arcsec_hr"),
                       "brightness_change_pct": d.get("change_pct"), "observation_ids": obs_ids,
                       "trajectory_id": f"{cid}-T1" if fit else None, "catalog_matches": m})

    # ---- evidence: індекс зображень
    fast_obs = {r["image_id"]: r["observation_id"] for r in obs_rows if r["image_id"]}
    size = (prov.get("cutouts") or {}).get("size_deg")
    cut_rows = [{"image_id": f["id"], "observation_id": fast_obs.get(f["id"]),
                 "fits_uri": (f"{f['url']}?center={tgt.get('ra_deg')},{tgt.get('dec_deg')}d&size={size}"
                              if f.get("url") and size else f.get("url")),
                 "local_path": None, "hdu": "IMAGE", "wcs_frame": "ICRS", "observation_time_utc": _iso(f["mjd"]),
                 "band": f"{f.get('det')} {f['lam_um']:.4f} um" if f.get("lam_um") else f.get("det"),
                 "sha256": None, "rights": "Public NASA/IPAC IRSA data; acknowledge SPHEREx and IRSA"}
                for f in frames]

    # ---- метадані
    sigma = par.get("sigma")
    t_first = _iso(min(f["mjd"] for f in frames)) if frames else None
    t_last = _iso(max(f["mjd"] for f in frames)) if frames else None
    n_epochs = len(set(res.get("epochs") or []))
    fov = tgt.get("fov_deg")
    ra_c, dec_c = tgt.get("ra_deg"), tgt.get("dec_deg")
    verts = ([_tan_inverse(sx * fov / 2, sy * fov / 2, ra_c, dec_c) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
             if None not in (fov, ra_c, dec_c) else None)
    search_parameters = {
        "run_id": run_id, "ra_center_deg": ra_c, "dec_center_deg": dec_c, "fov_width_deg": fov, "fov_height_deg": fov,
        "region_geometry": "BOX", "region_vertices_deg": verts, "coordinate_frame": "ICRS",
        "time_start_utc": _iso(par.get("date_from_mjd")), "time_end_utc": _iso(par.get("date_to_mjd")),
        "requested_catalogs": ["SPHEREx"] + (["Gaia DR3", "AllWISE", "IMCCE SkyBoT"] if gaia_tried or sky_log else []),
        "min_observations": 2, "min_independent_epochs": 1, "min_snr": sigma, "max_motion_arcsec_h": None,
        "selection_policy": {
            "detector": det_label, "wavelength_window_um": [lam_lo, lam_hi],
            "same_wavelength_tolerance_um": par.get("match_dlam_um"),
            "min_frame_coverage": par.get("min_frame_coverage"),
            "per_class": {
                "slow movers (between epochs)": {"min_observations": 2, "min_independent_epochs": 2,
                                                 "max_motion_arcsec_yr": par.get("max_pm_arcsec_yr"),
                                                 "max_motion_arcsec_h": _r((par.get("max_pm_arcsec_yr") or 0) / HOURS_PER_YEAR, 8)},
                "fast movers (within one epoch)": {"min_observations": 3, "min_independent_epochs": 1,
                                                   "detection_sigma": par.get("fast_sigma"), "max_motion_arcsec_h": None},
                "variables": {"min_flux_change_fraction": par.get("var_flux_frac"), "epochs_compared": 2},
            },
            "time_window_end_exclusive": True,
        },
        "applied_constraints": {"frames_used": len(frames), "epochs_used": n_epochs,
                                "time_start_utc": t_first, "time_end_utc": t_last,
                                "data_releases": data.get("releases")},
        "coverage_status": "NO_COVERAGE" if not frames else ("AVAILABLE" if n_epochs >= 2 else "PARTIAL"),
    }

    def src(source_id, archive, catalog, table, release, endpoint, adql, when, count, status, ref, rights, extra=None):
        return {"source_id": source_id, "archive": archive, "catalog": catalog, "table": table,
                "release_version": release, "tap_endpoint": endpoint, "query_adql": adql, "query_time_utc": when,
                "result_count": count, "retrieval_status": status, "dataset_doi": None, "rights_license": rights,
                "source_reference_url": ref, **(extra or {})}

    sources = [src("SRC-SPHEREX", "IRSA", "SPHEREx spectral images", "spherex.plane JOIN spherex.artifact",
                   ", ".join(data.get("releases") or []) or None, ss.get("endpoint"), ss.get("adql"),
                   ss.get("retrieved_utc"), ss.get("result_count"), "SUCCESS", "https://irsa.ipac.caltech.edu/",
                   "Public NASA mission data; acknowledge SPHEREx and NASA/IPAC IRSA",
                   {"frames_used": len(frames), "cutout_service": prov.get("cutouts")})]
    g = cats.get("gaia") or {}
    if g.get("via") or g.get("error"):
        q = g.get("query") or {}
        sources.append(src("SRC-GAIA", "ESA" if (g.get("via") or "").startswith("ESA") else "VizieR (CDS)",
                           "Gaia DR3", g.get("table") if (g.get("via") or "").startswith("ESA") else "I/355/gaiadr3",
                           "DR3", q.get("endpoint"), q.get("adql"), g.get("retrieved_utc"), g.get("result_count"),
                           "SUCCESS" if g.get("via") else "FAILED", "https://www.cosmos.esa.int/web/gaia/dr3",
                           "Acknowledge ESA Gaia / DPAC; see the Gaia archive terms of use",
                           {"reference_epoch": "J2016.0", "error": g.get("error")}))
    w = cats.get("allwise") or {}
    if w.get("via") or w.get("error"):
        q = w.get("query") or {}
        sources.append(src("SRC-ALLWISE", "VizieR (CDS)" if (w.get("via") or "").startswith("VizieR") else "IRSA",
                           "AllWISE", "II/328/allwise" if (w.get("via") or "").startswith("VizieR") else "allwise_p3as_psd",
                           "AllWISE", q.get("endpoint"), None, w.get("retrieved_utc"), w.get("result_count"),
                           "SUCCESS" if w.get("via") else "FAILED", "https://wise2.ipac.caltech.edu/docs/release/allwise/",
                           "Public NASA data; acknowledge WISE/NEOWISE and IRSA",
                           {"reference_epoch": "~J2010.5", "error": w.get("error")}))
    if sky_log:
        ok_n = sum(1 for q in sky_log if q.get("status") == "SUCCESS")
        sources.append(src("SRC-SKYBOT", "IMCCE", "SkyBoT (known Solar System objects)", None, "live service",
                           "astroquery.imcce.Skybot.cone_search", None, sky_log[0].get("request_time_utc"),
                           sum(q.get("result_count") or 0 for q in sky_log),
                           "SUCCESS" if ok_n == len(sky_log) else ("PARTIAL" if ok_n else "FAILED"),
                           "https://ssp.imcce.fr/webservices/skybot/", "Acknowledge IMCCE SkyBoT",
                           {"observer_location": "500 (geocentre) - SPHEREx parallax not applied"}))

    any_failed = any(s_["retrieval_status"] in ("FAILED", "PARTIAL") for s_ in sources)
    status = "PARTIAL" if any_failed else "COMPLETED"

    warnings = [
        "Scientific status PRELIMINARY: results are not independently validated.",
        "Observer ephemeris of SPHEREx is not loaded (verification/observer_ephemeris.csv has no rows); "
        "spacecraft geometry was not applied in any check.",
        "Observation times are IRSA exposure-start times (time_bounds_lower), not mid-exposure; time scale not converted.",
        "Astrometric and photometric uncertainties are not estimated in v1 (empty fields).",
        "MPC ADES files are not generated: they require observer state, mid-exposure times and uncertainties.",
        "AllWISE matches are positional proximity to a catalogued source (POSSIBLE_MATCH), not an identification.",
        "SkyBoT checks used a geocentric observer; SPHEREx parallax for near-Earth objects is not accounted for.",
        "UNMATCHED means no match by the performed checks, not a confirmed discovery.",
    ]
    if not frames:
        warnings.append("No frames in the result.")
    for s_ in sources:
        if s_["retrieval_status"] != "SUCCESS":
            warnings.append(f"{s_['catalog']}: retrieval {s_['retrieval_status']}" +
                            (f" ({s_.get('error')})" if s_.get("error") else ""))

    kinds = {}
    for d in res.get("candidates", []):
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    timing = prov.get("timing_s") or {}
    events = [e for e in (
        {"time_utc": ss.get("retrieved_utc"), "event": "SPHEREx frame search (IRSA TAP)"},
        {"time_utc": g.get("retrieved_utc"), "event": "Gaia DR3 query"} if g.get("via") else None,
        {"time_utc": w.get("retrieved_utc"), "event": "AllWISE query"} if w.get("via") else None,
        {"time_utc": prov.get("run_utc"), "event": "Detection and catalog verification finished"},
        {"time_utc": created, "event": "Data package generated"}) if e]
    processing_log = {
        "run_id": run_id, "status": status, "pipeline_version": f"{prov.get('tool')} {prov.get('version')}",
        "algorithms": [
            {"name": "same-wavelength epoch differencing", "module": "detect.compare_epochs",
             "description": "Pairs of frames from two epochs within match_dlam_um; MAD-normalised differences; median over pairs; PSF-size smoothing"},
            {"name": "moving-source detection", "module": "detect.compare_epochs",
             "description": "Disappear/appear pair with >= move_flux_frac of the flux moved; centroids on epoch images"},
            {"name": "variability detection", "module": "detect.compare_epochs",
             "description": "Aperture flux change >= var_flux_frac at a local maximum"},
            {"name": "fast-mover tracking", "module": "detect.fast_movers",
             "description": "Within one epoch: frame minus median of the others; >= 3 detections on a line; sharp single-pixel spikes rejected"},
            {"name": "track merging", "module": "detect.merge_tracks", "description": "Links positions from several epoch pairs"},
            {"name": "catalog cross-match", "module": "api.api_detect",
             "description": "Gaia DR3 (1.5 px) and AllWISE (2 px) moved to each date by proper motion; SkyBoT for fast movers"},
            {"name": "trajectory fit", "module": "results_package.linear_fit", "version": MODEL_VERSION,
             "description": "LINEAR_2D unweighted least squares on the tangent plane"},
        ],
        "parameters": par,
        "statistics": {"frames_found": ss.get("result_count"), "frames_used": len(frames), "epochs": n_epochs,
                       "epoch_pairs_compared": len(prov.get("epoch_pairs") or res.get("epoch_pairs") or []),
                       "candidates_by_kind": kinds,
                       "candidates_by_class": {c: sum(1 for r in cand_rows if r["candidate_class"] == c) for c in CANDIDATE_CLASS},
                       "identification_status": {s_: sum(1 for r in cand_rows if r["identification_status"] == s_) for s_ in IDENT_STATUS},
                       "catalog_rows": {"gaia": g.get("result_count"), "allwise": w.get("result_count")}},
        "durations_s": timing, "library_versions": ctx.get("library_versions") or {},
        "warnings": warnings, "events": events,
        "not_itemised": "Pixel-level rejections inside the detector (cosmic rays, PSF residuals) are not listed "
                        "individually in v1; rejected_detections.csv lists doubtful candidates' observations.",
    }

    schema = _schema_doc()

    # ---- exchange
    jpl = {"note": "Adapter request definitions for the JPL Small-Body Identification API; not an uploadable JPL "
                   "data format. Not ready: SPHEREx observer state is not loaded, so requests must not be sent as is.",
           "requests": []}
    for r in cand_rows:
        rep = next(x for x in report if x["candidate_id"] == r["candidate_id"])
        if rep["kind"] != "fast":
            continue
        jpl["requests"].append({
            "request_id": f"JPL-REQ-{len(jpl['requests']) + 1:03d}", "candidate_id": r["candidate_id"],
            "service": "JPL_SB_IDENT", "endpoint": "https://ssd-api.jpl.nasa.gov/sb_ident.api", "method": "GET",
            "observation_time_utc": r["reference_epoch_utc"],
            "fov": {"ra_center_deg": r["ra_ref_deg"], "dec_center_deg": r["dec_ref_deg"],
                    "ra_half_width_deg": 0.02, "dec_half_width_deg": 0.02},
            "observer_state_id": None, "observer_state_validated": False, "ready_for_submission": False,
            "parameters": {"two-pass": "true"}})
    exchange_readme = f"""# Exchange formats — {run_id}

| File | Standard | Status |
|---|---|---|
| `candidates.vot` | IVOA VOTable 1.4 (UCDs, units, ICRS) | generated from results/candidates.csv |
| `observations.ecsv` | Astropy ECSV 1.0 (types, units) | generated from results/observations.csv |
| `jpl_requests.json` | internal adapter definitions for JPL SB Identification | {len(jpl['requests'])} request(s), **not ready to send** |
| `observations.ades.psv` / `.ades.xml` | MPC ADES | **not generated** |

ADES files are deliberately not produced: the contract allows them only from validated observations with the
spacecraft observer state, mid-exposure times, astrometric uncertainties and submission metadata. This run has
none of those yet (see manifest.json warnings). JPL accepts API parameters, not an upload of these files.
"""
    vot = _votable(cand_rows, "results/candidates.csv", run_id, prov)
    ecsv = _ecsv(obs_rows, "results/observations.csv", run_id)

    readme = _readme(run_id, prov, cand_rows, obs_rows, traj_rows, xm_rows, warnings, status)

    files = {
        "README.md": (readme.encode("utf-8"), "documentation", "md"),
        "metadata/search_parameters.json": (_json(search_parameters), "core", "json"),
        "metadata/data_sources.json": (_json({"run_id": run_id, "sources": sources,
                                              "note": "Archives, tables, releases and exact queries used by this run."}),
                                        "core", "json"),
        "metadata/schema.json": (_json(schema), "core", "json"),
        "metadata/processing_log.json": (_json(processing_log), "extended", "json"),
        "results/candidates.csv": (_csv("results/candidates.csv", cand_rows), "core", "csv"),
        "results/observations.csv": (_csv("results/observations.csv", obs_rows), "core", "csv"),
        "results/trajectories.csv": (_csv("results/trajectories.csv", traj_rows), "core", "csv"),
        "results/crossmatches.csv": (_csv("results/crossmatches.csv", xm_rows), "extended", "csv"),
        "results/rejected_detections.csv": (_csv("results/rejected_detections.csv", rej_rows), "extended", "csv"),
        "results/candidate_report.json": (_json({
            "run_id": run_id, "schema_version": SCHEMA_VERSION, "status": status, "generated_at_utc": created,
            "summary": {"candidates": len(cand_rows), "by_class": processing_log["statistics"]["candidates_by_class"],
                        "by_identification_status": processing_log["statistics"]["identification_status"]},
            "ui_to_contract_mapping": {
                "known (Gaia / SkyBoT)": "KNOWN_MATCH", "known (AllWISE only)": "POSSIBLE_MATCH",
                "unmatched": "UNMATCHED", "inconclusive": "INCONCLUSIVE (candidate_class UNCERTAIN for movers)",
                "catalog check not run": "UNCHECKED"},
            "candidates": report,
            "note": "PRELIMINARY results. UNMATCHED means no match by the performed checks, not a discovery.",
            "source_api_response": res}), "extended", "json"),
        "verification/external_queries.json": (_json({"queries": queries}), "extended", "json"),
        "verification/external_responses.json": (_json({"responses": responses}), "extended", "json"),
        "verification/observer_ephemeris.csv": (_csv("verification/observer_ephemeris.csv", []), "extended", "csv"),
        "exchange/README.md": (exchange_readme.encode("utf-8"), "exchange", "md"),
        "exchange/candidates.vot": (vot, "exchange", "vot"),
        "exchange/observations.ecsv": (ecsv, "exchange", "ecsv"),
        "exchange/jpl_requests.json": (_json(jpl), "exchange", "json"),
        "evidence/cutout_index.csv": (_csv("evidence/cutout_index.csv", cut_rows), "evidence", "csv"),
        "evidence/fits/README.md": (b"FITS cutouts are not bundled in this package (size). Each image is listed in "
                                    b"../cutout_index.csv with its IRSA cutout URL (fits_uri), HDU, time and band.\n",
                                    "evidence", "md"),
    }
    manifest = {
        "package_id": run_id, "run_id": run_id, "package_version": PACKAGE_VERSION,
        "contract": {"name": CONTRACT_NAME, "version": CONTRACT_VERSION, "schema_version": SCHEMA_VERSION},
        "created_at_utc": created,
        "producer": {"name": PRODUCER, "version": f"{prov.get('tool')} {prov.get('version')}"},
        "processing_status": status, "scientific_status": "PRELIMINARY", "is_template": False,
        "counts": {"candidates": len(cand_rows), "observations": len(obs_rows), "trajectories": len(traj_rows),
                   "catalog_matches": len(xm_rows), "rejected_detections": len(rej_rows),
                   "external_queries": len(queries), "evidence_images": len(cut_rows)},
        "files": [{"path": p, "role": role, "format": fmt, "size_bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                  for p, (b, role, fmt) in sorted(files.items())],
        "warnings": warnings,
    }
    all_files = {p: b for p, (b, *_rest) in files.items()}
    all_files["manifest.json"] = _json(manifest)
    all_files["checksums.sha256"] = "".join(f"{hashlib.sha256(b).hexdigest()}  {p}\n"
                                            for p, b in sorted(all_files.items())).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in ["manifest.json", "README.md"] + sorted(k for k in all_files if k not in ("manifest.json", "README.md")):
            z.writestr(f"{run_id}/{p}", all_files[p])
        z.writestr(f"{run_id}/evidence/fits/", b"")                   # порожня тека-контейнер, як у контракті
    return buf.getvalue(), f"{run_id}.zip"


# ------------------------------------------------------------------------ schema.json --
def _schema_doc():
    csv_tables = {}
    for path, t in TABLES.items():
        cols = []
        for name, typ, unit, nullable, desc, extra in t["columns"]:
            c = {"name": name, "type": typ, "unit": unit, "nullable": nullable, "required": True, "description": desc}
            c.update({k: v for k, v in extra.items() if k != "ucd"})
            if "ucd" in extra:
                c["ucd"] = extra["ucd"]
            cols.append(c)
        csv_tables[path] = {"columns": cols, "key_columns": t["key"], "encoding": "UTF-8", "delimiter": ",",
                            "header": True, "boolean_values": ["true", "false"]}
    str_or_null = {"type": ["string", "null"]}
    num_or_null = {"type": ["number", "null"]}
    return {
        "schema_version": SCHEMA_VERSION, "contract": f"{CONTRACT_NAME} v{CONTRACT_VERSION}", "status": "PRODUCTION",
        "csv_tables": csv_tables,
        "relations": [
            {"from": "results/observations.csv", "columns": ["run_id", "candidate_id"], "to": "results/candidates.csv"},
            {"from": "results/trajectories.csv", "columns": ["run_id", "candidate_id"], "to": "results/candidates.csv"},
            {"from": "results/crossmatches.csv", "columns": ["run_id", "candidate_id"], "to": "results/candidates.csv"},
            {"from": "results/crossmatches.csv", "columns": ["query_id"], "to": "verification/external_queries.json#queries[].query_id"},
            {"from": "results/rejected_detections.csv", "columns": ["run_id", "observation_id"], "to": "results/observations.csv"},
            {"from": "results/observations.csv", "columns": ["image_id"], "to": "evidence/cutout_index.csv"},
            {"from": "results/observations.csv", "columns": ["observer_state_id"], "to": "verification/observer_ephemeris.csv"},
        ],
        "quality_flags": QUALITY_FLAGS,
        "json_schemas": {
            "$comment": "JSON Schema Draft 2020-12 for the JSON files of the package",
            "manifest.json": {
                "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                "required": ["package_id", "run_id", "package_version", "created_at_utc", "producer",
                             "processing_status", "scientific_status", "counts", "files", "warnings"],
                "properties": {
                    "package_id": {"type": "string"}, "run_id": {"type": "string"},
                    "package_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
                    "created_at_utc": {"type": "string", "format": "date-time"},
                    "producer": {"type": "object", "required": ["name", "version"]},
                    "processing_status": {"enum": ["COMPLETED", "PARTIAL", "FAILED"]},
                    "scientific_status": {"enum": SCI_STATUS}, "is_template": {"type": "boolean"},
                    "counts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
                    "files": {"type": "array", "items": {"type": "object",
                              "required": ["path", "role", "format", "size_bytes", "sha256"],
                              "properties": {"sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}}},
                    "warnings": {"type": "array", "items": {"type": "string"}}}},
            "metadata/search_parameters.json": {
                "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                "required": ["run_id", "ra_center_deg", "dec_center_deg", "coordinate_frame", "coverage_status"],
                "properties": {
                    "ra_center_deg": {"type": "number", "minimum": 0, "exclusiveMaximum": 360},
                    "dec_center_deg": {"type": "number", "minimum": -90, "maximum": 90},
                    "fov_width_deg": num_or_null, "fov_height_deg": num_or_null,
                    "region_geometry": {"enum": ["CIRCLE", "BOX", "POLYGON", None]},
                    "time_start_utc": str_or_null, "time_end_utc": str_or_null,
                    "coverage_status": {"enum": ["AVAILABLE", "PARTIAL", "NO_COVERAGE", "NOT_CHECKED"]}}},
            "metadata/data_sources.json": {
                "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                "required": ["run_id", "sources"],
                "properties": {"sources": {"type": "array", "items": {"type": "object",
                               "required": ["source_id", "archive", "catalog", "retrieval_status"],
                               "properties": {"retrieval_status": {"enum": ["SUCCESS", "PARTIAL", "FAILED"]}}}}}},
        },
        "conventions": {
            "missing_csv_value": "empty field (never zero as missing)",
            "missing_json_value": "null",
            "timestamp": "ISO 8601 UTC with millisecond precision and 'Z'",
            "native_time": TIME_NOTE,
            "coordinates": "ICRS decimal degrees as given by the SPHEREx pipeline WCS",
            "ra_range": "[0,360)", "dec_range": "[-90,90]",
            "motion_rate_convention": "dra_cosdec_arcsec_h = d(RA)/dt * cos(Dec)",
            "position_angle": "degrees from north through east",
            "scientific_status": "PRELIMINARY until independently validated",
            "unmatched_meaning": "No match by performed checks, not a confirmed discovery",
            "identification_vs_detection": "crossmatches never modify observations or trajectories",
        },
        "compatibility": {
            "versioning": "semver for package_version and schema_version",
            "minor": "new optional files, columns or enum values; readers must ignore unknown columns",
            "major": "removed/renamed columns or changed meaning",
            "extensions_in_this_version": ["candidate_class VARIABLE (brightness change)",
                                           "manifest.contract", "search_parameters.applied_constraints"],
        },
    }


# ---------------------------------------------------------------------- VOTable / ECSV --
_VOT_TYPE = {S: "char", F: "double", I: "int", B: "boolean"}


def _votable(rows, path, run_id, prov) -> bytes:
    cols = TABLES[path]["columns"]
    tgt = prov.get("target", {})
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3" '
           'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
           '<RESOURCE type="results">',
           f"<DESCRIPTION>{escape(CONTRACT_NAME)} {run_id}: moving-object candidates from SPHEREx images, "
           f"target RA {tgt.get('ra_deg')} Dec {tgt.get('dec_deg')} deg, field {tgt.get('fov_deg')} deg. "
           f"PRELIMINARY.</DESCRIPTION>",
           f'<INFO name="run_id" value={quoteattr(run_id)}/>',
           f'<INFO name="package_version" value="{PACKAGE_VERSION}"/>',
           '<COOSYS ID="icrs" system="ICRS"/>',
           '<TABLE name="candidates">',
           "<DESCRIPTION>One row per candidate (same content as results/candidates.csv)</DESCRIPTION>"]
    for name, typ, unit, nullable, desc, extra in cols:
        a = f'name={quoteattr(name)} datatype="{_VOT_TYPE[typ]}"'
        if typ == S:
            a += ' arraysize="*"'
        if name in ("ra_ref_deg", "dec_ref_deg"):
            a += ' ref="icrs"'
        if extra.get("ucd"):
            a += f" ucd={quoteattr(extra['ucd'])}"
        if unit:
            a += f" unit={quoteattr(unit.replace('hour', 'h'))}"
        out.append(f"<FIELD {a}><DESCRIPTION>{escape(desc)}</DESCRIPTION></FIELD>")
    out.append("<DATA><TABLEDATA>")
    for r in rows:
        tds = []
        for name, typ, *_ in cols:
            v = r.get(name)
            if v is None:
                tds.append("<TD>NaN</TD>" if typ == F else "<TD/>")
            elif typ == B:
                tds.append(f"<TD>{'T' if v else 'F'}</TD>")
            elif typ == I:
                tds.append(f"<TD>{int(v)}</TD>")
            elif typ == F:
                tds.append(f"<TD>{float(v)!r}</TD>")
            else:
                tds.append(f"<TD>{escape(str(v))}</TD>")
        out.append("<TR>" + "".join(tds) + "</TR>")
    out += ["</TABLEDATA></DATA>", "</TABLE>", "</RESOURCE>", "</VOTABLE>"]
    return ("\n".join(out) + "\n").encode("utf-8")


_ECSV_TYPE = {S: "string", F: "float64", I: "int64", B: "bool"}


def _yaml_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ecsv(rows, path, run_id) -> bytes:
    cols = TABLES[path]["columns"]
    head = ["# %ECSV 1.0", "# ---", "# delimiter: ','", "# datatype:"]
    for name, typ, unit, nullable, desc, extra in cols:
        item = f"name: {name}"
        u = {"JD (UTC)": "d", "dimensionless": None}.get(unit, unit)      # лише одиниці, які розуміє astropy
        if u and typ in (F, I):
            item += f", unit: {_yaml_str(u.replace('hour', 'h'))}"
        item += f", datatype: {_ECSV_TYPE[typ]}, description: {_yaml_str(desc)}"
        head.append(f"# - {{{item}}}")
    head.append(f"# meta: {{run_id: {_yaml_str(run_id)}, package_version: {_yaml_str(PACKAGE_VERSION)}, "
                f"source: {_yaml_str('results/observations.csv')}}}")
    head.append("# schema: astropy-2.0")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([c[0] for c in cols])
    for r in rows:
        line = []
        for name, typ, *_ in cols:
            v = r.get(name)
            line.append("" if v is None else ("True" if v else "False") if typ == B else v)
        w.writerow(line)
    return ("\n".join(head) + "\n" + buf.getvalue()).encode("utf-8")


# --------------------------------------------------------------------------- README.md --
def _readme(run_id, prov, cands, obs, trajs, xms, warnings, status) -> str:
    t = prov.get("target", {})
    p = prov.get("parameters", {})
    by_status = {}
    for c in cands:
        by_status[c["identification_status"]] = by_status.get(c["identification_status"], 0) + 1
    lines = [
        f"# {CONTRACT_NAME} — {run_id}", "",
        f"**Package ID:** {run_id}  ",
        f"**Contract:** {CONTRACT_NAME} v{CONTRACT_VERSION} · package_version {PACKAGE_VERSION} · schema {SCHEMA_VERSION}  ",
        f"**Producer:** {PRODUCER} ({prov.get('tool')} {prov.get('version')})  ",
        f"**Processing status:** {status} · **Scientific status:** PRELIMINARY", "",
        "## What was searched", "",
        f"- Target: RA {t.get('ra_deg')}°, Dec {t.get('dec_deg')}° (ICRS), field {t.get('fov_deg')}° × {t.get('fov_deg')}°, "
        f"detector {t.get('detector')}",
        f"- Detection threshold {p.get('sigma')}σ; wavelength window {p.get('lam_min_um') or 0.75}–{p.get('lam_max_um') or 5.0} µm",
        f"- Data: SPHEREx images from NASA/IPAC IRSA, {len((prov.get('data') or {}).get('frames') or [])} frames, "
        f"releases {', '.join((prov.get('data') or {}).get('releases') or []) or 'n/a'}", "",
        "## Result", "",
        f"- {len(cands)} candidates, {len(obs)} observations, {len(trajs)} trajectories, {len(xms)} catalog matches",
        "- Identification: " + (", ".join(f"{k} {v}" for k, v in sorted(by_status.items())) or "none"), "",
        "## Files", "",
        "| File | Contents |", "|---|---|",
        "| `manifest.json` | inventory, statuses, counts, SHA-256 of every file |",
        "| `metadata/search_parameters.json` | what was searched, and the constraints actually applied |",
        "| `metadata/data_sources.json` | archives, tables, releases, exact ADQL, query times, row counts |",
        "| `metadata/schema.json` | column dictionary (types, units, nullability, enums, ranges), relations, JSON Schemas |",
        "| `metadata/processing_log.json` | algorithms, parameters, statistics, library versions, events |",
        "| `results/candidates.csv` | one row per candidate |",
        "| `results/observations.csv` | one row per astrometric measurement |",
        "| `results/trajectories.csv` | LINEAR_2D motion models (not orbits) |",
        "| `results/crossmatches.csv` | matches with Gaia DR3, AllWISE, SkyBoT — kept separate from detections |",
        "| `results/rejected_detections.csv` | observations of doubtful candidates and why |",
        "| `results/candidate_report.json` | full machine-readable report incl. the original API response |",
        "| `verification/*` | every external query and response; observer ephemeris (not loaded) |",
        "| `exchange/*` | VOTable, ECSV, JPL adapter requests (see exchange/README.md) |",
        "| `evidence/cutout_index.csv` | every SPHEREx image used, with its IRSA cutout URL |",
        "| `checksums.sha256` | `sha256sum -c checksums.sha256` verifies the package |", "",
        "## Limitations (read before using the data elsewhere)", "",
    ] + [f"- {w_}" for w_ in warnings] + [
        "", "Columns and units: see `metadata/schema.json`. Missing values are empty fields / null, never invented numbers.", ""]
    return "\n".join(lines)
