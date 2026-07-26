# VASP Input Studio

VASP Input Studio is a local web workbench for preparing, reviewing, and launching VASP-style materials workflows. It is designed for workstation and small-cluster use where VASP, POTCAR data, VASPKIT, phonopy, and AiiDA are installed separately by the user.

Current release target: `0.0.5`

> **Disclaimer.** VASP Input Studio is an independent, unofficial tool. It is not affiliated with, endorsed by, or supported by the VASP developers or VASP Software GmbH. VASP is a trademark of its respective owner. Users must obtain their own VASP license, binaries, and POTCAR data.

## Status

This repository is suitable as an early public beta. The core application is a FastAPI backend with a single-page HTML/CSS/JavaScript interface. It is intentionally lightweight: there is no npm build step and no bundled VASP distribution.

The app can run in input-generation mode without external scientific executables. Job submission and post-processing features become available only when the relevant tools are configured.

## Features

- Import POSCAR/CIF structures into local projects.
- Generate `INCAR.*`, `KPOINTS.*`, `KPATH.in`, `band.conf`, and related workflow inputs.
- Track workflow state for relax, SCF, DOS, band, elastic, charge, phonon, and convergence stages.
- Preview and edit managed input files with stale/manual-override checks.
- Launch local jobs through a user-provided environment script.
- Optionally submit through AiiDA when AiiDA, profiles, codes, daemon, and POTCAR families are configured.
- Inspect selected result artifacts, including band, DOS, projected DOS, phonon dispersion, and phonon DOS data when available.

## What Is Not Bundled

This project does not include:

- VASP binaries
- VASP license files
- POTCAR archives or PAW datasets
- VASPKIT binaries
- phonopy binaries outside Python dependencies
- AiiDA profiles, databases, daemon state, or RabbitMQ
- user workspaces, calculation outputs, `WAVECAR`, `CHGCAR`, `OUTCAR`, or other run data

Keep licensed VASP/POTCAR content outside the repository.

## Repository Layout

- `app/`: FastAPI backend and workflow logic
- `static/`: browser-side JavaScript and CSS
- `templates/`: HTML template
- `resources/incar_templates/`: bundled neutral INCAR templates
- `resources/scripts/`: bundled helper scripts for local direct submission
- `resources/examples/`: example user configuration files
- `tests/`: unit tests
- `scripts/check_all.sh`: local validation script
- `packaging/build_linux_release.sh`: release archive builder

Runtime state is intentionally outside version control:

- `data/` or `$VASP_STUDIO_WORKSPACE_ROOT`: user projects
- `runtime/` or `$VASP_STUDIO_RUNTIME_DIR`: app runtime state
- `.env.local`: machine-local configuration

## Requirements

- Python `>= 3.10`
- Internet access during install, unless dependencies are pre-cached
- Optional for local calculations: `vasp`, `mpirun`, `vaspkit`, `phonopy`
- Optional for AiiDA workflows: AiiDA, aiida-vasp, a configured profile, daemon, VASP codes, and POTCAR family

## Installation Profiles

VASP Input Studio is intentionally layered. You can install only the parts you need:

- Minimal local UI: install `requirements.txt` only. This supports structure import, input generation, file review, and workflow state tracking. It does not require VASP, MPI, VASPKIT, phonopy, AiiDA, RabbitMQ, or an AiiDA daemon.
- Local direct VASP submission: configure `VASP_STUDIO_VASP_ENV_SCRIPT` to point at a machine-local shell file that exports VASP and MPI commands. VASPKIT, phonopy, Bader, and a local POTCAR archive remain optional helpers for specific post-processing features.
- AiiDA backend: install `requirements-aiida.txt` and set `VASP_STUDIO_ENABLE_AIIDA_BACKEND=1`. This is only needed for AiiDA-backed submissions and convergence workflows. RabbitMQ, the daemon, codes, and POTCAR families are not required for the minimal UI or direct local runner.

By default, `.env.example` keeps AiiDA disabled. Do not enable `VASP_STUDIO_ENABLE_AIIDA_BACKEND` unless you want the UI to check AiiDA profile and daemon health.

