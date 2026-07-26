# Installation Troubleshooting

This file records the most common "install succeeded, but the runtime integration is still incomplete" states for VASP Input Studio.

## What `./install.sh` Actually Does

The installer:

- creates `.venv`
- installs `requirements.txt`
- optionally installs `requirements-aiida.txt` when `INSTALL_AIIDA=1`
- creates a default `.env.local`
- creates workspace and runtime directories

The installer does **not** automatically:

- configure a local VASP launcher script
- point the app at a local POTCAR archive
- enable the AiiDA backend
- create or migrate an AiiDA profile
- start the AiiDA daemon
- register AiiDA VASP codes
- register an AiiDA POTCAR family
- verify that `vasp`, `mpirun`, `vaspkit`, or `phonopy` are callable on the target machine

## Common Post-Install Gaps

### 1. POTCAR Mapping shows only plain element names

Typical symptom:

- the POTCAR Mapping dropdown shows only `K`, `Ca`, `Co`, `H`, etc.
- variants such as `K_sv`, `Ca_sv`, `Sr_sv`, `Rh_pv` do not appear

Cause:

- `VASP_STUDIO_POTCAR_ARCHIVE_PBE_64` is unset, commented out, or points to a missing archive

Why:

- without a readable POTCAR archive, the app cannot enumerate available PAW labels and falls back to the raw species names only

Required fix:

- set `VASP_STUDIO_POTCAR_ARCHIVE_PBE_64=/absolute/path/to/POTCAR_PBE_64.tar.gz`
- restart the UI

### 2. Local VASP submission is unavailable

Typical symptom:

- the UI can generate inputs but cannot launch local VASP jobs
- backend status reports that the local VASP launcher is not configured

Cause:

- `VASP_STUDIO_VASP_ENV_SCRIPT` is unset or points to a missing file

Required fix:

- create a shell file based on `resources/examples/vasp_env.example.sh`
- set at least:
  - `MPI_CMD`
  - `VASP_CMD`
- optionally also set:
  - `VASP_GAM_CMD`
  - `VASPKIT_CMD`
  - `PHONOPY_CMD`
  - `PYTHON_CMD`
- point `.env.local` to that file with:
  - `VASP_STUDIO_VASP_ENV_SCRIPT=/absolute/path/to/vasp-env.sh`

### 3. AiiDA is not available after install

Typical symptom:

- the backend panel says AiiDA is disabled or unavailable
- converge submission is blocked
- AiiDA-related previews or submit actions fail

Causes:

- `requirements-aiida.txt` was not installed
- `VASP_STUDIO_ENABLE_AIIDA_BACKEND=1` was not set
- the configured AiiDA profile does not exist
- the profile was created with `verdi presto` on a machine without RabbitMQ, so it has no broker
- the AiiDA daemon is not running
- no AiiDA VASP codes are registered
- no AiiDA POTCAR family is available

Required fix sequence:

1. reinstall or extend the install with `INSTALL_AIIDA=1 ./install.sh`
2. set `VASP_STUDIO_ENABLE_AIIDA_BACKEND=1` in `.env.local`
3. ensure the target AiiDA profile exists
4. start the AiiDA daemon for that profile
5. register the required VASP codes
6. register or expose an AiiDA POTCAR family

Important:

- installing the optional AiiDA Python dependencies is only the first step
- it does not by itself bring up the profile, broker, daemon, codes, or POTCAR family
- `verdi presto` is a valid quick path for a local profile, but on a machine without RabbitMQ it will create a brokerless profile
- a brokerless profile can still be loaded by the UI and can expose codes and POTCAR families, but daemon-backed `submit()` workflows remain unavailable until RabbitMQ is installed and configured

### 4. VASPKIT / phonon helpers are missing

Typical symptom:

- auto-generated K-path helpers fall back instead of using VASPKIT
- PDOS or phonon helper actions are unavailable

Cause:

- `VASP_STUDIO_VASPKIT_CMD` or helper commands inside the VASP env script are unset or invalid

Required fix:

- point `VASP_STUDIO_VASPKIT_CMD` to a real `vaspkit` binary, or export `VASPKIT_CMD` from the VASP env script
- set `PHONOPY_CMD` in the VASP env script when phonopy-based helpers are needed

### 5. RabbitMQ is installed locally but still will not start

Typical symptom:

- `systemctl --user status rabbitmq-local.service` shows repeated `status=127`
- `rabbitmq-server` fails before opening port `5672`
- AiiDA profile configuration works only in brokerless mode

Possible cause on Linux workstations:

- a prebuilt Erlang/RabbitMQ package may expect a newer OpenSSL runtime than the operating system provides
- the Erlang `crypto` NIF can fail to load when the OpenSSL runtime is incompatible
- RabbitMQ can then abort before opening port `5672`

