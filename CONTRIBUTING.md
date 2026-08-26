# Contributing to VASP Input Studio

Thank you for helping improve VASP Input Studio.

## Before you start

- Search existing issues and pull requests to avoid duplicate work.
- For a substantial behavior change, open an issue first to discuss the design.
- Keep VASP binaries, license files, POTCAR archives, calculation outputs, credentials, and machine-local configuration out of the repository.
- Use a GitHub noreply address if you do not want your personal email exposed in commit metadata.

## Development setup

```bash
git clone https://github.com/bin121380/vasp-input-studio.git
cd vasp-input-studio
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Optional AiiDA support can be installed with:

```bash
.venv/bin/pip install -r requirements-aiida.txt
```

Copy the example configuration for local use, but do not commit the resulting file:

```bash
cp -n .env.example .env.local
```

## Making changes

1. Create a focused branch from `main`.
2. Keep each commit limited to one logical change.
3. Add or update tests for behavior changes and bug fixes.
4. Update the documentation when user-visible behavior changes.
5. Avoid generated outputs, large calculation artifacts, and licensed scientific data.

## Validation

Run the complete local check before opening a pull request:

```bash
./scripts/check_all.sh
```

This compiles the Python sources, checks the browser JavaScript syntax, and runs the unit tests. The JavaScript check requires Node.js on `PATH`.

## Pull requests

A pull request should:

- explain the problem and the chosen solution;
- identify user-visible or compatibility effects;
- describe how the change was tested;
- link related issues;
- keep unrelated formatting or refactoring out of the diff;
- pass the GitHub Actions matrix for Python 3.10, 3.11, and 3.12.

By submitting a contribution, you agree that it may be distributed under the repository's MIT License.
