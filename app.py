import os
import re
import shutil
import hashlib
import json
import time
import uuid
import shlex
import socket
import queue
import hmac
import secrets
import logging
import datetime
import ipaddress
import threading
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, Response,
    session, abort, send_file,
)
import docker
from docker.errors import NotFound

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zim-forge")


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# --- Configuration ---------------------------------------------------------
# LIBRARY_HOST_PATH must be the path as seen by the DOCKER HOST (Dockge/Docker
# daemon), NOT a path inside this app's own container. This app talks to the
# host's Docker socket and spawns sibling containers directly on the host, so
# any volume it hands to those sibling containers has to be a host path.
# On Docker Desktop for Windows that means a Windows path, e.g. C:\kiwix\library.
LIBRARY_HOST_PATH = os.environ.get("LIBRARY_HOST_PATH", "").strip()
if not LIBRARY_HOST_PATH:
    raise SystemExit(
        "LIBRARY_HOST_PATH is not set. Set it to the folder ON THE DOCKER HOST "
        "where finished ZIM files should be written (your Kiwix library)."
    )

APP_DATA_DIR = os.environ.get("APP_DATA_DIR", "/data")
JOBS_FILE = os.path.join(APP_DATA_DIR, "jobs.json")
SECRET_KEY_FILE = os.path.join(APP_DATA_DIR, "secret_key")
LOG_DIR = os.path.join(APP_DATA_DIR, "logs")

ZIMIT_IMAGE = os.environ.get("ZIMIT_IMAGE", "ghcr.io/openzim/zimit:latest")
MWOFFLINER_IMAGE = os.environ.get("MWOFFLINER_IMAGE", "ghcr.io/openzim/mwoffliner:latest")
# Zimit drives Chromium, which crashes on Docker's default 64 MB /dev/shm.
ZIMIT_SHM_SIZE = os.environ.get("ZIMIT_SHM_SIZE", "1g")
# 0 = no limit. Otherwise jobs running longer than this are stopped and marked failed.
JOB_TIMEOUT_HOURS = float(os.environ.get("JOB_TIMEOUT_HOURS", "0") or 0)
# Finished jobs beyond this count are pruned (oldest first).
MAX_JOBS_KEPT = int(os.environ.get("MAX_JOBS_KEPT", "100"))
# By default, refuse URLs that resolve to private/loopback/link-local addresses.
ALLOW_PRIVATE_URLS = env_bool("ALLOW_PRIVATE_URLS", False)
# Optional HTTP basic auth. Strongly recommended if the port is reachable by
# anything other than you: this app controls the Docker daemon.
AUTH_USERNAME = os.environ.get("ZIMFORGE_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("ZIMFORGE_PASSWORD", "")
# Optional: where the library folder is mounted INSIDE this app's container.
# When set, ZIM Forge checks free disk space before a job and verifies that a
# ZIM file was actually produced (recording its size and SHA-256) afterwards.
LIBRARY_PATH = os.environ.get("LIBRARY_PATH", "").strip()
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "5") or 0)
# Check the registry for a newer scraper image before each job.
PULL_IMAGES = env_bool("PULL_IMAGES", True)
# Optional caps on scraper containers, e.g. SCRAPER_MEMORY=8g SCRAPER_CPUS=4
SCRAPER_MEMORY = os.environ.get("SCRAPER_MEMORY", "").strip()
SCRAPER_CPUS = float(os.environ.get("SCRAPER_CPUS", "0") or 0)
# Before starting a scraper, check the site isn't serving a bot challenge.
BOT_CHECK = env_bool("BOT_CHECK", True)
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))

MAX_LOG_LINES = 500
SAVE_INTERVAL_SECONDS = 5
MAX_LOG_LINE_CHARS = 2000

SCRAPERS = ("zimit", "mwoffliner")
ACTIVE_STATUSES = ("queued", "running")
FINISHED_STATUSES = ("success", "failed", "cancelled")

