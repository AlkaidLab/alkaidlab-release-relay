#!/usr/bin/env python3
"""Resolve and validate a source GitHub Release for the relay dry-run."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "products.yml"


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_config() -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        fail("PyYAML is required; install it with `python -m pip install pyyaml`.")
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def github_request(path: str) -> Any:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "AlkaidLab-release-relay",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(3):
        request = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            fail(f"GitHub API {error.code} for {path}: {body}")
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            if attempt == 2:
                fail(f"GitHub API request failed for {path}: {error}")
            time.sleep(2**attempt)

    fail(f"GitHub API request failed for {path}")


def resolve_release(repository: str, tag: str | None) -> dict[str, Any]:
    if tag:
        encoded_tag = urllib.parse.quote(tag, safe="")
        return github_request(f"/repos/{repository}/releases/tags/{encoded_tag}")
    return github_request(f"/repos/{repository}/releases/latest")


def source_tag(repository: str, tag: str) -> dict[str, Any]:
    encoded_tag = urllib.parse.quote(tag, safe="")
    ref = github_request(f"/repos/{repository}/git/ref/tags/{encoded_tag}")
    object_info = ref.get("object", {})
    if object_info.get("type") == "tag":
        return github_request(f"/repos/{repository}/git/tags/{object_info['sha']}")
    return object_info


def validate_asset(asset: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    name = asset.get("name", "")
    if not fnmatch.fnmatchcase(name, rule["source_pattern"]):
        fail(f"asset {name!r} does not match {rule['source_pattern']!r}")
    if int(asset.get("size", 0)) <= 0:
        fail(f"asset {name!r} has no positive size")
    return {
        "id": rule["id"],
        "name": name,
        "size": asset["size"],
        "content_type": asset.get("content_type"),
        "browser_download_url": asset["browser_download_url"],
        "os": rule["os"],
        "arch": rule["arch"],
        "resource_type": rule.get("resource_type"),
    }


def resolve_product(config: dict[str, Any], product: str, tag: str | None) -> dict[str, Any]:
    products = config.get("products", {})
    if product not in products:
        fail(f"unknown product {product!r}")
    product_config = products[product]
    source = product_config["source"]
    if source.get("kind") != "github":
        fail(f"dry-run does not yet support source kind {source.get('kind')!r}")

    repository = source["repository"]
    release = resolve_release(repository, tag)
    if release.get("draft"):
        fail(f"source release {release.get('tag_name')} is a draft")

    release_assets = release.get("assets", [])
    selected_assets: list[dict[str, Any]] = []
    for rule in product_config.get("assets", []):
        matches = [
            asset
            for asset in release_assets
            if fnmatch.fnmatchcase(asset.get("name", ""), rule["source_pattern"])
        ]
        if len(matches) != 1:
            names = [asset.get("name") for asset in matches]
            fail(
                f"rule {rule['id']!r} matched {len(matches)} assets in "
                f"{release.get('tag_name')}: {names}"
            )
        selected_assets.append(validate_asset(matches[0], rule))

    tag_name = release["tag_name"]
    ref_object = source_tag(repository, tag_name)
    return {
        "schema_version": 1,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "product": product,
        "display_name": product_config.get("display_name", product),
        "source": {
            "kind": source["kind"],
            "repository": repository,
            "release_id": release["id"],
            "tag": tag_name,
            "target_commitish": release.get("target_commitish"),
            "tag_object": ref_object,
            "release_url": release["html_url"],
            "published_at": release.get("published_at"),
            "prerelease": release.get("prerelease", False),
            "release_name": release.get("name"),
            "release_body": release.get("body") or "",
        },
        "mirror": product_config.get("mirror", {}),
        "assets": selected_assets,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", required=True)
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path, default=Path("out/release-record.json"))
    args = parser.parse_args()

    record = resolve_product(load_config(), args.product, args.tag)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "product": record["product"],
        "source": record["source"]["repository"],
        "tag": record["source"]["tag"],
        "release_id": record["source"]["release_id"],
        "assets": [asset["name"] for asset in record["assets"]],
        "output": str(output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
