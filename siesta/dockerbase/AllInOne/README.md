# Siesta all-in-one image (`balaktsis/siesta`)

Siesta indexes, queries, mines and analyses event logs (CSV, XES) on
Apache Spark. This image runs the whole stack in **one container**:

| Process | Port variable | Internal default |
|---|---|---|
| Siesta API (FastAPI) | `SIESTA_API_PORT` | 8000 |
| Spark application UI (the API's driver) | `SIESTA_SPARK_UI_PORT` | 4040 |
| Spark master (RPC, for external workers) | `SIESTA_SPARK_MASTER_PORT` | 7077 |
| Spark master web UI | `SIESTA_SPARK_MASTER_UI_PORT` | 8080 |
| Spark worker web UI | `SIESTA_SPARK_WORKER_UI_PORT` | 8081 |
| MinIO S3 API | `SIESTA_MINIO_PORT` | 9000 |
| MinIO web console | `SIESTA_MINIO_CONSOLE_PORT` | 9001 |

**Exposure rule:** a service whose port variable is set listens on `0.0.0.0:<port>`.
If the variable is not set, the service listens on `127.0.0.1` at its internal default
port and can only be reached from inside the container. Nothing is exposed by
default, not even the API.

All data (MinIO objects, Spark scratch space, `output/`) lives in the `/data` volume.
The Spark jars (Delta Lake, S3) are baked in, so the container needs no internet access
at runtime.

## Quick start

```bash
docker run -d --name siesta \
  -e SIESTA_API_PORT=8000 -p 8000:8000 \
  -v siesta-data:/data \
  -e MINIO_ROOT_PASSWORD=change-me-please \
  balaktsis/siesta:latest
```

Startup takes about a minute (`docker logs -f siesta` shows `Uvicorn running`). Then open
`http://localhost:8000/docs`.

With the default bridge network, publish each port you set with `-p <port>:<port>`. With
`--network host`, setting the variable is enough. Note that the internal-only services
then sit on the host's loopback, so their default ports must be free there.

Example: index a CSV log, then query it:

```bash
curl -X POST http://localhost:8000/indexing/run \
  -F 'index_config={"log_name":"mylog","storage_namespace":"siesta","field_mappings":{"csv":{"trace_id":"trace_id","activity":"activity","start_timestamp":"timestamp"}}}' \
  -F "log_file=@mylog.csv"
```

## docker compose

`docker-compose.yml` and `.env.example` in this directory:

```bash
cp .env.example .env   # ports, passwords, Spark sizing
docker compose up -d
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `SIESTA_*_PORT` | unset | See the table above. Ports must be distinct. |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `minioadmin` / `minioadmin` | Storage credentials, also used by Siesta (password ≥ 8 characters). **Change them.** |
| `SPARK_WORKER_CORES` | `4` | Cores the in-container Spark worker offers. |
| `SPARK_WORKER_MEMORY` | `8g` | Memory the worker offers to executors. |
| `SPARK_DAEMON_MEMORY` | `1g` | Heap of the Spark master and worker daemons. |
| `SPARK_DRIVER_MEMORY` | `4g` | Heap of the API's Spark driver. |
| `SPARK_EXECUTOR_CORES` / `SPARK_EXECUTOR_MEMORY` | `2` / `3g` | Size of each executor. |
| `SPARK_EXECUTOR_MEMORY_OVERHEAD` | `512m` | Off-heap memory per executor. |
| `SPARK_CORES_MAX` | unset | Cap on total cores used by Siesta. |
| `SPARK_SHUFFLE_PARTITIONS` | unset | Fixed `spark.sql.shuffle.partitions` / `spark.default.parallelism`. |
| `SPARK_DRIVER_HOST` | unset | Address external executors use to reach the driver. When set, the driver binds to `0.0.0.0`. |
| `SPARK_DRIVER_PORT` / `SPARK_BLOCKMANAGER_PORT` | unset (random) | Fixed driver ports, so they can be published. |
| `SIESTA_CONFIG_OVERRIDE` | unset | Path to a mounted JSON file merged into the Siesta system config (e.g. `s3_endpoint`, `spark_master`). `s3_access_key`, `s3_secret_key` and `api` are managed by the container and ignored. |

Rough memory need with the defaults: 4g driver + 8g worker + 2g daemons + ~1g other, about 15 GB.

Invalid settings (bad or clashing port, short password) stop the container at startup
with an error in the logs.

### External Spark workers (distributed execution)

You can add Spark workers on other machines to the in-container master:

1. Set and publish `SIESTA_SPARK_MASTER_PORT`, `SPARK_DRIVER_PORT` and `SPARK_BLOCKMANAGER_PORT`,
   and set `SPARK_DRIVER_HOST` to this host's address as seen by the workers.
2. Expose MinIO (`SIESTA_MINIO_PORT`). Using `SIESTA_CONFIG_OVERRIDE`, set `s3_endpoint` to
   an address the remote executors can resolve (e.g. `http://<this-host>:<port>`).
3. Start workers with `spark-class org.apache.spark.deploy.worker.Worker spark://<this-host>:<port>`
   from an image with the same Spark 4.0.1 / Python 3.12 setup
   (`siesta/dockerbase/Spark/Spark.dockerfile`).

For a fully multi-node deployment, `docker-compose.distributed.yml` at the repository root
is usually simpler.

## Notes

- There is no TLS and no API authentication. Expose only what you need, and put a reverse
  proxy in front for anything public.
- Back up your data by backing up the `/data` volume.

## Build (maintainers)

The image is published for `linux/amd64` and `linux/arm64` (Apple Silicon, ARM servers), on
Docker Hub (`balaktsis/siesta`) and on GitHub Packages (`ghcr.io/siesta-tool/siesta`). Pushing
needs `docker login` for Docker Hub, and for ghcr.io a token with `write:packages`
(`gh auth token | docker login ghcr.io -u <github-user> --password-stdin`).

```bash
# Once per boot, to build the non-native architecture under emulation
docker run --privileged --rm tonistiigi/binfmt --install arm64

siesta/dockerbase/AllInOne/build.sh                      # build both, load locally
PLATFORMS=linux/amd64 siesta/dockerbase/AllInOne/build.sh   # quick single-arch build

# Release: push `latest` plus a version tag to both registries
siesta/dockerbase/AllInOne/build.sh --push \
  balaktsis/siesta:latest balaktsis/siesta:<version> \
  ghcr.io/siesta-tool/siesta:latest ghcr.io/siesta-tool/siesta:<version>
```

`build.sh` runs `docker buildx bake` with [`docker-bake.hcl`](docker-bake.hcl). It builds the
Spark base from `siesta/dockerbase/Spark/Spark.dockerfile` in the same run and never pulls it:
the Docker Hub image `siesta/spark` is unrelated and broken. Loading a multi-architecture image
locally (no `--push`) needs Docker's containerd image store. The emulated half of the build is
slow, so expect a long first build. MinIO comes from `chainguard/minio:latest` (override with
`MINIO_IMAGE`). The image contains `siesta/` and `main.py`, but not `config/`.
