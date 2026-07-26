from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


SUPPORTED_SCHEDULERS = {"ssh", "slurm", "pbs"}


class SchedulerError(RuntimeError):
    """Raised when a remote scheduler action fails."""


def _validated_profile(profile: dict[str, Any]) -> dict[str, Any]:
    record = dict(profile)

    name = str(record.get("name") or "").strip()
    if not name or name == "local":
        raise SchedulerError("Remote profile name is invalid.")
    if name.startswith("-") or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise SchedulerError("Remote profile name must not contain whitespace, control characters, or a leading '-'.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise SchedulerError("Remote profile name may contain only letters, digits, '_' and '-'.")

    host = str(record.get("host") or "").strip()
    if not host:
        raise SchedulerError("Remote profile host is required.")
    if host.startswith("-") or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in host):
        raise SchedulerError("Remote profile host must not contain whitespace, control characters, or a leading '-'.")

    user = str(record.get("user") or "").strip()
    if user.startswith("-") or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in user):
        raise SchedulerError("Remote profile user must not contain whitespace, control characters, or a leading '-'.")

    workspace_root = str(record.get("workspace_root") or "").strip()
    if not workspace_root:
        raise SchedulerError("Remote profile workspace root is required.")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in workspace_root):
        raise SchedulerError("Remote profile workspace root must not contain whitespace or control characters.")
    if not workspace_root.startswith("/"):
        raise SchedulerError("Remote profile workspace root must be an absolute POSIX path.")

    record["name"] = name
    record["host"] = host
    record["user"] = user
    record["workspace_root"] = workspace_root
    record["scheduler_kind"] = normalize_scheduler_kind(record)
    return record


def normalize_scheduler_kind(profile: dict[str, Any]) -> str:
    kind = str(profile.get("scheduler_kind", "ssh") or "ssh").strip().lower()
    return kind if kind in SUPPORTED_SCHEDULERS else "ssh"


def submission_mode(profile: dict[str, Any]) -> str:
    return {
        "ssh": "remote_ssh",
        "slurm": "remote_slurm",
        "pbs": "remote_pbs",
    }[normalize_scheduler_kind(profile)]


def host_string(profile: dict[str, Any]) -> str:
    return f"{profile['user']}@{profile['host']}" if profile.get("user") else profile["host"]


def preview_remote_submission(profile: dict[str, Any], system: str, step: str, mpi_np: int) -> dict[str, Any]:
    profile = _validated_profile(profile)
    job_id = f"{system}-{step}-preview"
    plan = _build_submission_plan(profile, system, step, mpi_np, job_id)
    return {
        "scheduler_kind": plan["scheduler_kind"],
        "submission_mode": plan["submission_mode"],
        "host": plan["host"],
        "remote_workspace_root": plan["remote_root"],
        "remote_log": plan["remote_log"],
        "remote_command_preview": plan["remote_command_preview"],
    }


