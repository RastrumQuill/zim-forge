"""End-to-end checks for ZIM Forge using a fake Docker client and a local fake site.

Run with:  python tests/test_app.py
No Docker daemon or internet access is needed.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="zimforge-test-")
DATA = os.path.join(WORK, "data")
LIBRARY = os.path.join(WORK, "library")
MIRRORS = os.path.join(WORK, "mirrors")
os.makedirs(LIBRARY)
os.makedirs(MIRRORS)
os.environ.update(
    LIBRARY_HOST_PATH="C:\\kiwix", LIBRARY_PATH=LIBRARY, APP_DATA_DIR=DATA,
    ALLOW_PRIVATE_URLS="true", MIN_FREE_GB="0", SCRAPER_MEMORY="2g", SCRAPER_CPUS="1.5",
    MIRROR_HOST_PATH="/mnt/mirrors", MIRROR_PATH=MIRRORS, KIWIX_CONTAINER="kiwix-serve",
)

import docker  # noqa: E402
from docker.errors import NotFound  # noqa: E402


class FakeContainer:
    def __init__(self, cid, chunks, code=0, delay=0, status="running", writes=None):
        self.id, self.chunks, self.code, self.delay, self.status = cid, chunks, code, delay, status
        self.writes = writes  # (filename, bytes) "ZIM" the scraper produces
        self.removed = self.stopped = False
        self.restarts = 0
        self.before_exit = None  # optional callable run when the "scraper" ends
        self.attrs = {"State": {"ExitCode": code}}

    def logs(self, **kw):
        for chunk in self.chunks:
            if self.stopped:
                return
            time.sleep(self.delay)
            yield chunk
        if self.writes:
            with open(os.path.join(LIBRARY, self.writes[0]), "wb") as f:
                f.write(self.writes[1])
        if self.before_exit:
            self.before_exit()

    def restart(self):
        self.restarts += 1

    def wait(self):
        return {"StatusCode": 137 if self.stopped else self.code}

    def remove(self, force=False):
        self.removed = True

    def stop(self, timeout=10):
        self.stopped = True


class FakeContainers:
    def __init__(self):
        self.made, self.by_id, self.next = [], {}, None

    def run(self, image, command, **kw):
        c = self.next or FakeContainer(
            f"c{len(self.made)}", [b"hello\nwor", b"ld\r\nprog 1\rprog 2\n", b"tail"],
            writes=(f"out{len(self.made)}.zim", b"ZIM" * 1000))
        self.next = None
        self.made.append((image, command, kw, c))
        self.by_id[c.id] = c
        return c

    def get(self, cid):
        if cid not in self.by_id:
            raise NotFound("gone")
        return self.by_id[cid]


class FakeImage:
    id = "sha256:local"
    attrs = {"RepoDigests": ["ghcr.io/openzim/zimit@sha256:abc123"]}


class FakeImages:
    pulled = []
    fail_pull = False

    def pull(self, image):
        if self.fail_pull:
            raise RuntimeError("registry unreachable")
        self.pulled.append(image)

    def get(self, image):
        return FakeImage()


class FakeClient:
    containers = FakeContainers()
    images = FakeImages()


docker.from_env = lambda: FakeClient()


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/cf"):
            self.send_response(403)
            self.send_header("cf-mitigated", "challenge")
            self.end_headers()
            self.wfile.write(b"<title>Just a moment...</title>")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Site)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"

sys.path.insert(0, ROOT)
import app as A  # noqa: E402

A.start_background()
fc = FakeClient.containers
kiwix = FakeContainer("kiwix-serve", [])
fc.by_id["kiwix-serve"] = kiwix
c = A.app.test_client()


def wait_status(jid, statuses, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if A.jobs[jid]["status"] in statuses:
            return A.jobs[jid]["status"]
        time.sleep(0.05)
    raise AssertionError(f"{jid} stuck in {A.jobs[jid]['status']}: {A.jobs[jid]['log']}")


def job_named(name):
    return [j for j in A.jobs.values() if j["name"] == name][-1]


def check(label):
    print("ok:", label)


html = c.get("/").data.decode()
tok = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
post = lambda **data: c.post("/submit", data={"csrf_token": tok, **data})  # noqa: E731
hdr = {"X-CSRF-Token": tok}

# --- submission validation ---
assert c.post("/submit", data={"url": BASE + "/x"}).status_code == 400
assert b"http://" in post(url="file:///etc/passwd").data
assert b"admin email" in post(url=BASE, scraper="mwoffliner").data
assert b"Unknown scraper" in post(url=BASE, scraper="evil").data
assert b"quotes" in post(url=BASE, extra_args='--depth "1').data
assert b"--output" in post(url=BASE, extra_args="--output=/tmp").data
check("validation and CSRF")

# --- successful zimit job ---
r = post(url=BASE + "/ok/page.php?id=3", name="<img src=x onerror=alert(1)>", extra_args="--depth 2")
assert r.status_code == 302, r.data
job = job_named("img_src_x_onerror_alert_1")
assert wait_status(job["id"], ("success", "failed")) == "success", job["log"]
image, cmd, kw, cont = fc.made[-1]
assert kw["shm_size"] == "1g" and kw["mem_limit"] == "2g" and kw["nano_cpus"] == 1_500_000_000
assert cont.removed and cmd[-2:] == ["--depth", "2"]
assert FakeImages.pulled[-1] == A.ZIMIT_IMAGE and job["image_digest"].endswith("abc123") and job["command"] == cmd
for expected in ["hello", "world", "prog 1", "prog 2", "tail"]:
    assert expected in job["log"], (expected, job["log"])
check("zimit job: name sanitized, limits applied, log split into lines, provenance recorded")

out = job["outputs"]
assert len(out) == 1 and out[0]["size"] == 3000 and out[0]["sha256"] == hashlib.sha256(b"ZIM" * 1000).hexdigest()
check("output verified with size and checksum")

full = c.get(f"/logs/{job['id']}/full")
assert full.status_code == 200 and b"hello" in full.data and b"Command: zimit" in full.data
assert c.get("/logs/..%2Fsecret_key/full").status_code == 404
check("full log on disk")

# --- success exit code but no ZIM produced is a failure ---
fc.next = FakeContainer("empty", [b"did nothing\n"], code=0)
post(url=BASE + "/ok", name="empty")
assert wait_status(job_named("empty")["id"], ("success", "failed")) == "failed"
check("no output file => failed")

# --- pull failure falls back to local image ---
FakeImages.fail_pull = True
post(url=BASE + "/ok", name="offline")
assert wait_status(job_named("offline")["id"], ("success", "failed")) == "success"
assert any("using the local copy" in line for line in job_named("offline")["log"])
FakeImages.fail_pull = False
check("image pull failure falls back to local copy")

# --- low disk space ---
A.MIN_FREE_GB = 10 ** 9
n = len(fc.made)
post(url=BASE + "/ok", name="nodisk")
assert wait_status(job_named("nodisk")["id"], ("success", "failed")) == "failed" and len(fc.made) == n
A.MIN_FREE_GB = 0
check("low disk space stops the job before it starts")

# --- kiwix restarted after a new ZIM ---
assert kiwix.restarts >= 1
check("kiwix restarted after new ZIM")

# --- failed zimit job: leftover temp folder is removed ---
os.makedirs(os.path.join(LIBRARY, ".tmpkeepme"))  # existed before the job: must survive
leaver = FakeContainer("leaver", [b"crawl failed\n"], code=9)
leaver.before_exit = lambda: os.makedirs(os.path.join(LIBRARY, ".tmpabc123", "collections"))
fc.next = leaver
post(url=BASE + "/ok", name="leaver")
assert wait_status(job_named("leaver")["id"], ("success", "failed")) == "failed"
assert not os.path.exists(os.path.join(LIBRARY, ".tmpabc123")) and os.path.isdir(os.path.join(LIBRARY, ".tmpkeepme"))
check("leftover temp folder cleaned up; pre-existing one untouched")

# --- file mirror ---
assert b"end the URL with" in post(url=BASE + "/files", scraper="mirror").data
assert b"-P" in post(url=BASE + "/files/", scraper="mirror", extra_args="-P /tmp").data


def fake_download(name, files):
    def run():
        root = os.path.join(MIRRORS, name, "host", "files")
        os.makedirs(root, exist_ok=True)
        for filename in files:
            with open(os.path.join(root, filename), "wb") as f:
                f.write(b"data")
        with open(os.path.join(MIRRORS, name, "SHA256SUMS"), "w") as f:
            f.write("manifest")
    return run


restarts = kiwix.restarts
m = FakeContainer("mirror1", [b"URL:... -> a.bin\n"], code=0)
m.before_exit = fake_download("my-files", ["a.bin", "b.zip"])
fc.next = m
post(url=BASE + "/files/", scraper="mirror", name="my-files", extra_args="--level=2")
mj = job_named("my-files")
assert wait_status(mj["id"], ("success", "failed")) == "success", mj["log"]
image, cmd, kw, _ = fc.made[-1]
assert image == A.MIRROR_IMAGE and cmd[:2] == ["sh", "-c"] and cmd[3:5] == ["mirror", "my-files"]
assert "--no-parent" in cmd and cmd[-2:] == ["--level=2", BASE + "/files/"]
assert list(kw["volumes"]) == ["/mnt/mirrors"] and "shm_size" not in kw
assert mj["outputs"][0]["file"] == "my-files/ (2 files)" and mj["outputs"][0]["size"] == 8
assert mj["outputs"][0]["sha256"] == hashlib.sha256(b"manifest").hexdigest() and not mj["note"]
assert kiwix.restarts == restarts
check("file mirror job")

m = FakeContainer("mirror2", [b"ERROR 404\n"], code=8)
m.before_exit = fake_download("partial", ["a.bin"])
fc.next = m
post(url=BASE + "/files/", scraper="mirror", name="partial")
pj = job_named("partial")
assert wait_status(pj["id"], ("success", "failed")) == "success" and "could not be downloaded" in pj["note"]
fc.next = FakeContainer("mirror3", [b"ERROR 404\n"], code=91)
post(url=BASE + "/files/", scraper="mirror", name="nothing")
assert wait_status(job_named("nothing")["id"], ("success", "failed")) == "failed"
fc.next = FakeContainer("mirror4", [b"network down\n"], code=4)
post(url=BASE + "/files/", scraper="mirror", name="netfail")
assert wait_status(job_named("netfail")["id"], ("success", "failed")) == "failed"
check("mirror: partial download noted, empty and network failures fail")

# --- derived name, bot challenge ---
post(url=BASE + "/ok/page.php?id=3")
assert wait_status(job_named("page")["id"], ("success",))
n = len(fc.made)
post(url=BASE + "/cf", name="cf")
assert wait_status(job_named("cf")["id"], ("failed", "success")) == "failed" and len(fc.made) == n
check("bot challenge detected before starting")

# --- mwoffliner failure, then retry ---
fc.next = FakeContainer("mw", [b"boom\n"], code=2)
post(url=BASE + "/wiki", scraper="mwoffliner", admin_email="a@b.co")
mw = job_named("wiki")
assert wait_status(mw["id"], ("failed", "success")) == "failed" and mw["exit_code"] == 2
assert fc.made[-1][1][:2] == ["mwoffliner", f"--mwUrl={BASE}/wiki"]
assert c.post(f"/jobs/{mw['id']}/retry").status_code == 400
r = c.post(f"/jobs/{mw['id']}/retry", headers=hdr)
assert r.status_code == 200
again = A.jobs[r.get_json()["id"]]
assert again["admin_email"] == "a@b.co" and wait_status(again["id"], ("success", "failed")) == "success"
check("mwoffliner failure and retry")

# --- cancel, duplicate names ---
fc.next = FakeContainer("slow", [b"x\n"] * 200, delay=0.05)
post(url=BASE + "/slow", name="slow")
slow = job_named("slow")
wait_status(slow["id"], ("running",))
while not slow["container_id"]:
    time.sleep(0.05)
assert b"already queued or running" in post(url=BASE + "/slow", name="slow").data
assert c.post(f"/jobs/{slow['id']}/retry", headers=hdr).status_code == 409
assert c.post(f"/jobs/{slow['id']}/cancel").status_code == 400
assert c.post(f"/jobs/{slow['id']}/cancel", headers=hdr).status_code == 200
assert wait_status(slow["id"], ("cancelled", "failed", "success")) == "cancelled"
assert c.post(f"/jobs/{slow['id']}/cancel", headers=hdr).status_code == 409
check("cancel and duplicate-name protection")

# --- API shape and persistence ---
listing = c.get("/jobs").get_json()
assert all("log" not in j and "zims_before" not in j for j in listing)
A.save_jobs()
with open(os.path.join(DATA, "jobs.json")) as f:
    assert len(json.load(f)) == len(A.jobs)
check("job list has no logs; jobs.json is valid")

# --- recovery after restart ---
fc.by_id["live"] = FakeContainer("live", [b"later\n"], writes=("live.zim", b"x"))
fc.by_id["done"] = FakeContainer("done", [], code=0, status="exited")
A.jobs.clear()
for i, (status, cid) in enumerate([("queued", None), ("running", "done"), ("running", "live"), ("running", "ghost")]):
    A.jobs[f"r{i}"] = {
        "id": f"r{i}", "url": BASE + "/ok", "name": f"r{i}", "scraper": "zimit", "admin_email": "",
        "extra_args": "", "status": status, "created_at": f"2026-01-0{i + 1}T00:00:00Z", "started_at": None,
        "finished_at": None, "exit_code": None, "log": [], "container_id": cid, "cancel_requested": False,
        "image": None, "image_digest": None, "command": None, "outputs": [], "note": None,
        "zims_before": A.list_zims(),
    }
A.recover_jobs()
for i in range(4):
    wait_status(f"r{i}", ("success", "failed"))
assert A.jobs["r0"]["status"] == "success"          # queued job resumed
assert A.jobs["r1"]["status"] == "failed"           # exited cleanly but produced no ZIM
assert A.jobs["r2"]["status"] == "success"          # reattached to live container
assert A.jobs["r2"]["outputs"][0]["file"] == "live.zim"
assert A.jobs["r3"]["status"] == "failed"           # container vanished
assert fc.by_id["live"].removed and fc.by_id["done"].removed
check("recovery after restart")

# --- corrupt jobs file is set aside, not silently dropped ---
with open(os.path.join(DATA, "jobs.json"), "w") as f:
    f.write("{broken")
A.log.disabled = True
A.load_jobs()
A.log.disabled = False
assert A.jobs == {} and any(name.startswith("jobs.json.corrupt") for name in os.listdir(DATA))
check("corrupt jobs file kept aside")

# --- basic auth ---
A.AUTH_PASSWORD = "pw"
assert c.get("/").status_code == 401 and c.get("/", auth=("admin", "pw")).status_code == 200
check("basic auth")

print("ALL OK")
