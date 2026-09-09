# Photo Drop

A small HEIC → JPG website: drop hundreds of photos, choose JPEG quality, and download one ZIP. Plain HTML/CSS/JavaScript frontend, Python/FastAPI backend, no frontend build step, database, or external conversion service.

The converter is the existing **libheif `heif-dec` CLI**, with a fallback to its older name, `heif-convert`. The Docker image installs Debian's `libheif-examples` and HEVC decoder plugin. The command is equivalent to `heif-dec -q 90 input.heic output.jpg`; libheif handles decoding and image transformations. See the [upstream project](https://github.com/strukturag/libheif) and [Debian CLI manual](https://manpages.debian.org/trixie/libheif-examples/heif-convert.1.en.html).

## Run locally

Install Docker, then from this folder:

```sh
docker compose -f compose.local.yml up --build -d
```

Open **http://localhost:8765**. Stop with `docker compose -f compose.local.yml down`. If the port is occupied, prefix the start command with `LOCAL_PORT=8766` and open that port instead.

Without Docker, install Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
# macOS
brew install libheif
# Debian / Ubuntu alternative:
# sudo apt-get update && sudo apt-get install libheif-examples

uv sync --locked
uv run uvicorn app:app --host 127.0.0.1 --port 8765
```

## Deploy on Coolify using your existing Cloudflare Tunnel

The default `docker-compose.yml` runs only the converter. Your existing Cloudflare Tunnel and Coolify proxy handle routing, so **no `TUNNEL_TOKEN` is required for this app**.

```text
Browser → HTTPS → Cloudflare → existing tunnel → Coolify proxy → converter:8000
```

### 1. Connect the project to Coolify

Push this folder to your Git repository, including `Dockerfile`, `uv.lock`, `docker-compose.yml`, `app.py`, and `static/`. Keep `.env`, `data/`, and `.venv/` out of Git.

In Coolify, add a Git-based Application and select **Docker Compose** as its build pack. Use base directory `/` and Compose location `/docker-compose.yml` when this folder is the repository root. For a repository containing the enclosing `lab` folder, set the base directory to `/heic-to-jpg` and select the Compose file there. [Coolify Compose deployment](https://coolify.io/docs/applications/build-packs/docker-compose)

### 2. Set the converter's domain

Load/reload the Compose file. In the **converter** service's **Domains** field, enter:

```text
http://photodrop.benjamin-marques-balula.fr:8000
```

The `:8000` tells Coolify which port to reach inside the container. The browser URL has no port suffix. Do not add a host port mapping. Keep one application instance. Save and deploy. [Coolify domain and port routing](https://coolify.io/docs/knowledge-base/docker/compose)

### 3. Reuse your existing tunnel route

If your existing tunnel and DNS already cover `*.benjamin-marques-balula.fr` and forward to Coolify's proxy, there is nothing to add in Cloudflare. Otherwise, add `photodrop.benjamin-marques-balula.fr` to that existing tunnel using the same origin service as your other working Coolify apps. Keep the original Host header so Coolify can select this app.

This assumes your existing tunnel sends HTTP to Coolify's proxy, which is why the Coolify Domains value starts with `http://`. Cloudflare provides HTTPS to visitors. [Coolify's shared tunnel guide](https://coolify.io/docs/integrations/cloudflare/tunnels/all-resource)

### 4. Open and test

Open **https://photodrop.benjamin-marques-balula.fr**.

1. Visit `/healthz`; expect `status: ok`.
2. Drop a few HEIC files, convert, download the ZIP, and open its JPGs.
3. Try a batch of hundreds. Duplicate filenames receive numbered prefixes.
4. Include a damaged `.heic` file: successful conversions still download, with failures listed in `conversion-errors.json`.
5. Click **Delete batch & start again** after the download finishes.

If you previously loaded the older Compose file with a `tunnel` service, push these updated files, reload the Compose definition in Coolify, and remove the now-unused app-level `TUNNEL_TOKEN` entry if Coolify still shows it. This does not change your existing shared tunnel.

For a private tool, you can protect the whole hostname with a Cloudflare Access application. The app itself has no login. Batch URLs contain random bearer secrets. [Cloudflare Access setup](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)

### Optional: a separate tunnel

Only use this alternative if you want a dedicated tunnel for Photo Drop. The optional `compose.tunnel.yml` adds a cloudflared container and requires `TUNNEL_TOKEN`. Load both Compose files, supply the token as a runtime environment variable, leave the converter's Coolify Domains field empty, and configure the dedicated tunnel hostname to reach `http://converter:8000` on their shared Docker network.

```sh
docker compose -f docker-compose.yml -f compose.tunnel.yml up --build -d
```

