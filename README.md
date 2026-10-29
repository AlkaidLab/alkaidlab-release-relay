# AlkaidLab Release Relay

Private test repository for collecting release assets from the project repositories,
validating them, and later publishing them to MirrorChyan.

The relay is deliberately separate from source repositories. A source release remains
the engineering source of truth; this repository records the exact source release,
asset selection, checksums, and distribution status.

## Current phase

The first phase is a dry-run for Foundation Sunshine. It does not upload to
MirrorChyan and does not change the source repository. It resolves a precise GitHub
Release, checks the expected Windows installer, and writes a machine-readable release
record as a workflow artifact.

Run it from **Actions → Relay release dry-run → Run workflow**. Leave `tag` empty to
resolve the latest non-draft release, or provide an exact source tag.

## Planned phases

1. Validate source releases without side effects.
2. Download and hash the selected assets, then create a namespaced relay Release.
3. Upload local files to MirrorChyan with the source version and explicit channel.
4. Add CNB as a source adapter without changing the GitHub/Mirror contract.

Only products with an official MirrorChyan resource ID and package mapping should be
added to `config/products.yml`.