# Flags ZIM Forge sets itself. Letting "Advanced options" override them would
# send output somewhere other than the library, or desync the job record.
RESERVED_FLAGS = {
    "zimit": {"--name", "--output", "--zim-file"},
    "mwoffliner": {"--mwUrl", "--adminEmail", "--outputDirectory", "--filenamePrefix"},
}

NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
docker_client = docker.from_env()

jobs = {}
jobs_lock = threading.Lock()   # guards `jobs`
save_lock = threading.Lock()   # serializes writes to JOBS_FILE; always taken BEFORE jobs_lock
job_queue = queue.Queue()
_dirty = False
_last_save = 0.0


def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- Secret key (for sessions / CSRF) ----------------------------------------
def load_secret_key():
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE) as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w") as f:
        f.write(key)
    return key


# --- Persistence -------------------------------------------------------------
def load_jobs():
    global jobs
    if not os.path.exists(JOBS_FILE):
        return
    try:
        with open(JOBS_FILE) as f:
            jobs = json.load(f)
    except Exception:
        # Don't silently throw history away: keep the broken file for inspection.
        backup = f"{JOBS_FILE}.corrupt-{int(time.time())}"
        os.replace(JOBS_FILE, backup)
        log.exception("Could not read %s; moved it to %s and starting empty", JOBS_FILE, backup)
        jobs = {}


def save_jobs(force=True):
    """Write jobs to disk atomically.

    With force=False the write is skipped if one happened recently; the
    background flusher picks up the change within SAVE_INTERVAL_SECONDS.
    """
    global _dirty, _last_save
    with save_lock:
        with jobs_lock:
            if not force and time.monotonic() - _last_save < SAVE_INTERVAL_SECONDS:
                _dirty = True
                return
            snapshot = json.dumps(jobs, indent=2)
            _dirty = False
            _last_save = time.monotonic()
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(snapshot)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, JOBS_FILE)


def flusher_loop():
    while True:
        time.sleep(SAVE_INTERVAL_SECONDS)
        if _dirty:
            try:
                save_jobs()
            except Exception:
                log.exception("Saving jobs failed")


def prune_jobs_locked():
    finished = [j for j in jobs.values() if j["status"] in FINISHED_STATUSES]
    excess = len(finished) - MAX_JOBS_KEPT
    if excess > 0:
        finished.sort(key=lambda j: j["created_at"])
        for j in finished[:excess]:
            del jobs[j["id"]]
            try:
                os.remove(log_file_path(j["id"]))
            except OSError:
                pass


# --- Job helpers -------------------------------------------------------------
def new_job(url, name, scraper, admin_email, extra_args):
    job_id = uuid.uuid4().hex[:8]
    job = {
        "id": job_id,
        "url": url,
        "name": name,
        "scraper": scraper,
        "admin_email": admin_email,
        "extra_args": extra_args,
        "status": "queued",
        "created_at": utcnow_iso(),
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "log": [],
        "container_id": None,
        "cancel_requested": False,
        # Provenance: exactly what ran, and what it produced.
        "image": None,
        "image_digest": None,
        "command": None,
        "outputs": [],
    }
    with jobs_lock:
        jobs[job_id] = job
        prune_jobs_locked()
    save_jobs()
    job_queue.put(job_id)
    return job_id


def log_file_path(job_id):
    return os.path.join(LOG_DIR, f"{job_id}.log")


