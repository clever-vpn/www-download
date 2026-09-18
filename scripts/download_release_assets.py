#!/usr/bin/env python3

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


GITHUB_API = "https://api.github.com"
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def fail(message: str) -> "NoReturn":
    print(message, file=sys.stderr)
    raise SystemExit(1)


def github_json(path: str, token: str | None, allow_404: bool = False) -> dict | None:
    request = urllib.request.Request(f"{GITHUB_API}{path}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "www-download-release-workflow")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if allow_404 and error.code == 404:
            return None
        body = error.read().decode("utf-8", errors="replace")
        fail(f"GitHub API request failed for {path}: HTTP {error.code} {body}")


def load_config(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    apps = raw.get("apps")
    if not isinstance(apps, list) or not apps:
        fail("config/apps.json must contain a non-empty apps array.")

    for index, app in enumerate(apps, start=1):
        repo = app.get("repo")
        target_dir = app.get("target_dir")
        source_release_tag = app.get("source_release_tag")
        suffixes = app.get("asset_suffixes")
        if not isinstance(repo, str) or "/" not in repo:
            fail(f"apps[{index}] is missing a valid repo value like owner/name.")
        if not isinstance(target_dir, str) or not target_dir or "/" in target_dir or ".." in target_dir:
            fail(f"apps[{index}] has invalid target_dir. Use a simple folder name such as windows.")
        if not isinstance(source_release_tag, str) or not source_release_tag.strip():
            fail(f"apps[{index}] must define a non-empty source_release_tag.")
        if not isinstance(suffixes, list) or not suffixes or not all(isinstance(item, str) for item in suffixes):
            fail(f"apps[{index}] must define a non-empty asset_suffixes array.")
    return apps


def matching_assets(release: dict, suffixes: list[str]) -> list[dict]:
    release_assets = release.get("assets", [])
    return [
        asset
        for asset in release_assets
        if any(asset.get("name", "").endswith(suffix) for suffix in suffixes)
    ]


def download_asset(url: str, destination: Path, token: str | None) -> str:
    request = urllib.request.Request(url)
    request.add_header("User-Agent", "www-download-release-workflow")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    digest = hashlib.sha256()
    with urllib.request.urlopen(request) as response, destination.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            handle.write(chunk)
    return digest.hexdigest()


def validate_directory(raw_directory: str) -> str:
    directory = raw_directory.strip()
    if not SAFE_SEGMENT_RE.fullmatch(directory):
        fail(
            "--directory must be a single path segment using letters, digits, dot, dash or "
            "underscore, for example stable, test or v2.1.2."
        )
    return directory


def select_apps(apps: list[dict], requested: str) -> list[dict]:
    raw = (requested or "").strip()
    if not raw or raw.lower() == "all":
        return apps

    available = [app["target_dir"] for app in apps]
    selected: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if not name:
            continue
        if name not in available:
            fail(f"Unknown app '{name}'. Valid values: {', '.join(available)} or all.")
        if name not in selected:
            selected.append(name)

    if not selected:
        fail("No app selected. Use a comma-separated list of target_dir values or all.")

    return [app for app in apps if app["target_dir"] in selected]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--apps", default="all")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    directory = validate_directory(args.directory)
    apps = select_apps(load_config(Path(args.config)), args.apps)
    token = os.environ.get("SOURCE_GH_TOKEN") or os.environ.get("GH_TOKEN")

    directory_root = Path(args.output_dir) / directory
    directory_root.mkdir(parents=True, exist_ok=True)

    for app in apps:
        source_release_tag = app["source_release_tag"].strip()
        release = github_json(
            f"/repos/{app['repo']}/releases/tags/{source_release_tag}", token, allow_404=True
        )
        if release is None:
            fail(
                f"Release tag {source_release_tag} was not found in source repo {app['repo']}."
            )

        assets = matching_assets(release, app["asset_suffixes"])
        if not assets:
            fail(
                f"Release {source_release_tag} in {app['repo']} has no assets matching {app['asset_suffixes']}."
            )

        target_dir = directory_root / app["target_dir"]
        target_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "directory": directory,
            "platform": app["target_dir"],
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "repo": app["repo"],
            "source_release_tag": source_release_tag,
            "release_html_url": release.get("html_url"),
            "assets": [],
        }

        for asset in assets:
            name = asset["name"]
            if "/" in name or "\\" in name or name in {".", ".."}:
                fail(f"Release asset name {name!r} is not a safe file name.")

            destination = target_dir / name
            sha256 = download_asset(asset["browser_download_url"], destination, token)

            actual_size = destination.stat().st_size
            expected_size = asset.get("size")
            if expected_size is not None and actual_size != expected_size:
                fail(
                    f"Downloaded {name} is {actual_size} bytes but GitHub reports "
                    f"{expected_size} bytes."
                )

            manifest["assets"].append(
                {
                    "name": name,
                    "size": actual_size,
                    "sha256": sha256,
                    "download_url": asset.get("browser_download_url"),
                    "r2_key": f"{directory}/{app['target_dir']}/{name}",
                }
            )

        (target_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"{app['target_dir']}: {len(manifest['assets'])} asset(s) from "
            f"{app['repo']}@{source_release_tag}"
        )


if __name__ == "__main__":
    main()