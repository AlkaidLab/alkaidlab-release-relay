# Relay operations

## Prerequisites

- Secret `MirrorChyanUploadToken` in this repository. It is the upload token
  supplied by MirrorChyan for resource `foundation-sunshine`. Never print it.
- Repository variable `RELAY_LIVE=1` once the owner approves scheduled uploads.
  While it is missing or `0`, the schedule only reports pending work.
- Optional secret `RELAY_SOURCE_TOKEN`: only for a source repository that is not
  public. `config/products.yml` marks public sources explicitly.

## Run a dry run

Actions -> Relay release import -> Run workflow:

- product: `foundation-sunshine`
- tag: empty, or an exact source tag such as `v2026.1031.121458.杂鱼`
- dry_run: `true`

The job downloads the installer into the runner workspace, verifies its size,
SHA256 and any `checksums.json` / `SHA256SUMS.txt` entry, and prints the result.
It creates no relay Release and sends nothing to MirrorChyan. The run summary
lists every release that a live run would import.

## Run a live import

Use `dry_run=false` for a single tag after verifying the dry run:

1. Confirm the source release and installer are the ones intended for users.
2. Run with that exact tag and `dry_run=false`.
3. The job creates `relay/<product>/<tag>-<release_id>` in this repository,
   uploads the verified file and manifest, uploads to MirrorChyan, confirms the
   version through the public API, and syncs the release note. The package
   upload calls MirrorChyan's documented three-step version API directly so a
   non-ASCII tag can be sent safely; see the note below.

The scheduled job performs the same steps for new releases once
`RELAY_LIVE=1`. It processes entries one at a time in release order.

## Re-run behaviour

A rerun is safe by design:

- A relay Release that already records a confirmed upload is skipped. The
  installer is not downloaded again, and MirrorChyan is not contacted again for
  the package.
- A release whose source notes changed gets only a note sync.
- A note that previously failed is retried by the next scheduled or manual run.
- A release whose tag is not newer than MirrorChyan's current latest in that
  channel is skipped with `skip upload` and is not uploaded. This covers a
  late-published older draft and a backfill attempt.
- A relay Release whose manifest says the upload was `started` or `failed` is
  flagged for manual review. The relay does not upload twice automatically
  because it cannot prove that an interrupted attempt never reached MirrorChyan.

Upload records are kept per channel. A release uploaded to `beta` and later
published as `prerelease=false` is planned once for `stable`: the same verified
bytes are uploaded with `channel=stable`, the stable note is synced, and the
manifest keeps both channel records. Rerunning that release is a no-op. A
release that goes back to `prerelease=true` after a stable upload is a demotion;
the relay logs `demotion ignored` and does not upload it to `beta` again.

## Version ordering

MirrorChyan treats the last successful upload as the latest version, so the
relay never lets a late or backfilled build move users backwards. Before an
upload it reads the public latest version for the target channel:

```text
GET https://mirrorchyan.com/api/resources/foundation-sunshine/latest?os=windows&arch=amd64&channel=stable
```

The comparison uses the project's tag rule `v<year>.<mmdd>.<hhmmss>[.<suffix>]`,
parsed as three integers; the time fields are not zero-padded and the suffix is
ignored. A candidate that is older than or equal to the reported version is
skipped and logged. A tag that does not parse is refused, because the relay
cannot prove the ordering. A response with code `8001` means the channel has no
package yet and the upload is allowed; any other API error fails the run.

## Roll back

There are two layers.

1. Stop automation: set `RELAY_LIVE=0` (or remove it) so the schedule stops
   uploading, and disable the workflow if a longer pause is needed. The source
   repository is unaffected.
2. Reset one relay entry: delete the relay Release only when MirrorChyan must be
   allowed to receive that source release again, for example after confirming
   with MirrorChyan that the version is absent there. Deleting the relay Release
   removes the idempotence record, so the next run imports the source release
   again and uploads it. Do not delete a relay Release whose upload succeeded
   unless a replacement upload was agreed with MirrorChyan.

MirrorChyan has no rollback call in this integration. If a bad package reached
users, ask MirrorChyan to withdraw the version, then publish a new source release
rather than reusing the tag.

## Notes and source edits

- The note is the source release body. A new release syncs it after the package
  upload.
- An edited body triggers a note sync on the next run, with or without a new
  package.