def append_log(job_id, line):
    """Add a line to the job's log: the complete log on disk, the tail in memory."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(log_file_path(job_id), "a", encoding="utf-8") as f:
            f.write(f"{utcnow_iso()} {line}\n")
    except OSError:
        pass
    if len(line) > MAX_LOG_LINE_CHARS:
        line = line[:MAX_LOG_LINE_CHARS] + " …"
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["log"].append(line)
        if len(job["log"]) > MAX_LOG_LINES:
            del job["log"][:-MAX_LOG_LINES]
    save_jobs(force=False)


def finish_job(job_id, status, exit_code=None):
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            job["status"] = status
            job["exit_code"] = exit_code
            job["finished_at"] = utcnow_iso()
            job.pop("zims_before", None)
    save_jobs()


def public_job(job):
    """Job fields for the browser: everything except the (large) log."""
    return {k: v for k, v in job.items() if k not in ("log", "zims_before")}


# --- Input validation --------------------------------------------------------
def sanitize_name(raw):
    return NAME_RE.sub("_", raw).strip("._-")[:80]


def default_name(parsed_url):
    segments = [s for s in parsed_url.path.split("/") if s]
    base = segments[-1] if segments else parsed_url.hostname or "site"
    base = re.sub(r"\.(html?|php|aspx?)$", "", base, flags=re.IGNORECASE)
    return sanitize_name(base) or sanitize_name(parsed_url.hostname or "") or "site"


def check_public_host(hostname):
    """Return an error string if hostname resolves to a non-public address."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return f"Could not resolve host “{hostname}”."
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not addr.is_global:
            return (f"“{hostname}” resolves to a private/internal address ({addr}). "
                    "Set ALLOW_PRIVATE_URLS=true if you really want to archive internal sites.")
    return None


def validate_submission(form):
    """Returns (cleaned_values, errors)."""
    errors = []
    url = form.get("url", "").strip()
    name = form.get("name", "").strip()
    scraper = form.get("scraper", "zimit")
    admin_email = form.get("admin_email", "").strip()
    extra_args = form.get("extra_args", "").strip()

    if scraper not in SCRAPERS:
        errors.append("Unknown scraper.")
        scraper = "zimit"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        errors.append("URL must be a full http:// or https:// address.")
    elif not ALLOW_PRIVATE_URLS:
        err = check_public_host(parsed.hostname)
        if err:
            errors.append(err)

    if name:
        clean = sanitize_name(name)
        if not clean:
            errors.append("Name must contain letters or numbers.")
        name = clean
    elif parsed.hostname:
        name = default_name(parsed)

    if scraper == "mwoffliner":
        if not admin_email:
            errors.append("mwoffliner needs a real admin email so wiki operators can contact you.")
        elif not EMAIL_RE.match(admin_email):
            errors.append("Admin email doesn't look valid.")
    else:
        admin_email = ""

    if extra_args:
        try:
            tokens = shlex.split(extra_args)
        except ValueError as e:
            errors.append(f"Advanced options can't be parsed ({e}). Check your quotes.")
        else:
            reserved = RESERVED_FLAGS[scraper]
            used = sorted({t.split("=", 1)[0] for t in tokens if t.split("=", 1)[0] in reserved})
            if used:
                errors.append(f"Advanced options can't set {', '.join(used)}; ZIM Forge sets those itself.")

    if name and not errors:
        with jobs_lock:
            clash = any(j["name"] == name and j["status"] in ACTIVE_STATUSES for j in jobs.values())
        if clash:
            errors.append(f"A job named “{name}” is already queued or running; its output would be overwritten.")

    values = {"url": url, "name": name, "scraper": scraper,
              "admin_email": admin_email, "extra_args": extra_args}
    return values, errors


# --- Scraper invocations -----------------------------------------------------
def build_run(job):
    """Returns (image, command, extra docker run kwargs) for a job."""
    extra = shlex.split(job["extra_args"]) if job.get("extra_args") else []
    if job["scraper"] == "mwoffliner":
        command = [
            "mwoffliner",
            f"--mwUrl={job['url']}",
            f"--adminEmail={job['admin_email']}",
            "--outputDirectory=/output",
            f"--filenamePrefix={job['name']}",
        ] + extra
        return MWOFFLINER_IMAGE, command, {}
    command = ["zimit", "--seeds", job["url"], "--name", job["name"], "--output", "/output"] + extra
    return ZIMIT_IMAGE, command, {"shm_size": ZIMIT_SHM_SIZE}


