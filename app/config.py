from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_VERSION = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip() if (PROJECT_ROOT / "VERSION").exists() else "0.0.0"


class Settings(BaseSettings):
    app_name: str = "VASP Input Studio"
    app_version: str = APP_VERSION
    project_root: Path = PROJECT_ROOT
    workspace_root: Path = PROJECT_ROOT / "data"
    runtime_dir: Path = PROJECT_ROOT / "runtime"
    resources_root: Path = PROJECT_ROOT / "resources"
    incar_template_dir: Path = PROJECT_ROOT / "resources" / "incar_templates"
    runner_script_dir: Path = PROJECT_ROOT / "resources" / "scripts"
    vasp_root: Path | None = None
    vasp_distribution_root: Path | None = None
    vasp_env_script: Path | None = None
    vaspkit_cmd: Path | None = None
    potcar_archive_pbe_64: Path | None = None
    vasp_mpi_np: int = 1
    backend_provider: str = "standalone"
    enable_aiida_backend: bool = False
    aiida_profile_name: str = "vasp_studio_pg"
    aiida_rest_api_url: str = ""
    aiida_localhost_label: str = "localhost"
    aiida_code_std_label: str = "vasp_std_localhost"
    aiida_code_gam_label: str = "vasp_gam_localhost"
    aiida_code_ncl_label: str = "vasp_ncl_localhost"
    aiida_potcar_family_name: str = "PBE_64"

    model_config = SettingsConfigDict(
        env_prefix="VASP_STUDIO_",
        extra="ignore",
        env_file=str(PROJECT_ROOT / ".env.local"),
    )

    def model_post_init(self, __context: Any) -> None:
        path_fields = (
            "project_root",
            "workspace_root",
            "runtime_dir",
            "resources_root",
            "incar_template_dir",
            "runner_script_dir",
            "vasp_root",
            "vasp_distribution_root",
            "vasp_env_script",
            "vaspkit_cmd",
            "potcar_archive_pbe_64",
        )
        for field_name in path_fields:
            raw_value = getattr(self, field_name)
            if raw_value is None:
                continue
            setattr(self, field_name, Path(raw_value).expanduser())

        for directory in (
            self.workspace_root,
            self.workspace_root / "systems",
            self.workspace_root / "results",
            self.workspace_root / "scripts",
            self.runtime_dir,
            self.resources_root,
            self.incar_template_dir,
            self.runner_script_dir,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SystemExit(
                    f"Cannot create required directory {directory}: {exc}. "
                    "Check VASP_STUDIO_WORKSPACE_ROOT / VASP_STUDIO_RUNTIME_DIR in .env.local."
                ) from exc


settings = Settings()