def submit_remote_job(
    root: Path,
    runtime_dir: Path,
    profile: dict[str, Any],
    system: str,
    step: str,
    mpi_np: int,
    job_id: str,
) -> dict[str, Any]:
    profile = _validated_profile(profile)
    launcher_log = runtime_dir / "job_logs" / f"{job_id}.log"
    launcher_log.parent.mkdir(parents=True, exist_ok=True)

    plan = _build_submission_plan(profile, system, step, mpi_np, job_id)
    result = subprocess.run(
        ["ssh", plan["host"], plan["remote_command"]],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    launcher_log.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")

    if result.returncode != 0:
        raise SchedulerError(result.stderr.strip() or result.stdout.strip() or "Remote submission failed")

    stdout = result.stdout.strip()
    record = {
        "id": job_id,
        "system": system,
        "step": step,
        "target": "remote",
        "backend": "legacy",
        "profile": profile["name"],
        "host": plan["host"],
        "mpi_np": plan["mpi_np"],
        "state": "submitted_remote",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "launcher_log": str(launcher_log),
        "target_log": plan["remote_log"],
        "scheduler_kind": plan["scheduler_kind"],
        "submission_mode": plan["submission_mode"],
        "remote_command_preview": plan["remote_command_preview"],
    }

    if plan["scheduler_kind"] == "ssh":
        remote_pid = stdout.splitlines()[-1] if stdout else ""
        if not re.fullmatch(r"\d+", remote_pid):
            raise SchedulerError(f"Remote SSH submission did not return a valid PID: {stdout or '<empty>'}")
        record["remote_pid"] = remote_pid
    else:
        scheduler_job_id = _parse_scheduler_job_id(stdout)
        if not scheduler_job_id:
            raise SchedulerError(f"Unable to parse scheduler job id from submission output: {stdout or '<empty>'}")
        record["scheduler_job_id"] = scheduler_job_id
        record["scheduler_stdout"] = stdout
        if plan.get("remote_script"):
            record["remote_script"] = plan["remote_script"]
        if plan.get("scheduler_stderr"):
            record["scheduler_stderr"] = plan["scheduler_stderr"]

    return record


def refresh_remote_job(profile: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    profile = _validated_profile(profile)
    kind = normalize_scheduler_kind(profile)
    host = host_string(profile)

    if kind == "ssh":
        remote_pid = str(job.get("remote_pid") or "").strip()
        if not re.fullmatch(r"\d+", remote_pid):
            job["remote_status"] = "INVALID_PID"
            job["state"] = "failed"
            return job
        command = f"kill -0 {shlex.quote(str(remote_pid))} >/dev/null 2>&1 && echo RUNNING || echo FINISHED"
        stdout = _run_remote_query(host, command)
        state = stdout.strip().upper() or "UNKNOWN"
        job["remote_status"] = state
        if state == "RUNNING":
            job["state"] = "running"
        elif state == "FINISHED":
            job["state"] = "finished"
    elif kind == "slurm":
        scheduler_job_id = job.get("scheduler_job_id")
        if not scheduler_job_id:
            return job
        command = (
            f"state=$(squeue -h -j {shlex.quote(str(scheduler_job_id))} -o '%T' 2>/dev/null | head -n1); "
            f"if [ -z \"$state\" ] && command -v sacct >/dev/null 2>&1; then "
            f"state=$(sacct -n -j {shlex.quote(str(scheduler_job_id))} --format=State 2>/dev/null | head -n1 | awk '{{print $1}}'); "
            "fi; echo ${state:-UNKNOWN}"
        )
        state = _run_remote_query(host, command).strip().upper() or "UNKNOWN"
        job["remote_status"] = state
        _apply_slurm_state(job, state)
    else:
        scheduler_job_id = job.get("scheduler_job_id")
        if not scheduler_job_id:
            return job
        command = (
            f"state=$(qstat -f {shlex.quote(str(scheduler_job_id))} 2>/dev/null | "
            "awk -F= '/job_state =/{gsub(/ /, \"\", $2); print $2; exit}'); "
            "echo ${state:-UNKNOWN}"
        )
        state = _run_remote_query(host, command).strip().upper() or "UNKNOWN"
        job["remote_status"] = state
        _apply_pbs_state(job, state)

    return job


def _build_submission_plan(profile: dict[str, Any], system: str, step: str, mpi_np: int, job_id: str) -> dict[str, Any]:
    scheduler_kind = normalize_scheduler_kind(profile)
    host = host_string(profile)
    remote_root = profile["workspace_root"].rstrip("/")
    mpi_value = mpi_np or int(profile.get("vasp_mpi_np", 1) or 1)
    pre_command = str(profile.get("pre_command", "") or "").strip()
    pre = f"{pre_command}\n" if pre_command else ""
    remote_jobs_dir = f"{remote_root}/runtime/remote_jobs"
    remote_log = f"{remote_jobs_dir}/{job_id}.log"
    system_arg = shlex.quote(f"systems/{system}")
    step_arg = shlex.quote(step)

    if scheduler_kind == "ssh":
        remote_command = (
            f"mkdir -p {shlex.quote(remote_jobs_dir)} && "
            f"cd {shlex.quote(remote_root)} && "
            f"{pre_command + ' && ' if pre_command else ''}"
            f"nohup env VASP_MPI_NP={mpi_value} ./scripts/run_step.sh {system_arg} {step_arg} "
            f"> {shlex.quote(remote_log)} 2>&1 < /dev/null & echo $!"
        )
        return {
            "scheduler_kind": scheduler_kind,
            "submission_mode": submission_mode(profile),
            "host": host,
            "remote_root": remote_root,
            "remote_log": remote_log,
            "remote_command": remote_command,
            "remote_command_preview": remote_command,
            "mpi_np": mpi_value,
        }

    remote_script = f"{remote_jobs_dir}/{job_id}.sh"
    scheduler_stderr = f"{remote_jobs_dir}/{job_id}.stderr"
    script_body = _scheduler_script_body(
        scheduler_kind=scheduler_kind,
        job_id=job_id,
        remote_root=remote_root,
        remote_log=remote_log,
        scheduler_stderr=scheduler_stderr,
        mpi_np=mpi_value,
        queue_name=str(profile.get("queue_name", "") or "").strip(),
        account=str(profile.get("account", "") or "").strip(),
        walltime=str(profile.get("walltime", "") or "").strip(),
        submit_options=str(profile.get("submit_options", "") or "").strip(),
        pre_command=pre,
        system_arg=system_arg,
        step_arg=step_arg,
    )
    submit_command = {
        "slurm": f"sbatch --parsable {shlex.quote(remote_script)}",
        "pbs": f"qsub {shlex.quote(remote_script)}",
    }[scheduler_kind]
    remote_command = (
        f"mkdir -p {shlex.quote(remote_jobs_dir)} && "
        f"cat > {shlex.quote(remote_script)} <<'EOF'\n{script_body}\nEOF\n"
        f"chmod +x {shlex.quote(remote_script)}\n"
        f"{submit_command}"
    )
    return {
        "scheduler_kind": scheduler_kind,
        "submission_mode": submission_mode(profile),
        "host": host,
        "remote_root": remote_root,
        "remote_log": remote_log,
        "remote_command": remote_command,
        "remote_command_preview": submit_command,
        "remote_script": remote_script,
        "scheduler_stderr": scheduler_stderr,
        "mpi_np": mpi_value,
    }


def _scheduler_script_body(
    *,
    scheduler_kind: str,
    job_id: str,
    remote_root: str,
    remote_log: str,
    scheduler_stderr: str,
    mpi_np: int,
    queue_name: str,
    account: str,
    walltime: str,
    submit_options: str,
    pre_command: str,
    system_arg: str,
    step_arg: str,
) -> str:
    header_lines = ["#!/bin/bash"]
    if scheduler_kind == "slurm":
        header_lines.extend(
            [
                f"#SBATCH -J {job_id}",
                f"#SBATCH -o {remote_log}",
                f"#SBATCH -e {scheduler_stderr}",
                "#SBATCH -N 1",
                f"#SBATCH -n {mpi_np}",
            ]
        )
        if queue_name:
            header_lines.append(f"#SBATCH -p {queue_name}")
        if account:
            header_lines.append(f"#SBATCH -A {account}")
        if walltime:
            header_lines.append(f"#SBATCH -t {walltime}")
        if submit_options:
            header_lines.extend(line for line in submit_options.splitlines() if line.strip())
    else:
        header_lines.extend(
            [
                f"#PBS -N {job_id}",
                f"#PBS -o {remote_log}",
                f"#PBS -e {scheduler_stderr}",
                f"#PBS -l select=1:ncpus={mpi_np}",
            ]
        )
        if queue_name:
            header_lines.append(f"#PBS -q {queue_name}")
        if account:
            header_lines.append(f"#PBS -A {account}")
        if walltime:
            header_lines.append(f"#PBS -l walltime={walltime}")
        if submit_options:
            header_lines.extend(line for line in submit_options.splitlines() if line.strip())

    body_lines = [
        "set -euo pipefail",
        f"cd {shlex.quote(remote_root)}",
    ]
    if pre_command:
        body_lines.extend(line for line in pre_command.splitlines() if line.strip())
    body_lines.append(f"env VASP_MPI_NP={mpi_np} ./scripts/run_step.sh {system_arg} {step_arg}")
    return "\n".join([*header_lines, "", *body_lines])


def _parse_scheduler_job_id(stdout: str) -> str:
    if not stdout:
        return ""
    token = stdout.strip().splitlines()[-1].strip()
    if ";" in token:
        token = token.split(";", 1)[0].strip()
    return token


def _run_remote_query(host: str, command: str) -> str:
    try:
        result = subprocess.run(["ssh", host, command], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise SchedulerError(f"Remote status query timed out after {int(exc.timeout or 30)}s") from exc
    if result.returncode != 0:
        raise SchedulerError(result.stderr.strip() or result.stdout.strip() or "Remote status query failed")
    return result.stdout


def _apply_slurm_state(job: dict[str, Any], state: str) -> None:
    if state in {"PENDING", "CONFIGURING", "SUSPENDED"}:
        job["state"] = "queued"
    elif state in {"RUNNING", "COMPLETING"}:
        job["state"] = "running"
    elif state in {"COMPLETED"}:
        job["state"] = "finished"
    elif state in {"FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "BOOT_FAIL"}:
        job["state"] = "failed"


def _apply_pbs_state(job: dict[str, Any], state: str) -> None:
    if state in {"Q", "H", "W"}:
        job["state"] = "queued"
    elif state in {"R", "E", "B"}:
        job["state"] = "running"
    elif state in {"C", "F"}:
        job["state"] = "finished"