def ensure_image(job_id, image):
    """Make sure the scraper image is present (and current); returns its digest."""
    if PULL_IMAGES:
        append_log(job_id, f"Checking for the latest {image} (a first download can take several minutes)…")
        try:
            docker_client.images.pull(image)
        except Exception as e:
            try:
                docker_client.images.get(image)
            except Exception:
                raise RuntimeError(f"could not download {image}: {e}")
            append_log(job_id, f"Couldn't check for a newer image ({e}); using the local copy.")
    img = docker_client.images.get(image)
    digests = img.attrs.get("RepoDigests") or []
    return digests[0] if digests else img.id


def start_container(job_id, job):
    image, command, extra_kwargs = build_run(job)
    digest = ensure_image(job_id, image)
    with jobs_lock:
        live = jobs.get(job_id)
        if live:
            live.update(image=image, image_digest=digest, command=command)
            if live["cancel_requested"]:
                return None
    append_log(job_id, f"Image: {digest}")
    append_log(job_id, "Command: " + shlex.join(command))
    kwargs = {
        "volumes": {LIBRARY_HOST_PATH: {"bind": "/output", "mode": "rw"}},
        "detach": True,
        "name": f"zimforge-{job_id}",
        "labels": {"zim-forge.job": job_id},
        **extra_kwargs,
    }
    if SCRAPER_MEMORY:
        kwargs["mem_limit"] = SCRAPER_MEMORY
    if SCRAPER_CPUS > 0:
        kwargs["nano_cpus"] = int(SCRAPER_CPUS * 1e9)
    return docker_client.containers.run(image, command=command, **kwargs)


# --- Output verification -----------------------------------------------------
def library_available():
    return bool(LIBRARY_PATH) and os.path.isdir(LIBRARY_PATH)


def list_zims():
    """{filename: [size, mtime_ns]} for ZIM files in the library folder."""
    found = {}
    for entry in os.scandir(LIBRARY_PATH):
        if entry.name.lower().endswith(".zim") and entry.is_file():
            st = entry.stat()
            found[entry.name] = [st.st_size, st.st_mtime_ns]
    return found


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def preflight_disk(job_id):
    """Returns an error string if there's too little free space, else None."""
    free_gb = shutil.disk_usage(LIBRARY_PATH).free / 1024 ** 3
    append_log(job_id, f"Free space in library: {free_gb:.1f} GB")
    if free_gb < MIN_FREE_GB:
        return (f"Only {free_gb:.1f} GB free in the library folder; at least {MIN_FREE_GB:g} GB "
                "is required (MIN_FREE_GB). Free up space and run the job again.")
    return None


def collect_outputs(job_id):
    """Find ZIM files this job created or changed; record size and checksum.

    Returns the list of outputs, or None if the library isn't mounted.
    """
    if not library_available():
        return None
    with jobs_lock:
        before = (jobs.get(job_id) or {}).get("zims_before") or {}
    outputs = []
    for name, stat in sorted(list_zims().items()):
        if before.get(name) == stat:
            continue
        append_log(job_id, f"Verifying {name} ({human_size(stat[0])})…")
        digest = sha256_file(os.path.join(LIBRARY_PATH, name))
        outputs.append({"file": name, "size": stat[0], "sha256": digest})
        append_log(job_id, f"Output: {name}  {human_size(stat[0])}  sha256={digest}")
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            job["outputs"] = outputs
    return outputs


BOT_CHECK_MESSAGE = (
    "This site is behind a bot-protection challenge ({detail}), so the scraper would be "
    "blocked. ZIM Forge does not try to get around this. Ask the site's admins to allow "
    "your crawler (or for a database dump), then try again."
)


