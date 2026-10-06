# CI/CD for raggae

Status: implemented and locally verified on `ci-cd`. The user reports completing
the GitHub settings except selecting required CI checks, which needs the first PR
run. PyPI Trusted Publishing must also be configured before a future release.

Changes follow: branch → PR → CI → squash merge into `master` → GitHub Release → PyPI.
Merging a PR does not publish a package.

## What the workflows do

- `ci.yml`: Python 3.13 tests on Linux and macOS; builds the source archive and
  wheel; checks metadata, private-file exclusion, and package contents; installs
  the wheel in a fresh environment and validates a text document.
- `publish.yml`: starts when a stable GitHub Release is published. It rejects
  tags whose commits are not on `origin/master`, or whose version differs from
  `pyproject.toml`. It reruns CI on that exact commit, publishes the same checked
  archives, then verifies their hashes and installation from PyPI.
- Only the publishing job receives permission to request a temporary PyPI
  credential. It does not check out source, run tests, or build packages.
- GitHub Actions are pinned to commit hashes. uv is pinned to `0.5.11`, matching
  the current local tooling; `uv.lock` is tracked and checked with `--locked`.
  The build backend is constrained to hatchling `1.27.0`; metadata checks use
  twine `6.2.0`. Updates to these pins must go through a PR.
- CI currently covers Python 3.13 and the core package, not every optional extra
  or every Python version allowed by `requires-python`.

## One-time GitHub settings — required before releasing

Workflow files cannot enforce PR-only updates or configure your account settings.
In `okanyenigun/raggae`:

1. Protect `master`: require a PR, require branches to be up to date, and require
   these checks after the first CI run:
   - `Tests (ubuntu-latest, Python 3.13)`
   - `Tests (macos-latest, Python 3.13)`
   - `Package checks`
2. Block force pushes and deletion; enforce the rules for administrators too,
   with no bypass. Leave required approving reviews at zero if working alone.
3. Enable squash merging. Disable merge commits and rebase merging if every PR
   should become exactly one commit on `master`.
4. Create the `pypi` Actions environment. Require your approval before deployment;
   allow self-approval if you work alone, but disable administrator bypass.
   Select allowed **tags** matching `v*`; do not allow arbitrary branches.
   GitHub Release runs use a tag ref, so a branch-only `master` environment rule
   would block legitimate releases. The workflow's ancestry guard separately
   verifies that the tagged code was merged into `master`.
5. Protect release tags (`v*`) against updates and deletion, and limit their
   creation to the release maintainer. PR jobs must never receive publishing
   credentials or permission to mint them.

## One-time PyPI settings

On the existing `raggae` project's Publishing settings, add a GitHub Trusted
Publisher with:

| Field | Value |
|---|---|
| Owner | `okanyenigun` |
| Repository | `raggae` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

No `PYPI_TOKEN` GitHub secret is needed. Keep `.env` local and ignored.

## Releasing a new version

1. Create a branch, update `pyproject.toml` (for example to `0.1.1`), and run
   `uv lock` so the lockfile includes the new version. Open a PR and merge it
   after CI passes. `0.1.0` is already published and cannot be overwritten.
2. Create a GitHub Release with a new tag such as `v0.1.1`, selecting `master`
   as the target. Publish the release; drafts and prereleases are not published
   by this workflow.
3. Wait for the ancestry/version guard and release CI to pass, then approve the
   `pypi` deployment. Wait for the public-release verification job too.
4. If verification fails after upload, the package may already be public. Inspect
   PyPI before retrying; never change built files under an existing version.

The workflow does not bump versions, create tags, merge PRs, or publish on ordinary
pushes. Those decisions remain yours.

## Implementation status

- Workflow files and release-check helpers are implemented and locally verified
  on `ci-cd`. The user approved committing and pushing this branch after adding
  `docs/assets/logo.png` to the README. This does not authorize merging into
  `master`, creating release tags, or publishing another package version.
- First local run: `.venv/bin/python -m pytest -x` collected 2,807 cases and
  stopped with **17 passed, 1 failed, 2,789 not run**. The failure is in
  `tests/ci/test_release_checks.py::test_name_or_version_mismatch_is_rejected[raggae-v0.1.2]`:
  its test repository already has the same metadata, so Git refuses the attempted
  unchanged commit. The release guard and assertion are not reached in that case.
  No tests were rerun or production/helper fixes made before the user's decision.
- The user approved correcting that fixture and explicitly prohibited committing
  or pushing without another approval. Added `--allow-empty` to that test commit;
  the version-mismatch assertion and release helper are unchanged. Full rerun:
  `.venv/bin/python -m pytest -x` → **2,807 passed**, no warnings or skips.
  This includes all 51 CI helper cases and all 2,756 SDK tests.
- `uv lock --check --offline --no-cache` passed with the existing lockfile.
- Both workflows passed actionlint `1.7.12` (provided by
  `actionlint-py==1.7.12.25`); external ShellCheck integration was disabled.
- The CI-pinned hatchling build succeeded in a fresh temporary directory, but
  the next verification step stopped: uv automatically adds `dist/.gitignore`
  containing `*`, while `read_distributions` currently permits only the two
  archives. This is an overly strict release-directory check, not an SDK failure.
  No helper fix or rerun followed this failure before the user's approval.
- The user approved allowing the directory-local `.gitignore` marker. The helper
  ignores only that filename when checking the two expected archives. Full
  existing-suite rerun: **2,807 passed**. Added four regressions for the allowed
  marker and continued rejection of `.env`, `.gitignore.old`, and ordinary extra
  files. Full suite with regressions: **2,811 passed**, no warnings or skips;
  this includes all 55 release-helper cases and all 2,756 SDK tests.
- The actual uv-built wheel/source archive pass private-content, metadata,
  module-content, and release-directory checks. Twine `6.2.0` passes both archives
  with non-failing warnings: the existing empty `README.md` supplies no long
  description or its content type. The README and SDK implementation are unchanged.
- Installing the built wheel with dependencies into a fresh Python 3.13
  environment passes isolated imports and ordinary-text document validation.
- Reproduced `uv sync --locked --dev` with the existing `uv.lock` in another
  temporary environment, preserving the user's `.venv`. Its full pytest run
  also passes **2,811 tests**, no warnings or skips.
- The public-release checker passes against the already-published `0.1.0`
  archives' original hashes; isolated smoke testing of its clean PyPI install
  passes too. This was read-only: no new release or upload was made.
- GitHub-hosted Linux/macOS runs, branch protections, the `pypi` environment,
  tag rules, and the PyPI Trusted Publisher require account-side setup. They are
  not enabled merely by adding these files.
- The user reports completing GitHub protection, merge, environment, and release
  tag settings. Selecting the three required CI checks still needs the first PR
  run. These account settings have not been independently inspected.
- Added the supplied logo, a short package description, and installation command
  to the previously empty README before the approved branch commit/push.
- Verification after that README change: **2,811 tests passed**; fresh wheel and
  source archives pass the distribution guard and `twine check --strict`, with
  no metadata warnings. The earlier empty-README warnings are now resolved.

References: [uv on GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/),
[PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/adding-a-publisher/),
[GitHub branch protections](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches),
[deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).
