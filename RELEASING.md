# Releasing flpkit

Releases are published by GitHub Actions only. Do not create or store a PyPI
token for this repository.

## One-time PyPI setup

Before the first release, sign in to PyPI as an owner and create a pending
trusted publisher at PyPI's Publishing settings. Enter:

- PyPI project name: `flpkit`
- Owner: `clarkipeng`
- Repository: `flpkit`
- Workflow filename: `publish.yml`
- Environment: `pypi`

For a project that already exists, add the same GitHub trusted publisher from
that project's Publishing settings. The pending publisher creates the project
when the first matching workflow publishes it.

## Release steps

1. Confirm `scripts/check-release.sh` and `uv run pytest -q` pass.
2. Update the version and changelog, then merge the release PR.
3. Create and push an annotated `vX.Y.Z` tag on the merged commit.
4. Watch the `Publish to PyPI` workflow. Its dedicated `pypi` environment
   exchanges GitHub's OIDC identity for PyPI publishing permission and uploads
   the distributions built from that tag.

The workflow triggers only for `v*` tag pushes and has no token or manual
upload fallback. PyPI keeps release versions immutable, so verify the version
and artifacts before pushing the tag.
