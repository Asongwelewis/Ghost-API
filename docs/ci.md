# CI and the GitHub Action

## GitHub Action

```yaml
name: Ghost API
on: pull_request
permissions:
  contents: read
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: Asongwelewis/Ghost-API@dev   # pin a release tag or a commit SHA once one exists
        with:
          fail-on: error
```

On pull requests, the action:

- checks only the files the pull request changes;
- annotates the problem lines in the pull request;
- applies `.ghost/baseline.json` when the file exists;
- caches package indexes, keyed on your lockfiles.

### Inputs

| Input | Default | Meaning |
|---|---|---|
| `path` | `.` | Project root, relative to the repository. |
| `fail-on` | `error` | `error`, `warning` or `never`. |
| `diff-only` | `true` | On pull requests, check only the changed files. |
| `baseline` | (auto) | Baseline file. Default `.ghost/baseline.json` when it exists. |
| `upload-sarif` | `false` | Also upload results to GitHub code scanning. |
| `version` | (this action) | A `ghostapi0` version from PyPI to install instead of the action's own code. |

### Outputs

| Output | Meaning |
|---|---|
| `sarif-file` | Path of the SARIF results file. |
| `exit-code` | `0` passed, `1` problems found at `fail-on` severity. |

### Code scanning

```yaml
permissions:
  contents: read
  security-events: write
steps:
  - uses: actions/checkout@v5
  - uses: Asongwelewis/Ghost-API@dev
    with:
      upload-sarif: "true"
```

## Adopting Ghost in an existing project

Run this once and commit the result:

```sh
ghostapi baseline              # writes .ghost/baseline.json
```

From then on, `ghostapi check --baseline .ghost/baseline.json` (and the action, automatically)
reports only new findings. Baseline entries are keyed by rule, file and target, not line
number, so ordinary edits don't invalidate them. A second copy of an accepted problem in the
same file still counts as new.

## Other CI systems

Ghost is a CLI, so any CI can run it:

```sh
uv tool install ghostapi0
ghostapi check --diff-base origin/main --baseline .ghost/baseline.json --sarif-out ghostapi.sarif
```

Cache the directory in `GHOSTAPI_CACHE_DIR` between runs to avoid re-downloading packages.

## Pre-commit

`ghostapi init` installs `.git/hooks/pre-commit` running `ghostapi check --staged`. If you
already have a pre-commit hook, `init` leaves it alone; add that command to it yourself.
