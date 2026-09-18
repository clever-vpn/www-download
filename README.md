# App Download Publishing Workflow

This repository exists only to collect app artifacts from other repositories' GitHub Releases and publish them into Cloudflare R2.

## Configuration

Edit `config/apps.json` and list each source repository, the destination platform folder, and the asset suffixes to copy.

```json
{
  "apps": [
    {
      "repo": "your-org/clever-vpn-windows",
      "source_release_tag": "v1.2.3",
      "target_dir": "windows",
      "asset_suffixes": [".exe", ".msi"]
    },
    {
      "repo": "your-org/clever-vpn-apple",
      "source_release_tag": "v1.2.3",
      "target_dir": "apple",
      "asset_suffixes": [".dmg", ".pkg"]
    }
  ]
}
```

The workflow input `directory` is the top-level R2 directory to sync, and it is the only version identity this repository manages. Each source repository and its release tag are read from `source_release_tag` in the config file.

Use a channel name such as `stable` or `test` when the content is meant to be updated in place, or a snapshot name such as `v2.1.2` when the content must never change. Downloads bind to a directory through the worker KV key, so serving a new version, rolling back, or separating production from test is only a matter of pointing KV at another directory.

Each workflow run writes files to `R2_BUCKET/<directory>/<target_dir>/...`.

The optional `apps` input restricts a run to specific `target_dir` values, for example `windows`, so upgrading one platform does not download, upload or delete anything for the other platforms. Leave it at `all` to sync every entry in `config/apps.json`.

## Workflow behavior

The manual workflow is defined in `.github/workflows/publish-to-r2.yml`. It runs as a single job and never uploads GitHub Actions artifacts.

- `directory` must be a single path segment made of letters, digits, dot, dash or underscore. It is validated before anything is downloaded.
- `tag` is optional and must already exist. It selects which revision of `config/apps.json` defines the content: the workflow resolves the tag to a commit and reads that file from it. When the input is empty, the `branches/main` HEAD is used. The workflow file and the scripts always come from the revision the run was dispatched on, so any existing tag stays usable.
- Every asset of every selected source release that matches `asset_suffixes` is downloaded from the source repository, and its size is checked against what GitHub reports.
- Each platform is synced with `aws s3 sync --delete`, scoped to `R2_BUCKET/<directory>/<target_dir>/`. Files that disappeared from the source release are removed inside that platform folder only; other platform folders in the same directory are never touched.
- A `manifest.json` describing the source repository, the source release tag, the file sizes and the `sha256` of every file is written into each platform folder, so it is published and replaced together with the content it describes.
- No git tag and no GitHub Release is created. The workflow only needs read access to this repository.

## Serving a directory

The download worker reads the top-level directory name from KV and serves files from it:

- Production worker: KV key `download-version`
- Test worker: KV key `download-test-version`

Pointing either key at another directory switches what that worker serves, which is how a rollback or a test rollout is done. The frontend does not need to change, because the public download URLs are version-free.

## Required GitHub secrets and variables

- GitHub secret: `BW_SM_ACCESS_TOKEN`
- GitHub variable: `BW_CLOUD_REGION` when not using the default Bitwarden US region
- GitHub variable: `R2_ACCOUNT_ID`
- GitHub variable: `R2_BUCKET`
- GitHub variable: `BW_SECRET_ID_R2_ACCESS_KEY_ID`
- GitHub variable: `BW_SECRET_ID_R2_SECRET_ACCESS_KEY`
- GitHub variable: `BW_SECRET_ID_SOURCE_GH_TOKEN`

`BW_SECRET_ID_SOURCE_GH_TOKEN` should point to a Bitwarden secret containing a GitHub token that can read the source repositories when they are private.

## Cloudflare Worker Deployment

The repository also includes a Cloudflare Worker under `worker/` that serves files from the current download version stored in KV.

Behavior:

- Read a configurable KV key from the `clever-vpn-www-version` namespace.
- Fetch the requested object from `www-download/<download-version>/...` in R2.
- If the exact file name does not exist, retry inside the same folder by matching a file name that inserts an arbitrary semantic version after the first `-`, for example `CleverVPN-arm64-v8a.apk` -> `CleverVPN-v2.1.0-arm64-v8a.apk`.

The deployment workflow publishes two Workers from the same source code:

- Production worker: uses KV key `download-version`
- Test worker: uses KV key `download-test-version`

The worker deployment workflow is manual-only and accepts an optional `tag` input.

- If `tag` is provided and already exists, the deployment builds from the commit currently pointed to by that tag.
- If `tag` is provided and does not exist, the deployment builds from the current `main` HEAD.
- If `tag` is omitted, the workflow reads the latest GitHub Release tag, increments the patch version, and uses the resulting `v`-prefixed tag.
- A missing tag is created only in the final publish stage immediately before the GitHub Release is created. If release creation fails, the workflow deletes that newly created tag before exiting.

Required GitHub secret for worker deployment:

- `BW_SM_ACCESS_TOKEN`

Required GitHub variables for worker deployment:

- `BW_SECRET_ID_CF_API_TOKEN`
- `CF_WORKER_NAME`
- `CF_WORKER_TEST_NAME`
- `CF_WORKER_KV_NAMESPACE_ID`
- `R2_BUCKET`

`BW_SECRET_ID_CF_API_TOKEN` should point to a Bitwarden secret containing the Cloudflare API token used for Worker deployment.

`CF_ACCOUNT_ID` is not required. The deployment relies on a Cloudflare API token that can query the target account, so Wrangler can resolve the account without a committed or repository-scoped account ID variable.

The deployment workflow renders `worker/wrangler.toml` at runtime, so those values do not need to be committed into the repository.

## Development Rules

The project copies the repository workflow rules into `.github/copilot-instructions.md` and provides a local Git pre-push hook in `.githooks/pre-push` that blocks pushes to `main`.

Enable the hook locally from the repository root:

```bash
git config core.hooksPath www-download/.githooks
chmod +x www-download/.githooks/pre-push
```