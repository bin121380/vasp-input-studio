#!/usr/bin/env bash

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MPI_CMD="${MPI_CMD:-mpirun}"
export VASP_MPI_NP="${VASP_MPI_NP:-1}"

export VASP_CMD="${VASP_CMD:-/opt/vasp/bin/vasp_std}"
export VASP_GAM_CMD="${VASP_GAM_CMD:-/opt/vasp/bin/vasp_gam}"
export VASPKIT_CMD="${VASPKIT_CMD:-/usr/local/bin/vaspkit}"
export PHONOPY_CMD="${PHONOPY_CMD:-/usr/local/bin/phonopy}"
export PYTHON_CMD="${PYTHON_CMD:-python3}"
