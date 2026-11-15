# SPHEREx Sky Motion Viewer

A public web tool that shows how the sky changes in NASA SPHEREx images.
You enter coordinates (or an object name) and it plays the SPHEREx images of that
patch of sky like a short video, so moving and changing objects are easy to spot.
Built for NASA Space Apps Challenge 2026: *Planet X and SPHEREx*.

## Quick start (Windows)

```bat
cd C:\path\to\project
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python api.py                 # one command; same as: python -m uvicorn api:app --port 5050
# python api.py --reload      # restart automatically when you edit the code
# python api.py --host 0.0.0.0  # let other computers in your network open it
```

Open **http://127.0.0.1:5050/**. The JSON API is documented at `/docs`.

**Before a demo**, warm the cache once so the five challenge targets open instantly:

```bat
python warmup.py
```

Why the first load takes time: the IRSA cutout service needs about 1–5 s per frame, and a
target has 50–250 frames. The site downloads 16 frames in parallel, shows each frame as soon
as it is ready (observation dates interleaved, so motion is visible within seconds), and caches
everything — a second visit is instant.

If you get `WinError 10013`, the port is blocked: use another one (`--port 9000`).

## Project layout

```
api.py              backend (FastAPI): data from IRSA + Gaia, processing, caching
warmup.py           pre-downloads the five challenge targets before a demo
detect.py           automatic search for moving / variable objects (used by api.py)
requirements.txt
static/index.html   page markup
static/style.css    styles
static/app.js       frontend logic (player, charts, overlays)
cache/              created automatically (downloaded cutouts, PNGs)
```

## Features

| Challenge item | How it works here |
|---|---|
| Enter RA/Dec, see images like a video | form accepts `269.452 4.693`, `08h55m10.8s -07d14m42s` or a name; player with speed, step, keyboard |
| Spot moving objects | all frames aligned to one north-up grid with a shared brightness scale |
| Easy to update with new releases | nothing hard-coded: every search goes live to IRSA (QR3 + DR1 today), list cached 12 h |
| Use the 102 bands / 6 detectors | detector filter D1–D6; **wavelength window** with presets (water ice ≈3.05 µm, methane ≈3.3 µm, CO₂ ≈4.27 µm, CO ≈4.67 µm); **Color** mode mixes 0.75–1.6 / 1.6–3.2 / 3.2–5.0 µm into blue/green/red, so cold brown dwarfs look red and hot stars blue; stacking by observation date for higher S/N |
| Spectra on the fly | click a point: brightness vs wavelength from every frame |
| Add / subtract epochs | *Stack by date* and *Clean difference* modes — the interface speaks only in date ranges (see below) |
| Choose field of view | 0.05° – 0.5° |
| Choose a time window | calendar *From / To date*; chips show exactly when SPHEREx observed the point (date ranges with frame counts). Only frames in the window are downloaded, shown, co-added, differenced and searched; the window is stored in the report |
| Overlay Gaia / WISE | Gaia DR3 stars and AllWISE sources with 10-year motion arrows, moved to each frame's date; hover a source for its name, brightness, proper motion and (Gaia) parallax/distance |
| Coordinates in the URL, embeddable | every view is a link; `&embed=1` shows only the viewer for iframes |
| Contamination flags | red flag overlay, flagged-pixel count and most common flag bits per frame |
| Variable stars | *Light curve* tab: brightness vs time, colored by detector |

## Dates instead of epochs

SPHEREx observes a point in a burst of a few days about every six months. The server still groups
those frames internally (that is how the detector pairs them), but the interface never shows the
groups: the calendar, the chips (“13–15 Aug 2025 · 34 frames”), the ribbon, *Stack by date*,
*Clean difference* (“Feb 2026 vs Aug 2025”), the spectrum legend and the detection results all use
date ranges. Every image endpoint takes a date range (`t0`, `t1`, MJD, end exclusive), and detection
returns `date_pairs` — the date ranges that were compared — next to the internal `epoch_pairs`.

