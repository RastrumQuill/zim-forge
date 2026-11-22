# ZIM Forge

A small web UI for turning websites into ZIM files for [Kiwix](https://kiwix.org).
It queues jobs and runs [Zimit](https://github.com/openzim/zimit) (any website) or
[mwoffliner](https://github.com/openzim/mwoffliner) (MediaWiki sites) as sibling
Docker containers, one job at a time. Finished ZIMs are written straight into your
Kiwix library folder.

## Running it

ZIM Forge needs the Docker socket so it can start scraper containers.
**Anything that can reach its web port can start containers on your Docker host**,
so set `ZIMFORGE_PASSWORD` unless the port is only reachable by you.

Example `docker-compose.yml` (Linux host):

```yaml
services:
  zim-forge:
    build: .
    ports:
      - "5000:5000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./data:/data
    environment:
      LIBRARY_HOST_PATH: /srv/kiwix/library   # path on the HOST, not in this container
      ZIMFORGE_PASSWORD: change-me
    restart: unless-stopped
```

### Docker Desktop on Windows

Same file, but `LIBRARY_HOST_PATH` must be a Windows path, because the scraper
containers mount it from your PC:

```yaml
    environment:
      LIBRARY_HOST_PATH: 'C:\Users\you\kiwix-library'
```

The socket mount stays `/var/run/docker.sock:/var/run/docker.sock`. Docker Desktop
provides it.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `LIBRARY_HOST_PATH` | *(required)* | Folder on the Docker host where ZIMs are written. |
| `ZIMFORGE_PASSWORD` | *(unset)* | Enables HTTP basic auth. Strongly recommended. |
| `ZIMFORGE_USERNAME` | `admin` | Basic auth username. |
| `APP_DATA_DIR` | `/data` | Where job history and the session key are stored. |
| `ZIMIT_IMAGE` | `ghcr.io/openzim/zimit:latest` | Pin to a version tag for reproducible runs. |
| `MWOFFLINER_IMAGE` | `ghcr.io/openzim/mwoffliner:latest` | Pin to a version tag for reproducible runs. |
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

- One job runs at a time. Jobs can be cancelled from the jobs table.
- If the app restarts mid-job, it reattaches to the still-running scraper
  container. Queued jobs are resumed.
- Two queued or running jobs can't share a name, since they'd write the same file.
