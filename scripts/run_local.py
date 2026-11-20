"""Keep the local API, Streamlit UI and private object store running together."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def require_free_port(port):
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit(f"Port {port} is already in use. Stop its owning application or use the existing tracker.")


def wait_ready(process, url, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"A local service exited before startup. Inspect logs under {ROOT / '.local'}.")
        try:
            response = httpx.get(url, timeout=2, trust_env=False)
            if response.status_code == 200:
                return
        except httpx.TransportError:
            pass  # Services are expected to reject connections while starting.
        time.sleep(0.5)
    raise RuntimeError(f"Service did not become ready at {url}. Inspect local logs.")


def main():
    load_dotenv(ROOT / ".env")
    for port in (8000, 8501, 9000, 9001):
        require_free_port(port)
    binary = ROOT / ".local" / "bin" / "minio.exe"
    if not binary.exists():
        raise SystemExit("MinIO is missing. Run setup_local.ps1 first.")
    if not os.getenv("OBJECT_STORAGE_ACCESS_KEY") or not os.getenv("OBJECT_STORAGE_SECRET_KEY"):
        raise SystemExit("Object-storage credentials are missing. Run scripts\\bootstrap.py first.")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True)
    environment = os.environ.copy()
    environment["MINIO_ROOT_USER"] = os.environ["OBJECT_STORAGE_ACCESS_KEY"]
    environment["MINIO_ROOT_PASSWORD"] = os.environ["OBJECT_STORAGE_SECRET_KEY"]
    environment["MINIO_BROWSER"] = "off"
    environment["MINIO_UPDATE"] = "off"
    processes = []
    handles = []

    def start(command, log_name, env=None):
        handle = (ROOT / ".local" / log_name).open("a", encoding="utf-8")
        handles.append(handle)
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    try:
        storage = start(
            [str(binary), "server", str(ROOT / ".local" / "objects"),
             "--address", "127.0.0.1:9000", "--console-address", "127.0.0.1:9001"],
            "minio.log", environment,
        )
        wait_ready(storage, "http://127.0.0.1:9000/minio/health/live")
        api = start(
            [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000"],
            "backend.log",
        )
        wait_ready(api, "http://127.0.0.1:8000/health")
        ui = start(
            [sys.executable, "-m", "streamlit", "run", "frontend\\app.py", "--server.address", "127.0.0.1",
             "--server.port", "8501", "--server.headless", "true", "--browser.gatherUsageStats", "false"],
            "streamlit.log",
        )
        wait_ready(ui, "http://127.0.0.1:8501/_stcore/health")
        print("Nutrition Tracker is ready: http://localhost:8501", flush=True)
        print("API documentation: http://localhost:8000/docs", flush=True)
        print("All services listen on loopback only. Press Ctrl+C in this launcher to stop.", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(1)
        raise RuntimeError("A tracker service stopped unexpectedly. Inspect the logs in .local.")
    except KeyboardInterrupt:
        print("Stopping local tracker services.")
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()