For the existing Coolify tunnel setup above, use **only `docker-compose.yml`**. [Cloudflare tunnel setup](https://developers.cloudflare.com/tunnel/setup/)

### Why large batches work through the tunnel

Cloudflare Free/Pro commonly caps each upload request at 100 MB; requests also have timeouts. This app sends **8 MiB chunks**, with three file upload loops and automatic retry from the server's saved offset. It starts conversion with a short request and polls progress. ZIP creation happens before download, and the browser downloads it directly, with range requests supported. No request waits for hundreds of conversions. Keep Cloudflare's upload limit above 8 MiB and avoid a “Cache Everything” rule on this hostname; responses use `Cache-Control: no-store`. [Upload limits](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/), [connection limits](https://developers.cloudflare.com/fundamentals/reference/connection-limits/)

## Capacity and temporary storage

| Setting | Default | Meaning |
| --- | --- | --- |
| `MAX_FILES` | `1000` | Photos per batch |
| `MAX_FILE_MB` | `200` | MiB per photo, sent in chunks |
| `MAX_BATCH_MB` | `5120` | MiB of originals per batch (5 GiB) |
| `MAX_JOBS` | `10` | Retained batches, including completed ones |
| `CONVERSION_WORKERS` | `2` | Concurrent batches; each converts one image at a time |
| `CONVERSION_TIMEOUT` | `120` | Seconds allowed for each image |
| `RETENTION_SECONDS` | `3600` | Idle upload / completed batch retention |
| `MIN_FREE_MB` | `1024` | Free-disk reserve checked before uploads/conversions |

Start with a server providing at least 2 CPU cores, 4 GB RAM, and 30 GB free disk for occasional large batches; actual requirements depend on image resolution and how many users overlap. The app container is capped at 2 CPUs / 2 GB RAM by Compose. Very large images may require more memory. To lower peak memory use, set `CONVERSION_WORKERS=1`; to raise the memory cap, edit `mem_limit` in Compose.

The named `photo-data` volume stores temporary files. JPGs can be larger than HEICs, and packaging temporarily needs space for both the JPGs and ZIP. The free-disk guard is not a storage quota or reservation. Budget more disk for concurrent large batches and monitor usage. On insufficient storage, a batch fails with an error instead of returning an incomplete ZIP as successful.

Originals are removed after each conversion. Output JPGs are removed after ZIP creation. Results and abandoned uploads are deleted on a 30-second cleanup sweep after their retention period; active conversion/downloads are protected. Download completion refreshes the retention timer. Users can delete their batch earlier. Queued cancellation is cleaned when its worker wakes; active cancellation stops the decoder and cleans its files.

**Temporary jobs do not survive an app restart/redeploy.** Job metadata is in memory, and the app removes its old batch folders at startup. Keep exactly **one Uvicorn worker and one app replica**, and avoid deployments while users are converting. Refreshing the same browser tab reconnects to a converting/completed batch. Uploads retry network errors while the page stays open; after reloading mid-upload, delete the batch and select the originals again.

JPEG output keeps full image dimensions, but JPEG is lossy and does not retain HEIC's HDR/alpha/live-photo capabilities. Metadata/color handling follows the installed libheif CLI; this app does not promise EXIF/GPS preservation or removal. A HEIF with multiple top-level images can produce multiple JPGs. Unicode names are sanitized and numbered so files do not overwrite one another.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Tunnel disconnected | Your existing tunnel connector and its logs; app-level tokens are only used by the optional dedicated tunnel |
| Cloudflare 502 | Existing tunnel reaches Coolify's proxy; converter is healthy; its Coolify Domains entry ends with `:8000` |
| Cloudflare 1033 | Connector has not connected; inspect tunnel status/logs |
| Redirect loop | When the existing tunnel reaches Coolify over HTTP, use `http://` in the app's Coolify Domains entry |
| 413 during upload | Zone/proxy upload limits must allow 8 MiB; app also enforces file and batch limits |
| 403 during upload | Cloudflare Access session/WAF rules, and an empty Host Header override on the tunnel route |
| 429 / server busy | Delete a completed batch or wait for expired batches to be cleaned |
| 507 / storage error | Free disk or lower batch/concurrency limits |
| Batch disappeared | Retention expired or converter restarted/redeployed |
| Decoder killed / timeout | Review image validity, container memory, and timeout settings |

Rebuild regularly to receive Debian/libheif updates. The optional dedicated cloudflared image uses `latest`; pin it to a tested release/digest if your deployment policy requires that. Updating/redeploying interrupts temporary jobs.

## Verification

```sh
uv sync --locked
uv run pytest -q
# Run a browser upload/download test against the local server:
uv run playwright install chromium
uv run python tests/browser_smoke.py http://localhost:8765
# Larger HTTP integration test against a running local or Docker server:
uv run python tests/batch_smoke.py http://localhost:8765 --count 300
```

The small HEIC fixture in `tests/fixtures` is generated test artwork, encoded using libheif. The batch smoke test invokes the real server-side CLI for each image and decodes every downloaded JPG to verify the archive contents. It measures batch handling, not throughput for a particular phone's full-resolution photos.