The **Show epochs** checkbox under the calendar (URL `epochs=1`) brings the epoch numbers back for
users who want them: chips “Epoch 1 · 13–15 Aug 2025”, *Co-add by epoch*, ribbon “E2−E1”, an *Epoch*
row in *This frame*, the spectrum legend and the detection results. Off by default.

## Settings (on the page)

The **Settings** card in the sidebar holds everything a visitor may change; each choice is kept in the
link (`cov`, `fastmin`) and sent with every request that depends on it:

| Setting | Values | API parameter |
|---|---|---|
| Hide frames covering less than | 50 / **70** / 90 / 100 % of the field | `min_coverage` (frames/start), `min_cov` (coadd, diff, rgb, detect/map, detect, detect/package) |
| Asteroid search needs | 3 / 4 / **5** detections | `fast_min` (detect, detect/package) |
| Check IRSA for new frames now | button | `/api/search?fresh=1` (at most once a minute per point) |

Server-wide values (cache size, Gaia table, number of parallel IRSA downloads) stay environment
variables — on a public site a visitor must not change them for everyone; the card shows them read-only.

## A new search always wins

* Every request of a search (name lookup, IRSA search, frame download, Gaia, AllWISE, Find, spectrum)
  runs on an abortable channel. Starting a new search — Show the sky, an example, a wider date
  range — aborts all of them, and stops the browser loading images of the old target.
* The page has its own id (`client`). `frames/start` cancels the page's previous download on the
  server, and `frames/cancel?client=` does it even if the old job id never reached the page.
  A download nobody polls for 45 s (tab closed) stops by itself.
* Clicking another point while a spectrum is loading drops the older request too.

## When IRSA is slow or down

* The page waits for IRSA at most `IRSA_WAIT_S` (45 s). The query is **not** dropped: it finishes in
  the background and fills the cache, so pressing *Show the sky* again a minute later is instant.
  Clicking again meanwhile joins the running query instead of starting a second one. While waiting,
  the status line counts the seconds.
* Errors such as `502 Proxy Error` / `503` are retried 4 times (2, 4, 6 s apart; permanent errors
  are not). If IRSA still fails and this point was searched before, the saved frame list is used.

## Find never hangs on slow catalogs

Gaia, AllWISE and SkyBoT are queried **in parallel with the search itself**. Find waits for Gaia and
AllWISE at most `CAT_WAIT_S` (25 s) and for SkyBoT at most 30 s in total; whatever has not answered is
reported as "still loading", its checks become *inconclusive* (never a false *unmatched*), and the
query continues in the background into the cache — pressing Find a minute later includes it. The
Gaia / AllWISE overlays use the same limit (HTTP 504 with a clear message). The page shows a seconds
counter while Find runs.

## Object names

The five challenge targets resolve instantly (built in). Other names go to SIMBAD (then Sesame),
15 s at most each, and are cached for 30 days. Stars with a large proper motion are moved to the
middle of the SPHEREx data (`TARGET_EPOCH`, 2026.0): Barnard's Star has moved ~4.5′ north since
J2000, which would put it at the edge of a 0.1° field.

## Automatic search for moving objects (`detect.py`)

Button **Find** in the *Find moving objects* panel, or `GET /api/detect`.

1. **Same wavelength only.** Every frame of a later epoch is paired with the frame of the earlier
   epoch taken at the closest wavelength (|Δλ| < 0.03 µm, same detector), so a star's spectrum is
   never mistaken for a change.
2. **Noise removal.** Each difference is scaled to its noise (σ from MAD), then the median of all
   pairs is taken — cosmic rays and hot pixels, present in one frame only, drop out — and a
   star-sized filter is applied. This *clean difference* is also the *Clean difference* view.
