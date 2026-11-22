# ZIM Forge

A small web UI for turning websites into ZIM files for [Kiwix](https://kiwix.org).
It queues jobs and runs [Zimit](https://github.com/openzim/zimit) (any website) or
[mwoffliner](https://github.com/openzim/mwoffliner) (MediaWiki sites) as sibling
Docker containers, one job at a time. Finished ZIMs are written straight into your
Kiwix library folder.

## Running it

ZIM Forge needs the Docker socket so it can start scraper containers.
**Anything that can reach its web port can start containers on your Docker host.**
If the port is reachable by anyone other than you, set `ZIMFORGE_PASSWORD`.

1. Copy `.env.example` to `.env` and set `LIBRARY_HOST_PATH` to your Kiwix
   library folder **as the Docker host sees it**:
   - Linux: `LIBRARY_HOST_PATH=/srv/kiwix/library`
   - Docker Desktop on Windows: `LIBRARY_HOST_PATH=C:\Users\you\kiwix-library`
2. Start it:

   ```bash
   docker compose up -d
   ```

   This pulls the image published from the `main` branch
   (`ghcr.io/rastrumquill/zim-forge:latest`). To update later, run
   `docker compose pull` and `docker compose up -d`, or press **Update** in
   Dockge. To run a local checkout instead, add `--build`.

3. Open <http://localhost:5000>.

The included `docker-compose.yml` also mounts the library into ZIM Forge itself
(at `/library`), which enables the free-space check and output verification
described below.

## What a job records

Each job keeps what's needed to trust the result later:

- **Output verification.** A job only counts as a success if a new ZIM file
  appeared in the library. Its filename, size and SHA-256 are recorded.
- **Provenance.** The exact scraper image digest and command line are saved with
  the job and written to its log.
- **Full log.** The page shows the last 500 lines; the complete timestamped log
  is kept in `data/logs/` and can be downloaded from the job's details.
- **Free-space check.** A job won't start with less than `MIN_FREE_GB` free.

Verification and the free-space check need `LIBRARY_PATH` (set for you in the
compose file). Without it, success is judged by the scraper's exit code alone.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `LIBRARY_HOST_PATH` | *(required)* | Folder on the Docker host where ZIMs are written. |
| `ZIMFORGE_PASSWORD` | *(unset)* | Enables HTTP basic auth. Strongly recommended. |
| `ZIMFORGE_USERNAME` | `admin` | Basic auth username. |
| `APP_DATA_DIR` | `/data` | Where job history and the session key are stored. |
| `ZIMIT_IMAGE` | `ghcr.io/openzim/zimit:latest` | Pin to a version tag for reproducible runs. |
| `MWOFFLINER_IMAGE` | `ghcr.io/openzim/mwoffliner:latest` | Pin to a version tag for reproducible runs. |
| `LIBRARY_PATH` | *(unset)* | Where the library is mounted inside ZIM Forge's own container. Enables verification and the free-space check. |
| `MIN_FREE_GB` | `5` | Minimum free space in the library before a job may start. |
| `PULL_IMAGES` | `true` | Check for a newer scraper image before each job. |
| `SCRAPER_MEMORY` | *(none)* | Memory cap for scraper containers, e.g. `8g`. |
| `SCRAPER_CPUS` | *(none)* | CPU cap for scraper containers, e.g. `4`. |
| `ZIMIT_SHM_SIZE` | `1g` | Shared memory for Zimit's Chromium. Lower values crash on big sites. |
| `JOB_TIMEOUT_HOURS` | `0` (none) | Stop and fail jobs that run longer than this. |
| `MAX_JOBS_KEPT` | `100` | Oldest finished jobs beyond this are removed from history. |
| `ALLOW_PRIVATE_URLS` | `false` | Allow URLs on private/internal networks. |
| `BOT_CHECK` | `true` | Before starting, check the site isn't serving a bot challenge. |
| `SECRET_KEY` | *(generated)* | Session key. Generated and saved in `APP_DATA_DIR` if unset. |
| `HOST` / `PORT` | `0.0.0.0` / `5000` | Listen address. |

## Sites behind Cloudflare or other bot protection

Some sites (for example tcrf.net) put a JavaScript challenge in front of
automated clients. Scrapers can't pass it, so the job would fail, often hours in.
ZIM Forge makes one plain request to the URL before starting and stops with an
explanation if it sees a challenge.

ZIM Forge deliberately doesn't try to get around these protections. To archive
such a site, ask its admins either to let your crawler through (mwoffliner
identifies itself with your admin email) or for a database dump.

## Notes

- One job runs at a time. Jobs can be cancelled or run again from the jobs table.
- If the app restarts mid-job, it reattaches to the still-running scraper
  container. Queued jobs are resumed.
- Two queued or running jobs can't share a name, since they'd write the same file.

## Tests

```bash
pip install -r requirements.txt
python tests/test_app.py
```

The tests use a fake Docker client and a local fake site, so they need neither
Docker nor internet access.
