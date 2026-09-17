#!/usr/bin/env python3
"""Astral: a small, safer package installer for source and binary packages."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin, urlparse

import requests


APP_DIR = Path(os.environ.get("ASTRAL_HOME", "~/.config/astral")).expanduser()
CONFIG_PATH = APP_DIR / "astral.toml"
LOCK_PATH = APP_DIR / "astral.lock"
SOURCE_DIR = Path(os.environ.get("ASTRAL_SOURCE_DIR", "~/.astral/src")).expanduser()
DEFAULT_BIN_PATH = Path(os.environ.get("ASTRAL_BIN_PATH", "~/.local/bin")).expanduser()
ARCHIVE_TIMEOUT = (5, 30)
IGNORED_NAMES = {".git", ".gitignore", ".github", ".gitattributes"}
VERBOSE = False
_LOCK_MUTEX = Lock()


class AstralError(RuntimeError):
    """An expected Astral error."""


def log(message: str) -> None:
    print(f"\033[1;36m[astral]\033[0m {message}")


def verbose(message: str) -> None:
    if VERBOSE:
        print(f"\033[90m[astral {time.strftime('%H:%M:%S')}]\033[0m {message}")


def error(message: str) -> None:
    print(f"\033[1;31m[astral error]\033[0m {message}", file=sys.stderr)


def ensure_state() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            '[repos]\nmain = "https://raw.githubusercontent.com/anorberg001-web/Testingserverforast/main"\n',
            encoding="utf-8",
        )
    if not LOCK_PATH.exists():
        atomic_write_json(LOCK_PATH, {})


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_config() -> dict:
    ensure_state()
    try:
        with CONFIG_PATH.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AstralError(f"Invalid configuration: {exc}") from exc


def load_lock() -> dict:
    ensure_state()
    with _LOCK_MUTEX:
        try:
            return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            verbose(f"Resetting unreadable lockfile: {exc}")
            atomic_write_json(LOCK_PATH, {})
            return {}


def save_lock(lock: dict) -> None:
    with _LOCK_MUTEX:
        atomic_write_json(LOCK_PATH, lock)


def update_lock(name: str, **fields: object) -> None:
    lock = load_lock()
    if fields:
        lock[name] = {"name": name, **fields}
    else:
        lock.pop(name, None)
    save_lock(lock)


def safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    """Extract a ZIP only when every member stays inside destination."""
    destination = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if target != destination and destination not in target.parents:
            raise AstralError(f"Unsafe archive member: {member.filename}")
    archive.extractall(destination)


def request(session: requests.Session, url: str, **kwargs) -> requests.Response:
    response = session.get(url, timeout=ARCHIVE_TIMEOUT, **kwargs)
    response.raise_for_status()
    return response


def ping_repo(url: str, package: str) -> tuple[float, str, dict | None]:
    base = url.rstrip("/")
    target = f"{base}/{package}/package.toml"
    started = time.monotonic()
    try:
        with requests.Session() as session:
            response = session.get(target, timeout=(3, 5))
            if response.status_code != 200:
                return float("inf"), base, None
            manifest = tomllib.loads(response.text)
        return time.monotonic() - started, base, manifest
    except (OSError, requests.RequestException, tomllib.TOMLDecodeError) as exc:
        verbose(f"Mirror {base} failed: {exc}")
        return float("inf"), base, None


def fastest_repo(package: str) -> tuple[str, dict]:
    mirrors = list(load_config().get("repos", {}).values())
    if not mirrors:
        raise AstralError("No repositories are configured in astral.toml")
    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(mirrors))) as pool:
        futures = [pool.submit(ping_repo, url, package) for url in mirrors]
        for future in as_completed(futures):
            results.append(future.result())
    valid = sorted((item for item in results if item[2] is not None), key=lambda item: item[0])
    if not valid:
        raise AstralError(f"Package {package!r} was not found in any configured mirror")
    elapsed, base, manifest = valid[0]
    verbose(f"Selected {base} in {elapsed:.3f}s")
    return base, manifest or {}


def metadata(base: str, package: str) -> tuple[dict, dict]:
    result: dict = {}
    dependencies: dict = {}
    with requests.Session() as session:
        for filename, target in (("package.toml", result), ("dependencies.toml", dependencies)):
            try:
                response = session.get(f"{base.rstrip('/')}/{package}/{filename}", timeout=ARCHIVE_TIMEOUT)
                if response.status_code == 200:
                    parsed = tomllib.loads(response.text)
                    target.update(parsed.get("metadata" if filename == "package.toml" else "dependencies", {}))
            except (requests.RequestException, tomllib.TOMLDecodeError) as exc:
                verbose(f"Unable to read {filename}: {exc}")
    result.setdefault("name", package)
    return result, dependencies


def confirm(package: dict, dependencies: dict) -> bool:
    print(f"Package: {package.get('name', 'unknown')}")
    print(f"Version: {package.get('version', 'unknown')}")
    print(f"Description: {package.get('description', '')}")
    if dependencies:
        print("Dependencies: " + ", ".join(f"{name} ({version})" for name, version in dependencies.items()))
    return input("Install this package? [y/N]: ").strip().lower() in {"y", "yes"}


def download(url: str, destination: Path) -> None:
    log(f"Downloading {url}")
    with requests.get(url, stream=True, timeout=ARCHIVE_TIMEOUT) as response:
        response.raise_for_status()
        with destination.open("wb") as output:
            for chunk in response.iter_content(1024 * 64):
                if chunk:
                    output.write(chunk)


def source_archive(base: str, package: str, destination: Path) -> None:
    archive_url = f"{base.rstrip('/')}/{package}.zip"
    try:
        download(archive_url, destination)
        return
    except requests.RequestException as exc:
        verbose(f"Archive endpoint failed: {exc}")

    source_url = f"{base.rstrip('/')}/{package}/"
    with requests.Session() as session, tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / package
        if not crawl_directory(session, source_url, root, set()):
            raise AstralError(f"Could not download source for {package!r}")
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in root.rglob("*"):
                if file.is_file() and file.name not in IGNORED_NAMES:
                    archive.write(file, file.relative_to(root))


def crawl_directory(session: requests.Session, url: str, destination: Path, visited: set[str]) -> bool:
    url = url.rstrip("/") + "/"
    if url in visited:
        return True
    visited.add(url)
    try:
        response = request(session, url)
        content_type = response.headers.get("content-type", "")
        destination.mkdir(parents=True, exist_ok=True)
        if "application/json" in content_type:
            entries = response.json()
            for entry in entries:
                if entry.get("type") == "file":
                    (destination / entry["name"]).write_bytes(request(session, entry["download_url"]).content)
                elif entry.get("type") == "dir":
                    crawl_directory(session, entry["url"], destination / entry["name"], visited)
            return True

        from html.parser import HTMLParser

        class Links(HTMLParser):
            links: list[str] = []
            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    href = dict(attrs).get("href")
                    if href:
                        self.links.append(href)

        parser = Links()
        parser.feed(response.text)
        for href in parser.links:
            if href in {"../", "./", "/"} or href.startswith(("?", "#")):
                continue
            child = urljoin(url, href)
            parsed = urlparse(child)
            if parsed.netloc != urlparse(url).netloc or not child.startswith(url):
                continue
            name = Path(parsed.path.rstrip("/")).name
            if not name or name in IGNORED_NAMES:
                continue
            if href.endswith("/"):
                crawl_directory(session, child, destination / name, visited)
            else:
                (destination / name).write_bytes(request(session, child).content)
        return True
    except (OSError, requests.RequestException, ValueError) as exc:
        verbose(f"Directory crawl failed at {url}: {exc}")
        return False


def read_package(root: Path) -> tuple[dict, dict]:
    manifest = root / "package.toml"
    dependencies_file = root / "dependencies.toml"
    config = tomllib.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    dependencies = tomllib.loads(dependencies_file.read_text(encoding="utf-8")).get("dependencies", {}) if dependencies_file.exists() else {}
    package = dict(config.get("metadata", {}))
    package.setdefault("name", root.name)
    return package, dependencies


def find_binary(root: Path, package: dict) -> Path:
    name = package.get("binary", package["name"])
    names = [name, f"{name}.exe"] if os.name == "nt" else [name]
    candidates = [root / "bin" / item for item in names]
    candidates += [root / "target" / "release" / item for item in names]
    candidates += [root / item for item in names]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AstralError(f"Declared binary {name!r} was not found")


def install_root(root: Path, package: dict, binary_path: Path) -> None:
    build = package.get("build")
    if build:
        subprocess.run(build, cwd=root, shell=True, check=True)
    elif (root / "Cargo.toml").exists():
        subprocess.run(["cargo", "build", "--release"], cwd=root, check=True)
    source = find_binary(root, package)
    binary_path.mkdir(parents=True, exist_ok=True)
    target = binary_path / source.name
    shutil.copy2(source, target)
    target.chmod(0o755)
    update_lock(package["name"], binary_path=str(binary_path), version=package.get("version", "unknown"), binary=str(target))
    log(f"Installed {package['name']} -> {target}")


def install(package_name: str, binary_path: Path, link: str | None = None, keep_source: bool = False, seen: set[str] | None = None) -> None:
    seen = set() if seen is None else seen
    if package_name in seen:
        raise AstralError(f"Dependency cycle detected at {package_name!r}")
    seen.add(package_name)
    base, remote_manifest = (link.rsplit("/", 1)[0], {}) if link else fastest_repo(package_name)
    package, dependencies = metadata(base, package_name) if link else metadata(base, package_name)
    package = {**remote_manifest.get("metadata", {}), **package}
    package.setdefault("name", package_name)
    if not confirm(package, dependencies):
        log("Installation cancelled")
        return
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "package.zip"
        source_archive(base, package_name, archive)
        extracted = Path(temporary) / "extracted"
        extracted.mkdir()
        with zipfile.ZipFile(archive) as zip_file:
            safe_extract(zip_file, extracted)
        entries = list(extracted.iterdir())
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else extracted
        local_package, local_dependencies = read_package(root)
        package.update(local_package)
        for dependency in local_dependencies:
            if dependency not in load_lock():
                install(dependency, binary_path, keep_source=keep_source, seen=seen)
        install_root(root, package, binary_path)
        if keep_source:
            destination = SOURCE_DIR / package["name"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(root, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*IGNORED_NAMES))


def remove(package: str, binary: bool = False) -> None:
    record = load_lock().get(package)
    if not record:
        raise AstralError(f"Package {package!r} is not installed")
    target = Path(record.get("binary", ""))
    if target.exists():
        target.unlink()
    source = SOURCE_DIR / package
    if source.exists():
        shutil.rmtree(source)
    update_lock(package)
    log(f"Removed {package}")


def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(prog="astral")
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install")
    install_parser.add_argument("package")
    install_parser.add_argument("--link")
    install_parser.add_argument("--keep-source", action="store_true")
    install_parser.add_argument("--bin-path", type=Path, default=DEFAULT_BIN_PATH)
    remove_parser = commands.add_parser("remove")
    remove_parser.add_argument("package")
    remove_parser.add_argument("-b", "--binary", action="store_true")
    commands.add_parser("lock").add_argument("--clean", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose
    try:
        if args.command == "install":
            install(args.package, args.bin_path, args.link, args.keep_source)
        elif args.command == "remove":
            remove(args.package, args.binary)
        elif args.command == "lock":
            lock = load_lock()
            if args.clean:
                save_lock({name: item for name, item in lock.items() if Path(item.get("binary", "")).exists()})
            else:
                print(json.dumps(lock, indent=2))
        return 0
    except (AstralError, OSError, subprocess.CalledProcessError) as exc:
        error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
