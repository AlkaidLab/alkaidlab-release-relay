"""Unit tests for the release relay.  The GitHub API is fully mocked."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import relay_release as relay

SOURCE_REPO = "AlkaidLab/foundation-sunshine"
RELAY_REPO = "AlkaidLab/release-relay"


def make_config() -> dict:
    return {
        "relay": {"repository": RELAY_REPO},
        "products": {
            "foundation-sunshine": {
                "display_name": "Foundation Sunshine",
                "source": {
                    "kind": "github",
                    "repository": SOURCE_REPO,
                    "public": True,
                },
                "mirror": {"resource_id": "foundation-sunshine"},
                "asset_wait": {"timeout_seconds": 30, "interval_seconds": 15},
                "checksums": {"assets": ["checksums.json", "SHA256SUMS.txt"]},
                "assets": [
                    {
                        "id": "windows-installer",
                        "source_pattern": "Sunshine.*.WindowsInstaller.exe",
                        "os": "win",
                        "arch": "x64",
                        "query_os": "windows",
                        "query_arch": "amd64",
                        "resource_type": "exec",
                    }
                ],
            }
        },
    }


class FakeGitHub:
    """In-memory implementation of the client surface used by the relay."""

    def __init__(self) -> None:
        self.repositories: dict[str, dict[int, dict]] = {}
        self.texts: dict[str, str] = {}
        self.files: dict[str, bytes] = {}
        self.next_id = 1000
        self.created: list[dict] = []
        self.uploads: list[tuple[int, str, bytes]] = []
        self.deleted: list[int] = []
        self.downloads: list[str] = []
        self.sequence: list[dict] = []

    # -- reads -------------------------------------------------------------
    def list_releases(self, repository: str) -> list[dict]:
        releases = sorted(
            self.repositories.get(repository, {}).values(),
            key=lambda item: int(item["id"]),
            reverse=True,
        )
        return copy.deepcopy(releases)

    def get_release(self, repository: str, release_id: int) -> dict | None:
        if self.sequence:
            return copy.deepcopy(self.sequence.pop(0))
        release = self.repositories.get(repository, {}).get(int(release_id))
        return copy.deepcopy(release) if release is not None else None

    def get_release_by_tag(self, repository: str, tag: str) -> dict | None:
        for release in self.repositories.get(repository, {}).values():
            if release.get("tag_name") == tag:
                return copy.deepcopy(release)
        return None

    # -- writes ------------------------------------------------------------
    def create_release(
        self, repository: str, tag: str, name: str, body: str, prerelease: bool = True
    ) -> dict:
        self.next_id += 1
        release = {
            "id": self.next_id,
            "tag_name": tag,
            "name": name,
            "body": body,
            "draft": False,
            "prerelease": prerelease,
            "published_at": "2026-11-03T00:00:00Z",
            "updated_at": "2026-11-03T00:00:00Z",
            "html_url": f"https://example.test/{repository}/releases/{self.next_id}",
            "assets": [],
        }
        self.repositories.setdefault(repository, {})[release["id"]] = release
        self.created.append(copy.deepcopy(release))
        return copy.deepcopy(release)

    def delete_asset(self, repository: str, asset_id: int) -> None:
        self.deleted.append(int(asset_id))
        for release in self.repositories.get(repository, {}).values():
            release["assets"] = [
                asset for asset in release["assets"] if int(asset["id"]) != int(asset_id)
            ]

    def upload_asset(
        self,
        repository: str,
        release_id: int,
        name: str,
        path: Path,
        content_type: str = "application/octet-stream",
    ) -> dict:
        return self.upload_asset_bytes(
            repository, release_id, name, Path(path).read_bytes(), content_type
        )

    def upload_asset_bytes(
        self,
        repository: str,
        release_id: int,
        name: str,
        data: bytes,
        content_type: str = "application/json",
    ) -> dict:
        release = self.repositories[repository][int(release_id)]
        self.next_id += 1
        url = f"https://relay.test/{repository}/{release_id}/{name}"
        asset = {
            "id": self.next_id,
            "name": name,
            "size": len(data),
            "state": "uploaded",
            "browser_download_url": url,
            "content_type": content_type,
        }
        release["assets"].append(asset)
        self.uploads.append((int(release_id), name, bytes(data)))
        self.files[url] = bytes(data)
        self.texts[url] = data.decode("utf-8", errors="replace")
        return copy.deepcopy(asset)

    # -- transfer ----------------------------------------------------------
    def download(
        self, url: str, destination: Path, expected_size: int | None = None
    ) -> tuple[int, str]:
        self.downloads.append(url)
        data = self.files[url]
        if expected_size is not None and len(data) != expected_size:
            raise relay.RelayError("fixture size mismatch")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return len(data), hashlib.sha256(data).hexdigest()

    def fetch_text(self, url: str, limit: int = 1024 * 1024) -> str:
        if url in self.texts:
            return self.texts[url]
        return self.files[url].decode("utf-8", errors="replace")


def add_source_release(
    fake: FakeGitHub,
    release_id: int,
    tag: str,
    *,
    prerelease: bool = False,
    body: str = "release notes",
    installer: bytes = b"installer-bytes",
    checksums: tuple[str, ...] = ("json", "txt"),
    asset_state: str = "uploaded",
    published_at: str = "2026-11-01T00:00:00Z",
) -> dict:
    name = f"Sunshine.{tag}.WindowsInstaller.exe"
    url = f"https://source.test/{release_id}/{name}"
    fake.files[url] = installer
    digest = hashlib.sha256(installer).hexdigest()
    release = {
        "id": release_id,
        "tag_name": tag,
        "name": f"Release {tag}",
        "body": body,
        "draft": False,
        "prerelease": prerelease,
        "published_at": published_at,
        "updated_at": published_at,
        "html_url": f"https://github.com/{SOURCE_REPO}/releases/tag/{tag}",
        "assets": [
            {
                "id": release_id * 10 + 1,
                "name": name,
                "size": len(installer),
                "state": asset_state,
                "browser_download_url": url,
            }
        ],
    }
    if "json" in checksums:
        text = json.dumps(
            [{"Hash": digest.upper(), "File": name, "Size": "1 MB"}], indent=2
        )
        checksum_url = f"https://source.test/{release_id}/checksums.json"
        fake.texts[checksum_url] = text
        release["assets"].append(
            {
                "id": release_id * 10 + 2,
                "name": "checksums.json",
                "size": len(text),
                "state": "uploaded",
                "browser_download_url": checksum_url,
            }
        )
    if "txt" in checksums:
        text = f"# generated\n{digest}  {name}\n"
        checksum_url = f"https://source.test/{release_id}/SHA256SUMS.txt"
        fake.texts[checksum_url] = text
        release["assets"].append(
            {
                "id": release_id * 10 + 3,
                "name": "SHA256SUMS.txt",
                "size": len(text),
                "state": "uploaded",
                "browser_download_url": checksum_url,
            }
        )
    fake.repositories.setdefault(SOURCE_REPO, {})[release_id] = release
    return release


def add_relay_release(
    fake: FakeGitHub,
    release: dict,
    *,
    mirror_status: str = "uploaded",
    note_sha: str | None = None,
    note_status: str = "synced",
    source_asset_id: int | None = None,
) -> dict:
    tag = relay.build_relay_tag("foundation-sunshine", release["tag_name"], release["id"])
    created = fake.create_release(RELAY_REPO, tag, "relay", "manifest")
    asset = release["assets"][0]
    channel = relay.channel_for(release)
    manifest = {
        "schema_version": 2,
        "product": "foundation-sunshine",
        "source": {
            "kind": "github",
            "repository": SOURCE_REPO,
            "release_id": release["id"],
            "tag": release["tag_name"],
            "published_at": release["published_at"],
            "prerelease": release["prerelease"],
            "body_sha256": relay.text_sha256(release["body"]),
        },
        "asset": {
            "rule": "windows-installer",
            "source_asset_id": source_asset_id or asset["id"],
            "name": asset["name"],
            "size": asset["size"],
            "sha256": hashlib.sha256(
                fake.files[asset["browser_download_url"]]
            ).hexdigest(),
            "checksum_files": [],
        },
        "relay": {"tag": tag, "created_at": "2026-11-01T00:00:00Z"},
        "mirror": {
            "resource_id": "foundation-sunshine",
            "version_name": release["tag_name"],
            "channel": channel,
            "os": "win",
            "arch": "x64",
            "uploads": {channel: {"status": mirror_status}},
        },
        "note": {
            "channel": channel,
            "uploads": {
                channel: {
                    "status": note_status,
                    "body_sha256": note_sha
                    if note_sha is not None
                    else relay.text_sha256(release["body"]),
                }
            },
        },
    }
    fake.upload_asset_bytes(
        RELAY_REPO,
        created["id"],
        asset["name"],
        fake.files[asset["browser_download_url"]],
        "application/octet-stream",
    )
    relay.write_manifest(fake, RELAY_REPO, created, manifest)
    return created


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class AssetSelectionTests(unittest.TestCase):
    def test_selects_single_matching_asset(self) -> None:
        fake = FakeGitHub()
        release = add_source_release(fake, 1, "v1")
        rule = make_config()["products"]["foundation-sunshine"]["assets"][0]
        matches = relay.matching_assets(release, rule)
        self.assertEqual(1, len(matches))
        self.assertTrue(matches[0]["name"].endswith("WindowsInstaller.exe"))

    def test_rejects_ambiguous_asset_match(self) -> None:
        fake = FakeGitHub()
        release = add_source_release(fake, 1, "v1")
        duplicate = copy.deepcopy(release["assets"][0])
        duplicate["id"] = 99
        duplicate["name"] = "Sunshine.v1.Other.WindowsInstaller.exe"
        fake.repositories[SOURCE_REPO][1]["assets"].append(duplicate)
        rule = make_config()["products"]["foundation-sunshine"]["assets"][0]
        clock = FakeClock()
        with self.assertRaisesRegex(relay.RelayError, "matched 2 assets"):
            relay.wait_for_asset(
                fake, SOURCE_REPO, 1, rule, timeout=30, interval=15,
                sleep=clock.sleep, clock=clock.clock,
            )

    def test_rejects_missing_asset_on_explicit_tag(self) -> None:
        fake = FakeGitHub()
        release = add_source_release(fake, 1, "v1")
        fake.repositories[SOURCE_REPO][1]["assets"] = [
            asset for asset in release["assets"] if "Installer" not in asset["name"]
        ]
        with self.assertRaisesRegex(relay.RelayError, "exactly one asset"):
            relay.plan_releases(
                make_config(), fake, fake, RELAY_REPO, tag="v1"
            )


class ChannelTests(unittest.TestCase):
    def test_channel_is_beta_for_prerelease(self) -> None:
        self.assertEqual("beta", relay.channel_for({"prerelease": True}))

    def test_channel_is_stable_for_release(self) -> None:
        self.assertEqual("stable", relay.channel_for({"prerelease": False}))

    def test_relay_tag_sanitizes_unicode(self) -> None:
        self.assertEqual(
            "relay/foundation-sunshine/v2026.1031.121458-429927943",
            relay.build_relay_tag(
                "foundation-sunshine", "v2026.1031.121458.杂鱼", 429927943
            ),
        )


class AssetWaitTests(unittest.TestCase):
    def test_wait_retries_until_asset_uploaded(self) -> None:
        fake = FakeGitHub()
        release = add_source_release(fake, 1, "v1", asset_state="uploading")
        first = copy.deepcopy(release)
        second = copy.deepcopy(release)
        uploaded = copy.deepcopy(release)
        uploaded["assets"][0]["state"] = "uploaded"
        fake.sequence = [first, second, uploaded]
        rule = make_config()["products"]["foundation-sunshine"]["assets"][0]
        clock = FakeClock()
        _, asset = relay.wait_for_asset(
            fake, SOURCE_REPO, 1, rule, timeout=60, interval=15,
            sleep=clock.sleep, clock=clock.clock,
        )
        self.assertEqual("uploaded", asset["state"])
        self.assertEqual(2, len(clock.sleeps))

    def test_wait_times_out_with_asset_states(self) -> None:
        fake = FakeGitHub()
        release = add_source_release(fake, 1, "v1", asset_state="uploading")
        fake.sequence = [copy.deepcopy(release) for _ in range(3)]
        rule = make_config()["products"]["foundation-sunshine"]["assets"][0]
        clock = FakeClock()
        with self.assertRaisesRegex(relay.RelayError, "timed out after 30s.*uploading"):
            relay.wait_for_asset(
                fake, SOURCE_REPO, 1, rule, timeout=30, interval=15,
                sleep=clock.sleep, clock=clock.clock,
            )


class ChecksumTests(unittest.TestCase):
    def _release(self, **kwargs) -> tuple[FakeGitHub, dict]:
        fake = FakeGitHub()
        release = add_source_release(fake, 1, "v1", **kwargs)
        return fake, release

    def test_checksums_json_and_txt_match(self) -> None:
        fake, release = self._release()
        asset = release["assets"][0]
        digest = hashlib.sha256(fake.files[asset["browser_download_url"]]).hexdigest()
        checked = relay.verify_checksums(
            fake, release, asset["name"], digest, ("checksums.json", "SHA256SUMS.txt")
        )
        self.assertEqual(["checksums.json", "SHA256SUMS.txt"], checked)

    def test_checksum_mismatch_fails(self) -> None:
        fake, release = self._release()
        with self.assertRaisesRegex(relay.RelayError, "records"):
            relay.verify_checksums(
                fake, release, release["assets"][0]["name"], "f" * 64,
                ("SHA256SUMS.txt",),
            )

    def test_checksum_file_without_entry_fails(self) -> None:
        fake, release = self._release(checksums=("txt",))
        text_url = release["assets"][1]["browser_download_url"]
        fake.texts[text_url] = f"{'0' * 64}  Other.exe\n"
        with self.assertRaisesRegex(relay.RelayError, "has no entry"):
            relay.verify_checksums(
                fake, release, release["assets"][0]["name"], "a" * 64,
                ("SHA256SUMS.txt",),
            )

    def test_absent_checksum_files_are_skipped(self) -> None:
        fake, release = self._release(checksums=())
        checked = relay.verify_checksums(
            fake, release, release["assets"][0]["name"], "a" * 64,
            ("checksums.json", "SHA256SUMS.txt"),
        )
        self.assertEqual([], checked)


class PlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeGitHub()

    def test_bootstraps_newest_per_channel(self) -> None:
        add_source_release(self.fake, 100, "v1")
        add_source_release(self.fake, 200, "v2", body="stable two")
        add_source_release(self.fake, 150, "v3b", prerelease=True)
        add_source_release(self.fake, 250, "v4b", prerelease=True)
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual(
            [(200, "import", "bootstrap"), (250, "import", "bootstrap")],
            [(entry["release_id"], entry["action"], entry["reason"]) for entry in entries],
        )

    def test_detects_new_release_after_state(self) -> None:
        known = add_source_release(self.fake, 200, "v2")
        add_relay_release(self.fake, known)
        add_source_release(self.fake, 300, "v3")
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual(
            [(300, "import", "new")],
            [(entry["release_id"], entry["action"], entry["reason"]) for entry in entries],
        )

    def test_detects_edited_note(self) -> None:
        known = add_source_release(self.fake, 200, "v2", body="old notes")
        add_relay_release(self.fake, known, note_sha=relay.text_sha256("old notes"))
        self.fake.repositories[SOURCE_REPO][200]["body"] = "new notes"
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual(
            [(200, "note", "edited")],
            [(entry["release_id"], entry["action"], entry["reason"]) for entry in entries],
        )

    def test_retries_failed_note(self) -> None:
        known = add_source_release(self.fake, 200, "v2", body="same notes")
        add_relay_release(
            self.fake, known, note_sha=relay.text_sha256("same notes"), note_status="failed"
        )
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual(
            [(200, "note", "retry-note")],
            [(entry["release_id"], entry["action"], entry["reason"]) for entry in entries],
        )

    def test_flags_unfinished_upload(self) -> None:
        known = add_source_release(self.fake, 200, "v2")
        add_relay_release(self.fake, known, mirror_status="started")
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual("attention", entries[0]["action"])
        self.assertIn("started", entries[0]["reason"])

    def test_flags_missing_manifest(self) -> None:
        known = add_source_release(self.fake, 200, "v2")
        relay_tag = relay.build_relay_tag(
            "foundation-sunshine", known["tag_name"], known["id"]
        )
        self.fake.create_release(RELAY_REPO, relay_tag, "relay", "no manifest")
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual("attention", entries[0]["action"])
        self.assertIn("relay-manifest.json", entries[0]["reason"])

    def test_flags_missing_relay_asset(self) -> None:
        known = add_source_release(self.fake, 200, "v2")
        created = add_relay_release(self.fake, known)
        stored = self.fake.repositories[RELAY_REPO][created["id"]]
        stored["assets"] = [
            asset for asset in stored["assets"] if asset["name"] != known["assets"][0]["name"]
        ]
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual("attention", entries[0]["action"])
        self.assertIn("0 assets named", entries[0]["reason"])

    def test_dry_dispatch_reports_nothing_for_complete_release(self) -> None:
        known = add_source_release(self.fake, 200, "v2", body="old")
        add_relay_release(self.fake, known)
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO, tag="v2", dry_run=True
        )
        self.assertEqual([], entries)

    def test_live_dispatch_plans_import_for_missing_relay_release(self) -> None:
        add_source_release(self.fake, 200, "v2")
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO, tag="v2", dry_run=False
        )
        self.assertEqual(
            [(200, "import", "dispatched")],
            [(entry["release_id"], entry["action"], entry["reason"]) for entry in entries],
        )

    def test_does_not_replay_older_releases_after_bootstrap(self) -> None:
        newest = add_source_release(
            self.fake, 250, "v2b", prerelease=True, published_at="2026-11-02T18:39:00Z"
        )
        add_relay_release(self.fake, newest)
        add_source_release(
            self.fake, 240, "v2a", prerelease=True, published_at="2026-11-02T10:35:00Z"
        )
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual([], entries)

    def test_imports_late_published_draft_with_older_id(self) -> None:
        known = add_source_release(
            self.fake, 250, "v2", published_at="2026-11-01T00:00:00Z"
        )
        add_relay_release(self.fake, known)
        add_source_release(
            self.fake, 240, "v1held", published_at="2026-11-02T12:00:00Z"
        )
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual(
            [(240, "import", "new")],
            [(entry["release_id"], entry["action"], entry["reason"]) for entry in entries],
        )

    def test_unknown_product_is_refused(self) -> None:
        with self.assertRaisesRegex(relay.RelayError, "unknown product"):
            relay.get_product(make_config(), "other-product")

    def _promotion(self) -> dict:
        release = add_source_release(self.fake, 200, "v2", prerelease=True, body="notes")
        add_relay_release(self.fake, release)
        self.fake.repositories[SOURCE_REPO][200]["prerelease"] = False
        return release

    def test_promotion_plans_one_stable_upload(self) -> None:
        self._promotion()
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual(1, len(entries))
        self.assertEqual(
            (200, "promote", "promotion", "stable"),
            (
                entries[0]["release_id"],
                entries[0]["action"],
                entries[0]["reason"],
                entries[0]["channel"],
            ),
        )

    def test_promotion_rerun_is_noop(self) -> None:
        release = self._promotion()
        tag = relay.build_relay_tag("foundation-sunshine", release["tag_name"], release["id"])
        relay.record_outcome(
            self.fake, RELAY_REPO, 200, tag, "stable", "success", "success",
            relay.text_sha256(release["body"]), None,
        )
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO
        )
        self.assertEqual([], entries)

    def test_demotion_is_logged_not_uploaded(self) -> None:
        release = add_source_release(self.fake, 200, "v2", prerelease=False)
        add_relay_release(self.fake, release)
        self.fake.repositories[SOURCE_REPO][200]["prerelease"] = True
        with mock.patch.object(relay, "log") as log:
            entries = relay.plan_releases(
                make_config(), self.fake, self.fake, RELAY_REPO
            )
        self.assertEqual([], entries)
        self.assertTrue(
            any("demotion ignored" in str(call) for call in log.call_args_list)
        )

    def test_dry_run_reports_promotion(self) -> None:
        self._promotion()
        entries = relay.plan_releases(
            make_config(), self.fake, self.fake, RELAY_REPO, tag="v2", dry_run=True
        )
        self.assertEqual(1, len(entries))
        self.assertEqual("promote", entries[0]["action"])
        self.assertEqual("promotion", entries[0]["reason"])
        self.assertTrue(entries[0]["dry_run"])


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeGitHub()
        self.config = make_config()
        self.release = add_source_release(self.fake, 429927943, "v2026.1031.121458.杂鱼")
        self.tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self.tmp.name) / "staging"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _prepare(self, live: bool = True) -> dict:
        clock = FakeClock()
        return relay.prepare_release(
            self.config,
            self.fake,
            self.fake if live else None,
            RELAY_REPO,
            "foundation-sunshine",
            self.release["id"],
            "import",
            live,
            self.staging,
            sleep=clock.sleep,
            clock=clock.clock,
        )

    def test_dry_run_does_not_write(self) -> None:
        result = self._prepare(live=False)
        self.assertFalse(result["upload"])
        self.assertEqual([], self.fake.created)
        self.assertEqual([], self.fake.uploads)
        self.assertTrue((self.staging / "foundation-sunshine" / str(self.release["id"])).exists())

    def test_import_creates_relay_release_and_manifest(self) -> None:
        result = self._prepare()
        self.assertTrue(result["upload"])
        self.assertEqual(1, len(self.fake.created))
        names = [name for _, name, _ in self.fake.uploads]
        self.assertEqual(
            [self.release["assets"][0]["name"], "relay-manifest.json"], names
        )
        doc = json.loads(
            next(
                data
                for _, name, data in self.fake.uploads
                if name == "relay-manifest.json"
            )
        )
        self.assertEqual("started", doc["mirror"]["uploads"]["stable"]["status"])
        self.assertEqual(self.release["id"], doc["source"]["release_id"])

    def test_import_is_idempotent_after_upload(self) -> None:
        self._prepare()
        relay.record_outcome(
            self.fake, RELAY_REPO, self.release["id"],
            relay.build_relay_tag(
                "foundation-sunshine", self.release["tag_name"], self.release["id"]
            ),
            "stable", "success", "success",
            relay.text_sha256(self.release["body"]), None,
        )
        downloads_before = len(self.fake.downloads)
        uploads_before = len(self.fake.uploads)
        result = self._prepare()
        self.assertFalse(result["upload"])
        self.assertEqual("already-uploaded", result["mirror_state"])
        self.assertEqual(1, len(self.fake.created))
        self.assertEqual(downloads_before, len(self.fake.downloads))
        self.assertEqual(uploads_before, len(self.fake.uploads))

    def test_import_refuses_when_previous_upload_started(self) -> None:
        self._prepare()
        with self.assertRaisesRegex(relay.RelayError, "is 'started'"):
            self._prepare()

    def test_import_detects_replaced_source_asset(self) -> None:
        self._prepare()
        self.fake.repositories[SOURCE_REPO][self.release["id"]]["assets"][0]["id"] = 7
        with self.assertRaisesRegex(relay.RelayError, "changed after import"):
            self._prepare()

    def test_edited_note_is_prepared_without_redownload(self) -> None:
        self._prepare()
        relay.record_outcome(
            self.fake, RELAY_REPO, self.release["id"],
            relay.build_relay_tag(
                "foundation-sunshine", self.release["tag_name"], self.release["id"]
            ),
            "stable", "success", "success",
            relay.text_sha256(self.release["body"]), None,
        )
        self.fake.repositories[SOURCE_REPO][self.release["id"]]["body"] = "new body"
        downloads_before = len(self.fake.downloads)
        result = self._prepare()
        self.assertFalse(result["upload"])
        self.assertTrue(result["sync_note"])
        self.assertEqual(downloads_before, len(self.fake.downloads))

    def _promoted_release(self, release_id: int = 300) -> dict:
        release = add_source_release(
            self.fake, release_id, "v3", prerelease=True, body="promoted notes"
        )
        add_relay_release(self.fake, release)
        self.fake.repositories[SOURCE_REPO][release_id]["prerelease"] = False
        return release

    def test_prepare_promotion_marks_stable_without_reuploading_asset(self) -> None:
        release = self._promoted_release()
        downloads_before = len(self.fake.downloads)
        result = relay.prepare_release(
            self.config, self.fake, self.fake, RELAY_REPO,
            "foundation-sunshine", 300, "promote", True, self.staging,
            sleep=lambda _: None, clock=lambda: 0.0,
        )
        self.assertTrue(result["upload"])
        self.assertEqual("promotion", result["mirror_state"])
        self.assertTrue(result["sync_note"])
        installer_uploads = [item for item in self.fake.uploads if item[1].endswith(".exe")]
        self.assertEqual(1, len(installer_uploads))
        self.assertEqual(downloads_before + 1, len(self.fake.downloads))
        tag = relay.build_relay_tag("foundation-sunshine", release["tag_name"], release["id"])
        relay_release = self.fake.get_release_by_tag(RELAY_REPO, tag)
        manifest = relay.migrate_manifest(
            relay.read_manifest(self.fake, RELAY_REPO, relay_release)
        )
        self.assertEqual("uploaded", relay.upload_record(manifest, "beta")["status"])
        self.assertEqual("started", relay.upload_record(manifest, "stable")["status"])

    def test_prepare_promotion_rerun_is_noop(self) -> None:
        release = self._promoted_release(301)
        relay.prepare_release(
            self.config, self.fake, self.fake, RELAY_REPO,
            "foundation-sunshine", 301, "promote", True, self.staging,
            sleep=lambda _: None, clock=lambda: 0.0,
        )
        tag = relay.build_relay_tag("foundation-sunshine", release["tag_name"], release["id"])
        relay.record_outcome(
            self.fake, RELAY_REPO, 301, tag, "stable", "success", "success",
            relay.text_sha256(release["body"]), None,
        )
        downloads_before = len(self.fake.downloads)
        result = relay.prepare_release(
            self.config, self.fake, self.fake, RELAY_REPO,
            "foundation-sunshine", 301, "promote", True, self.staging,
            sleep=lambda _: None, clock=lambda: 0.0,
        )
        self.assertFalse(result["upload"])
        self.assertEqual("already-uploaded", result["mirror_state"])
        self.assertEqual(downloads_before, len(self.fake.downloads))

    def test_prepare_demotion_does_not_upload(self) -> None:
        release = add_source_release(self.fake, 302, "v4", prerelease=False)
        add_relay_release(self.fake, release)
        self.fake.repositories[SOURCE_REPO][302]["prerelease"] = True
        with mock.patch.object(relay, "log") as log:
            result = relay.prepare_release(
                self.config, self.fake, self.fake, RELAY_REPO,
                "foundation-sunshine", 302, "import", True, self.staging,
                sleep=lambda _: None, clock=lambda: 0.0,
            )
        self.assertFalse(result["upload"])
        self.assertEqual("demotion-ignored", result["mirror_state"])
        self.assertTrue(
            any("demotion ignored" in str(call) for call in log.call_args_list)
        )


class ManifestTests(unittest.TestCase):
    def test_legacy_manifest_migrates_single_channel(self) -> None:
        legacy = {
            "schema_version": 1,
            "mirror": {"channel": "stable", "status": "uploaded"},
            "note": {"status": "failed", "body_sha256": "abc"},
        }
        merged = relay.migrate_manifest(legacy)
        self.assertEqual("uploaded", relay.upload_record(merged, "stable")["status"])
        self.assertEqual("failed", relay.note_record(merged, "stable")["status"])
        self.assertEqual(2, merged["schema_version"])
        self.assertEqual({}, relay.upload_record(merged, "beta"))


class WorkflowTests(unittest.TestCase):
    def test_official_action_fallback_is_wired(self) -> None:
        text = (ROOT / ".github/workflows/relay.yml").read_text(encoding="utf-8")
        self.assertIn("use_official_action:", text)
        self.assertIn("MirrorChyan/uploading-action@v1", text)
        self.assertIn("macos-latest", text)
        self.assertIn("--channel", text)


class LiveGatingTests(unittest.TestCase):
    def test_blank_boolean_uses_default(self) -> None:
        self.assertFalse(relay.parse_bool("", default=False))
        self.assertTrue(relay.parse_bool("", default=True))
        self.assertFalse(relay.parse_bool("false", default=True))

    def test_dispatch_without_dry_run_is_live(self) -> None:
        self.assertTrue(relay.resolve_live("workflow_dispatch", "false", ""))

    def test_dispatch_dry_run_wins_over_variable(self) -> None:
        self.assertFalse(relay.resolve_live("workflow_dispatch", "true", "1"))

    def test_schedule_requires_relay_live(self) -> None:
        self.assertTrue(relay.resolve_live("schedule", "", "1"))
        self.assertFalse(relay.resolve_live("schedule", "", ""))

    def test_dry_schedule_has_no_jobs(self) -> None:
        summary, has_work, matrix = relay.summary_and_matrix(
            [{"product": "p", "release_id": 1, "tag": "v1", "action": "import", "reason": "new"}],
            "schedule",
            False,
        )
        self.assertFalse(has_work)
        self.assertEqual([], matrix)
        self.assertIn("RELAY_LIVE", summary)

    def test_live_schedule_has_jobs(self) -> None:
        _, has_work, matrix = relay.summary_and_matrix(
            [{"product": "p", "release_id": 1, "tag": "v1", "action": "import", "reason": "new"}],
            "schedule",
            True,
        )
        self.assertTrue(has_work)
        self.assertEqual(1, len(matrix))


class MirrorVerificationTests(unittest.TestCase):
    def test_accepts_version_reported_by_api(self) -> None:
        calls = []

        def fetch(rid, channel, os_name, arch):
            calls.append((rid, channel, os_name, arch))
            return {"code": 0, "data": {"version_name": "v2"}}

        observed = relay.verify_mirror_version(
            "rid", "v2", "stable", "windows", "amd64", "success",
            attempts=3, interval=0, sleep=lambda _: None, fetch=fetch,
        )
        self.assertEqual("v2", observed)
        self.assertEqual([("rid", "stable", "windows", "amd64")], calls)

    def test_confirms_when_action_failed_after_upload(self) -> None:
        responses = [
            {"code": 0, "data": {"version_name": "v1"}},
            {"code": 0, "data": {"version_name": "v2"}},
        ]
        observed = relay.verify_mirror_version(
            "rid", "v2", "beta", "windows", "amd64", "failure",
            attempts=3, interval=0, sleep=lambda _: None,
            fetch=lambda *_: responses.pop(0),
        )
        self.assertEqual("v2", observed)

    def test_fails_when_unconfirmed_and_action_failed(self) -> None:
        with self.assertRaisesRegex(relay.RelayError, "does not report"):
            relay.verify_mirror_version(
                "rid", "v2", "stable", "windows", "amd64", "failure",
                attempts=2, interval=0, sleep=lambda _: None,
                fetch=lambda *_: {"code": 0, "data": {"version_name": "v1"}},
            )


class OutputTests(unittest.TestCase):
    def test_multiline_github_output_uses_a_delimiter(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "output"
            relay.write_github_output(str(path), {"note": "line one\nline two"})
            text = path.read_text(encoding="utf-8")
            self.assertRegex(text, r"^note<<RELAY_[0-9a-f]+\nline one\nline two\nRELAY_[0-9a-f]+\n$")


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self.payload = payload
        self.text = text

    def json(self):
        if self.payload is None:
            raise ValueError("no JSON body")
        return self.payload


class FakeMirrorSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.posts.append((url, kwargs))
        return self.responses.pop(0)

    def get(self, url: str, **kwargs):
        self.gets.append((url, kwargs))
        return self.responses.pop(0)


class MirrorUploadTests(unittest.TestCase):
    def _success_responses(self) -> list[FakeResponse]:
        return [
            FakeResponse(
                payload={
                    "data": {
                        "host": "https://objects.example/upload",
                        "name": "object-name",
                        "signature": "signature",
                        "key": "object-key",
                        "policy": "policy",
                        "access_key": "access-key",
                    }
                }
            ),
            FakeResponse(status_code=200),
            FakeResponse(payload={"data": {"status_key": "status-key"}}),
            FakeResponse(payload={"data": {"status": 2}}),
        ]

    def test_upload_uses_documented_fields_and_unicode_safe_header(self) -> None:
        session = FakeMirrorSession(self._success_responses())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Sunshine.v2026.1031.WindowsInstaller.exe"
            path.write_bytes(b"installer")
            result = relay.upload_to_mirror(
                "foundation-sunshine",
                "v2026.1031.121458.杂鱼",
                "win",
                "x64",
                "stable",
                path,
                "token",
                session=session,
                sleep=lambda _: None,
            )
        self.assertIn("v2026.1031.121458.杂鱼", result["filename"])
        create = session.posts[0]
        self.assertTrue(create[0].endswith("/api/resources/foundation-sunshine/versions"))
        self.assertEqual("v2026.1031.121458.杂鱼", create[1]["data"]["name"])
        self.assertEqual("stable", create[1]["data"]["channel"])
        self.assertEqual(
            "foundation-sunshine-v2026.1031.121458.杂鱼-win-x64.exe",
            create[1]["data"]["filename"],
        )
        disposition = session.posts[1][1]["headers"]["Content-Disposition"]
        self.assertTrue(disposition.isascii())
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn("%E6%9D%82%E9%B1%BC", disposition)
        self.assertEqual("object-key", session.posts[2][1]["data"]["key"])

    def test_processing_failure_raises(self) -> None:
        responses = self._success_responses()
        responses[-1] = FakeResponse(payload={"data": {"status": 3}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "installer.exe"
            path.write_bytes(b"x")
            with self.assertRaisesRegex(relay.RelayError, "failed to process"):
                relay.upload_to_mirror(
                    "foundation-sunshine", "v1", "win", "x64", "stable",
                    path, "token", session=FakeMirrorSession(responses),
                    sleep=lambda _: None, attempts=1,
                )

    def test_empty_token_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "installer.exe"
            path.write_bytes(b"x")
            with self.assertRaisesRegex(relay.RelayError, "Token is empty"):
                relay.upload_to_mirror(
                    "rid", "v1", "win", "x64", "stable", path, "",
                    session=FakeMirrorSession([]), sleep=lambda _: None,
                )

    def test_create_failure_is_retried(self) -> None:
        responses = [FakeResponse(status_code=500, text="busy")] + self._success_responses()
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "installer.exe"
            path.write_bytes(b"x")
            relay.upload_to_mirror(
                "rid", "v1", "win", "x64", "stable", path, "token",
                session=FakeMirrorSession(responses), sleep=sleeps.append,
                poll_interval=0,
            )
        self.assertEqual([15], [value for value in sleeps if value])

    def test_status_failure_after_callback_is_not_retried(self) -> None:
        class RaisingGet(FakeMirrorSession):
            def get(self, url: str, **kwargs):
                raise OSError("connection reset")

        session = RaisingGet(self._success_responses()[:3])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "installer.exe"
            path.write_bytes(b"x")
            with self.assertRaisesRegex(relay.RelayError, "accepted the callback"):
                relay.upload_to_mirror(
                    "rid", "v1", "win", "x64", "stable", path, "token",
                    session=session, sleep=lambda _: None, poll_interval=0,
                )
        self.assertEqual(3, len(session.posts))


if __name__ == "__main__":
    unittest.main()
