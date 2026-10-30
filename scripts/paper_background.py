"""Start a detached local PAPER server using its existing account and rotating logs."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
from pathlib import Path

from app.config.loader import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = (root / args.config).resolve()
    config = load_config(config_path)
    if config.app.mode != "paper" or config.api.host != "127.0.0.1":
        parser.error("Only local PAPER profiles are supported")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", config.api.port)) == 0:
            parser.error(f"Port {config.api.port} is already in use; no duplicate server started")
    folder = root / "datasets" / "runtime"
    folder.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CLE_CONFIG": str(config_path),
        "PYTHONPATH": str(root / "backend"),
        "CLE_LOG_FILE": str(folder / f"paper-{config.api.port}.log"),
        "PYTHONUNBUFFERED": "1",
    }
    with (folder / f"paper-{config.api.port}-startup.log").open("w") as output:
        child = subprocess.Popen(
            [
                str(root / ".venv/bin/python"),
                "-m",
                "uvicorn",
                "app.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(config.api.port),
                "--no-access-log",
            ],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    metadata = {
        "pid": child.pid,
        "config": str(config_path),
        "port": config.api.port,
        "database": config.paper.database_path,
        "log": env["CLE_LOG_FILE"],
    }
    (folder / f"paper-{config.api.port}.json").write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata))
    print("Process spawned. Wait for /api/health, then explicitly start PAPER via /api/paper/start.")


if __name__ == "__main__":
    main()