- An empty body is recorded as `empty` and is not sent. MirrorChyan's upload
  action would otherwise fetch and re-send the release, so clearing a note is a
  manual MirrorChyan operation.
- Releases published before the relay's first live run are out of scope, except
  that the first run bootstraps the newest stable and newest pre-release for
  each product. This prevents replaying old versions into MirrorChyan; in
  particular, `v2026.1020` is already served there and is not uploaded again.

## Why the package upload does not use the uploading action

`MirrorChyan/uploading-action@v1` builds
`Content-Disposition: attachment; filename="...<version>..."` from the raw
version name. Python's `http.client` encodes header values as Latin-1, so an
upload with a tag such as `v2026.1031.121458.杂鱼` raises `UnicodeEncodeError`
before any request is sent on a Linux runner. The relay sends the same API
fields with an RFC 6266/5987 `filename*=UTF-8''...` header instead, and it keeps
TLS certificate verification enabled (the action's uploader disables it). The
release-note action is still used because it sends JSON rather than a header.

The plan job still deletes artifacts in this repository at the start of each
run; the note action creates a small `body.json` artifact. Artifacts are not
part of the relay's recovery path; relay Releases and MirrorChyan storage are.

For comparison, dispatch with `use_official_action=true`. That sends the import
job to `macos-latest`, where the reference PR #1197 proved the vendor uploader
works, and uses `MirrorChyan/uploading-action@v1` with `filetype: local` for the
package. The default (`false`) uses the relay client on `ubuntu-latest`. Do not
run both modes for the same release: the per-channel record prevents the relay
client from uploading twice, but the vendor action does not consult it.

## What to tell MirrorChyan

PR #1197 on `AlkaidLab/foundation-sunshine` is replaced by this relay. Do not
merge it; close it instead.

- The resource ID and token are unchanged: `foundation-sunshine`, Windows x64,
  `exec` installer, channel `stable` for a normal release and `beta` for a
  pre-release.
- `MirrorChyanUploadToken` will be configured in `AlkaidLab/alkaidlab-release-relay`, not
  in the source repository.
- The relay uploads the original source tag as the version name, for example
  `v2026.1031.121458.杂鱼`.
- Releases continue to be published on GitHub; `AlkaidLab/foundation-sunshine`
  needs no workflow change and no secret.
- Ask MirrorChyan to confirm that the token is valid for uploads from
  `AlkaidLab/alkaidlab-release-relay` and that `foundation-sunshine` still accepts
  `os=win`, `arch=x64`.
- Tell them that `uploading-action@v1` cannot upload a non-ASCII version name on
  Linux because of the `Content-Disposition` header, so the relay calls the
  version API directly. A fix in the action would let the relay return to it.

Bug report text for MirrorChyan:

```text
Title: uploading-action@v1 fails for non-ASCII version names on Linux

`python/upload.py` sends Content-Disposition with the raw download file name:

  Content-Disposition: attachment; filename="<rid>-<version>-<os>-<arch><ext>"

CPython's http.client encodes header values as Latin-1, so a version such as
v2026.1031.121458.杂鱼 raises UnicodeEncodeError before the object upload. This
reproduces on ubuntu-latest with requests 2.34.2, urllib3 2.8.0 and CPython
3.14.6; the same request succeeds on the macOS runner used by the reference PR.

Encoding the parameter as RFC 6266/5987 (filename="..."; filename*=UTF-8''...)
keeps the header ASCII and works on both runners. The JSON release-note upload
is not affected.
```

## Troubleshooting

- `unknown product`: add an explicit entry to `config/products.yml`; the relay
  never guesses a resource ID.
- `waiting for ... timed out`: the source release published before its installer
  finished uploading, or the asset name no longer matches the rule. Check the
  source release, then rerun.
- checksum mismatch: the release is inconsistent. Do not upload; ask the source
  maintainer to republish the release and its checksum files.
- `skip upload ... already serves`: the candidate is not newer than MirrorChyan's
  latest in that channel. Nothing is wrong; the relay protected users from a
  downgrade. A newer release will upload normally.
- `changed after import`: the source asset was replaced under the same release.
  Review the bytes; if a new upload is intended, get MirrorChyan's agreement,
  delete the relay Release and rerun.
- `MirrorChyan does not report ...`: the upload step failed, or a newer version
  is already live in that channel. Inspect the run and the MirrorChyan dashboard
  before retrying.