3. **Detection.** Spots above kσ: white = appeared, black = disappeared. A disappear+appear pair of
   similar strength is a **moving object**; changes are measured with aperture photometry, so a
   slightly different PSF between epochs does not create false alarms. Single spots on a real source
   are **brighter / fainter** (variables). Positions are refined on each epoch's own image.
4. **Fast movers (asteroids)** are searched inside each epoch: each frame minus the median of the
   others; ≥3 detections on a straight line at constant speed form a track; one-pixel spikes
   (cosmic rays) are rejected.
5. Detections from several epoch pairs are merged into one track and **cross-matched**:
   Gaia DR3 and AllWISE (both moved to each date by proper motion) for stars, and
   **IMCCE SkyBoT** (known asteroids and comets at that place and time) for fast movers.
6. Every candidate gets one of three verdicts:
   - **known** — found in Gaia, AllWISE or SkyBoT;
   - **unmatched** — in no catalog *and* the signal is clean (worth reporting);
   - **inconclusive** — the reason is listed: seen in only one epoch pair, weak signal,
     near the field edge, next to a bright star, or a catalog check was unavailable.
     A candidate is never called unmatched if its catalog check failed.
7. **Report**: *Download CSV / JSON* in the panel. The JSON holds full provenance — archive, data
   releases, every frame used (id, epoch, MJD, detector, wavelength, URL), all thresholds,
   catalogs and how they were reached, run time — so a result can be checked and reproduced.
8. **Full data package** (`results_package.py`) — button **Download Full Data Package (ZIP)**, or
   `POST /api/package` (packs the result shown) / `GET /api/detect/package.zip` (re-runs the search).
   Built to the **SOLAR Candidate Data Package — Data Contract v1.0** (template `SOLAR_RUN_2026_0001`):

   ```
   SOLAR_RUN_<year>_<id>/
     manifest.json  README.md  checksums.sha256
     metadata/      search_parameters.json  data_sources.json  schema.json  processing_log.json
     results/       candidates.csv  observations.csv  trajectories.csv  crossmatches.csv
                    rejected_detections.csv  candidate_report.json
     verification/  external_queries.json  external_responses.json  observer_ephemeris.csv
     exchange/      candidates.vot (VOTable)  observations.ecsv (Astropy ECSV)  jpl_requests.json  README.md
     evidence/      cutout_index.csv  fits/
   ```

   * Same file set, CSV columns (names and order) and JSON keys as the contract template; `schema.json`
     adds types, units, enums, ranges, relations and JSON Schemas (Draft 2020-12).
   * One canonical data set → all formats: observations → LINEAR_2D trajectories → candidates;
     catalog identifications are separate rows in `crossmatches.csv` and never change a trajectory.
   * Mapping from the site's verdicts: Gaia or SkyBoT match → `KNOWN_MATCH`; AllWISE only →
     `POSSIBLE_MATCH` (positional proximity, not an identification); `unmatched` → `UNMATCHED`;
     `inconclusive` → `INCONCLUSIVE` (movers get `candidate_class` `UNCERTAIN`); catalogs off → `UNCHECKED`.
     Brightness changes use the extension class `VARIABLE`.
   * Unknown values stay empty (uncertainties, magnitudes, observer state) — nothing is invented.
     MPC ADES is **not** generated: the contract allows it only with spacecraft observer state,
     mid-exposure times and uncertainties. `jpl_requests.json` holds JPL SB-Ident adapter requests
     marked `ready_for_submission: false`.
   * `run_id` is derived from the content, so the same result always gets the same id;
     `sha256sum -c checksums.sha256` verifies the unpacked package.

**False fast movers.** Hot pixels sit at fixed places on the detector, so on the sky they jump from
frame to frame and a few of them line up by chance; frame edges do the same. A fast-mover track
therefore needs ≥ 5 detections (one per frame), must be seen in ≥ 60 % of the frames where it should
be, and only pixels covered by ≥ 80 % of the frames (minus a 2-pixel margin) are searched.
`test/test_edges.py` (no real movers, jumping hot pixels, slanted frame edges): ~100 false tracks
before, 0 after; the synthetic asteroid is still found.

