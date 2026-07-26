from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .aiida_service import aiida_inventory, get_import_state
from .structure_resolution import resolved_mesh_kpoints_path, resolved_structure_path


SUPPORTED_AIIDA_STEPS = {"relax", "scf", "converge"}


class AiiDASubmissionError(RuntimeError):
    """Raised when an AiiDA-backed submission cannot be prepared."""


def ensure_aiida_backend_available() -> None:
    state = get_import_state()
    if not state.available:
        raise AiiDASubmissionError(state.error or "AiiDA support is unavailable in the current environment.")


def supports_aiida_submission(step: str) -> bool:
    return step in SUPPORTED_AIIDA_STEPS


def preview_aiida_submission(profile_name: str, system_dir: Path, step: str, mpi_np: int) -> dict[str, Any]:
    ensure_aiida_backend_available()
    _, preview = _build_aiida_builder(profile_name, system_dir, step, mpi_np, require_daemon=False)
    return preview


def submit_aiida_submission(
    profile_name: str,
    system_dir: Path,
    step: str,
    mpi_np: int,
    *,
    resume: bool = False,
) -> dict[str, Any]:
    ensure_aiida_backend_available()
    from aiida.engine import submit

    builder, preview = _build_aiida_builder(profile_name, system_dir, step, mpi_np, require_daemon=True, resume=resume)
    node = submit(builder)

    return {
        "system": system_dir.name,
        "step": step,
        "target": "local",
        "backend": "aiida",
        "submission_mode": "aiida",
        "profile": profile_name,
        "workchain": preview["workchain"],
        "code_label": preview["code_label"],
        "mpi_np": mpi_np,
        "process_pk": node.pk,
        "process_uuid": str(node.uuid),
        "process_label": node.process_label,
        "state": "queued",
        "warnings": preview.get("warnings", []),
        "input_files": preview["input_files"],
        "resume": resume,
    }


def request_aiida_pause(profile_name: str, record: dict[str, Any]) -> dict[str, Any]:
    ensure_aiida_backend_available()
    remote_path = aiida_remote_path(profile_name, record)
    stopcar = remote_path / "STOPCAR"
    stopcar.write_text("LSTOP = .TRUE.\n", encoding="utf-8")
    return {"remote_path": str(remote_path), "control_file": str(stopcar), "mode": "stopcar"}


def stop_aiida_process(profile_name: str, record: dict[str, Any]) -> dict[str, Any]:
    ensure_aiida_backend_available()
    from aiida.engine.processes.control import kill_processes
    from aiida.manage import load_profile
    from aiida.orm import load_node

    process_pk = record.get("process_pk")
    if not process_pk:
        raise AiiDASubmissionError("Missing AiiDA process PK")

    load_profile(profile_name)
    node = load_node(process_pk)
    descendants = sorted(node.called_descendants, key=lambda item: item.pk)
    targets = [child for child in reversed(descendants) if not child.is_terminated]
    if not node.is_terminated:
        targets.append(node)

    errors: list[str] = []
    for target in targets:
        try:
            # Kill leaf processes explicitly before the root workflow so local scheduler
            # jobs are cancelled even if the parent workchain terminates first.
            kill_processes([target], msg_text="Stopped from VASP Input Studio", timeout=10.0)
        except Exception as exc:  # pragma: no cover - daemon/scheduler timing dependent
            errors.append(f"Process<{target.pk}>: {exc}")

    if errors and len(errors) == len(targets):
        raise AiiDASubmissionError("; ".join(errors))

    return {
        "process_pk": process_pk,
        "mode": "kill",
        "target_pks": [target.pk for target in targets],
        "errors": errors,
    }


def aiida_remote_path(profile_name: str, record: dict[str, Any]) -> Path:
    ensure_aiida_backend_available()
    from aiida.manage import load_profile
    from aiida.orm import load_node

    process_pk = record.get("process_pk")
    if not process_pk:
        raise AiiDASubmissionError("Missing AiiDA process PK")

    load_profile(profile_name)
    node = load_node(process_pk)
    descendants = sorted(node.called_descendants, key=lambda item: item.pk)
    remote_path = _latest_remote_path(node, descendants)
    if remote_path is None or not remote_path.exists():
        raise AiiDASubmissionError("No remote working directory is available")
    return remote_path