Possible fix:

- install an Erlang/RabbitMQ combination that is supported by your operating system
- or install a local OpenSSL runtime that matches the Erlang build
- prepend that runtime's library directory to `LD_LIBRARY_PATH`
- ensure `ERL_ROOTDIR` points at the Erlang tree used by RabbitMQ
- inject required variables into the service manager environment, not only into `rabbitmq-env.conf`

Files to check:

- the user or system service file that starts RabbitMQ
- `rabbitmq-env.conf`
- `rabbitmq.conf`

Verification:

- `systemctl --user status rabbitmq-local.service`
- `ss -ltnp | rg '(:5672|:15672|:25672)'`
- `curl -u <user>:<password> http://127.0.0.1:15672/api/overview`

### 6. AiiDA still says the profile has no broker

Typical symptom:

- `verdi profile show` lists `core.rabbitmq`
- `verdi -p <profile> status` says the daemon is running
- but the UI still reports `profile does not define a broker`

Cause:

- the web UI process was started before RabbitMQ and the profile were updated
- the running `uvicorn` process is still serving stale AiiDA state

Required fix:

- restart the UI process after RabbitMQ and `verdi profile configure-rabbitmq`
- if you manage the UI with systemd, restart the relevant `vasp-input-studio` service
- then verify:
  - `systemctl --user status vasp-input-studio.service`
  - `curl http://127.0.0.1:8010/api/backend/status`

Note:

- do not try to treat `verdi daemon start-circus` like a normal long-running foreground service unless you also handle the circus forking model correctly
- a stable local setup is often to keep RabbitMQ and the UI under the service manager, but start the AiiDA daemon itself with `verdi -p <profile> daemon start`

### 7. AiiDA daemon reports a stale PID file

Typical symptom:

- the UI reports `AiiDA daemon fail`
- `verdi -p <profile> status` says the daemon could not be reached because of a stale PID file
- RabbitMQ is running and the AiiDA profile can still be loaded

Cause:

- the old circus daemon process exited or the machine restarted, but its PID file remained in the AiiDA daemon directory

Required fix:

```bash
source .venv/bin/activate
verdi -p <profile> daemon stop
verdi -p <profile> daemon start
verdi -p <profile> daemon status
```

If you are starting it from a non-interactive shell without activating the virtual environment, put the project virtual environment on `PATH` explicitly:

```bash
env PATH="$PWD/.venv/bin:$PATH" .venv/bin/verdi -p <profile> daemon start
```

Note:

- `daemon stop` is safe for this stale-PID case; it removes the obsolete daemon PID file when the process no longer exists
- do not kill unrelated `vasp_std`, `vasp_gam`, or scheduler-launched VASP processes while fixing the daemon
- after the daemon is running, refresh the UI or recheck `curl http://127.0.0.1:8010/api/backend/status`

### 8. RabbitMQ works, but AiiDA warns about the version

Typical symptom:

- `verdi -p <profile> status` prints a warning that RabbitMQ `4.0.6` is unsupported

Meaning:

- the broker is reachable and the daemon can run
- however, AiiDA `2.7.3` does not consider RabbitMQ `4.x` a supported production target

Practical guidance:

- treat the current setup as functional but higher-risk
- if you see duplicated submissions or unstable long-running workflows, replace the broker with a RabbitMQ version that AiiDA explicitly supports
- keep `consumer_timeout` large enough for long VASP workflows

## Minimum `.env.local` Example

```bash
VASP_STUDIO_WORKSPACE_ROOT=/path/to/workspace
VASP_STUDIO_RUNTIME_DIR=/path/to/runtime

VASP_STUDIO_ENABLE_AIIDA_BACKEND=0
```

For direct local VASP submission, also set:

```bash
VASP_STUDIO_VASP_ENV_SCRIPT=/path/to/vasp-env.sh
VASP_STUDIO_VASPKIT_CMD=/path/to/vaspkit
VASP_STUDIO_POTCAR_ARCHIVE_PBE_64=/path/to/POTCAR_PBE_64.tar.gz
```

For optional AiiDA-backed workflows, install `requirements-aiida.txt`, then set:

```bash
VASP_STUDIO_ENABLE_AIIDA_BACKEND=1
VASP_STUDIO_AIIDA_PROFILE_NAME=vasp_studio_pg
```

## Recommended Install Acceptance Check

After installation, verify all of the following explicitly:

- `.venv` exists and `./start.sh` works
- `.env.local` contains real paths instead of placeholder comments
- POTCAR Mapping exposes the expected `_sv` / `_pv` options
- backend status shows local submission available if you intend to run local VASP
- backend status shows AiiDA available only if the profile, daemon, codes, and POTCAR family are actually ready

If any of these are missing, the install is only partially complete from a production-workflow perspective.
