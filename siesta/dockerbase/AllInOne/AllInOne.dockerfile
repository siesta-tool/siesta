# Siesta all-in-one image (balaktsis/siesta): Siesta API + Spark standalone
# master/worker + MinIO in one container, supervised by supervisord.
#
# Build from the repository root (BuildKit picks up AllInOne.dockerfile.dockerignore):
#   siesta/dockerbase/AllInOne/build.sh [--push] [image:tag ...]
#
# Network exposure is decided at start-up by the SIESTA_*_PORT variables: a service
# whose port variable is set listens on 0.0.0.0:<port>; otherwise it listens on
# 127.0.0.1 only and cannot be reached from outside the container. See README.md.

# BASE_IMAGE is the image built from siesta/dockerbase/Spark/Spark.dockerfile.
# No default on purpose: the short name siesta/spark resolves to an unrelated, broken
# Docker Hub repository. build.sh builds and passes localhost/siesta-spark:4.0.1,
# which can never be pulled from Docker Hub.
ARG BASE_IMAGE
ARG MINIO_IMAGE=chainguard/minio:latest

FROM ${MINIO_IMAGE} AS minio

FROM ${BASE_IMAGE}

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends supervisor curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=minio /usr/bin/minio /usr/local/bin/minio

# Python dependencies of all Siesta modules (system site-packages of python3.12).
WORKDIR /workspace
COPY siesta/requirements.txt                    /tmp/req/siesta/requirements.txt
COPY siesta/api/requirements.txt                /tmp/req/siesta/api/requirements.txt
COPY siesta/core/requirements.txt               /tmp/req/siesta/core/requirements.txt
COPY siesta/storage/S3/requirements.txt         /tmp/req/siesta/storage/S3/requirements.txt
COPY siesta/modules/analyser/requirements.txt   /tmp/req/siesta/modules/analyser/requirements.txt
COPY siesta/modules/index/requirements.txt      /tmp/req/siesta/modules/index/requirements.txt
COPY siesta/modules/mine/requirements.txt       /tmp/req/siesta/modules/mine/requirements.txt
COPY siesta/modules/query/requirements.txt      /tmp/req/siesta/modules/query/requirements.txt
RUN find /tmp/req -name requirements.txt -printf '-r %p\n' | \
      xargs python3.12 -m pip install --no-cache-dir --prefer-binary && \
    rm -rf /tmp/req

# Runtime user (spark, uid 185 from the base image) gets a real home directory.
RUN usermod -d /home/spark spark && mkdir -p /home/spark && chown spark:spark /home/spark

# Pre-fetch the Maven packages the driver requests (spark.jars.packages in
# siesta/core/sparkManager.py), so the container starts without internet access.
ENV SPARK_IVY_DIR=/opt/siesta/ivy
RUN mkdir -p $SPARK_IVY_DIR && chown spark:spark $SPARK_IVY_DIR
USER spark
RUN env -u SPARK_HOME HOME=/home/spark python3.12 -c "from pyspark.sql import SparkSession; \
SparkSession.builder.master('local[1]') \
.config('spark.jars.packages', 'org.apache.hadoop:hadoop-aws:3.4.1,com.amazonaws:aws-java-sdk-bundle:1.12.540,io.delta:delta-spark_2.13:4.0.0,io.delta:delta-storage:4.0.0,org.apache.spark:spark-sql-kafka-0-10_2.13:4.0.0') \
.config('spark.jars.ivy', '$SPARK_IVY_DIR').getOrCreate().stop()"
USER root

# Application code (config/ is deliberately not copied: the system config is
# rendered from environment variables at start-up).
COPY main.py /workspace/main.py
COPY siesta  /workspace/siesta
RUN rm -rf /workspace/siesta/dockerbase && \
    mkdir -p /data /etc/siesta && ln -s /data/output /workspace/output

COPY siesta/dockerbase/AllInOne/render_config.py /usr/local/bin/siesta-render-config
COPY siesta/dockerbase/AllInOne/entrypoint.sh    /usr/local/bin/siesta-entrypoint
COPY siesta/dockerbase/AllInOne/run-api.sh       /usr/local/bin/siesta-run-api
RUN chmod 0755 /usr/local/bin/siesta-*

# ---- Configuration (override with -e / docker compose `environment`) --------
# Ports: unset = internal only (127.0.0.1). Set = listen on 0.0.0.0:<port>.
#   SIESTA_API_PORT              Siesta API (internal default 8000)
#   SIESTA_SPARK_UI_PORT         Spark application UI of the API's driver (4040)
#   SIESTA_SPARK_MASTER_PORT     Spark master RPC, for external workers (7077)
#   SIESTA_SPARK_MASTER_UI_PORT  Spark master web UI (8080)
#   SIESTA_SPARK_WORKER_UI_PORT  Spark worker web UI (8081)
#   SIESTA_MINIO_PORT            MinIO S3 API (9000)
#   SIESTA_MINIO_CONSOLE_PORT    MinIO web console (9001)
# Spark sizing / distributed execution (read by siesta/core/sparkManager.py):
#   SPARK_CORES_MAX, SPARK_SHUFFLE_PARTITIONS, SPARK_DRIVER_HOST,
#   SPARK_DRIVER_PORT, SPARK_BLOCKMANAGER_PORT are optional and unset by default.
ENV MINIO_ROOT_USER=minioadmin \
    MINIO_ROOT_PASSWORD=minioadmin \
    SPARK_WORKER_CORES=4 \
    SPARK_WORKER_MEMORY=8g \
    SPARK_DAEMON_MEMORY=1g \
    SPARK_DRIVER_MEMORY=4g \
    SPARK_EXECUTOR_CORES=2 \
    SPARK_EXECUTOR_MEMORY=3g \
    SPARK_EXECUTOR_MEMORY_OVERHEAD=512m
# Internal settings (not meant to be changed)
ENV SIESTA_PROJECT_ROOT=/workspace \
    SIESTA_CONFIG=/etc/siesta/siesta.config.json \
    PYSPARK_PYTHON=/usr/bin/python3.12 \
    PYSPARK_DRIVER_PYTHON=/usr/bin/python3.12 \
    SPARK_LOCAL_DIRS=/data/spark/tmp \
    SPARK_USER=spark \
    HADOOP_USER_NAME=spark \
    MPLCONFIGDIR=/tmp/matplotlib \
    SPARK_SUBMIT_OPTS=-Dlog4j.configurationFile=/opt/spark/conf/log4j2.properties

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${SIESTA_API_PORT:-8000}/health" >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/siesta-entrypoint"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