def bot_protection_check(url, contact=""):
    """Make one plain request to the seed URL and detect a Cloudflare-style challenge.

    Returns an explanation string if the site is challenging automated clients,
    otherwise None. Network errors return None: the scraper will report those itself.
    """
    import urllib.request
    import urllib.error

    agent = "ZIM-Forge/1.0 (offline archiving for Kiwix"
    agent += f"; contact {contact})" if contact else ")"
    req = urllib.request.Request(url, headers={"User-Agent": agent})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            headers, status = resp.headers, resp.status
            body = resp.read(65536)
    except urllib.error.HTTPError as e:
        headers, status = e.headers, e.code
        body = e.read(65536) if e.fp else b""
    except Exception:
        return None
    mitigated = (headers.get("cf-mitigated") or "").lower()
    if mitigated == "challenge" or (status in (403, 429, 503) and (
            b"Just a moment..." in body or b"challenges.cloudflare.com" in body)):
        return BOT_CHECK_MESSAGE.format(detail=f"Cloudflare, HTTP {status}")
    return None


def stop_container_async(container_id):
    def _stop():
        try:
            docker_client.containers.get(container_id).stop(timeout=30)
        except NotFound:
            pass
        except Exception:
            log.exception("Stopping container %s failed", container_id)
    threading.Thread(target=_stop, daemon=True).start()


# --- Worker: runs one job at a time ------------------------------------------
def stream_logs(job_id, container, since=None):
    """Feed container output into the job log, split into real lines."""
    buf = b""
    kwargs = {"stream": True, "follow": True}
    if since is not None:
        kwargs["since"] = since
    for chunk in container.logs(**kwargs):
        buf += chunk
        *lines, buf = re.split(rb"\r\n|\n|\r", buf)
        for line in lines:
            if line.strip():
                append_log(job_id, line.decode(errors="replace").rstrip())
    if buf.strip():
        append_log(job_id, buf.decode(errors="replace").rstrip())


def supervise(job_id, container, reattached=False):
    """Follow a started container to completion, then record the result and remove it."""
    timed_out = threading.Event()
    timer = None
    if JOB_TIMEOUT_HOURS > 0:
        with jobs_lock:
            started = jobs[job_id].get("started_at")
        elapsed = 0.0
        if started:
            started_dt = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
            elapsed = (datetime.datetime.now(datetime.timezone.utc) - started_dt).total_seconds()
        remaining = max(1.0, JOB_TIMEOUT_HOURS * 3600 - elapsed)

        def _timeout():
            timed_out.set()
            append_log(job_id, f"Time limit of {JOB_TIMEOUT_HOURS:g}h reached; stopping.")
            stop_container_async(container.id)
        timer = threading.Timer(remaining, _timeout)
        timer.daemon = True
        timer.start()

    try:
        stream_logs(job_id, container, since=int(time.time()) if reattached else None)
        exit_code = container.wait().get("StatusCode", 1)
        with jobs_lock:
            cancelled = jobs.get(job_id, {}).get("cancel_requested")
        if cancelled:
            append_log(job_id, "Cancelled.")
            finish_job(job_id, "cancelled", exit_code)
        elif timed_out.is_set():
            finish_job(job_id, "failed", exit_code)
        elif exit_code != 0:
            append_log(job_id, f"Scraper exited with code {exit_code}.")
            finish_job(job_id, "failed", exit_code)
        else:
            outputs = collect_outputs(job_id)
            if outputs is not None and not outputs:
                append_log(job_id, "Scraper exited cleanly but no new ZIM file appeared in the library. "
                                   "Treating this as a failure.")
                finish_job(job_id, "failed", exit_code)
            else:
                finish_job(job_id, "success", exit_code)
    finally:
        if timer:
            timer.cancel()
        try:
            container.remove(force=True)
        except Exception:
            log.exception("Removing container %s failed", container.id)


