#!/usr/bin/python3.12
"""Render the Siesta system config and the supervisord config from environment variables.

Runs as root from the container entrypoint, before supervisord starts, and exits
non-zero (stopping the container) on invalid settings.

Exposure rule: every service has a SIESTA_*_PORT variable. When it is set, the
service listens on 0.0.0.0:<port> (reachable from outside the container once the
port is published, or directly with --network host). When it is not set, the
service listens on 127.0.0.1:<internal default> and is only reachable inside the
container.
"""
import json
import os
import sys

SIESTA_CONFIG = os.environ.get("SIESTA_CONFIG", "/etc/siesta/siesta.config.json")
SUPERVISORD_CONF = "/etc/supervisor/supervisord.conf"

# name -> (environment variable, internal default port)
SERVICES = {
    "api":            ("SIESTA_API_PORT",             8000),
    "spark_ui":       ("SIESTA_SPARK_UI_PORT",        4040),
    "spark_master":   ("SIESTA_SPARK_MASTER_PORT",    7077),
    "spark_master_ui": ("SIESTA_SPARK_MASTER_UI_PORT", 8080),
    "spark_worker_ui": ("SIESTA_SPARK_WORKER_UI_PORT", 8081),
    "minio":          ("SIESTA_MINIO_PORT",           9000),
    "minio_console":  ("SIESTA_MINIO_CONSOLE_PORT",   9001),
}

