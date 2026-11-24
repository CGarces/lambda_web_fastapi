"""Package FastAPI Lambdas for AWS Lambda arm64 without external Python dependencies.

- Builds build/events.zip (FastAPI + Uvicorn for Python 3.15 arm64 + run.sh) and
  uploads it to the Terraform S3 bucket before running terraform plan/apply.
- Prepares build/web/ (FastAPI for Python 3.15 arm64 + main.py) so Terraform can
  package it with data "archive_file" and deploy it to Lambda Web Functions.
"""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

DEFAULT_S3_KEY = "fastapi-benchmark/events.zip"
DEFAULT_REGION = "eu-west-1"


def install_dependencies(
    target_dir: Path, python_version: str, packages: list[str]
) -> None:
    """Install Linux aarch64 binary wheels into target_dir using pip."""
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--quiet",
        "--target",
        str(target_dir),
        "--platform",
        "manylinux2014_aarch64",
        "--implementation",
        "cp",
        "--python-version",
        python_version,
        "--only-binary=:all:",
        *packages,
    ]
    print(f"Installing {packages} for cp{python_version} aarch64 into {target_dir}...")
    subprocess.run(cmd, check=True)


def remove_pycache(root_dir: Path) -> None:
    """Strip __pycache__ directories for smaller, deterministic archives."""
    for cache_dir in root_dir.rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)


def create_zip_from_dir(
    source_dir: Path, zip_path: Path, executable_files: set[str]
) -> None:
    """Create a zip archive preserving Unix LF line endings and executable bits."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(source_dir.rglob("*")):
            if not file_path.is_file():
                continue
            rel_posix = file_path.relative_to(source_dir).as_posix()
            data = file_path.read_bytes()
            if rel_posix in executable_files:
                data = data.replace(b"\r\n", b"\n")
                mode = 0o100755
            else:
                mode = 0o100644

            info = zipfile.ZipInfo(rel_posix)
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix permissions
            info.external_attr = mode << 16
            zf.writestr(info, data)


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_to_s3(
    local_path: Path, bucket: str, key: str, profile: str | None, region: str
) -> None:
    """Upload the packaged zip to Amazon S3 using the AWS CLI."""
    s3_uri = f"s3://{bucket}/{key}"
    cmd = [
        "aws",
        "s3",
        "cp",
        str(local_path),
        s3_uri,
        "--region",
        region,
    ]
    if profile:
        cmd.extend(["--profile", profile])
    print(f"Uploading {local_path} -> {s3_uri}...")
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(
        description="Package FastAPI Lambdas for Terraform."
    )
    parser.add_argument("--bucket", help="Terraform S3 bucket.")
    parser.add_argument(
        "--events-key", default=DEFAULT_S3_KEY, help="S3 object key for events.zip."
    )
    parser.add_argument("--profile", help="AWS CLI profile.")
    parser.add_argument("--region", default=DEFAULT_REGION, help="AWS region.")
    parser.add_argument(
        "--skip-upload", action="store_true", help="Build locally without S3 upload."
    )
    args = parser.parse_args()
    if not args.skip_upload and not args.bucket:
        parser.error("--bucket is required unless --skip-upload is set.")

    repo_root = Path(__file__).resolve().parent.parent
    build_root = repo_root / "build"
    events_build = build_root / "events"
    web_build = build_root / "web"
    events_zip = build_root / "events.zip"

    for folder in (events_build, web_build):
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True, exist_ok=True)

    # 1. Classic Lambda + Lambda Web Adapter (Python 3.15 + FastAPI + Uvicorn)
    install_dependencies(events_build, "3.15", ["fastapi", "uvicorn"])
    shutil.copy2(
        repo_root / "lambdas" / "events_fastapi" / "main.py", events_build / "main.py"
    )
    run_sh_src = repo_root / "lambdas" / "events_fastapi" / "run.sh"
    (events_build / "run.sh").write_bytes(
        run_sh_src.read_bytes().replace(b"\r\n", b"\n")
    )
    remove_pycache(events_build)
    create_zip_from_dir(events_build, events_zip, executable_files={"run.sh"})

    events_size_mb = events_zip.stat().st_size / (1024 * 1024)
    print(
        f"Created {events_zip} ({events_size_mb:.2f} MB, sha256={sha256_file(events_zip)})"
    )

    if not args.skip_upload:
        upload_to_s3(
            events_zip, args.bucket, args.events_key, args.profile, args.region
        )

    # 2. AWS Lambda Web Function (Python 3.15 + FastAPI, packaged by Terraform archive_file)
    install_dependencies(web_build, "3.15", ["fastapi"])
    shutil.copy2(
        repo_root / "lambdas" / "web_fastapi" / "main.py", web_build / "main.py"
    )
    remove_pycache(web_build)

    web_files = sum(1 for p in web_build.rglob("*") if p.is_file())
    print(f"Prepared {web_build} ({web_files} files) ready for Terraform archive_file.")


if __name__ == "__main__":
    main()