def run_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        status = job["status"]
        container_id = job.get("container_id")
        if status == "queued":
            job["status"] = "running"
            job["started_at"] = utcnow_iso()
        spec = dict(job)

    if status == "running" and container_id:
        reattach(job_id, container_id)
        return
    if status != "queued":
        return  # e.g. cancelled while waiting in the queue
    save_jobs()

    if BOT_CHECK:
        append_log(job_id, f"Checking {spec['url']} is reachable by a crawler…")
        problem = bot_protection_check(spec["url"], spec.get("admin_email", ""))
        if problem:
            append_log(job_id, problem)
            finish_job(job_id, "failed")
            return

    if library_available():
        problem = preflight_disk(job_id)
        if problem:
            append_log(job_id, problem)
            finish_job(job_id, "failed")
            return
        with jobs_lock:
            job["zims_before"] = list_zims()

    container = None
    try:
        container = start_container(job_id, spec)
        if container is None:  # cancelled while the image was downloading
            append_log(job_id, "Cancelled.")
            finish_job(job_id, "cancelled")
            return
        with jobs_lock:
            job["container_id"] = container.id
            cancel_now = job["cancel_requested"]
        save_jobs()
        if cancel_now:  # cancel clicked while the container was being created
            stop_container_async(container.id)
    except Exception as e:
        append_log(job_id, f"ERROR starting scraper: {e}")
        finish_job(job_id, "failed")
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
        return

    try:
        supervise(job_id, container)
    except Exception as e:
        append_log(job_id, f"ERROR: {e}")
        finish_job(job_id, "failed")


def reattach(job_id, container_id):
    """Pick a job back up after the app restarted while it was running."""
    try:
        container = docker_client.containers.get(container_id)
    except NotFound:
        append_log(job_id, "Interrupted by app restart (scraper container is gone).")
        finish_job(job_id, "failed")
        return
    except Exception as e:
        append_log(job_id, f"Interrupted by app restart; couldn't check container: {e}")
        finish_job(job_id, "failed")
        return

    if container.status == "running":
        append_log(job_id, "App restarted; reattached to running scraper "
                           "(output produced while the app was down isn't shown).")
        try:
            supervise(job_id, container, reattached=True)
        except Exception as e:
            append_log(job_id, f"ERROR: {e}")
            finish_job(job_id, "failed")
    else:
        exit_code = container.attrs.get("State", {}).get("ExitCode", 1)
        append_log(job_id, f"Scraper finished while the app was down (exit code {exit_code}).")
        ok = exit_code == 0
        if ok:
            outputs = collect_outputs(job_id)
            if outputs is not None and not outputs:
                append_log(job_id, "No new ZIM file appeared in the library. Treating this as a failure.")
                ok = False
        finish_job(job_id, "success" if ok else "failed", exit_code)
        try:
            container.remove(force=True)
        except Exception:
            pass


def worker_loop():
    while True:
        job_id = job_queue.get()
        try:
            run_job(job_id)
        except Exception:
            log.exception("Job %s crashed the worker step", job_id)


def recover_jobs():
    """Requeue work that was in flight when the app last stopped.

    Running jobs go first so they're reattached before anything new starts,
    keeping to one scraper at a time.
    """
    with jobs_lock:
        in_flight = sorted(
            (j for j in jobs.values() if j["status"] in ACTIVE_STATUSES),
            key=lambda j: (j["status"] != "running", j["created_at"]),
        )
        for job in in_flight:
            if job["status"] == "running" and not job.get("container_id"):
                job["status"] = "queued"  # died before the container started; just retry
            job_queue.put(job["id"])
    save_jobs()