Validated on synthetic skies with a known answer (moving star, variable, asteroid, cosmic rays,
changing PSF, cut corners): 0 false detections in 20 random skies; the moving star's speed is
recovered within 1%.

## URL parameters

`target`, `fov`, `det` (`all`, `D1`…`D6`), `view` (`frames`, `coadd`, `diff`, `rgb`), `lam` (e.g. `3.2,3.45`),
`speed` (fps), `bright`, `contrast` (%), `gaia=1`, `wise=1`, `flags=1`, `embed=1`, `epochs=1`, `from`, `to` (YYYY-MM-DD).

Example: `/?target=Barnard's Star&fov=0.25&det=D2&view=diff&gaia=1`

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/resolve?name=` | name or sexagesimal string -> RA/Dec |
| `GET /api/search?ra=&dec=` | SPHEREx frames covering the point + `windows`: observation date ranges (`t0`, `t1` MJD, ISO dates, frame counts) |
| `GET /api/frames/start` · `/status?job=&since=` · `/cancel?job=` | background loading; the page polls and shows frames as they arrive |
| `GET /api/frames?ra=&dec=&fov=&det=&epoch=` | same, blocking (for scripts) |
| `GET /api/frame/{id}.png` | one aligned frame |
| `GET /api/coadd.png?t0=&t1=` / `GET /api/diff.png?a_t0=&a_t1=&b_t0=&b_t1=` | stack of a date range / later range minus earlier range (MJD); optional `lam_min`, `lam_max`. Legacy `epoch`, `a`, `b` still accepted |
| `GET /api/rgb.png?t0=&t1=&b=&g=&r=` | color composite of a date range from three wavelength ranges |
| `GET /api/spectrum?x_ra=&x_dec=` | brightness of a point in every frame |
| `GET /api/flags/{id}.png` / `.json` | flag overlay / flag-bit statistics |
| `GET /api/detect?sigma=&t0=&t1=` | automatic search: candidates with verdicts + provenance (t0/t1 = MJD window; also accepted by frames/start, coadd, diff, rgb, detect/map) |
| `GET /api/detect/map.png?a_t0=&a_t1=&b_t0=&b_t1=` | clean difference of two date ranges (same wavelengths) |
| `GET /api/detect/package.zip` | same parameters as `/api/detect`; returns the SOLAR data package (ZIP) |
| `POST /api/package` | body = a `/api/detect` result; returns it as the SOLAR data package (ZIP) |
| `GET /api/fits/{id}` | original cutout (MEF) |
| `GET /api/gaia?gmax=` | Gaia DR3 stars (bright + fast movers) at epoch 2016.0, with parallax |
| `GET /api/wise?wmax=` | AllWISE sources (W1 < 15) at epoch 2010.5 — VizieR, IRSA fallback |

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `CACHE_DIR` | `cache` | where downloads are stored |
| `WORKERS` | `16` | processing threads (alignment, PNG) |
| `IRSA_CONCURRENCY` | `6` | simultaneous downloads from IRSA; on HTTP 503/429 the server waits 2–16 s and retries |
| `SEARCH_TTL_S` | `43200` | how long a frame list is cached (IRSA adds data weekly) |
| `GAIA_TABLE` | `gaiadr3.gaia_source` | switch to Gaia DR4 when released (2 Dec 2026) |
| `CACHE_MAX_GB` | `2` | cache size limit; oldest files are deleted above it |
| `MIN_COVERAGE` | `0.7` | a frame is shown only if it covers this share of the field **and** the whole central 40% (where the target is) |
| `SPHEREX_BAD_BITS` | `0` | flag bits to blank out (e.g. `0x10`); 0 = show everything |

## Test targets

Barnard's Star · Epsilon Indi B (try D5–D6) · WISE 0855−0714 (D6, co-add) ·
RW Aurigae and R Draconis (Light curve tab).

## Data and credits

- SPHEREx spectral images from the NASA/IPAC Infrared Science Archive (IRSA).
  *This publication makes use of data products from the Spectro-Photometer for the History
  of the Universe, Epoch of Reionization and Ices Explorer (SPHEREx), which is a joint project
  of the Jet Propulsion Laboratory and the California Institute of Technology, and is funded
  by the National Aeronautics and Space Administration.*
- Gaia DR3, ESA — DOI 10.5270/esa-qa4lep3.
- Built with Astropy, Astroquery, reproject, FastAPI; sky atlas link to Aladin Lite (CDS).

### When IRSA is down
If IRSA does not answer, the site uses a saved frame list: first the one for this exact point,
otherwise the one for a point up to 15′ away (SPHEREx frames are ~3.5° wide, so it is the same set
of frames). Example: Barnard's Star is now searched at its 2026 position, 4.5′ from J2000; an old
J2000 list still works. The status line says which list is used. After a failure the server waits
60 s before asking IRSA again, so clicks don't hang. Run `python warmup.py` on a good connection
before a demo — then the example objects work even with IRSA offline.

### Running on Flask
The same site and API also run on Flask — all logic stays in `api.py`, `flask_app.py` registers
every `/api/...` route in Flask:

    pip install flask waitress
    python flask_app.py                  # http://127.0.0.1:5050/
    python flask_app.py --host 0.0.0.0   # open to other computers in the network

For WSGI hosting (PythonAnywhere, gunicorn, waitress-serve) the app object is `flask_app:app`.
FastAPI must stay installed (api.py imports it); `/docs` exists only with `python api.py`.

### No time limit on the IRSA search
`/api/search` never fails just because IRSA is slow. Each request waits up to 20 s (`SEARCH_POLL_S`)
and, if IRSA has not answered yet, returns `{"pending": true, "elapsed_s": …}`; the site immediately
asks again and keeps showing a seconds counter. On the server there is one IRSA query per sky point,
running until IRSA answers. Short HTTP requests also survive tunnels and hosts that cut long requests.
If a saved frame list exists (this point earlier, or a point up to 15′ away), it is shown after
5 s (`SAVED_WAIT_S`) while the IRSA query finishes in the background and refreshes the cache.
An IRSA attempt that hangs longer than 180 s (`IRSA_RESTART_S`) gets a second attempt next to it.

### Find, overlays and spectrum when archives are slow
- Find, the spectrum, frame images and stacks never wait for the IRSA search: if any saved frame
  list exists (this point or one up to 15′ away) they use it at once and IRSA is asked in the
  background (at most once a minute after a failure).
- Gaia: ESA Gaia Archive -> VizieR (CDS, Strasbourg) -> VizieR mirror at CfA (Harvard).
  AllWISE: VizieR (CDS) -> VizieR CfA mirror -> NOIRLab Astro Data Lab (`allwise.source`) -> IRSA.
  The next source starts when the previous one fails or stays silent for 6 s / 8 s
  (`GAIA_HEAD_S`, `WISE_HEAD_S`); the first answer wins and is named in the overlay status and
  in the data package. The console names each failed source with its HTTP code.
- Both overlays load in parallel with the frames. If a catalog answers 504 ("still loading in the
  background"), the page retries by itself every 20 s for ~3 minutes.
- Spectrum: click a star — near a Gaia star (or the target itself) the click snaps to it, and for a
  moving star the aperture follows its proper motion in every frame (Barnard's Star moves ~1.7 SPHEREx
  pixels a year). The wavelength is measured at the star's own position on the detector. Selecting a
  Find candidate measures it along its track. The spectrum refreshes when all frames have loaded.
  `/api/spectrum` takes `pmra`, `pmdec` (mas/yr) and `pm_year` and reports skipped frames.