## Install From Source

```bash
git clone https://github.com/bin121380/vasp-input-studio.git
cd vasp-input-studio
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Optional AiiDA dependencies:

```bash
.venv/bin/pip install -r requirements-aiida.txt
```

The bundled installer follows the same split. For the minimal local UI, run:

```bash
./install.sh
```

To also install optional AiiDA Python dependencies, run:

```bash
INSTALL_AIIDA=1 ./install.sh
```

Create local configuration (skip if you ran `./install.sh`, which already
creates `.env.local`; `-n` avoids overwriting an existing file):

```bash
cp -n .env.example .env.local
```

Edit `.env.local` for your machine. At minimum, set workspace and runtime paths you want the app to use.

## Run

```bash
./start.sh
```

The default URL is:

```text
http://127.0.0.1:8010
```

You can override host and port:

```bash
VASP_STUDIO_HOST=0.0.0.0 VASP_STUDIO_PORT=8010 ./start.sh
```

> **Security warning.** The application has no authentication and is designed for single-user use on a trusted machine. It can execute user-configured scripts and manage files in the workspace. Keep the default `127.0.0.1` binding unless you fully control the network; never expose the port to an untrusted network or the public internet. If you must reach it from another machine, use an SSH tunnel (`ssh -L 8010:127.0.0.1:8010 user@host`) instead of binding to `0.0.0.0`.

## Optional Local VASP Submission

Local submission requires a shell script that exports the commands used by the runner:

```bash
cp resources/examples/vasp_env.example.sh ./vasp-env.local.sh
```

Edit the copied file and set real commands, then point `.env.local` at it:

```bash
VASP_STUDIO_VASP_ENV_SCRIPT=/absolute/path/to/vasp-env.local.sh
VASP_STUDIO_VASPKIT_CMD=/absolute/path/to/vaspkit
VASP_STUDIO_POTCAR_ARCHIVE_PBE_64=/absolute/path/to/POTCAR_PBE_64.tar.gz
```

The sample `vasp-env.local.sh` should stay local and should not be committed.

## Optional AiiDA Backend

Install optional Python dependencies, either with `INSTALL_AIIDA=1 ./install.sh` or manually:

```bash
.venv/bin/pip install -r requirements-aiida.txt
```

Then configure `.env.local`:

```bash
VASP_STUDIO_ENABLE_AIIDA_BACKEND=1
VASP_STUDIO_AIIDA_PROFILE_NAME=vasp_studio_pg
VASP_STUDIO_AIIDA_CODE_STD_LABEL=vasp_std_localhost
VASP_STUDIO_AIIDA_CODE_GAM_LABEL=vasp_gam_localhost
VASP_STUDIO_AIIDA_POTCAR_FAMILY_NAME=PBE_64
```

The installer does not create an AiiDA profile, start a daemon, register codes, or register POTCAR families. See [INSTALLATION_TROUBLESHOOTING.md](INSTALLATION_TROUBLESHOOTING.md) for the required post-install checks.

## Check Before Committing

```bash
./scripts/check_all.sh
```

This runs:

- Python bytecode compilation for `app/`
- JavaScript syntax check for `static/app.js`
- the unit test suite in `tests/`

The JavaScript syntax check requires a Node.js runtime on `PATH` (or set
`NODE=/path/to/node`); the app itself does not need Node.js.

## Build A Release Bundle

```bash
./packaging/build_linux_release.sh
```

The archive is written to `dist/` and intentionally excludes virtual environments, local `.env.local`, workspace data, runtime state, tests, and generated calculation outputs.

## Publishing To GitHub

Before publishing, review [docs/GITHUB_RELEASE_CHECKLIST.md](docs/GITHUB_RELEASE_CHECKLIST.md).

Important:

- Do not commit `.env.local`, virtual environments, release archives, POTCAR files, VASP outputs, or local launcher scripts.
- Configure `git config user.name` and `user.email` before the first commit; a GitHub noreply address avoids publishing a personal email.

## License

This project is licensed under the [MIT License](LICENSE).
