#!/usr/bin/env bash
set -euo pipefail

RUN_STEP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$RUN_STEP_DIR/.." && pwd)"

if [[ -n "${VASP_ENV_SH:-}" ]]; then
  ENV_SH="$VASP_ENV_SH"
elif [[ -f "$ROOT/../vasp/env.sh" ]]; then
  ENV_SH="$ROOT/../vasp/env.sh"
  echo "Note: VASP_ENV_SH is unset; sourcing fallback environment script $ENV_SH" >&2
elif [[ -f "$HOME/vasp/env.sh" ]]; then
  ENV_SH="$HOME/vasp/env.sh"
  echo "Note: VASP_ENV_SH is unset; sourcing fallback environment script $ENV_SH" >&2
else
  echo "Missing VASP environment script. Set VASP_ENV_SH to a file that exports VASP_CMD and related runtime commands." >&2
  exit 1
fi

source "$ENV_SH"

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MPI_CMD="${MPI_CMD:-mpirun}"
export VASP_MPI_NP="${VASP_MPI_NP:-1}"
export VASP_CMD="${VASP_CMD:-vasp_std}"
export VASP_GAM_CMD="${VASP_GAM_CMD:-$VASP_CMD}"
export VASPKIT_CMD="${VASPKIT_CMD:-vaspkit}"
export PHONOPY_CMD="${PHONOPY_CMD:-phonopy}"
export PYTHON_CMD="${PYTHON_CMD:-python3}"

if ! ulimit -s unlimited 2>/dev/null; then
  echo "Warning: could not set stack size to unlimited; large VASP jobs may crash on startup." >&2
fi

require_cmd() {
  if [[ ! -x "$1" ]] && ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    return 1
  fi
}

if ! declare -F run_vasp >/dev/null 2>&1; then
  run_vasp() {
    require_cmd "$MPI_CMD"
    require_cmd "$VASP_CMD"
    "$MPI_CMD" -np "$VASP_MPI_NP" "$VASP_CMD" "$@"
  }
fi

if ! declare -F run_vasp_gamma >/dev/null 2>&1; then
  run_vasp_gamma() {
    require_cmd "$MPI_CMD"
    require_cmd "$VASP_GAM_CMD"
    "$MPI_CMD" -np "$VASP_MPI_NP" "$VASP_GAM_CMD" "$@"
  }
fi

usage() {
  cat <<'EOF'
Usage:
  run_step.sh <target-dir> <step>

Examples:
  run_step.sh systems/MgO relax
  run_step.sh systems/MgO scf
  run_step.sh systems/MgO dos
  run_step.sh systems/MgO band
  run_step.sh systems/MgO elastic
  run_step.sh systems/MgO charge
  run_step.sh systems/MgO phonon
  run_step.sh atoms/Mg atom
EOF
}