def sync_aiida_job_to_workspace(profile_name: str, record: dict[str, Any], system_dir: Path) -> dict[str, Any]:
    ensure_aiida_backend_available()
    from aiida.manage import load_profile
    from aiida.orm import load_node

    process_pk = record.get("process_pk")
    step = record.get("step")
    if not process_pk or not step:
        raise AiiDASubmissionError("Missing process PK or step for workspace sync")

    load_profile(profile_name)
    node = load_node(process_pk)
    descendants = sorted(node.called_descendants, key=lambda item: item.pk)
    remote_path = _latest_remote_path(node, descendants)
    if remote_path is None or not remote_path.exists():
        raise AiiDASubmissionError("No remote working directory is available for sync")

    run_dir = system_dir / "runs" / step
    run_dir.mkdir(parents=True, exist_ok=True)

    for filename in (
        "CHG",
        "CHGCAR",
        "CONTCAR",
        "DOSCAR",
        "EIGENVAL",
        "IBZKPT",
        "INCAR",
        "KPOINTS",
        "OSZICAR",
        "OUTCAR",
        "PCDAT",
        "POSCAR",
        "POTCAR",
        "PROCAR",
        "REPORT",
        "WAVECAR",
        "XDATCAR",
        "vasprun.xml",
    ):
        src = remote_path / filename
        if src.exists():
            shutil.copy2(src, run_dir / filename)

    vasp_output = remote_path / "vasp_output"
    if vasp_output.exists():
        shutil.copy2(vasp_output, run_dir / "log")

    for filename in ("_scheduler-stdout.txt", "_scheduler-stderr.txt", "_aiidasubmit.sh"):
        src = remote_path / filename
        if src.exists():
            shutil.copy2(src, run_dir / filename)

    metadata = {
        "backend": "aiida",
        "process_pk": process_pk,
        "process_uuid": str(node.uuid),
        "process_label": node.process_label,
        "synced_at": node.mtime.isoformat(timespec="seconds"),
        "remote_path": str(remote_path),
    }
    (run_dir / "aiida_process.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    if step == "relax":
        try:
            structure = node.outputs.relax.structure
            _write_structure_poscar(structure, run_dir / "CONTCAR")
        except Exception:
            pass

    return {"run_dir": str(run_dir), "remote_path": str(remote_path)}


def refresh_aiida_job(profile_name: str, record: dict[str, Any]) -> dict[str, Any]:
    ensure_aiida_backend_available()
    from aiida.manage import load_profile
    from aiida.orm import load_node

    process_pk = record.get("process_pk")
    if not process_pk:
        record["state"] = "failed"
        record["process_status"] = "Missing AiiDA process PK"
        return record

    load_profile(profile_name)
    node = load_node(process_pk)
    descendants = sorted(node.called_descendants, key=lambda item: item.pk)
    active_descendants = [child for child in descendants if not child.is_terminated]
    process_state = node.process_state.value if node.process_state else "created"
    record["backend_state"] = process_state
    record["process_status"] = node.process_status
    record["exit_status"] = node.exit_status
    record["process_label"] = node.process_label
    record["process_uuid"] = str(node.uuid)
    record["active_descendant_pks"] = [child.pk for child in active_descendants]

    if active_descendants:
        record["state"] = "running"
    elif node.is_terminated:
        if getattr(node, "is_killed", False):
            record["state"] = "stopped"
        elif getattr(node, "is_excepted", False):
            record["state"] = "failed"
        else:
            record["state"] = "finished" if (node.exit_status or 0) == 0 else "failed"
        record.setdefault("finished_at", node.mtime.isoformat(timespec="seconds"))
    elif process_state in {"running", "waiting"}:
        record["state"] = "running"
    else:
        record["state"] = "queued"

    return record


def aiida_job_log_payload(profile_name: str, record: dict[str, Any]) -> dict[str, Any]:
    ensure_aiida_backend_available()
    from aiida.manage import load_profile
    from aiida.orm import Log, OrderSpecifier, load_node

    process_pk = record.get("process_pk")
    if not process_pk:
        raise AiiDASubmissionError("Missing AiiDA process PK")

    load_profile(profile_name)
    node = load_node(process_pk)
    logs = Log.collection.get_logs_for(node, order_by=[OrderSpecifier("time", "asc")])

    lines = [
        f"AiiDA Process<{node.pk}> {node.process_label}",
        f"UUID: {node.uuid}",
        f"State: {node.process_state.value if node.process_state else 'created'}",
        f"Exit status: {node.exit_status if node.exit_status is not None else 'pending'}",
    ]
    if node.process_status:
        lines.append(f"Status: {node.process_status}")

    descendants = sorted(node.called_descendants, key=lambda item: item.pk)
    if descendants:
        lines.append("")
        lines.append("Called descendants:")
        for child in descendants:
            child_state = child.process_state.value if child.process_state else "created"
            exit_status = child.exit_status if child.exit_status is not None else "pending"
            lines.append(f"- {child.process_label}<{child.pk}> state={child_state} exit={exit_status}")

    if logs:
        lines.append("")
        lines.append("Reports:")
        for log in logs:
            timestamp = log.time.isoformat(timespec="seconds")
            lines.append(f"[{timestamp}] {log.levelname} {log.message}")

    remote_path = _latest_remote_path(node, descendants)
    log_notice = _aiida_log_notice(record, descendants, logs, remote_path)
    if remote_path:
        for filename, title in (
            ("vasp_output", "VASP output tail"),
            ("_scheduler-stdout.txt", "Scheduler stdout tail"),
            ("_scheduler-stderr.txt", "Scheduler stderr tail"),
        ):
            file_path = remote_path / filename
            if file_path.exists():
                tail = _tail_text(file_path)
                if tail:
                    lines.append("")
                    lines.append(f"{title}: {file_path}")
                    lines.append(tail)

    return {
        "job_id": record["id"],
        "state": record["state"],
        "log_path": f"aiida://process/{node.pk}",
        "log_notice": log_notice,
        "content": "\n".join(lines) + "\n",
    }


def _aiida_log_notice(
    record: dict[str, Any],
    descendants: list[Any],
    logs: list[Any],
    remote_path: Path | None,
) -> str | None:
    step = str(record.get("step") or "").strip().lower()
    if step != "relax" or remote_path is None:
        return None

    child_runs: list[tuple[Any, Path]] = []
    for child in descendants:
        if getattr(child, "process_label", "") != "VaspWorkChain":
            continue
        try:
            child_remote = Path(child.outputs.remote_folder.get_remote_path())
        except Exception:
            continue
        child_runs.append((child, child_remote))

    if len(child_runs) < 2:
        return None

    current_index = next(
        (index for index, (_, child_remote) in enumerate(child_runs, start=1) if child_remote == remote_path),
        len(child_runs),
    )
    final_calculation = any(
        "performing a final calculation" in str(getattr(log, "message", "")).lower()
        for log in logs
    )
    if final_calculation and current_index == len(child_runs):
        current_label = f"Showing final calculation {current_index}/{len(child_runs)}."
    else:
        current_label = f"Showing child run {current_index}/{len(child_runs)}."

    return (
        f"{current_label} AiiDA relax can launch a fresh VASP run after structural optimization, "
        "so a restart-like header here can be normal."
    )


def _build_aiida_builder(
    profile_name: str,
    system_dir: Path,
    step: str,
    mpi_np: int,
    *,
    require_daemon: bool,
    resume: bool = False,
) -> tuple[Any, dict[str, Any]]:
    if not supports_aiida_submission(step):
        raise AiiDASubmissionError(f"AiiDA submission is currently enabled only for: {', '.join(sorted(SUPPORTED_AIIDA_STEPS))}")

    from aiida import orm
    from aiida.common.extendeddicts import AttributeDict
    from aiida.manage import load_profile
    from aiida.orm import load_code
    from aiida.plugins import WorkflowFactory
    from aiida_vasp.parsers.content_parsers.incar import IncarParser
    from aiida_vasp.parsers.content_parsers.kpoints import KpointsParser
    from aiida_vasp.parsers.content_parsers.poscar import PoscarParser
    from aiida_vasp.parsers.node_composer import NodeComposer

    backend = aiida_inventory(profile_name, force_refresh=require_daemon)
    metadata = _read_json(system_dir / "metadata.json", {})
    warnings: list[str] = []

    if not backend.get("available"):
        raise AiiDASubmissionError("AiiDA is not available in the current environment")
    if not backend.get("profile_exists"):
        raise AiiDASubmissionError(f"AiiDA profile does not exist: {profile_name}")
    if require_daemon and not backend.get("daemon_running"):
        raise AiiDASubmissionError(f"AiiDA daemon is not running for profile: {profile_name}")
    if not backend.get("codes"):
        raise AiiDASubmissionError("No AiiDA VASP codes are registered")
    if not backend.get("potcar_families"):
        raise AiiDASubmissionError("No AiiDA POTCAR family is available")
    if not backend.get("daemon_running"):
        warnings.append(
            f"AiiDA daemon is not running for profile {profile_name}; preview is available but real submit will fail until the daemon is started."
        )

    rabbitmq = backend.get("rabbitmq", {})
    if backend.get("has_broker") and rabbitmq.get("compatibility") == "risk":
        raise AiiDASubmissionError(
            rabbitmq.get("note")
            or "RabbitMQ is configured in a way that can break long-running AiiDA jobs."
        )
    poscar_path = _resolve_structure_path(system_dir, step, resume=resume)
    incar_path = _resolve_incar_path(system_dir, step)
    mesh_kpoints_path = _resolve_mesh_kpoints_path(system_dir, step)
    preview_kpoints_path = _resolve_preview_kpoints_path(system_dir, step)
    potcar_path = system_dir / "POTCAR"

    if not poscar_path.exists():
        raise AiiDASubmissionError(f"Missing structure file: {poscar_path}")
    if not incar_path.exists():
        raise AiiDASubmissionError(f"Missing INCAR file: {incar_path}")
    if not mesh_kpoints_path.exists():
        raise AiiDASubmissionError(f"Missing KPOINTS file: {mesh_kpoints_path}")
    if not potcar_path.exists():
        raise AiiDASubmissionError(f"Missing POTCAR file: {potcar_path}")

    load_profile(profile_name)

    with poscar_path.open("r", encoding="utf-8") as handle:
        structure_data = PoscarParser(handler=handle).get_quantity("poscar-structure")
    structure = NodeComposer.compose_core_structure("core.structure", {"structure": structure_data})

    with mesh_kpoints_path.open("r", encoding="utf-8") as handle:
        mesh_kpoints_data = KpointsParser(handler=handle).get_quantity("kpoints-kpoints")
    if mesh_kpoints_data is None:
        raise AiiDASubmissionError("Line-mode KPOINTS is not supported for SCF/relax/converge meshes")
    mesh_kpoints = NodeComposer.compose_core_array_kpoints("core.array.kpoints", {"kpoints": mesh_kpoints_data})
    mesh_kpoints.set_cell_from_structure(structure)

    with incar_path.open("r", encoding="utf-8") as handle:
        incar = IncarParser(handler=handle).get_quantity("incar")
    incar = dict(incar)

    restart_folder = None
    restart_info: dict[str, Any] | None = None
    if resume:
        restart_folder, restart_info = _restart_folder_from_previous_step(profile_name, system_dir, step)
        incar["istart"] = 1
        incar["icharg"] = 1
        warnings.append(f"Resuming {step} from the previous AiiDA remote folder.")

    potcar_family = _select_potcar_family(backend, metadata)
    mapping = _potcar_mapping(poscar_path, potcar_path)
    if not mapping:
        raise AiiDASubmissionError("Could not infer POTCAR mapping from POSCAR/POTCAR")

    code_label = _choose_code_label(backend["codes"], mesh_kpoints, incar)
    workchain_entry = _workchain_entry(step)
    workchain = WorkflowFactory(workchain_entry)
    builder = workchain.get_builder()
    builder.code = load_code(code_label)
    builder.structure = structure
    builder.potential_family = orm.Str(potcar_family)
    builder.potential_mapping = orm.Dict(dict=mapping)
    builder.options = orm.Dict(
        dict={
            "withmpi": True,
            "import_sys_environment": True,
            "resources": {
                "num_machines": 1,
                "num_mpiprocs_per_machine": mpi_np,
            },
            "max_wallclock_seconds": int(metadata.get("wallclock_seconds", 12 * 3600)),
        }
    )
    builder.settings = orm.Dict(dict=_build_settings_payload(step, incar, metadata))
    builder.max_iterations = orm.Int(4 if step in {"relax", "converge"} else 3)
    builder.clean_workdir = orm.Bool(False)
    builder.verbose = orm.Bool(True)
    builder.metadata.label = f"{system_dir.name} {step}"
    builder.metadata.description = f"Submitted from VASP Input Studio via AiiDA for {system_dir.name} {step}"

    input_files = {
        "structure": str(poscar_path),
        "incar": str(incar_path),
        "kpoints": str(preview_kpoints_path),
        "potcar": str(potcar_path),
    }

    if step == "relax":
        builder.kpoints = mesh_kpoints
        if restart_folder is not None:
            builder.restart_folder = restart_folder
        relax_inputs = _relax_inputs_from_incar(incar, orm, AttributeDict)
        builder.relax = relax_inputs
        incar = {key: value for key, value in incar.items() if key not in {"ibrion", "isif", "nsw", "ediffg"}}
        builder.parameters = orm.Dict(dict={"incar": incar})
    elif step == "scf":
        builder.kpoints = mesh_kpoints
        requirements = _restart_requirements_from_incar(incar)
        icharg = _int_like(incar.get("icharg"), 2)
        istart = _int_like(incar.get("istart"), 0)
        if requirements["charge_density"] or requirements["wavefunctions"]:
            restart_folder, charge_density, wavefunctions, restart_info = _restart_inputs_from_previous_step(
                profile_name,
                system_dir,
                "relax",
                require_charge_density=requirements["charge_density"],
                require_wavefunctions=requirements["wavefunctions"],
                icharg=icharg,
                istart=istart,
            )
            if restart_folder is not None:
                builder.restart_folder = restart_folder
                input_files["restart_from"] = restart_info["remote_path"]
                needed = []
                if requirements["charge_density"]:
                    needed.append("CHGCAR")
                if requirements["wavefunctions"]:
                    needed.append("WAVECAR")
                warnings.append(f"SCF is reusing {' and '.join(needed)} from the previous relax remote folder.")
            if charge_density is not None:
                builder.chgcar = charge_density
                input_files["charge_density"] = restart_info["local_paths"]["charge_density"]
                warnings.append("SCF is uploading CHGCAR from runs/relax because ICHARG requests a restart charge density.")
            if wavefunctions is not None:
                builder.wavecar = wavefunctions
                input_files["wavefunctions"] = restart_info["local_paths"]["wavefunctions"]
                warnings.append("SCF is uploading WAVECAR from runs/relax because ISTART requests restart wavefunctions.")
        elif restart_folder is not None:
            builder.restart_folder = restart_folder
        builder.parameters = orm.Dict(dict={"incar": incar})
        if poscar_path.name == "CONTCAR":
            warnings.append("SCF is using the latest relaxed CONTCAR as structure input.")
    elif step == "band":
        restart_folder, restart_info = _restart_folder_from_previous_step(profile_name, system_dir, "scf")
        builder.restart_folder = restart_folder
        builder.parameters = orm.Dict(dict={"incar": incar})
        builder.bands.kpoints_distance = orm.Float(float(metadata.get("band_kpoints_distance", 0.05)))
        if _truthy(metadata.get("band_decompose_bands")) or int(incar.get("lorbit", 0) or 0) >= 10:
            builder.bands.decompose_bands = orm.Bool(True)
        if _truthy(metadata.get("band_decompose_wave")):
            builder.bands.decompose_wave = orm.Bool(True)
        input_files["restart_from"] = restart_info["remote_path"]
        warnings.append("AiiDA bands uses a SeekPath-generated line path and restarts from the previous SCF remote folder.")
    elif step == "converge":
        builder.kpoints = mesh_kpoints
        if restart_folder is not None:
            builder.restart_folder = restart_folder
        builder.relax = _converge_relax_inputs_from_incar(incar, orm, AttributeDict)
        builder.parameters = orm.Dict(dict={"incar": incar})
        _apply_converge_inputs(builder, orm, metadata, incar)
        warnings.append("Convergence uses AiiDA-VASP sampling rather than a single legacy shell step.")

    if restart_info is not None and "remote_path" in restart_info:
        input_files["restart_from"] = restart_info["remote_path"]

    preview = {
        "system": system_dir.name,
        "step": step,
        "target": "local",
        "target_kind": "local",
        "submit_backend": "aiida",
        "submission_mode": "aiida",
        "profile": profile_name,
        "mpi_np": mpi_np,
        "workchain": workchain_entry,
        "code_label": code_label,
        "potcar_family": potcar_family,
        "potential_mapping": mapping,
        "input_files": input_files,
        "warnings": warnings,
        "resume": resume,
    }
    return builder, preview


def _resolve_structure_path(system_dir: Path, step: str, *, resume: bool = False) -> Path:
    return resolved_structure_path(system_dir, step, resume=resume)


def _resolve_incar_path(system_dir: Path, step: str) -> Path:
    candidates = {
        "relax": ["INCAR.relax"],
        "scf": ["INCAR.scf"],
        "band": ["INCAR.band", "INCAR.scf"],
        "converge": ["INCAR.converge", "INCAR.scf"],
    }[step]
    for name in candidates:
        path = system_dir / name
        if path.exists():
            return path
    return system_dir / candidates[0]


def _resolve_mesh_kpoints_path(system_dir: Path, step: str) -> Path:
    return resolved_mesh_kpoints_path(system_dir, step)


def _resolve_preview_kpoints_path(system_dir: Path, step: str) -> Path:
    if step == "band":
        candidate = system_dir / "KPOINTS.band"
        if candidate.exists():
            return candidate
    return _resolve_mesh_kpoints_path(system_dir, step)


def _build_settings_payload(step: str, incar: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    lorbit = int(incar.get("lorbit", 0) or 0)
    lepsilon = _truthy(incar.get("lepsilon"))
    retrieve_charge_density = _truthy(incar.get("lcharg")) or step in {"scf"}
    wavecar = _truthy(incar.get("lwave")) or step in {"scf"}

    parser_settings: dict[str, Any] = {
        "add_misc": True,
        "add_structure": step == "relax",
        "add_kpoints": True,
        # Keep retrieving CHGCAR for downstream reuse, but do not ask aiida-vasp
        # to parse it into a node. The parsevasp version bundled here fails on some
        # VASP 6 CHGCAR layouts that use whitespace-only separator lines.
        "add_charge_density": False,
        # aiida-vasp can retrieve WAVECAR, but parsing it into a wavecar node is
        # not implemented in the plugin version we use. Keep retrieval/restart
        # support via files, but do not request parser output nodes for wavecar.
        "add_wavecar": False,
        "add_dos": step == "scf",
        "add_bands": step == "band",
        "add_forces": step != "band",
        "add_stress": step != "band",
        "add_projectors": lorbit >= 10,
        "add_site_magnetization": lorbit >= 10 and int(incar.get("ispin", 1) or 1) == 2,
        "add_born_charges": lepsilon,
        "add_dielectrics": lepsilon,
    }

    additional_retrieve_list: list[str] = []
    if retrieve_charge_density:
        additional_retrieve_list.append("CHGCAR")
    if wavecar:
        additional_retrieve_list.append("WAVECAR")
    if parser_settings["add_dos"]:
        additional_retrieve_list.append("DOSCAR")
    if parser_settings["add_projectors"]:
        additional_retrieve_list.append("PROCAR")

    if extra := metadata.get("additional_retrieve_list"):
        if isinstance(extra, list):
            additional_retrieve_list.extend(str(item) for item in extra)

    payload = {
        "parser_settings": parser_settings,
        "ADDITIONAL_RETRIEVE_LIST": sorted(set(additional_retrieve_list)),
        "USE_WAVECAR_FOR_RESTART": wavecar,
    }
    return payload


def _restart_requirements_from_incar(incar: dict[str, Any]) -> dict[str, bool]:
    icharg = _int_like(incar.get("icharg"), 2)
    istart = _int_like(incar.get("istart"), 0)
    return {
        "charge_density": icharg in {1, 11},
        "wavefunctions": istart in {1, 2, 3},
    }


def _restart_inputs_from_previous_step(
    profile_name: str,
    system_dir: Path,
    previous_step: str,
    *,
    require_charge_density: bool = False,
    require_wavefunctions: bool = False,
    icharg: int | None = None,
    istart: int | None = None,
) -> tuple[Any | None, Any | None, Any | None, dict[str, Any]]:
    restart_folder = None
    restart_info: dict[str, Any] = {"previous_step": previous_step}

    if require_charge_density or require_wavefunctions:
        try:
            restart_folder, remote_info = _restart_folder_from_previous_step(profile_name, system_dir, previous_step)
            remote_path = Path(remote_info["remote_path"])
            missing_remote: list[str] = []
            if require_charge_density and not _nonempty_file(remote_path / "CHGCAR"):
                missing_remote.append("CHGCAR")
            if require_wavefunctions and not _nonempty_file(remote_path / "WAVECAR"):
                missing_remote.append("WAVECAR")
            if not missing_remote:
                restart_info.update(remote_info)
                restart_info["source"] = "remote_folder"
                return restart_folder, None, None, restart_info
            restart_info["remote_path"] = remote_info["remote_path"]
            restart_info["remote_missing"] = missing_remote
        except AiiDASubmissionError as exc:
            restart_info["remote_error"] = str(exc)

    from aiida.plugins import DataFactory
    from aiida_vasp.data.wavefun import WavefunData

    charge_density = None
    wavefunctions = None
    local_paths: dict[str, str] = {}

    if require_charge_density:
        chgcar_path = system_dir / "runs" / previous_step / "CHGCAR"
        if not _nonempty_file(chgcar_path):
            raise AiiDASubmissionError(
                f"INCAR requires CHGCAR (ICHARG={icharg if icharg is not None else 'restart'}), "
                f"but no usable CHGCAR was found in {chgcar_path}."
            )
        ChargedensityData = DataFactory("vasp.chargedensity")
        charge_density = ChargedensityData(str(chgcar_path))
        local_paths["charge_density"] = str(chgcar_path)

    if require_wavefunctions:
        wavecar_path = system_dir / "runs" / previous_step / "WAVECAR"
        if not _nonempty_file(wavecar_path):
            raise AiiDASubmissionError(
                f"INCAR requires WAVECAR (ISTART={istart if istart is not None else 'restart'}), "
                f"but no usable WAVECAR was found in {wavecar_path}."
            )
        wavefunctions = WavefunData(str(wavecar_path))
        local_paths["wavefunctions"] = str(wavecar_path)

    restart_info["source"] = "local_files"
    restart_info["local_paths"] = local_paths
    return None, charge_density, wavefunctions, restart_info


def _apply_converge_inputs(builder: Any, orm: Any, metadata: dict[str, Any], incar: dict[str, Any]) -> None:
    config = metadata.get("converge") if isinstance(metadata.get("converge"), dict) else {}
    encut = float(incar.get("encut", 500) or 500)

    builder.converge.cutoff_type = orm.Str(str(config.get("cutoff_type", "energy")))
    builder.converge.cutoff_value = orm.Float(float(config.get("cutoff_value", 0.01)))
    builder.converge.cutoff_value_r = orm.Float(float(config.get("cutoff_value_r", 0.01)))
    builder.converge.pwcutoff_start = orm.Float(float(config.get("pwcutoff_start", max(200.0, encut - 150.0))))
    builder.converge.pwcutoff_step = orm.Float(float(config.get("pwcutoff_step", 50.0)))
    builder.converge.pwcutoff_samples = orm.Int(int(config.get("pwcutoff_samples", 5)))
    builder.converge.k_spacing = orm.Float(float(config.get("k_spacing", 0.10)))
    builder.converge.k_samples = orm.Int(int(config.get("k_samples", 5)))
    builder.converge.relax = orm.Bool(_truthy(config.get("relax")))
    builder.converge.testing = orm.Bool(_truthy(config.get("testing")))
    builder.converge.compress = orm.Bool(_truthy(config.get("compress")))
    builder.converge.displace = orm.Bool(_truthy(config.get("displace")))


def _restart_folder_from_previous_step(profile_name: str, system_dir: Path, previous_step: str) -> tuple[Any, dict[str, Any]]:
    from aiida.manage import load_profile
    from aiida.orm import load_node

    metadata_path = system_dir / "runs" / previous_step / "aiida_process.json"
    metadata = _read_json(metadata_path, {})
    process_pk = metadata.get("process_pk")
    if not process_pk:
        raise AiiDASubmissionError(
            f"AiiDA {previous_step} restart metadata is missing. Run {previous_step} via the AiiDA backend first."
        )

    load_profile(profile_name)
    node = load_node(process_pk)
    if node.exit_status not in (None, 0):
        raise AiiDASubmissionError(f"Previous AiiDA {previous_step} process did not finish successfully: {node.exit_status}")

    remote_folder = None
    try:
        remote_folder = node.outputs.remote_folder
    except Exception:
        descendants = sorted(node.called_descendants, key=lambda item: item.pk)
        for candidate in reversed(descendants):
            try:
                remote_folder = candidate.outputs.remote_folder
                break
            except Exception:
                continue
    if remote_folder is None:
        raise AiiDASubmissionError(f"No remote folder is available from the previous {previous_step} process.")

    return remote_folder, {"process_pk": process_pk, "remote_path": remote_folder.get_remote_path()}


def _workchain_entry(step: str) -> str:
    return {
        "relax": "vasp.relax",
        "scf": "vasp.vasp",
        "band": "vasp.bands",
        "converge": "vasp.converge",
    }[step]


def _select_potcar_family(backend: dict[str, Any], metadata: dict[str, Any]) -> str:
    requested = metadata.get("potcar_family")
    families = backend.get("potcar_families", [])
    if requested:
        for item in families:
            if item.get("label") == requested:
                return requested
    return families[0]["label"]


def _potcar_mapping(poscar_path: Path, potcar_path: Path) -> dict[str, str]:
    species = _poscar_species(poscar_path)
    titles = _potcar_titles(potcar_path)
    return {element: potential for element, potential in zip(species, titles)}


def _poscar_species(path: Path) -> list[str]:
    lines = [line.rstrip() for line in path.read_text(errors="ignore").splitlines() if line.strip()]
    if len(lines) < 7:
        return []
    return lines[5].split()


def _potcar_titles(path: Path) -> list[str]:
    titles: list[str] = []
    pattern = re.compile(r"TITEL\s*=\s*PAW_\S+\s+(\S+)")
    for line in path.read_text(errors="ignore").splitlines():
        match = pattern.search(line)
        if match:
            titles.append(match.group(1))
    return titles


def _choose_code_label(codes: list[dict[str, Any]], kpoints: Any, incar: dict[str, Any]) -> str:
    if _truthy(incar.get("lsorbit")) or _truthy(incar.get("lnoncollinear")):
        ncl_code = next((item["full_label"] for item in codes if "vasp_ncl" in item["full_label"]), None)
        if ncl_code:
            return ncl_code

    mesh = None
    try:
        mesh, _ = kpoints.get_kpoints_mesh()
    except Exception:  # pragma: no cover - defensive fallback
        mesh = None

    if mesh and tuple(int(item) for item in mesh) == (1, 1, 1):
        gamma_code = next((item["full_label"] for item in codes if "vasp_gam" in item["full_label"]), None)
        if gamma_code:
            return gamma_code

    std_code = next((item["full_label"] for item in codes if "vasp_std" in item["full_label"]), None)
    if std_code:
        return std_code

    return codes[0]["full_label"]


def _relax_inputs_from_incar(incar: dict[str, Any], orm: Any, attribute_dict_cls: type[Any]) -> Any:
    isif = int(incar.get("isif", 2))
    nsw = int(incar.get("nsw", 60))
    ediffg = incar.get("ediffg")

    relax = attribute_dict_cls()
    relax.perform = orm.Bool(True)
    relax.keep_magnetization = orm.Bool(True)
    relax.steps = orm.Int(nsw)

    dof = _relax_dof_from_isif(isif)
    relax.positions = orm.Bool(dof["positions"])
    relax.shape = orm.Bool(dof["shape"])
    relax.volume = orm.Bool(dof["volume"])

    if isinstance(ediffg, (int, float)):
        if ediffg < 0:
            relax.force_cutoff = orm.Float(abs(ediffg))
        elif ediffg > 0:
            relax.energy_cutoff = orm.Float(ediffg)

    return relax


def _converge_relax_inputs_from_incar(incar: dict[str, Any], orm: Any, attribute_dict_cls: type[Any]) -> Any:
    relax = _relax_inputs_from_incar(incar, orm, attribute_dict_cls)
    relax.perform = orm.Bool(False)
    return relax


def _relax_dof_from_isif(isif: int) -> dict[str, bool]:
    mapping = {
        2: {"positions": True, "shape": False, "volume": False},
        3: {"positions": True, "shape": True, "volume": True},
        4: {"positions": True, "shape": True, "volume": False},
        5: {"positions": False, "shape": True, "volume": False},
        6: {"positions": False, "shape": False, "volume": True},
        7: {"positions": False, "shape": True, "volume": True},
    }
    return mapping.get(isif, {"positions": True, "shape": False, "volume": False})


def _latest_remote_path(node: Any, descendants: list[Any]) -> Path | None:
    candidates = [*descendants, node]
    for candidate in reversed(candidates):
        try:
            remote_folder = candidate.outputs.remote_folder
        except Exception:
            continue
        try:
            return Path(remote_folder.get_remote_path())
        except Exception:
            continue
    return None


def _tail_text(path: Path, max_lines: int = 40) -> str:
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return ""
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])


def _write_structure_poscar(structure: Any, path: Path) -> None:
    from aiida_vasp.parsers.content_parsers.poscar import PoscarParser

    parser = PoscarParser(data=structure)
    parser.write(path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().upper() in {".TRUE.", "TRUE", "T", "1", "YES", "Y"}


def _int_like(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _nonempty_file(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False
