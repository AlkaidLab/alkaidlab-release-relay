#!/usr/bin/env python3
"""Import source releases into the relay and publish them to MirrorChyan.

Every imported source release gets one GitHub Release in this repository under the
tag ``relay/<product>/<tag>-<release_id>``.  That release carries the exact bytes
that were verified and a ``relay-manifest.json`` record.  The manifest is the
idempotence and audit point: a source release is never uploaded twice, and a note
is sent again only when the source body changed or an earlier sync failed.

Subcommands:

``plan``        Decide which source releases need an import or a note sync.
``prepare``     Download and verify one release, or prepare one note sync.
``record``      Write the outcome of the MirrorChyan steps into the manifest.
``verify-mirror``  Confirm that MirrorChyan serves the uploaded version.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "products.yml"
MANIFEST_NAME = "relay-manifest.json"
DEFAULT_CHECKSUM_ASSETS = ("checksums.json", "SHA256SUMS.txt")
CHUNK_SIZE = 1024 * 1024
USER_AGENT = "AlkaidLab-release-relay"
MIRROR_API = "https://mirrorchyan.com/api/resources"
MIRROR_BASE = "https://mirrorchyan.com"
STATUS_PENDING = 1
STATUS_COMPLETED = 2
STATUS_FAILED = 3


class RelayError(RuntimeError):
    """A failure that must stop the current operation with a clear message."""


def log(message: str) -> None:
    print(message, flush=True)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        return default
    return text.lower() in {"1", "true", "yes", "on"}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# GitHub API


class GitHubClient:
    """Small REST client.  Only the endpoints the relay needs are implemented."""

    def __init__(
        self,
        token: str | None = None,
        api_url: str = "https://api.github.com",
        uploads_url: str = "https://uploads.github.com",
        timeout: float = 120.0,
    ) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.uploads_url = uploads_url.rstrip("/")
        self.timeout = timeout

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _api_headers(self) -> dict[str, str]:
        headers = self._headers("application/vnd.github+json")
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        data = None
        headers = self._api_headers()
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                if error.code == 404 and allow_404:
                    return None
                if method == "GET" and error.code >= 500 and attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise RelayError(
                    f"GitHub API returned {error.code} for {method} {url}: {body[:400]}"
                ) from None
            except (urllib.error.URLError, OSError, TimeoutError) as error:
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise RelayError(f"GitHub API request failed for {method} {url}: {error}") from None
        raise RelayError(f"GitHub API request failed for {method} {url}")

    def list_releases(self, repository: str) -> list[dict[str, Any]]:
        releases: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request(
                "GET", f"/repos/{repository}/releases?per_page=100&page={page}"
            )
            if not batch:
                break
            releases.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return releases

    def get_release(self, repository: str, release_id: int) -> dict[str, Any] | None:
        return self._request(
            "GET", f"/repos/{repository}/releases/{release_id}", allow_404=True
        )

    def get_release_by_tag(self, repository: str, tag: str) -> dict[str, Any] | None:
        quoted = urllib.parse.quote(tag, safe="")
        return self._request(
            "GET", f"/repos/{repository}/releases/tags/{quoted}", allow_404=True
        )

    def create_release(
        self,
        repository: str,
        tag: str,
        name: str,
        body: str,
        prerelease: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{repository}/releases",
            {
                "tag_name": tag,
                "name": name,
                "body": body,
                "draft": False,
                "prerelease": prerelease,
            },
        )

    def delete_asset(self, repository: str, asset_id: int) -> None:
        self._request("DELETE", f"/repos/{repository}/releases/assets/{asset_id}")

    def _upload(
        self,
        repository: str,
        release_id: int,
        name: str,
        data: Any,
        size: int,
        content_type: str,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"name": name})
        url = f"{self.uploads_url}/repos/{repository}/releases/{release_id}/assets?{query}"
        headers = self._headers("application/vnd.github+json")
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(size)
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RelayError(
                f"GitHub asset upload returned {error.code} for {name}: {body[:400]}"
            ) from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise RelayError(f"GitHub asset upload failed for {name}: {error}") from None

    def upload_asset(
        self,
        repository: str,
        release_id: int,
        name: str,
        path: Path,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        size = path.stat().st_size
        with path.open("rb") as handle:
            return self._upload(repository, release_id, name, handle, size, content_type)

    def upload_asset_bytes(
        self,
        repository: str,
        release_id: int,
        name: str,
        data: bytes,
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        import io

        return self._upload(
            repository, release_id, name, io.BytesIO(data), len(data), content_type
        )

    def download(
        self, url: str, destination: Path, expected_size: int | None = None
    ) -> tuple[int, str]:
        """Stream ``url`` to ``destination`` and return (size, sha256)."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        attempts = 3
        for attempt in range(attempts):
            digest = hashlib.sha256()
            total = 0
            request = urllib.request.Request(
                url, headers=self._headers("application/octet-stream"), method="GET"
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    with destination.open("wb") as handle:
                        while True:
                            chunk = response.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            handle.write(chunk)
                            digest.update(chunk)
                            total += len(chunk)
            except urllib.error.HTTPError as error:
                destination.unlink(missing_ok=True)
                if error.code in (401, 403, 404):
                    raise RelayError(
                        f"download returned {error.code} for {url}"
                    ) from None
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise RelayError(f"download failed for {url}: {error}") from None
            except (urllib.error.URLError, OSError, TimeoutError) as error:
                destination.unlink(missing_ok=True)
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise RelayError(f"download failed for {url}: {error}") from None
            if expected_size is not None and total != expected_size:
                destination.unlink(missing_ok=True)
                raise RelayError(
                    f"downloaded {total} bytes for {url}, release asset declares {expected_size}"
                )
            return total, digest.hexdigest()
        raise RelayError(f"download failed for {url}")

    def fetch_text(self, url: str, limit: int = 1024 * 1024) -> str:
        request = urllib.request.Request(
            url, headers=self._headers("application/octet-stream"), method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            raise RelayError(f"download returned {error.code} for {url}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise RelayError(f"download failed for {url}: {error}") from None
        if len(data) > limit:
            raise RelayError(f"response from {url} is larger than {limit} bytes")
        return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Configuration and naming


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover - exercised by the workflow only
        raise RelayError(
            "PyYAML is required; install it with `python -m pip install pyyaml`"
        ) from None
    with path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict) or not isinstance(config.get("products"), dict):
        raise RelayError("config/products.yml must contain a products mapping")
    if not config["products"]:
        raise RelayError("config/products.yml contains no products")
    return config


def get_product(config: dict[str, Any], product_key: str) -> dict[str, Any]:
    products = config["products"]
    if product_key not in products:
        known = ", ".join(sorted(products))
        raise RelayError(f"unknown product {product_key!r}; configured products: {known}")
    product = products[product_key]
    source = product.get("source") or {}
    if source.get("kind") != "github":
        raise RelayError(
            f"product {product_key!r} uses unsupported source kind {source.get('kind')!r}"
        )
    if not source.get("repository"):
        raise RelayError(f"product {product_key!r} has no source repository")
    if "public" not in source:
        raise RelayError(
            f"product {product_key!r} must declare source.public so the relay knows "
            "whether a read token is required"
        )
    mirror = product.get("mirror") or {}
    if not mirror.get("resource_id"):
        raise RelayError(f"product {product_key!r} has no mirror.resource_id")
    get_rule(product_key, product)
    return product


def get_rule(product_key: str, product: dict[str, Any]) -> dict[str, Any]:
    rules = product.get("assets") or []
    if len(rules) != 1:
        raise RelayError(
            f"product {product_key!r} must describe exactly one asset rule; "
            "separate resource streams belong in separate products"
        )
    rule = rules[0]
    for field in ("id", "source_pattern", "os", "arch"):
        if not rule.get(field):
            raise RelayError(f"product {product_key!r} asset rule is missing {field!r}")
    return rule


def relay_repository(config: dict[str, Any]) -> str:
    repository = os.environ.get("RELAY_REPOSITORY") or (config.get("relay") or {}).get(
        "repository"
    )
    if not repository:
        raise RelayError("relay.repository is not configured")
    return str(repository)


def source_client(product: dict[str, Any]) -> GitHubClient:
    source = product["source"]
    if parse_bool(source.get("public")):
        return GitHubClient(token=None)
    token = os.environ.get("RELAY_SOURCE_TOKEN")
    if not token:
        raise RelayError(
            f"source repository {source['repository']} is private and "
            "RELAY_SOURCE_TOKEN is not set"
        )
    return GitHubClient(token=token)


def relay_client() -> GitHubClient:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RelayError("GITHUB_TOKEN or GH_TOKEN is required for relay releases")
    return GitHubClient(token=token)


def sanitize_tag_fragment(value: str, limit: int = 60) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    safe = re.sub(r"-{2,}", "-", safe)
    return safe[:limit].strip(".-_") or "release"


def build_relay_tag(product_key: str, tag: str, release_id: int) -> str:
    return f"relay/{product_key}/{sanitize_tag_fragment(tag)}-{int(release_id)}"


def parse_relay_release_id(tag: str, product_key: str) -> int | None:
    prefix = f"relay/{product_key}/"
    if not tag.startswith(prefix):
        return None
    match = re.search(r"-(\d+)$", tag)
    return int(match.group(1)) if match else None


def channel_for(release: dict[str, Any]) -> str:
    return "beta" if release.get("prerelease") else "stable"


def matching_assets(release: dict[str, Any], rule: dict[str, Any]) -> list[dict[str, Any]]:
    pattern = rule["source_pattern"]
    return [
        asset
        for asset in release.get("assets") or []
        if fnmatch.fnmatchcase(str(asset.get("name", "")), pattern)
    ]


def wait_for_asset(
    client: GitHubClient,
    repository: str,
    release_id: int,
    rule: dict[str, Any],
    timeout: float = 600.0,
    interval: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wait until exactly one asset matches and its state is ``uploaded``."""
    deadline = clock() + timeout
    attempt = 0
    while True:
        attempt += 1
        release = client.get_release(repository, release_id)
        if release is None:
            raise RelayError(f"source release {release_id} disappeared from {repository}")
        if release.get("draft"):
            raise RelayError(f"source release {release.get('tag_name')} is a draft")
        matches = matching_assets(release, rule)
        if len(matches) > 1:
            names = [asset.get("name") for asset in matches]
            raise RelayError(
                f"rule {rule['id']!r} matched {len(matches)} assets in "
                f"{release.get('tag_name')}: {names}; exactly one is required"
            )
        if len(matches) == 1:
            asset = matches[0]
            if asset.get("state") == "uploaded" and int(asset.get("size", 0)) > 0:
                return release, asset
        if clock() >= deadline:
            states = [(a.get("name"), a.get("state")) for a in matches]
            raise RelayError(
                f"waiting for {rule['source_pattern']!r} in release "
                f"{release.get('tag_name')} timed out after {timeout:.0f}s; "
                f"current assets: {states}"
            )
        log(
            f"waiting for {rule['source_pattern']!r} "
            f"({attempt}, {max(deadline - clock(), 0):.0f}s remaining)"
        )
        sleep(interval)


# ---------------------------------------------------------------------------
# Checksums


def checksum_asset_names(product: dict[str, Any]) -> tuple[str, ...]:
    configured = (product.get("checksums") or {}).get("assets")
    return tuple(str(name) for name in configured) if configured else DEFAULT_CHECKSUM_ASSETS


def parse_checksums_json(text: str) -> dict[str, str]:
    data = json.loads(text)
    if isinstance(data, dict):
        files = data.get("files") or data.get("Files")
        if isinstance(files, list):
            data = files
        elif all(isinstance(value, str) for value in data.values()):
            return {str(name): value.lower() for name, value in data.items()}
        else:
            raise RelayError("checksums.json has an unsupported structure")
    if not isinstance(data, list):
        raise RelayError("checksums.json must contain a list of files")
    result: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            raise RelayError("checksums.json entries must be objects")
        name = item.get("File") or item.get("file") or item.get("name")
        digest = item.get("Hash") or item.get("hash") or item.get("sha256")
        if not name or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise RelayError(f"checksums.json entry is incomplete: {item!r}")
        result[str(name)] = digest.lower()
    return result


def parse_checksums_txt(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", stripped)
        if match:
            result[match.group(2)] = match.group(1).lower()
    return result


def verify_checksums(
    client: GitHubClient,
    release: dict[str, Any],
    asset_name: str,
    digest: str,
    names: tuple[str, ...],
) -> list[str]:
    """Cross-check the asset against every checksum file present in the release."""
    by_name = {str(asset.get("name")): asset for asset in release.get("assets") or []}
    checked: list[str] = []
    for name in names:
        asset = by_name.get(name)
        if asset is None:
            continue
        text = client.fetch_text(str(asset["browser_download_url"]))
        if name.endswith(".json"):
            entries = parse_checksums_json(text)
        else:
            entries = parse_checksums_txt(text)
        if asset_name not in entries:
            raise RelayError(f"{name} has no entry for {asset_name!r}")
        if entries[asset_name] != digest.lower():
            raise RelayError(
                f"{name} records {entries[asset_name]} for {asset_name!r}, "
                f"downloaded bytes hash to {digest}"
            )
        checked.append(name)
    return checked


# ---------------------------------------------------------------------------
# Relay manifest


def read_manifest(
    client: GitHubClient, repository: str, relay_release: dict[str, Any]
) -> dict[str, Any] | None:
    for asset in relay_release.get("assets") or []:
        if asset.get("name") == MANIFEST_NAME:
            text = client.fetch_text(str(asset["browser_download_url"]))
            try:
                manifest = json.loads(text)
            except json.JSONDecodeError as error:
                raise RelayError(
                    f"relay manifest in {relay_release.get('tag_name')} is not valid JSON: {error}"
                ) from None
            if not isinstance(manifest, dict):
                raise RelayError(
                    f"relay manifest in {relay_release.get('tag_name')} is not an object"
                )
            return manifest
    return None


def write_manifest(
    client: GitHubClient,
    repository: str,
    relay_release: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    for asset in relay_release.get("assets") or []:
        if asset.get("name") == MANIFEST_NAME:
            client.delete_asset(repository, int(asset["id"]))
    data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    client.upload_asset_bytes(
        repository, int(relay_release["id"]), MANIFEST_NAME, data, "application/json"
    )


def relay_asset_problem(
    relay_release: dict[str, Any], manifest: dict[str, Any]
) -> str | None:
    """Return a reason when the relay Release no longer carries the recorded asset."""
    asset = manifest.get("asset") or {}
    name = asset.get("name")
    found = [
        item
        for item in relay_release.get("assets") or []
        if item.get("name") == name
    ]
    if len(found) != 1:
        return f"relay release has {len(found)} assets named {name!r}"
    if int(found[0].get("size", -1)) != int(asset.get("size", -2)):
        return f"relay asset {name!r} size does not match the manifest"
    return None


def migrate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Normalise an older single-channel manifest to per-channel records."""
    mirror = manifest.setdefault("mirror", {})
    uploads = mirror.get("uploads")
    if not isinstance(uploads, dict):
        uploads = {}
        channel = mirror.get("channel")
        if channel and mirror.get("status"):
            uploads[channel] = {
                "status": mirror["status"],
                "uploaded_at": mirror.get("uploaded_at"),
                "run_url": mirror.get("run_url"),
            }
        mirror["uploads"] = uploads
    note = manifest.setdefault("note", {})
    notes = note.get("uploads")
    if not isinstance(notes, dict):
        notes = {}
        channel = mirror.get("channel") or note.get("channel")
        if channel and note.get("status"):
            notes[channel] = {
                "status": note["status"],
                "body_sha256": note.get("body_sha256"),
                "synced_at": note.get("synced_at"),
            }
        note["uploads"] = notes
    manifest["schema_version"] = 2
    return manifest


def upload_record(manifest: dict[str, Any], channel: str) -> dict[str, Any]:
    return (manifest.get("mirror") or {}).get("uploads", {}).get(channel) or {}


def note_record(manifest: dict[str, Any], channel: str) -> dict[str, Any]:
    return (manifest.get("note") or {}).get("uploads", {}).get(channel) or {}


def build_manifest(
    product_key: str,
    product: dict[str, Any],
    release: dict[str, Any],
    asset: dict[str, Any],
    size: int,
    digest: str,
    checksum_files: list[str],
    relay_tag: str,
    live: bool,
) -> dict[str, Any]:
    rule = get_rule(product_key, product)
    mirror = product["mirror"]
    return {
        "schema_version": 2,
        "product": product_key,
        "source": {
            "kind": "github",
            "repository": product["source"]["repository"],
            "release_id": int(release["id"]),
            "tag": release["tag_name"],
            "published_at": release.get("published_at"),
            "prerelease": bool(release.get("prerelease")),
            "body_sha256": text_sha256(release.get("body") or ""),
        },
        "asset": {
            "rule": rule["id"],
            "source_asset_id": int(asset["id"]),
            "name": asset["name"],
            "size": int(size),
            "sha256": digest,
            "checksum_files": checksum_files,
        },
        "relay": {"tag": relay_tag, "created_at": iso_now()},
        "mirror": {
            "resource_id": mirror["resource_id"],
            "version_name": release["tag_name"],
            "channel": channel_for(release),
            "os": rule["os"],
            "arch": rule["arch"],
            "uploads": {
                channel_for(release): {
                    "status": "started" if live else "pending",
                    "started_at": iso_now() if live else None,
                    "run_url": os.environ.get("RELAY_RUN_URL"),
                }
            },
        },
        "note": {"channel": channel_for(release), "uploads": {}},
    }


def relay_release_body(
    product_key: str,
    product: dict[str, Any],
    release: dict[str, Any],
    manifest: dict[str, Any],
) -> str:
    asset = manifest["asset"]
    mirror = manifest["mirror"]
    return "\n".join(
        [
            f"Source: {product['source']['repository']}",
            f"Tag: {release['tag_name']}",
            f"Release: {release['html_url']}",
            f"Asset: {asset['name']} ({asset['size']} bytes)",
            f"SHA256: {asset['sha256']}",
            f"MirrorChyan: {mirror['resource_id']} / {mirror['channel']} / "
            f"{mirror['os']} / {mirror['arch']}",
            "",
            f"Generated by the {product_key} relay.",
        ]
    )


# ---------------------------------------------------------------------------
# Planning


def make_entry(
    product_key: str,
    release: dict[str, Any],
    action: str,
    reason: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    return {
        "product": product_key,
        "release_id": int(release["id"]),
        "tag": release["tag_name"],
        "channel": channel_for(release),
        "action": action,
        "reason": reason,
        "dry_run": dry_run,
    }


def attention_entry(product_key: str, release: dict[str, Any], reason: str) -> dict[str, Any]:
    return make_entry(product_key, release, "attention", reason)


def classify_known_release(
    product_key: str,
    product: dict[str, Any],
    release: dict[str, Any],
    relay_release: dict[str, Any],
    relay: GitHubClient,
    relay_repo: str,
    dry_run: bool = False,
    latest_lookup: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    rule = get_rule(product_key, product)
    matches = matching_assets(release, rule)
    if len(matches) != 1:
        names = [asset.get("name") for asset in matches]
        return [
            attention_entry(
                product_key,
                release,
                f"asset rule {rule['id']!r} now matches {len(matches)} assets: {names}",
            )
        ]
    manifest = read_manifest(relay, relay_repo, relay_release)
    if manifest is None:
        return [
            attention_entry(
                product_key,
                release,
                f"relay release {relay_release.get('tag_name')} has no {MANIFEST_NAME}",
            )
        ]
    manifest = migrate_manifest(manifest)
    problem = relay_asset_problem(relay_release, manifest)
    if problem is not None:
        return [attention_entry(product_key, release, problem)]
    channel = channel_for(release)
    current = upload_record(manifest, channel)
    status = current.get("status")
    if status in {"started", "failed"}:
        return [
            attention_entry(
                product_key,
                release,
                f"MirrorChyan {channel} upload for {relay_release.get('tag_name')} "
                f"is {status!r}; inspect MirrorChyan before removing the relay release",
            )
        ]
    stable_uploaded = upload_record(manifest, "stable").get("status") == "uploaded"
    beta_uploaded = upload_record(manifest, "beta").get("status") == "uploaded"
    if status != "uploaded":
        if channel == "beta" and stable_uploaded:
            log(
                f"demotion ignored for {release['tag_name']}: the stable upload "
                "already exists; not uploading to beta"
            )
            return []
        if channel == "stable" and beta_uploaded:
            if not upload_allowed(release, channel, latest_lookup):
                return []
            return [
                make_entry(product_key, release, "promote", "promotion", dry_run=dry_run)
            ]
        if not upload_allowed(release, channel, latest_lookup):
            return []
        return [make_entry(product_key, release, "import", "new", dry_run=dry_run)]
    body = release.get("body") or ""
    note = note_record(manifest, channel)
    if note.get("status") == "failed":
        return [make_entry(product_key, release, "note", "retry-note", dry_run=dry_run)]
    if note.get("body_sha256") != text_sha256(body) and body:
        reason = "note-missing" if note.get("body_sha256") is None else "edited"
        return [make_entry(product_key, release, "note", reason, dry_run=dry_run)]
    return []


def plan_releases(
    config: dict[str, Any],
    source: GitHubClient,
    relay: GitHubClient | None,
    relay_repo: str,
    product_key: str | None = None,
    tag: str | None = None,
    dry_run: bool = False,
    latest_lookup: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    keys = [product_key] if product_key else sorted(config["products"])
    entries: list[dict[str, Any]] = []
    for key in keys:
        product = get_product(config, key)
        repository = product["source"]["repository"]
        rule = get_rule(key, product)
        if tag:
            release = source.get_release_by_tag(repository, tag)
            if release is None:
                raise RelayError(f"source release tag {tag!r} not found in {repository}")
            if release.get("draft"):
                raise RelayError(f"source release {tag!r} is a draft")
            if len(matching_assets(release, rule)) != 1:
                raise RelayError(
                    f"source release {tag!r} does not contain exactly one asset matching "
                    f"{rule['source_pattern']!r}"
                )
            relay_tag = build_relay_tag(key, release["tag_name"], int(release["id"]))
            known = (
                relay.get_release_by_tag(relay_repo, relay_tag)
                if relay is not None
                else None
            )
            if dry_run and known is not None:
                entries.extend(
                    classify_known_release(
                        key,
                        product,
                        release,
                        known,
                        relay,
                        relay_repo,
                        dry_run=True,
                        latest_lookup=latest_lookup,
                    )
                )
                continue
            if dry_run:
                if upload_allowed(release, channel_for(release), latest_lookup):
                    entries.append(
                        make_entry(key, release, "import", "dry-run", dry_run=True)
                    )
                continue
            if known is None:
                if upload_allowed(release, channel_for(release), latest_lookup):
                    entries.append(make_entry(key, release, "import", "dispatched"))
            else:
                entries.extend(
                    classify_known_release(
                        key,
                        product,
                        release,
                        known,
                        relay,
                        relay_repo,
                        latest_lookup=latest_lookup,
                    )
                )
            continue

        releases = [item for item in source.list_releases(repository) if not item.get("draft")]
        if relay is None:
            raise RelayError("relay client is required to list relay releases")
        relay_releases = relay.list_releases(relay_repo)
        known: dict[int, dict[str, Any]] = {}
        for relay_release in relay_releases:
            release_id = parse_relay_release_id(
                str(relay_release.get("tag_name", "")), key
            )
            if release_id is None:
                continue
            if release_id in known:
                entries.append(
                    attention_entry(
                        key,
                        {"id": release_id, "tag_name": f"release {release_id}"},
                        "two relay releases carry the same source release id",
                    )
                )
                continue
            known[release_id] = relay_release

        importable = [item for item in releases if len(matching_assets(item, rule)) == 1]
        if not known:
            for channel in ("stable", "beta"):
                candidates = [item for item in importable if channel_for(item) == channel]
                if candidates:
                    newest = max(candidates, key=lambda item: int(item["id"]))
                    if upload_allowed(newest, channel, latest_lookup):
                        entries.append(make_entry(key, newest, "import", "bootstrap"))
            continue

        newest_known_id = max(known)
        known_published = [
            parse_time(item.get("published_at"))
            for item in releases
            if int(item["id"]) in known
        ]
        newest_known_published = max(
            (value for value in known_published if value is not None), default=None
        )
        for release in sorted(releases, key=lambda item: int(item["id"])):
            release_id = int(release["id"])
            if release_id in known:
                entries.extend(
                    classify_known_release(
                        key,
                        product,
                        release,
                        known[release_id],
                        relay,
                        relay_repo,
                        latest_lookup=latest_lookup,
                    )
                )
                continue
            if len(matching_assets(release, rule)) != 1:
                continue
            published = parse_time(release.get("published_at"))
            # A late-published draft can carry an older id; its publication time
            # distinguishes it from history replayed after the bootstrap.
            newer_than_state = (
                newest_known_published is not None
                and published is not None
                and published > newest_known_published
            )
            if release_id > newest_known_id or newer_than_state:
                if upload_allowed(release, channel_for(release), latest_lookup):
                    entries.append(make_entry(key, release, "import", "new"))
    return entries


def resolve_live(event: str, dry_run_input: Any, relay_live: Any) -> bool:
    if event == "workflow_dispatch":
        return not parse_bool(dry_run_input, default=True)
    if event == "schedule":
        return str(relay_live or "").strip() == "1"
    return False


# ---------------------------------------------------------------------------
# Prepare


def prepare_release(
    config: dict[str, Any],
    source: GitHubClient,
    relay: GitHubClient | None,
    relay_repo: str,
    product_key: str,
    release_id: int,
    action: str,
    live: bool,
    staging_dir: Path,
    reason: str = "",
    wait_timeout: float | None = None,
    wait_interval: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    latest_lookup: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    product = get_product(config, product_key)
    rule = get_rule(product_key, product)
    mirror = product["mirror"]
    wait_config = product.get("asset_wait") or {}
    if wait_timeout is None:
        wait_timeout = float(wait_config.get("timeout_seconds", 600))
    if wait_interval is None:
        wait_interval = float(wait_config.get("interval_seconds", 15))
    repository = product["source"]["repository"]
    release = source.get_release(repository, release_id)
    if release is None:
        raise RelayError(f"source release {release_id} not found in {repository}")
    if release.get("draft"):
        raise RelayError(f"source release {release['tag_name']} is a draft")
    body = release.get("body") or ""
    body_hash = text_sha256(body)
    channel = channel_for(release)
    relay_tag = build_relay_tag(product_key, release["tag_name"], int(release["id"]))
    result: dict[str, Any] = {
        "product": product_key,
        "release_id": int(release["id"]),
        "tag": release["tag_name"],
        "version_name": release["tag_name"],
        "channel": channel,
        "resource_id": mirror["resource_id"],
        "os": rule["os"],
        "arch": rule["arch"],
        "query_os": rule.get("query_os") or rule["os"],
        "query_arch": rule.get("query_arch") or rule["arch"],
        "relay_tag": relay_tag,
        "body_sha256": body_hash,
        "release_note": body,
        "upload": False,
        "sync_note": False,
        "record": False,
        "live": live,
    }

    if action == "attention":
        raise RelayError(reason or "release needs attention")

    if action == "note":
        if not live:
            result["preview"] = f"would sync notes to {channel} for {release['tag_name']}"
            return result
        if relay is None:
            raise RelayError("relay client is required for a live note sync")
        relay_release = relay.get_release_by_tag(relay_repo, relay_tag)
        if relay_release is None:
            raise RelayError(f"relay release {relay_tag} does not exist")
        manifest = read_manifest(relay, relay_repo, relay_release)
        if manifest is None:
            raise RelayError(f"relay release {relay_tag} has no {MANIFEST_NAME}")
        manifest = migrate_manifest(manifest)
        if not body:
            result["note_outcome"] = "empty"
            result["record"] = True
            return result
        note = note_record(manifest, channel)
        if note.get("body_sha256") == body_hash and note.get("status") != "failed":
            return result
        result["sync_note"] = True
        result["record"] = True
        return result

    if action not in {"import", "promote"}:
        raise RelayError(f"unknown action {action!r}")

    matches = matching_assets(release, rule)
    if len(matches) != 1:
        raise RelayError(
            f"rule {rule['id']!r} matched {len(matches)} assets in "
            f"{release['tag_name']}; exactly one is required"
        )
    current_asset = matches[0]

    manifest: dict[str, Any] | None = None
    relay_release: dict[str, Any] | None = None
    needs_upload = True
    if live:
        if relay is None:
            raise RelayError("relay client is required for a live import")
        relay_release = relay.get_release_by_tag(relay_repo, relay_tag)
        if relay_release is not None:
            manifest = read_manifest(relay, relay_repo, relay_release)
            if manifest is None:
                raise RelayError(f"relay release {relay_tag} has no {MANIFEST_NAME}")
            manifest = migrate_manifest(manifest)
            problem = relay_asset_problem(relay_release, manifest)
            if problem is not None:
                raise RelayError(problem)
            recorded = manifest.get("asset") or {}
            if int(recorded.get("source_asset_id", -1)) != int(current_asset["id"]):
                raise RelayError(
                    f"source asset for {release['tag_name']} changed after import; "
                    "the relay release must be reviewed before another upload"
                )
            if int(recorded.get("size", -1)) != int(current_asset["size"]):
                raise RelayError(
                    f"source asset size for {release['tag_name']} changed after import"
                )
            status = upload_record(manifest, channel).get("status")
            if status in {"started", "failed"}:
                raise RelayError(
                    f"MirrorChyan {channel} upload for {relay_tag} is {status!r}; inspect "
                    "MirrorChyan before removing the relay release"
                )
            if status == "uploaded":
                needs_upload = False
            elif (
                channel == "beta"
                and upload_record(manifest, "stable").get("status") == "uploaded"
            ):
                log(
                    f"demotion ignored for {release['tag_name']}: the stable upload "
                    "already exists; not uploading to beta"
                )
                needs_upload = False
                result["mirror_state"] = "demotion-ignored"
            else:
                result["promotion"] = (
                    channel == "stable"
                    and upload_record(manifest, "beta").get("status") == "uploaded"
                )
            if not needs_upload:
                note = note_record(manifest, channel)
                if body and (
                    note.get("status") == "failed"
                    or note.get("body_sha256") != body_hash
                ):
                    result["sync_note"] = True
                    result["record"] = True
                elif not body and note.get("body_sha256") != body_hash:
                    result["note_outcome"] = "empty"
                    result["record"] = True
                result["mirror_state"] = result.get("mirror_state", "already-uploaded")
                return result

    if latest_lookup is not None and not upload_allowed(release, channel, latest_lookup):
        result["mirror_state"] = "skipped-not-newer"
        return result

    release, asset = wait_for_asset(
        source,
        repository,
        release_id,
        rule,
        timeout=wait_timeout,
        interval=wait_interval,
        sleep=sleep,
        clock=clock,
    )
    filename = Path(str(asset["name"])).name
    if filename != asset["name"]:
        raise RelayError(f"asset name {asset['name']!r} is not a plain file name")
    destination = staging_dir / product_key / str(release_id) / filename
    size, digest = source.download(
        str(asset["browser_download_url"]), destination, expected_size=int(asset["size"])
    )
    checksum_files = verify_checksums(
        source, release, str(asset["name"]), digest, checksum_asset_names(product)
    )
    result.update(
        {
            "asset_path": str(destination),
            "asset_name": asset["name"],
            "asset_size": size,
            "asset_sha256": digest,
            "checksum_files": checksum_files,
        }
    )

    if manifest is not None and digest != (manifest.get("asset") or {}).get("sha256"):
        raise RelayError(
            f"source bytes for {release['tag_name']} differ from the recorded relay asset"
        )

    if not live:
        result["pending_reason"] = reason
        return result

    if relay is None:
        raise RelayError("relay client is required for a live import")
    if relay_release is None:
        manifest = build_manifest(
            product_key, product, release, asset, size, digest, checksum_files, relay_tag, live
        )
        created = relay.create_release(
            relay_repo,
            relay_tag,
            f"{product.get('display_name', product_key)} {release['tag_name']}",
            relay_release_body(product_key, product, release, manifest),
        )
        relay.upload_asset(relay_repo, int(created["id"]), str(asset["name"]), destination)
        write_manifest(relay, relay_repo, created, manifest)
        result["relay_release_id"] = int(created["id"])
    else:
        mirror = manifest.setdefault("mirror", {})
        mirror["channel"] = channel
        mirror.setdefault("uploads", {})[channel] = {
            "status": "started",
            "started_at": iso_now(),
            "run_url": os.environ.get("RELAY_RUN_URL"),
        }
        write_manifest(relay, relay_repo, relay_release, manifest)
    note = note_record(manifest, channel)
    result.update(
        {
            "upload": True,
            "sync_note": bool(body)
            and (
                note.get("status") == "failed"
                or note.get("body_sha256") != body_hash
            ),
            "record": True,
            "mirror_state": "promotion" if result.get("promotion") else "started",
        }
    )
    return result


# ---------------------------------------------------------------------------
# Recording and verification


def record_outcome(
    relay: GitHubClient,
    relay_repo: str,
    release_id: int,
    relay_tag: str,
    channel: str,
    mirror_outcome: str,
    note_outcome: str,
    body_sha256: str | None,
    run_url: str | None,
) -> dict[str, Any]:
    relay_release = relay.get_release_by_tag(relay_repo, relay_tag)
    if relay_release is None:
        raise RelayError(f"relay release {relay_tag} does not exist")
    manifest = read_manifest(relay, relay_repo, relay_release)
    if manifest is None:
        raise RelayError(f"relay release {relay_tag} has no {MANIFEST_NAME}")
    if int((manifest.get("source") or {}).get("release_id", -1)) != int(release_id):
        raise RelayError(f"relay release {relay_tag} belongs to another source release")
    manifest = migrate_manifest(manifest)
    mirror = manifest.setdefault("mirror", {})
    note = manifest.setdefault("note", {})
    mirror["channel"] = channel
    uploads = mirror.setdefault("uploads", {})
    if mirror_outcome in {"success", "failure"}:
        record = uploads.setdefault(channel, {})
        if mirror_outcome == "success":
            record["status"] = "uploaded"
            record["uploaded_at"] = iso_now()
            if run_url:
                record["run_url"] = run_url
        else:
            record["status"] = "failed"
    note["channel"] = channel
    notes = note.setdefault("uploads", {})
    if note_outcome in {"success", "failure", "empty"}:
        note_state = notes.setdefault(channel, {})
        if note_outcome == "success":
            note_state["status"] = "synced"
            note_state["body_sha256"] = body_sha256
            note_state["synced_at"] = iso_now()
        elif note_outcome == "failure":
            note_state["status"] = "failed"
        else:
            note_state["status"] = "empty"
            note_state["body_sha256"] = body_sha256
    if run_url:
        manifest["last_run"] = {"url": run_url, "at": iso_now()}
    write_manifest(relay, relay_repo, relay_release, manifest)
    return manifest


def fetch_mirror_latest(
    resource_id: str, channel: str, os_name: str, arch: str, timeout: float = 30.0
) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"os": os_name, "arch": arch, "channel": channel}, quote_via=urllib.parse.quote
    )
    url = f"{MIRROR_API}/{urllib.parse.quote(resource_id, safe='')}/latest?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise RelayError(
                f"MirrorChyan version check returned HTTP {error.code}"
            ) from None
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise RelayError(f"MirrorChyan version check failed: {error}") from None
    except json.JSONDecodeError as error:
        raise RelayError(f"MirrorChyan version check returned invalid JSON: {error}") from None


VERSION_TAG_PATTERN = re.compile(r"^v(\d{4})\.(\d{1,4})\.(\d{1,6})(?:\..*)?$")


def parse_version_tag(tag: str) -> tuple[int, int, int]:
    """Parse ``v<year>.<mmdd>.<hhmmss>[.<suffix>]`` into comparable integers."""
    match = VERSION_TAG_PATTERN.match(tag or "")
    if not match:
        raise RelayError(
            f"cannot compare version tag {tag!r}; expected "
            "v<year>.<mmdd>.<hhmmss>[.<suffix>]"
        )
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def version_is_newer(candidate: str, latest: str) -> bool:
    return parse_version_tag(candidate) > parse_version_tag(latest)


def mirror_latest_tag(
    product_key: str,
    product: dict[str, Any],
    channel: str,
    fetch: Callable[..., dict[str, Any]] = fetch_mirror_latest,
) -> str | None:
    """Return the version MirrorChyan serves for a channel, or None when empty."""
    rule = get_rule(product_key, product)
    payload = fetch(
        str(product["mirror"]["resource_id"]),
        channel,
        rule.get("query_os") or rule["os"],
        rule.get("query_arch") or rule["arch"],
    )
    code = payload.get("code")
    if code == 0:
        data = payload.get("data") or {}
        version = data.get("version_name")
        if not version:
            raise RelayError("MirrorChyan latest response has no version_name")
        return str(version)
    if code == 8001:
        return None
    raise RelayError(
        f"MirrorChyan latest query failed: code={code!r} msg={payload.get('msg')!r}"
    )


def upload_allowed(
    release: dict[str, Any],
    channel: str,
    latest_lookup: Callable[[str], str | None] | None,
) -> bool:
    if latest_lookup is None:
        return True
    latest = latest_lookup(channel)
    if latest is None:
        return True
    if version_is_newer(release["tag_name"], latest):
        return True
    log(
        f"skip upload for {release['tag_name']}: MirrorChyan {channel} "
        f"already serves {latest}"
    )
    return False


def mirror_download_name(resource_id: str, version: str, os_name: str, arch: str, path: Path) -> str:
    suffix = ".tar.gz" if path.name.endswith(".tar.gz") else path.suffix
    return "-".join(filter(None, [resource_id, version, os_name, arch])) + suffix


def mirror_content_disposition(filename: str) -> str:
    """Return an HTTP-header-safe attachment name for any Unicode file name."""
    fallback = "".join(
        character
        if 0x20 <= ord(character) < 0x7F and character not in {'"', "\\"}
        else "_"
        for character in filename
    )
    if fallback == filename:
        return f'attachment; filename="{fallback}"'
    encoded = urllib.parse.quote(filename, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _mirror_response_json(response: Any, stage: str) -> dict[str, Any]:
    if response.status_code != 200:
        body = getattr(response, "text", "")
        raise RelayError(
            f"MirrorChyan {stage} returned {response.status_code}: {str(body)[:400]}"
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise RelayError(f"MirrorChyan {stage} returned invalid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise RelayError(f"MirrorChyan {stage} returned an unexpected payload")
    return payload


def upload_to_mirror(
    resource_id: str,
    version: str,
    os_name: str,
    arch: str,
    channel: str,
    path: Path,
    token: str,
    *,
    session: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    base_url: str = MIRROR_BASE,
    attempts: int = 3,
    poll_attempts: int = 60,
    poll_interval: float = 5.0,
) -> dict[str, Any]:
    """Upload a verified file with MirrorChyan's documented three-step API.

    The vendor action's uploader sends the raw version as a Content-Disposition
    header, which http.client cannot encode when the tag is not Latin-1 (all
    Foundation Sunshine tags end in a Chinese suffix).  This uses the same API
    with an RFC 6266/5987 header.  TLS verification stays enabled; the vendor
    script disables it.
    """
    if not token:
        raise RelayError("MirrorChyanUploadToken is empty")
    if not path.is_file() or path.stat().st_size <= 0:
        raise RelayError(f"upload file {path} is missing or empty")
    if session is None:
        import requests  # type: ignore

        session = requests.Session()
    filename = mirror_download_name(resource_id, version, os_name, arch, path)
    fields = {"name": version, "os": os_name, "arch": arch, "channel": channel, "filename": filename}
    headers = {
        "Authorization": token.strip(),
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    disposition = mirror_content_disposition(filename)
    last_error: Exception | None = None
    for attempt in range(attempts):
        callback_done = False
        try:
            response = session.post(
                f"{base_url}/api/resources/{urllib.parse.quote(resource_id, safe='')}/versions",
                headers=headers,
                data=fields,
                timeout=120,
            )
            created = _mirror_response_json(response, "version create")
            info = created.get("data") or {}
            missing = [
                key
                for key in ("host", "name", "signature", "key", "policy", "access_key")
                if not info.get(key)
            ]
            if missing:
                raise RelayError(f"MirrorChyan version create is missing {missing}")
            with path.open("rb") as handle:
                response = session.post(
                    info["host"],
                    data={
                        "success_action_status": "200",
                        "name": info["name"],
                        "signature": info["signature"],
                        "key": info["key"],
                        "policy": info["policy"],
                        "OSSAccessKeyId": info["access_key"],
                    },
                    files={"file": (filename, handle, "application/octet-stream")},
                    headers={"Content-Disposition": disposition},
                    timeout=600,
                )
            if response.status_code != 200:
                raise RelayError(
                    f"MirrorChyan object upload returned {response.status_code}: "
                    f"{str(getattr(response, 'text', ''))[:400]}"
                )
            callback_fields = dict(fields)
            callback_fields["key"] = info["key"]
            response = session.post(
                f"{base_url}/api/resources/{urllib.parse.quote(resource_id, safe='')}/versions/callback",
                headers=headers,
                data=callback_fields,
                timeout=120,
            )
            callback = _mirror_response_json(response, "upload callback")
            callback_done = True
            status_key = (callback.get("data") or {}).get("status_key")
            if not status_key:
                return {"filename": filename}
            poll_headers = {key: value for key, value in headers.items() if key != "Content-Type"}
            for poll in range(poll_attempts):
                sleep(poll_interval)
                response = session.get(
                    f"{base_url}/api/resources/{urllib.parse.quote(resource_id, safe='')}/versions/status",
                    headers=poll_headers,
                    params={"key": status_key},
                    timeout=60,
                )
                status_payload = _mirror_response_json(response, "status check")
                status = (status_payload.get("data") or {}).get("status")
                if status == STATUS_COMPLETED:
                    return {"filename": filename, "status_key": status_key}
                if status == STATUS_FAILED:
                    raise RelayError("MirrorChyan failed to process the uploaded file")
                if status != STATUS_PENDING:
                    raise RelayError(f"MirrorChyan returned unexpected status {status!r}")
            raise RelayError(f"MirrorChyan processing did not finish after {poll_attempts} polls")
        except RelayError as error:
            last_error = error
            if callback_done:
                raise RelayError(
                    f"MirrorChyan accepted the callback but processing failed: {error}"
                ) from None
        except Exception as error:  # noqa: BLE001 - network and TLS errors
            last_error = error
            if callback_done:
                raise RelayError(
                    f"MirrorChyan accepted the callback but the status check failed: {error}"
                ) from None
        if attempt + 1 < attempts:
            sleep((attempt + 1) * 15)
    raise RelayError(f"MirrorChyan upload failed: {last_error}")


def verify_mirror_version(
    resource_id: str,
    version: str,
    channel: str,
    os_name: str,
    arch: str,
    action_outcome: str,
    attempts: int = 6,
    interval: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    fetch: Callable[..., dict[str, Any]] = fetch_mirror_latest,
) -> str:
    observed = ""
    for attempt in range(attempts):
        payload = fetch(resource_id, channel, os_name, arch)
        if payload.get("code") == 0:
            observed = str((payload.get("data") or {}).get("version_name") or "")
            if observed == version:
                return observed
        if attempt + 1 < attempts:
            sleep(interval)
    if action_outcome == "success":
        log(
            f"warning: MirrorChyan reports {observed!r} for {resource_id} {channel}, "
            f"not {version!r}; the upload step reported success"
        )
        return observed
    raise RelayError(
        f"MirrorChyan does not report {version!r} for {resource_id} {channel}; "
        f"observed {observed!r} and the upload step ended with {action_outcome!r}"
    )


# ---------------------------------------------------------------------------
# Workflow helpers


def write_github_output(path: str | None, values: dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            text = "" if value is None else str(value)
            if "\n" in text or "\r" in text:
                delimiter = f"RELAY_{uuid.uuid4().hex}"
                while delimiter in text:
                    delimiter = f"RELAY_{uuid.uuid4().hex}"
                handle.write(f"{key}<<{delimiter}\n{text}\n{delimiter}\n")
            else:
                handle.write(f"{key}={text}\n")


def render_plan_summary(entries: list[dict[str, Any]], event: str, live: bool) -> str:
    lines = [
        "## Relay plan",
        "",
        f"- event: `{event}`",
        f"- live: `{'yes' if live else 'no'}`",
        f"- entries: `{len(entries)}`",
        "",
    ]
    if not entries:
        lines.append("No release needs an upload or a note sync.")
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "| product | release | tag | channel | action | reason |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for entry in entries:
        tag = str(entry.get("tag") or "").replace("|", "\\|")
        reason = str(entry.get("reason") or "").replace("|", "\\|")
        lines.append(
            f"| {entry['product']} | {entry['release_id']} | {tag} | "
            f"{entry.get('channel', '')} | "
            f"{entry['action']} | {reason} |"
        )
    if not live and event == "schedule":
        lines.extend(
            [
                "",
                "The schedule only imports while the repository variable "
                "`RELAY_LIVE` is `1`.",
            ]
        )
    elif not live:
        lines.extend(["", "This run does not create relay releases or upload."])
    return "\n".join(lines) + "\n"


def summary_and_matrix(
    entries: list[dict[str, Any]], event: str, live: bool
) -> tuple[str, bool, list[dict[str, Any]]]:
    run_jobs = live or event != "schedule"
    matrix_entries = entries if run_jobs else []
    return render_plan_summary(entries, event, live), bool(matrix_entries), matrix_entries


# ---------------------------------------------------------------------------
# CLI


def client_for_product(product: dict[str, Any]) -> GitHubClient:
    return source_client(product)


def cmd_plan(args: argparse.Namespace) -> int:
    config = load_config()
    relay_repo = relay_repository(config)
    event = args.event or "local"
    live = resolve_live(event, args.dry_run, args.relay_live)
    dry_run = parse_bool(args.dry_run, default=True)
    keys = [args.product] if args.product else sorted(config["products"])
    entries: list[dict[str, Any]] = []
    relay: GitHubClient | None = None
    try:
        relay = relay_client()
    except RelayError:
        if not (args.tag and dry_run):
            raise
    for key in keys:
        product = get_product(config, key)
        source = client_for_product(product)
        cache: dict[str, str | None] = {}

        def lookup(
            channel: str,
            _key: str = key,
            _product: dict = product,
            _cache: dict[str, str | None] = cache,
        ) -> str | None:
            if channel not in _cache:
                _cache[channel] = mirror_latest_tag(_key, _product, channel)
            return _cache[channel]

        entries.extend(
            plan_releases(
                config,
                source,
                relay,
                relay_repo,
                product_key=key,
                tag=args.tag or None,
                dry_run=bool(args.tag and dry_run),
                latest_lookup=lookup,
            )
        )
    summary, has_work, matrix_entries = summary_and_matrix(entries, event, live)
    matrix = {
        "include": [
            {
                "product": entry["product"],
                "release_id": entry["release_id"],
                "tag": entry["tag"],
                "action": entry["action"],
                "reason": entry["reason"],
            }
            for entry in matrix_entries
        ]
    }
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "event": event,
                    "live": live,
                    "entries": entries,
                    "matrix": matrix,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    if args.summary_out:
        path = Path(args.summary_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(summary, encoding="utf-8")
    write_github_output(
        args.github_output,
        {
            "live": str(live).lower(),
            "pending": len(entries),
            "has_work": str(has_work).lower(),
            "matrix": json.dumps(matrix, ensure_ascii=False),
        },
    )
    log(summary)
    for entry in entries:
        log(
            f"{entry['product']} {entry['tag']} ({entry['release_id']}): "
            f"{entry['action']} [{entry['reason']}]"
        )
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    config = load_config()
    relay_repo = relay_repository(config)
    product = get_product(config, args.product)
    source = client_for_product(product)
    live = parse_bool(args.live)
    relay: GitHubClient | None = None
    if live:
        relay = relay_client()
    latest_lookup: Callable[[str], str | None] | None = None
    if args.action in {"import", "promote"}:
        latest_cache: dict[str, str | None] = {}

        def latest_for(channel: str) -> str | None:
            if channel not in latest_cache:
                latest_cache[channel] = mirror_latest_tag(args.product, product, channel)
            return latest_cache[channel]

        latest_lookup = latest_for
    result = prepare_release(
        config,
        source,
        relay,
        relay_repo,
        args.product,
        args.release_id,
        args.action,
        live,
        Path(args.staging_dir),
        reason=args.reason or "",
        wait_timeout=args.wait_timeout,
        wait_interval=args.wait_interval,
        latest_lookup=latest_lookup,
    )
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    write_github_output(
        args.github_output,
        {
            "upload": str(bool(result.get("upload"))).lower(),
            "sync_note": str(bool(result.get("sync_note"))).lower(),
            "record": str(bool(result.get("record"))).lower(),
            "asset_path": result.get("asset_path"),
            "version_name": result["version_name"],
            "channel": result["channel"],
            "resource_id": result["resource_id"],
            "os": result["os"],
            "arch": result["arch"],
            "query_os": result["query_os"],
            "query_arch": result["query_arch"],
            "relay_tag": result["relay_tag"],
            "body_sha256": result["body_sha256"],
            "release_note": result.get("release_note") or "",
            "promotion": str(bool(result.get("promotion"))).lower(),
        },
    )
    log(
        f"{result['product']} {result['version_name']}: "
        f"channel={result['channel']} upload={result['upload']} "
        f"sync_note={result['sync_note']} record={result['record']}"
    )
    if result.get("promotion"):
        log(f"promotion: {result['channel']} upload planned for {result['version_name']}")
    if result.get("pending_reason"):
        log(f"planned action: {result['pending_reason']} channel={result['channel']}")
    if result.get("asset_name"):
        log(f"asset: {result['asset_name']} {result['asset_size']} bytes")
        log(f"sha256: {result['asset_sha256']}")
        log(f"checksums: {', '.join(result['checksum_files']) or 'none present'}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    config = load_config()
    relay_repo = relay_repository(config)
    relay = relay_client()
    manifest = record_outcome(
        relay,
        relay_repo,
        args.release_id,
        args.relay_tag,
        args.channel,
        args.mirror_outcome or "skipped",
        args.note_outcome or "skipped",
        args.body_sha256,
        args.run_url,
    )
    log(
        f"recorded {args.relay_tag} {args.channel}: "
        f"mirror={upload_record(manifest, args.channel).get('status')} "
        f"note={note_record(manifest, args.channel).get('status')}"
    )
    return 0


def cmd_verify_mirror(args: argparse.Namespace) -> int:
    observed = verify_mirror_version(
        args.rid,
        args.version,
        args.channel,
        args.os,
        args.arch,
        args.action_outcome or "unknown",
        attempts=args.attempts,
        interval=args.interval,
    )
    log(f"MirrorChyan reports {observed!r} for {args.rid} {args.channel}")
    return 0


def cmd_upload_mirror(args: argparse.Namespace) -> int:
    token = os.environ.get("MIRROR_UPLOAD_TOKEN") or ""
    result = upload_to_mirror(
        args.rid,
        args.version,
        args.os,
        args.arch,
        args.channel,
        Path(args.file),
        token,
        attempts=args.attempts,
    )
    log(f"uploaded {result['filename']} to {args.rid} {args.channel}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="list releases that need work")
    plan.add_argument("--product")
    plan.add_argument("--tag")
    plan.add_argument("--event", default="local")
    plan.add_argument("--dry-run")
    plan.add_argument("--relay-live")
    plan.add_argument("--json-out")
    plan.add_argument("--summary-out")
    plan.add_argument("--github-output")
    plan.set_defaults(func=cmd_plan)

    prepare = subparsers.add_parser("prepare", help="download, verify and stage one release")
    prepare.add_argument("--product", required=True)
    prepare.add_argument("--release-id", type=int, required=True)
    prepare.add_argument(
        "--action", default="import", choices=["import", "promote", "note", "attention"]
    )
    prepare.add_argument("--reason", default="")
    prepare.add_argument("--live")
    prepare.add_argument("--staging-dir", default="staging")
    prepare.add_argument("--wait-timeout", type=float)
    prepare.add_argument("--wait-interval", type=float)
    prepare.add_argument("--json-out")
    prepare.add_argument("--github-output")
    prepare.set_defaults(func=cmd_prepare)

    record = subparsers.add_parser("record", help="store MirrorChyan outcomes")
    record.add_argument("--product", required=True)
    record.add_argument("--release-id", type=int, required=True)
    record.add_argument("--relay-tag", required=True)
    record.add_argument("--channel", required=True, choices=["stable", "beta"])
    record.add_argument("--mirror-outcome", default="skipped")
    record.add_argument("--note-outcome", default="skipped")
    record.add_argument("--body-sha256")
    record.add_argument("--run-url")
    record.set_defaults(func=cmd_record)

    verify = subparsers.add_parser("verify-mirror", help="confirm the published version")
    verify.add_argument("--rid", required=True)
    verify.add_argument("--version", required=True)
    verify.add_argument("--channel", required=True)
    verify.add_argument("--os", required=True)
    verify.add_argument("--arch", required=True)
    verify.add_argument("--action-outcome", default="unknown")
    verify.add_argument("--attempts", type=int, default=6)
    verify.add_argument("--interval", type=float, default=10.0)
    verify.set_defaults(func=cmd_verify_mirror)

    upload = subparsers.add_parser("upload-mirror", help="upload a verified file")
    upload.add_argument("--rid", required=True)
    upload.add_argument("--version", required=True)
    upload.add_argument("--os", required=True)
    upload.add_argument("--arch", required=True)
    upload.add_argument("--channel", required=True)
    upload.add_argument("--file", required=True)
    upload.add_argument("--attempts", type=int, default=3)
    upload.set_defaults(func=cmd_upload_mirror)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RelayError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