[[ $# -eq 2 ]] || { usage >&2; exit 1; }

TARGET_INPUT="$1"
STEP="$2"

if [[ "$TARGET_INPUT" = /* ]]; then
  TARGET="$TARGET_INPUT"
else
  TARGET="$ROOT/$TARGET_INPUT"
fi

[[ -d "$TARGET" ]] || { echo "Missing target directory: $TARGET" >&2; exit 1; }

now_iso() {
  date --iso-8601=seconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%:z'
}

json_escape() {
  local value="${1:-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "$value"
}

JOB_STARTED_AT="$(now_iso)"
JOB_STATE_FILE=""

write_job_state_file() {
  local status="$1"
  local exit_code_raw="${2:-}"
  local finished_at="${3:-}"
  [[ -n "$JOB_STATE_FILE" ]] || return 0

  local work_dir
  work_dir="$(dirname "$JOB_STATE_FILE")"
  local exit_code_json="null"
  local signal_json="null"
  local finished_at_json="null"

  if [[ -n "$exit_code_raw" ]]; then
    exit_code_json="$exit_code_raw"
    if [[ "$exit_code_raw" =~ ^[0-9]+$ ]] && (( exit_code_raw > 128 )); then
      signal_json="$((exit_code_raw - 128))"
    fi
  fi
  if [[ -n "$finished_at" ]]; then
    finished_at_json="\"$(json_escape "$finished_at")\""
  fi

  cat > "$JOB_STATE_FILE" <<EOF
{
  "status": "$(json_escape "$status")",
  "step": "$(json_escape "$STEP")",
  "target": "$(json_escape "$TARGET")",
  "work_dir": "$(json_escape "$work_dir")",
  "started_at": "$(json_escape "$JOB_STARTED_AT")",
  "finished_at": $finished_at_json,
  "exit_code": $exit_code_json,
  "signal": $signal_json,
  "pid": $$
}
EOF
}

record_job_start() {
  local work_dir="$1"
  JOB_STATE_FILE="$work_dir/exit_code.json"
  write_job_state_file "running" "" ""
}

record_job_exit() {
  local exit_code="$1"
  local status="finished"
  if [[ "$exit_code" != "0" ]]; then
    status="failed"
  fi
  write_job_state_file "$status" "$exit_code" "$(now_iso)" || true
}

trap 'exit_code=$?; trap - EXIT; record_job_exit "$exit_code"; exit "$exit_code"' EXIT

copy_if_exists() {
  local src="$1"
  local dest="$2"
  [[ -f "$src" ]] && cp "$src" "$dest"
}

incar_get() {
  local file="$1"
  local key="$2"
  awk -F= -v key="$key" '
    BEGIN { IGNORECASE = 1 }
    $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      value = $2
      sub(/^[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      print value
      exit
    }
  ' "$file"
}

incar_upsert() {
  local file="$1"
  local key="$2"
  local value="$3"
  if grep -qiE "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
    sed -i -E "s|^[[:space:]]*(${key})[[:space:]]*=.*$|${key} = ${value}|I" "$file"
  else
    printf '%s = %s\n' "$key" "$value" >> "$file"
  fi
}

structure_input_for_step() {
  local step_name="${1:-}"
  if [[ "$step_name" != "relax" && -f "$TARGET/runs/relax/PRIMCELL.vasp" && -s "$TARGET/runs/relax/PRIMCELL.vasp" ]]; then
    printf '%s\n' "$TARGET/runs/relax/PRIMCELL.vasp"
  elif [[ -f "$TARGET/runs/relax/CONTCAR" && -s "$TARGET/runs/relax/CONTCAR" ]]; then
    printf '%s\n' "$TARGET/runs/relax/CONTCAR"
  else
    printf '%s\n' "$TARGET/POSCAR"
  fi
}

mesh_kpoints_input_for_step() {
  local step_name="${1:-}"
  if [[ "$step_name" = "relax" && -f "$TARGET/KPOINTS.relax" && -s "$TARGET/KPOINTS.relax" ]]; then
    printf '%s\n' "$TARGET/KPOINTS.relax"
  elif [[ "$step_name" != "relax" && -f "$TARGET/KPOINTS.scf" && -s "$TARGET/KPOINTS.scf" ]]; then
    printf '%s\n' "$TARGET/KPOINTS.scf"
  elif [[ "$step_name" != "relax" && -f "$TARGET/KPOINTS.downstream" && -s "$TARGET/KPOINTS.downstream" ]]; then
    printf '%s\n' "$TARGET/KPOINTS.downstream"
  else
    printf '%s\n' "$TARGET/KPOINTS.scf"
  fi
}

step_work_dir() {
  local step_name="${1:-}"
  if [[ -n "${VASP_WORK_DIR:-}" ]]; then
    printf '%s\n' "$VASP_WORK_DIR"
  else
    printf '%s\n' "$TARGET/runs/$step_name"
  fi
}

prepare_run_dir() {
  local work_dir="$1"
  local allow_resume="${2:-0}"
  if [[ "$allow_resume" = "1" && "${VASP_RESUME:-0}" = "1" && -d "$work_dir" ]]; then
    mkdir -p "$work_dir"
    return
  fi
  rm -rf "$work_dir"
  mkdir -p "$work_dir"
}

run_and_log() {
  local work_dir="$1"
  local append_log="${2:-0}"
  (
    cd "$work_dir"
    if [[ "$append_log" = "1" && "${VASP_RESUME:-0}" = "1" ]]; then
      run_vasp >> log 2>&1
    else
      run_vasp > log 2>&1
    fi
  )
}

outcar_completed() {
  local outcar="$1"
  [[ -f "$outcar" && -s "$outcar" ]] || return 1
  tail -c 65536 "$outcar" | grep -qi "General timing and accounting informations for this job"
}

require_completed_scf_baseline() {
  local scf_dir="$1"
  local require_charge="${2:-1}"
  local require_wavecar="${3:-0}"

  outcar_completed "$scf_dir/OUTCAR" || { echo "Run scf first: $scf_dir/OUTCAR does not show a completed SCF baseline" >&2; exit 1; }
  if [[ "$require_charge" = "1" ]]; then
    [[ -f "$scf_dir/CHGCAR" ]] || { echo "Run scf first: missing $scf_dir/CHGCAR" >&2; exit 1; }
  fi
  if [[ "$require_wavecar" = "1" ]]; then
    [[ -f "$scf_dir/WAVECAR" ]] || { echo "Run scf first: missing $scf_dir/WAVECAR" >&2; exit 1; }
  fi
}

resume_copy_poscar() {
  local work_dir="$1"
  local fresh_poscar="$2"
  if [[ "${VASP_RESUME:-0}" = "1" && -f "$work_dir/CONTCAR" && -s "$work_dir/CONTCAR" ]]; then
    cp "$work_dir/CONTCAR" "$work_dir/POSCAR"
  else
    cp "$fresh_poscar" "$work_dir/POSCAR"
  fi
}

configure_resume_incar() {
  local work_dir="$1"
  [[ "${VASP_RESUME:-0}" = "1" ]] || return 0

  if [[ -f "$work_dir/WAVECAR" && -s "$work_dir/WAVECAR" ]]; then
    incar_upsert "$work_dir/INCAR" "ISTART" "1"
  else
    incar_upsert "$work_dir/INCAR" "ISTART" "0"
  fi

  if [[ -f "$work_dir/CHGCAR" && -s "$work_dir/CHGCAR" ]]; then
    incar_upsert "$work_dir/INCAR" "ICHARG" "1"
  else
    incar_upsert "$work_dir/INCAR" "ICHARG" "2"
  fi
}

phonopy_dim_from_band_conf() {
  local conf_path="$1"
  local dim
  if [[ -f "$conf_path" ]]; then
    dim="$(awk -F= '/^[[:space:]]*DIM[[:space:]]*=/{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2; exit}' "$conf_path")"
  fi
  if [[ -n "${dim:-}" ]]; then
    printf '%s\n' "$dim"
  else
    printf '2 2 2\n'
  fi
}

kpoints_mesh_triplet() {
  local path="$1"
  [[ -f "$path" ]] || return 1
  awk '
    NF {
      count += 1
      if (count == 4) {
        if (NF >= 3) {
          printf "%s %s %s\n", $1, $2, $3
          found = 1
        }
        exit
      }
    }
    END { exit(found ? 0 : 1) }
  ' "$path"
}

metadata_triplet() {
  local key="$1"
  local metadata_path="$TARGET/metadata.json"
  [[ -f "$metadata_path" ]] || return 1
  "$PYTHON_CMD" - "$metadata_path" "$key" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)

value = payload.get(key)
if not isinstance(value, list) or len(value) < 3:
    raise SystemExit(1)

try:
    mesh = [max(1, int(item)) for item in value[:3]]
except Exception:
    raise SystemExit(1)

print(f"{mesh[0]} {mesh[1]} {mesh[2]}")
PY
}

band_conf_requests_nac() {
  local conf_path="$1"
  [[ -f "$conf_path" ]] || return 1
  awk -F= '
    BEGIN { IGNORECASE = 1 }
    /^[[:space:]]*NAC[[:space:]]*=/ {
      value = $2
      gsub(/[[:space:]]/, "", value)
      value = toupper(value)
      if (value == ".TRUE." || value == "TRUE") {
        found = 1
      }
      exit
    }
    END { exit(found ? 0 : 1) }
  ' "$conf_path"
}

band_conf_requests_force_constants() {
  local conf_path="$1"
  [[ -f "$conf_path" ]] || return 1
  awk -F= '
    BEGIN { IGNORECASE = 1 }
    /^[[:space:]]*FORCE_CONSTANTS[[:space:]]*=/ {
      value = $2
      gsub(/[[:space:]]/, "", value)
      value = toupper(value)
      if (value == ".TRUE." || value == "TRUE" || value == "READ") {
        found = 1
      }
      exit
    }
    END { exit(found ? 0 : 1) }
  ' "$conf_path"
}

copy_first_existing() {
  local dest="$1"
  shift
  local candidate
  for candidate in "$@"; do
    if [[ -f "$candidate" ]]; then
      cp "$candidate" "$dest"
      return 0
    fi
  done
  return 1
}

prepare_phonon_inputs() {
  local work_dir="$1"
  local report="$work_dir/phonon_input_check.log"
  : > "$report"

  incar_upsert "$work_dir/INCAR" "IBRION" "-1"
  incar_upsert "$work_dir/INCAR" "NSW" "0"
  incar_upsert "$work_dir/INCAR" "LWAVE" ".FALSE."
  incar_upsert "$work_dir/INCAR" "LCHARG" ".FALSE."

  {
    echo "Forced phonon tags for finite-displacement force calculations:"
    echo "  IBRION = -1"
    echo "  NSW = 0"
    echo "  LWAVE = .FALSE."
    echo "  LCHARG = .FALSE."
  } >> "$report"

  local lreal_value
  lreal_value="$(incar_get "$work_dir/INCAR" "LREAL" || true)"
  if [[ -z "$lreal_value" ]]; then
    echo "WARNING: LREAL is not set. For paper-grade phonon forces, LREAL = .FALSE. is recommended." >> "$report"
  elif [[ "${lreal_value^^}" != ".FALSE." ]]; then
    echo "WARNING: LREAL = $lreal_value. This is memory-friendly but can degrade phonon force accuracy." >> "$report"
  else
    echo "LREAL = .FALSE. matches the recommended phonon force setting." >> "$report"
  fi

  if band_conf_requests_nac "$work_dir/band.conf"; then
    local born_dest="$work_dir/BORN"
    if copy_first_existing \
      "$born_dest" \
      "$TARGET/BORN" \
      "$TARGET/runs/phonon/BORN" \
      "$TARGET/runs/charge/BORN"
    then
      echo "NAC = .TRUE. detected; copied BORN file to phonon workspace." >> "$report"
    else
      echo "ERROR: NAC = .TRUE. but no BORN file was found in the system workspace." >> "$report"
      return 1
    fi
  else
    echo "NAC is not enabled in band.conf." >> "$report"
  fi
}

case "$STEP" in
  relax)
    WORK="$(step_work_dir relax)"
    prepare_run_dir "$WORK" 1
    record_job_start "$WORK"
    resume_copy_poscar "$WORK" "$TARGET/POSCAR"
    cp "$TARGET/POTCAR" "$WORK/POTCAR"
    cp "$(mesh_kpoints_input_for_step relax)" "$WORK/KPOINTS"
    cp "$TARGET/INCAR.relax" "$WORK/INCAR"
    configure_resume_incar "$WORK"
    run_and_log "$WORK" 1
    ;;

  scf)
    WORK="$(step_work_dir scf)"
    prepare_run_dir "$WORK" 1
    record_job_start "$WORK"
    resume_copy_poscar "$WORK" "$(structure_input_for_step scf)"
    cp "$TARGET/POTCAR" "$WORK/POTCAR"
    cp "$(mesh_kpoints_input_for_step scf)" "$WORK/KPOINTS"
    cp "$TARGET/INCAR.scf" "$WORK/INCAR"
    configure_resume_incar "$WORK"
    run_and_log "$WORK" 1
    (
      cd "$WORK"
      printf '11\n113\n' | "$VASPKIT_CMD" > vaspkit_pdos.log 2>&1 || true
    )
    ;;

  dos)
    WORK="$(step_work_dir dos)"
    SCF_DIR="$TARGET/runs/scf"
    require_completed_scf_baseline "$SCF_DIR" 1 0
    prepare_run_dir "$WORK"
    record_job_start "$WORK"
    cp "$(structure_input_for_step dos)" "$WORK/POSCAR"
    cp "$TARGET/POTCAR" "$WORK/POTCAR"
    cp "$TARGET/KPOINTS.dos" "$WORK/KPOINTS"
    cp "$TARGET/INCAR.dos" "$WORK/INCAR"
    cp "$SCF_DIR/CHGCAR" "$WORK/CHGCAR"
    copy_if_exists "$SCF_DIR/WAVECAR" "$WORK/WAVECAR"
    run_and_log "$WORK"
    (
      cd "$WORK"
      printf '11\n111\n' | "$VASPKIT_CMD" > vaspkit_dos.log 2>&1 || true
    )
    ;;

  band)
    WORK="$(step_work_dir band)"
    SCF_DIR="$TARGET/runs/scf"
    require_completed_scf_baseline "$SCF_DIR" 1 0
    prepare_run_dir "$WORK"
    record_job_start "$WORK"
    cp "$(structure_input_for_step band)" "$WORK/POSCAR"
    cp "$TARGET/POTCAR" "$WORK/POTCAR"
    cp "$TARGET/INCAR.band" "$WORK/INCAR"
    if grep -qiE '^[[:space:]]*LHFCALC[[:space:]]*=[[:space:]]*(\.TRUE\.|TRUE|T|1)' "$WORK/INCAR"; then
      require_completed_scf_baseline "$SCF_DIR" 1 1
      cp "$(mesh_kpoints_input_for_step band)" "$WORK/KPOINTS"
      cp "$TARGET/KPOINTS.band" "$WORK/KPOINTS_OPT"
      cp "$SCF_DIR/WAVECAR" "$WORK/WAVECAR"
      copy_if_exists "$SCF_DIR/CHGCAR" "$WORK/CHGCAR"
      echo "Hybrid/HSE band detected: using SCF mesh KPOINTS plus KPOINTS_OPT line path." > "$WORK/vaspkit_band.log"
    else
      cp "$TARGET/KPOINTS.band" "$WORK/KPOINTS"
      cp "$SCF_DIR/CHGCAR" "$WORK/CHGCAR"
    fi
    run_and_log "$WORK"
    if [[ ! -f "$WORK/KPOINTS_OPT" ]]; then
      (
        cd "$WORK"
        printf '21\n211\n1\n' | "$VASPKIT_CMD" > vaspkit_band.log 2>&1 || true
      )
    else
      [[ -f "$WORK/PROCAR_OPT" || -f "$WORK/EIGENVAL" ]] || { echo "Hybrid/HSE band completed without PROCAR_OPT or EIGENVAL output." >> "$WORK/vaspkit_band.log"; exit 1; }
      echo "Hybrid/HSE band run finished. Legacy VASPKIT line-mode post-processing was skipped." >> "$WORK/vaspkit_band.log"
    fi
    ;;

  elastic)
    WORK="$(step_work_dir elastic)"
    prepare_run_dir "$WORK"
    record_job_start "$WORK"
    cp "$(structure_input_for_step elastic)" "$WORK/POSCAR"
    cp "$TARGET/POTCAR" "$WORK/POTCAR"
    cp "$(mesh_kpoints_input_for_step elastic)" "$WORK/KPOINTS"
    cp "$TARGET/INCAR.elastic" "$WORK/INCAR"
    run_and_log "$WORK"
    (
      cd "$WORK"
      printf '2\n203\n' | "$VASPKIT_CMD" > vaspkit_elastic.log 2>&1 || true
    )
    ;;

  charge)
    WORK="$(step_work_dir charge)"
    prepare_run_dir "$WORK"
    record_job_start "$WORK"
    cp "$(structure_input_for_step charge)" "$WORK/POSCAR"
    cp "$TARGET/POTCAR" "$WORK/POTCAR"
    cp "$(mesh_kpoints_input_for_step charge)" "$WORK/KPOINTS"
    cp "$TARGET/INCAR.charge" "$WORK/INCAR"
    run_and_log "$WORK"
    (
      cd "$WORK"
      if [[ -f AECCAR0 && -f AECCAR2 ]]; then
        "$PYTHON_CMD" "$RUN_STEP_DIR/chgcar_sum.py" AECCAR0 AECCAR2 CHGCAR_sum > chgsum.log 2>&1
      else
        echo "Missing AECCAR0/AECCAR2, cannot build CHGCAR_sum for reference-charge Bader analysis." > chgsum.log
        exit 1
      fi

      if command -v bader >/dev/null 2>&1; then
        if [[ -f CHGCAR_sum ]]; then
          bader CHGCAR -ref CHGCAR_sum > bader.log 2>&1
          [[ -f ACF.dat ]] || { echo "Bader completed without ACF.dat output." >> bader.log; exit 1; }
        else
          echo "CHGCAR_sum is missing, skipping reference-charge Bader analysis." > bader.log
          exit 1
        fi
      else
        echo "bader executable not found; CHGCAR_sum preparation finished but charge partitioning was skipped." > bader.log
        exit 1
      fi
    )
    ;;

  phonon)
    WORK="$(step_work_dir phonon)"
    prepare_run_dir "$WORK"
    record_job_start "$WORK"
    cp "$(structure_input_for_step phonon)" "$WORK/POSCAR"
    cp "$TARGET/POTCAR" "$WORK/POTCAR"
    cp "$TARGET/KPOINTS.phonon" "$WORK/KPOINTS"
    cp "$TARGET/INCAR.phonon" "$WORK/INCAR"
    cp "$TARGET/band.conf" "$WORK/band.conf"
    prepare_phonon_inputs "$WORK"
    (
      cd "$WORK"
      require_cmd "$PHONOPY_CMD"
      phonon_dim="$(phonopy_dim_from_band_conf band.conf)"
      "$PHONOPY_CMD" -d --dim="$phonon_dim"
      shopt -s nullglob
      poscars=(POSCAR-*)
      [[ ${#poscars[@]} -gt 0 ]] || { echo "No POSCAR-* files were generated by phonopy" >&2; exit 1; }
      for poscar in "${poscars[@]}"; do
        idx="${poscar#POSCAR-}"
        disp_dir="dis-$idx"
        mkdir -p "$disp_dir"
        cp INCAR KPOINTS POTCAR "$disp_dir"/
        cp "$poscar" "$disp_dir/POSCAR"
        (
          cd "$disp_dir"
          run_vasp > log 2>&1
        )
      done
      vaspruns=()
      for disp_dir in dis-*; do
        [[ -f "$disp_dir/vasprun.xml" ]] && vaspruns+=("$disp_dir/vasprun.xml")
      done
      [[ ${#vaspruns[@]} -gt 0 ]] || { echo "No vasprun.xml found in phonon displacements" >&2; exit 1; }
      "$PHONOPY_CMD" -f "${vaspruns[@]}"
      [[ -f FORCE_SETS ]] || { echo "FORCE_SETS was not generated by phonopy." >&2; exit 1; }
      plot_conf="band.conf"
      if band_conf_requests_force_constants "$plot_conf" && [[ ! -f FORCE_CONSTANTS ]]; then
        awk 'BEGIN { IGNORECASE = 1 } !/^[[:space:]]*FORCE_CONSTANTS[[:space:]]*=/' "$plot_conf" > band.runtime.conf
      plot_conf="band.runtime.conf"
      fi
      "$PHONOPY_CMD" -p -s "$plot_conf" > phonopy_plot.log 2>&1
      [[ -f band.yaml ]] || { echo "band.yaml was not generated by phonopy plotting." >&2; exit 1; }
      phonon_dos_mesh="$(metadata_triplet phonon_dos_kmesh || true)"
      phonon_dos_mesh_source="metadata.json:phonon_dos_kmesh"
      if [[ -z "${phonon_dos_mesh:-}" ]]; then
        phonon_dos_mesh="$(kpoints_mesh_triplet KPOINTS || true)"
        phonon_dos_mesh_source="KPOINTS fallback"
      fi
      if [[ -n "${phonon_dos_mesh:-}" ]]; then
        if "$PHONOPY_CMD" --dim="$phonon_dim" --mesh="$phonon_dos_mesh" --dos > phonopy_dos.log 2>&1; then
          printf 'Using phonon DOS q-mesh: %s (%s)\n' "$phonon_dos_mesh" "$phonon_dos_mesh_source" >> phonopy_dos.log
          if [[ ! -f total_dos.dat ]]; then
            echo "phonopy --dos completed without total_dos.dat output." >> phonopy_dos.log
          fi
        else
          rm -f total_dos.dat mesh.yaml
          echo "phonopy --dos failed; continuing without phonon DOS artifacts." >> phonopy_dos.log
        fi
        if "$PHONOPY_CMD" --dim="$phonon_dim" --mesh="$phonon_dos_mesh" --dos --pdos auto > phonopy_pdos.log 2>&1; then
          printf 'Using phonon DOS q-mesh: %s (%s)\n' "$phonon_dos_mesh" "$phonon_dos_mesh_source" >> phonopy_pdos.log
          if [[ ! -f projected_dos.dat ]]; then
            echo "phonopy --pdos completed without projected_dos.dat output." >> phonopy_pdos.log
          fi
        else
          rm -f projected_dos.dat
          echo "phonopy --pdos failed; continuing without projected phonon DOS artifacts." >> phonopy_pdos.log
        fi
      else
        echo "Could not determine a phonon DOS q-mesh from metadata.json or KPOINTS; skipping phonon DOS generation." > phonopy_dos.log
        echo "Could not determine a phonon DOS q-mesh from metadata.json or KPOINTS; skipping projected phonon DOS generation." > phonopy_pdos.log
      fi
    )
    ;;

  atom)
    WORK="$(step_work_dir atom)"
    prepare_run_dir "$WORK"
    record_job_start "$WORK"
    cp "$TARGET/POSCAR" "$WORK/POSCAR"
    cp "$TARGET/POTCAR" "$WORK/POTCAR"
    cp "$TARGET/KPOINTS" "$WORK/KPOINTS"
    cp "$TARGET/INCAR.atom" "$WORK/INCAR"
    (
      cd "$WORK"
      run_vasp_gamma > log 2>&1
    )
    ;;

  *)
    echo "Unknown step: $STEP" >&2
    usage >&2
    exit 1
    ;;
esac

echo "Finished $STEP in $TARGET"
