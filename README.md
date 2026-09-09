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

## Deploy on Coolify through Cloudflare Tunnel

The included `docker-compose.yml` runs the app and `cloudflared` together. Traffic follows:

```text
Browser → HTTPS → Cloudflare → encrypted tunnel → cloudflared
        → HTTP over the private Docker network → converter:8000
```

This tutorial assumes Coolify already manages your server and your domain uses Cloudflare DNS. Allow build downloads and outbound tunnel traffic; Cloudflare documents port **7844** for tunnel connectivity. You do not need to forward a public app port. [Cloudflare setup documentation](https://developers.cloudflare.com/tunnel/setup/)

### 1. Put this project in a Git repository

Create a repository on your preferred Git host and push the contents of **this folder**, including `Dockerfile`, `uv.lock`, `docker-compose.yml`, `app.py`, and `static/`. Keep `.env`, `data/`, and `.venv/` out of Git; `.gitignore` already covers them. The repository can be private.

### 2. Create a dedicated Cloudflare Tunnel

In Cloudflare, open **Networking → Tunnels → Create Tunnel**. Some dashboard versions place this under **Zero Trust → Networks → Connectors → Cloudflare Tunnels**. Select the cloudflared connector if asked, and name it `photo-drop`.

Choose Docker in the setup instructions. Copy **only the token** following `--token` in Cloudflare's example command. Save it for the next step. Coolify will run the included tunnel container, so there is no separate install command to run. The tunnel will stay disconnected until deployment. [Create a tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/)

### 3. Add the app in Coolify

In your project/environment, choose **New Resource → Application**, then your Git source: public repository, GitHub App, or private repository with a deploy key. Choose the repository, branch, server, and destination. Select **Docker Compose** as the build pack.

Set the base directory to `/` and the Compose location to `/docker-compose.yml` when this project is at the repository root. If you committed the enclosing `lab` folder, use `/heic-to-jpg` as the base directory and select the Compose file inside it.

Load/reload the Compose file in Coolify. In the resource's environment variables, set **`TUNNEL_TOKEN`** to the token from step 2, as a runtime variable. Do not put it into the Dockerfile or Git. Other settings have defaults.

Keep both service **Domains** fields empty and remove any automatically generated domain. Leave **Connect to Predefined Network** off. The two services use the same private Compose network; `converter` is their internal DNS name. Keep a single application instance. Save and deploy. Coolify supports private services with no assigned domain or published port. [Compose networking](https://coolify.io/docs/knowledge-base/docker/compose), [Git-based Compose deployment](https://coolify.io/docs/applications/build-packs/docker-compose)

The app's health check must pass before the tunnel starts. The tunnel reads the secret from its **`TUNNEL_TOKEN` environment variable**. [cloudflared run parameters](https://developers.cloudflare.com/tunnel/advanced/run-parameters/)

### 4. Publish the hostname

Return to your tunnel in Cloudflare. Under **Routes → Add route → Published application** (older UI: **Public Hostnames**), enter:

| Field | Value |
| --- | --- |
| Subdomain | `photos` |
| Domain | Your domain, e.g. `example.com` |
| Path | Leave empty |
| Service type | `HTTP` |
| Service URL | `converter:8000` (or `http://converter:8000` in a single URL field) |

Save the route. Use `converter`, not `localhost`: the connector runs in its own container. Keep the HTTP Host Header override empty. Cloudflare creates the tunnel DNS route; resolve any existing conflicting DNS record for `photos` first. Open **https://photos.example.com** after the tunnel shows Healthy. [Published application routes](https://developers.cloudflare.com/tunnel/setup/)

### 5. Test your deployment

1. Open `https://photos.example.com/healthz`; expect `status: ok`.
2. Drop a few HEIC files, convert, download the ZIP, and open its JPGs.
3. Drop 300 photos. Watch upload progress, then conversion progress, then download the ZIP. Duplicate filenames receive numbered prefixes.
4. Include a damaged `.heic` file: successful photos should still download and `conversion-errors.json` should list the failure.
5. Click **Delete batch & start again** after your download finishes.

For a private tool, configure a Cloudflare Access self-hosted application covering the **entire hostname** and allow your email/team. The app itself has no login; without Access anyone who can reach it can create batches. Batch URLs contain random bearer secrets, so only share a URL when you mean to share that batch. [Cloudflare Access setup](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)

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
| Tunnel disconnected | Token in Coolify, tunnel logs, outbound TCP/UDP 7844, DNS resolution |
| Cloudflare 502 | Route is `http://converter:8000`; converter is healthy; both containers share the stack network |
| Cloudflare 1033 | Connector has not connected; inspect tunnel status/logs |
| Redirect loop | Use HTTP to the converter; remove any Coolify domain/proxy route for this app |
| 413 during upload | Zone/proxy upload limits must allow 8 MiB; app also enforces file and batch limits |
| 403 during upload | Cloudflare Access session/WAF rules, and an empty Host Header override on the tunnel route |
| 429 / server busy | Delete a completed batch or wait for expired batches to be cleaned |
| 507 / storage error | Free disk or lower batch/concurrency limits |
| Batch disappeared | Retention expired or converter restarted/redeployed |
| Decoder killed / timeout | Review image validity, container memory, and timeout settings |

Rebuild regularly to receive Debian/libheif updates. The cloudflared image uses `latest`; pin it to a tested release/digest if your deployment policy requires that. Updating/redeploying interrupts temporary jobs.

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
