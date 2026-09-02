import os
import json
import uuid
import shlex
import threading
import queue
import datetime

from flask import Flask, render_template, request, redirect, url_for, jsonify, Response
import docker

# --- Configuration ---------------------------------------------------------
# LIBRARY_HOST_PATH must be the path as seen by the DOCKER HOST (Dockge/Docker
# daemon), NOT a path inside this app's own container. This app talks to the
# host's Docker socket and spawns sibling containers directly on the host, so
# any volume it hands to those sibling containers has to be a host path.
LIBRARY_HOST_PATH = os.environ["LIBRARY_HOST_PATH"]
APP_DATA_DIR = os.environ.get("APP_DATA_DIR", "/data")
JOBS_FILE = os.path.join(APP_DATA_DIR, "jobs.json")
MAX_LOG_LINES = 500

app = Flask(__name__)
docker_client = docker.from_env()

jobs = {}
jobs_lock = threading.Lock()
job_queue = queue.Queue()


# --- Persistence -------------------------------------------------------
def load_jobs():
    global jobs
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE) as f:
                jobs = json.load(f)
        except Exception:
            jobs = {}


def save_jobs():
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    with jobs_lock:
        with open(JOBS_FILE, "w") as f:
            json.dump(jobs, f, indent=2)


# --- Job helpers ---------------------------------------------------------
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
        "created_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "log": [],
        "container_id": None,
    }
    with jobs_lock:
        jobs[job_id] = job
    save_jobs()
    job_queue.put(job_id)
    return job_id


def append_log(job_id, line):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["log"].append(line)
        if len(job["log"]) > MAX_LOG_LINES:
            job["log"] = job["log"][-MAX_LOG_LINES:]
    save_jobs()


def set_status(job_id, status):
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            job["status"] = status
    save_jobs()


# --- Scraper invocations ---------------------------------------------------
def run_zimit(job):
    safe_name = job["name"] or job["id"]
    command = ["zimit", "--seeds", job["url"], "--name", safe_name]
    if job.get("extra_args"):
        command += shlex.split(job["extra_args"])
    return docker_client.containers.run(
        "ghcr.io/openzim/zimit:latest",
        command=command,
        volumes={LIBRARY_HOST_PATH: {"bind": "/output", "mode": "rw"}},
        detach=True,
    )


def run_mwoffliner(job):
    safe_name = job["name"] or job["id"]
    command = [
        "mwoffliner",
        f"--mwUrl={job['url']}",
        f"--adminEmail={job['admin_email']}",
        "--outputDirectory=/output",
        f"--filenamePrefix={safe_name}",
    ]
    if job.get("extra_args"):
        command += shlex.split(job["extra_args"])
    return docker_client.containers.run(
        "ghcr.io/openzim/mwoffliner:latest",
        command=command,
        volumes={LIBRARY_HOST_PATH: {"bind": "/output", "mode": "rw"}},
        detach=True,
    )


# --- Worker: runs one job at a time ----------------------------------------
def worker_loop():
    while True:
        job_id = job_queue.get()
        with jobs_lock:
            job = jobs.get(job_id)
        if not job:
            continue

        set_status(job_id, "running")
        try:
            if job["scraper"] == "mwoffliner":
                container = run_mwoffliner(job)
            else:
                container = run_zimit(job)

            with jobs_lock:
                job["container_id"] = container.id
            save_jobs()

            for line in container.logs(stream=True):
                append_log(job_id, line.decode(errors="replace").rstrip())

            result = container.wait()
            exit_code = result.get("StatusCode", 1)
            set_status(job_id, "success" if exit_code == 0 else "failed")

            try:
                container.remove()
            except Exception:
                pass

        except Exception as e:
            append_log(job_id, f"ERROR: {e}")
            set_status(job_id, "failed")


# --- Routes ---------------------------------------------------------
@app.route("/")
def index():
    with jobs_lock:
        job_list = sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)
    return render_template("index.html", jobs=job_list)


@app.route("/submit", methods=["POST"])
def submit():
    url = request.form.get("url", "").strip()
    name = request.form.get("name", "").strip()
    scraper = request.form.get("scraper", "zimit")
    admin_email = request.form.get("admin_email", "").strip()
    extra_args = request.form.get("extra_args", "").strip()

    if not url:
        return redirect(url_for("index"))
    if not name:
        name = url.rstrip("/").split("/")[-1] or "site"
    if scraper == "mwoffliner" and not admin_email:
        admin_email = "archive@example.com"

    new_job(url, name, scraper, admin_email, extra_args)
    return redirect(url_for("index"))


@app.route("/jobs")
def jobs_json():
    with jobs_lock:
        job_list = sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)
    return jsonify(job_list)


@app.route("/logs/<job_id>")
def logs(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return "no such job", 404
    return Response("\n".join(job["log"]), mimetype="text/plain")


if __name__ == "__main__":
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    load_jobs()

    # If the app restarted mid-job, those jobs are orphaned (the container
    # may still be running on the host, but we lost track of it). Mark them
    # failed rather than silently losing them.
    with jobs_lock:
        for job in jobs.values():
            if job["status"] in ("queued", "running"):
                job["status"] = "failed"
                job["log"].append("Interrupted by app restart.")
    save_jobs()

    threading.Thread(target=worker_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
