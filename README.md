# AlkaidLab Release Relay

Private relay that imports project releases from GitHub, verifies them, keeps an
auditable copy in this repository, and publishes the Windows installer to
MirrorChyan (Mirror酱). A source release stays the engineering source of truth;
the relay records the exact source release, asset selection, checksums and
distribution status.

The relay never writes to a source repository. Source repositories keep their
existing release process.

## Current phase

| Phase | Contents | State |
| --- | --- | --- |
| 1 | Resolve an exact source release and validate the installer without side effects | implemented |
| 2 | Download, verify (size, SHA256, source checksum files), create a namespaced relay Release | implemented |
| 3 | Upload the verified file and release note to MirrorChyan with an explicit channel | implemented |
| 4 | Add CNB as a source adapter without changing the GitHub/Mirror contract | not started |

Only `foundation-sunshine` Windows x64 is configured. See
`config/products.yml`; unknown products are refused.

## How a release flows

1. `plan` lists source releases and relay releases and decides what needs work:
   a new import, a release-note sync, or a manual review.
2. `prepare` waits for the configured installer to reach state `uploaded`
   (40 x 15 s by default), downloads it, verifies its size and SHA256, and
   cross-checks `checksums.json` and `SHA256SUMS.txt` when the source release
   carries them.
3. A live run creates `relay/<product>/<tag>-<release_id>`, uploads the exact
   bytes and a `relay-manifest.json`, then uploads the local file to MirrorChyan.
4. The published version is confirmed through the public MirrorChyan API before
   the manifest records the upload.
5. The release note is sent for new releases and whenever the source body
   changes. A failed note is retried by the next run.

State lives in the relay Releases and their manifests. No Actions artifacts and
no committed state files are required. See [docs/relay-architecture.md](docs/relay-architecture.md)
for the design and [docs/operations.md](docs/operations.md) for running,
re-running and rolling back.

The package upload calls MirrorChyan's documented version API directly. The
uploading action's bundled uploader puts the raw version in a
`Content-Disposition` header, which fails on Linux runners when the tag is not
Latin-1; every Foundation Sunshine tag ends in a Chinese suffix. The relay sends
an RFC 6266/5987 header instead and keeps TLS verification enabled. The release
note is still sent with `MirrorChyan/release-note-action@v1`.

## Triggers

- `workflow_dispatch`: `product`, optional exact `tag`, and `dry_run` (default
  `true`). With an empty tag the job processes every pending or edited release.
- `schedule`: every 15 minutes. It imports only when the repository variable
  `RELAY_LIVE` is `1`; otherwise it reports the pending work in the run summary.

`workflow_dispatch` with `dry_run=false` uploads. A scheduled run uploads only
after `RELAY_LIVE=1` is set.

## Configuration

- Secret `MirrorChyanUploadToken`: required for live uploads and note syncs.
- Repository variable `RELAY_LIVE`: `1` enables scheduled uploads.
- Optional secret `RELAY_SOURCE_TOKEN`: only needed if a configured source
  repository becomes private. Public sources are read without a token.

The secret and the variable belong to this repository. The source repository
needs neither.

## Local checks

```bash
python -m pip install pyyaml requests
python -m unittest discover -s tests -v
actionlint
python scripts/relay_release.py plan --product foundation-sunshine \
  --tag 'v2026.1031.121458.杂鱼' --dry-run true --event workflow_dispatch
```

The dry run reads the public source release, downloads the installer, and
verifies it without creating a relay Release or contacting MirrorChyan.

## Planned phases

4. Add CNB as a source adapter without changing the GitHub/Mirror contract.
5. Add further products only with an official MirrorChyan resource ID and an
   explicit platform mapping.