# --- Auth & CSRF -------------------------------------------------------------
@app.before_request
def require_auth():
    if not AUTH_PASSWORD:
        return None
    auth = request.authorization
    if (auth and auth.username is not None and auth.password is not None
            and hmac.compare_digest(auth.username, AUTH_USERNAME)
            and hmac.compare_digest(auth.password, AUTH_PASSWORD)):
        return None
    return Response("Login required.", 401, {"WWW-Authenticate": 'Basic realm="ZIM Forge"'})


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_protected(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Invalid or missing CSRF token. Reload the page and try again.")
        return view(*args, **kwargs)
    return wrapper


app.jinja_env.globals["csrf_token"] = csrf_token


# --- Routes ------------------------------------------------------------------
def sorted_public_jobs():
    with jobs_lock:
        return [public_job(j) for j in sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)]


@app.route("/")
def index():
    return render_template("index.html", jobs=sorted_public_jobs(), errors=[], form={})


@app.route("/submit", methods=["POST"])
@csrf_protected
def submit():
    values, errors = validate_submission(request.form)
    if errors:
        return render_template("index.html", jobs=sorted_public_jobs(), errors=errors, form=values), 400
    new_job(**values)
    return redirect(url_for("index"))


@app.route("/jobs")
def jobs_json():
    return jsonify(sorted_public_jobs())


@app.route("/logs/<job_id>")
def logs(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        text = "\n".join(job["log"]) if job else None
    if text is None:
        return "no such job", 404
    return Response(text, mimetype="text/plain")


@app.route("/logs/<job_id>/full")
def full_log(job_id):
    with jobs_lock:
        job = jobs.get(job_id)  # also guarantees job_id is a real id, not a path
        name = job["name"] if job else None
    if not job or not os.path.exists(log_file_path(job_id)):
        return "no log file for this job", 404
    return send_file(log_file_path(job_id), mimetype="text/plain", as_attachment=True,
                     download_name=f"{name}-{job_id}.log")


@app.route("/jobs/<job_id>/retry", methods=["POST"])
@csrf_protected
def retry(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="no such job"), 404
        if job["status"] not in FINISHED_STATUSES:
            return jsonify(error=f"job is still {job['status']}"), 409
        if any(j["name"] == job["name"] and j["status"] in ACTIVE_STATUSES for j in jobs.values()):
            return jsonify(error="a job with this name is already queued or running"), 409
        params = {k: job[k] for k in ("url", "name", "scraper", "admin_email", "extra_args")}
    return jsonify(ok=True, id=new_job(**params))


@app.route("/jobs/<job_id>/cancel", methods=["POST"])
@csrf_protected
def cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="no such job"), 404
        if job["status"] not in ACTIVE_STATUSES:
            return jsonify(error=f"job is already {job['status']}"), 409
        job["cancel_requested"] = True
        container_id = job.get("container_id")
        if job["status"] == "queued":
            job["status"] = "cancelled"
            job["finished_at"] = utcnow_iso()
    save_jobs()
    if container_id:
        append_log(job_id, "Cancel requested; stopping scraper…")
        stop_container_async(container_id)
    return jsonify(ok=True)


# --- Startup -----------------------------------------------------------------
def start_background():
    app.secret_key = load_secret_key()
    load_jobs()
    recover_jobs()
    threading.Thread(target=worker_loop, daemon=True, name="worker").start()
    threading.Thread(target=flusher_loop, daemon=True, name="flusher").start()
    if LIBRARY_PATH and not os.path.isdir(LIBRARY_PATH):
        log.warning("LIBRARY_PATH=%s is not a folder inside this container; disk-space checks "
                    "and output verification are disabled.", LIBRARY_PATH)
    elif not LIBRARY_PATH:
        log.info("LIBRARY_PATH is not set; disk-space checks and output verification are disabled.")
    if not AUTH_PASSWORD:
        log.warning("ZIMFORGE_PASSWORD is not set: anyone who can reach port %s can start "
                    "containers on this Docker host. Set a password unless the port is private.", PORT)


if __name__ == "__main__":
    start_background()
    from waitress import serve
    log.info("ZIM Forge listening on %s:%s", HOST, PORT)
    serve(app, host=HOST, port=PORT, threads=8)