# Keys wired to the container; a SIESTA_CONFIG_OVERRIDE file cannot change them.
MANAGED_KEYS = {"s3_access_key", "s3_secret_key", "api"}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def fail(msg: str) -> None:
    print(f"[siesta] configuration error: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_port(name: str, value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        fail(f"{name}: '{value}' is not a port number (1-65535)")
    return int(value)


def resolve_ports() -> dict:
    """Return {service: (bind_address, port, exposed)} and check for clashes."""
    resolved = {}
    for service, (var, default) in SERVICES.items():
        value = env(var)
        if value:
            resolved[service] = ("0.0.0.0", parse_port(var, value), True)
        else:
            resolved[service] = ("127.0.0.1", default, False)

    used = {service: port for service, (_, port, _) in resolved.items()}
    for var in ("SPARK_DRIVER_PORT", "SPARK_BLOCKMANAGER_PORT"):
        if env(var):
            used[var] = parse_port(var, env(var))
    seen = {}
    for owner, port in used.items():
        if port in seen:
            fail(f"port {port} is used by both {seen[port]} and {owner}; pick another port")
        seen[port] = owner
    return resolved


def siesta_config(ports: dict) -> dict:
    user = env("MINIO_ROOT_USER", "minioadmin")
    password = env("MINIO_ROOT_PASSWORD", "minioadmin")
    if len(user) < 3:
        fail("MINIO_ROOT_USER must be at least 3 characters")
    if len(password) < 8:
        fail("MINIO_ROOT_PASSWORD must be at least 8 characters")

    api_bind, api_port, _ = ports["api"]
    # Spark sizing keys (spark_driver_memory, ...) are deliberately absent:
    # sparkManager.py prefers config keys over SPARK_* env vars, and the env vars
    # are the documented way to size this container.
    config = {
        "App Name": "Siesta Framework",
        "storage_type": "s3",
        "s3_access_key": user,
        "s3_secret_key": password,
        "s3_endpoint": f"http://minio:{ports['minio'][1]}",
        "s3_region": "us-east-1",
        "spark_master": f"spark://127.0.0.1:{ports['spark_master'][1]}",
        "spark_app_name": "SiestaFramework",
        "kafka_bootstrap_servers": "localhost:9092",
        "raw_events_dir": "raw_events",
        "checkpoint_dir": "checkpoints",
        "api": {"host": api_bind, "port": api_port},
        "enable_timing": True,
        "enable_streaming": False,
    }

    override_path = env("SIESTA_CONFIG_OVERRIDE")
    if override_path:
        try:
            with open(override_path) as f:
                override = json.load(f)
        except (OSError, ValueError) as e:
            fail(f"cannot read SIESTA_CONFIG_OVERRIDE '{override_path}': {e}")
        if not isinstance(override, dict):
            fail("SIESTA_CONFIG_OVERRIDE must contain a JSON object")
        for key, value in override.items():
            if key in MANAGED_KEYS:
                print(f"[siesta] ignoring '{key}' from {override_path}: it is managed by the container", file=sys.stderr)
                continue
            config[key] = value
    return config


def spark_submit_args(ports: dict) -> str:
    """Extra spark-submit options for the API's driver (via PYSPARK_SUBMIT_ARGS)."""
    args = [f"--conf spark.ui.port={ports['spark_ui'][1]}"]
    # With SPARK_DRIVER_HOST set, sparkManager.py binds the driver to 0.0.0.0 and
    # advertises that host (distributed execution). Otherwise keep it on loopback.
    if not env("SPARK_DRIVER_HOST"):
        args += ["--conf spark.driver.host=127.0.0.1", "--conf spark.driver.bindAddress=127.0.0.1"]
    return " ".join(args + ["pyspark-shell"])


def supervisord_config(ports: dict) -> str:
    master_bind, master_port, _ = ports["spark_master"]
    minio_bind, minio_port, _ = ports["minio"]
    console_bind, console_port, _ = ports["minio_console"]
    # Spark web UIs bind to SPARK_LOCAL_IP (0.0.0.0 when unset), per process.
    master_ui_ip = ports["spark_master_ui"][0]
    worker_ui_ip = ports["spark_worker_ui"][0]
    driver_ui_ip = ports["spark_ui"][0]

    def program(name, command, priority, extra_env="", extra=""):
        environment = 'HOME="/home/spark"' + (f",{extra_env}" if extra_env else "")
        return f"""
[program:{name}]
command={command}
user=spark
directory=/workspace
environment={environment}
priority={priority}
autorestart=true
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
redirect_stderr=true
{extra}"""

    return (
        """[supervisord]
nodaemon=true
user=root
logfile=/dev/null
logfile_maxbytes=0
pidfile=/tmp/supervisord.pid

[unix_http_server]
file=/tmp/supervisor.sock
chmod=0700

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix:///tmp/supervisor.sock
"""
        + program(
            "minio",
            f"/usr/local/bin/minio server /data/minio --address {minio_bind}:{minio_port} "
            f"--console-address {console_bind}:{console_port}",
            10,
        )
        + program(
            "spark-master",
            f"/opt/spark/bin/spark-class org.apache.spark.deploy.master.Master --host {master_bind} "
            f"--port {master_port} --webui-port {ports['spark_master_ui'][1]}",
            20,
            extra_env=f'SPARK_LOCAL_IP="{master_ui_ip}"',
        )
        + program(
            "spark-worker",
            f"/opt/spark/bin/spark-class org.apache.spark.deploy.worker.Worker spark://127.0.0.1:{master_port} "
            f"--host 127.0.0.1 --webui-port {ports['spark_worker_ui'][1]} --work-dir /data/spark/work",
            20,
            extra_env=f'SPARK_LOCAL_IP="{worker_ui_ip}"',
        )
        + program(
            "siesta-api",
            "/usr/local/bin/siesta-run-api",
            30,
            extra_env=(
                f'SPARK_LOCAL_IP="{driver_ui_ip}",'
                f'PYSPARK_SUBMIT_ARGS="{spark_submit_args(ports)}",'
                f'SIESTA_MINIO_ADDR="127.0.0.1:{minio_port}",'
                f'SIESTA_MASTER_ADDR="127.0.0.1:{master_port}"'
            ),
            extra="startsecs=20\nstopwaitsecs=60\n",
        )
    )


def main() -> None:
    ports = resolve_ports()
    config = siesta_config(ports)

    os.makedirs(os.path.dirname(SIESTA_CONFIG), exist_ok=True)
    with open(SIESTA_CONFIG, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(SIESTA_CONFIG, 0o640)
    os.chown(SIESTA_CONFIG, 0, 185)  # root:spark, the API runs as spark

    with open(SUPERVISORD_CONF, "w") as f:
        f.write(supervisord_config(ports))

    for service, (var, _) in SERVICES.items():
        bind, port, exposed = ports[service]
        where = f"exposed on 0.0.0.0:{port}" if exposed else f"internal only (127.0.0.1:{port})"
        print(f"[siesta] {service:<16} {where}")


if __name__ == "__main__":
    main()
