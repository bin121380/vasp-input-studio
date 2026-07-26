#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "index.html"
VERSION_PATH = PROJECT_ROOT / "VERSION"
DEFAULT_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "docs"
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

# Optional local sample-project names for baseline snapshots, injected via
# environment variables so machine-specific project names stay out of the repo.
# Example: VASP_STUDIO_BASELINE_PRIMARY=MgO ./scripts/refresh_ui_baseline.py
PREFERRED_SAMPLE_SYSTEMS: dict[str, str] = {
    role: name
    for role, name in {
        "primary_full_feature": os.environ.get("VASP_STUDIO_BASELINE_PRIMARY", "Si"),
        "fallback_dos_unstable_phonon": os.environ.get("VASP_STUDIO_BASELINE_FALLBACK", "MgO"),
        "partial_workflow": os.environ.get("VASP_STUDIO_BASELINE_PARTIAL", ""),
        "high_risk_submission": os.environ.get("VASP_STUDIO_BASELINE_HIGH_RISK", ""),
    }.items()
    if name
}


@dataclass
class PanelRecord:
    title: str
    view: str | None
    sidebar: bool
    flow_panels: list[str]
    complexity: str | None


class TemplateStructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.element_stack: list[str] = []
        self.section_stack: list[str | None] = []
        self.current_views: list[str] = []
        self.panel_stack: list[dict[str, Any]] = []
        self.current_sidebar_depth = 0
        self.capture_h2: list[str] | None = None
        self.capture_button_label: list[str] | None = None
        self.capture_setup_note: list[str] | None = None
        self.current_select_id: str | None = None

        self.views: list[str] = []
        self.view_buttons: list[str] = []
        self.workflow_tabs: list[str] = []
        self.panels: list[PanelRecord] = []
        self.material_classes: list[str] = []
        self.electronic_types: list[str] = []
        self.setup_note: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: (value or "") for key, value in attrs}
        classes = set(attr.get("class", "").split())

        if tag not in VOID_TAGS:
            self.element_stack.append(tag)

        if tag == "aside" and "sidebar" in classes:
            self.current_sidebar_depth += 1

        view_name = None
        if tag == "section":
            raw_id = attr.get("id", "")
            if raw_id.startswith("view-"):
                view_name = raw_id.removeprefix("view-")
                self.views.append(view_name)
                self.current_views.append(view_name)
            self.section_stack.append(view_name)

        if "panel" in classes:
            flow_panels = [
                item.strip()
                for item in attr.get("data-flow-panels", "").split(",")
                if item.strip()
            ]
            self.panel_stack.append(
                {
                    "tag": tag,
                    "depth": len(self.element_stack),
                    "titles": [],
                    "view": self.current_views[-1] if self.current_views else None,
                    "sidebar": bool(self.current_sidebar_depth),
                    "flow_panels": flow_panels,
                    "complexity": attr.get("data-complexity") or None,
                }
            )

        if tag == "h2" and self.panel_stack:
            self.capture_h2 = []

        if tag == "button":
            if "data-view-target" in attr:
                self.capture_button_label = []
            elif "data-flow-target" in attr:
                self.capture_button_label = []

        if tag == "select":
            self.current_select_id = attr.get("id") or None

        if tag == "option" and self.current_select_id in {"material-class", "material-electronic-type"}:
            self.capture_button_label = []

        if tag == "p":
            classes = set(attr.get("class", "").split())
            if "beginner-advanced" in classes and "full" in classes:
                self.capture_setup_note = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self.capture_h2 is not None:
            title = "".join(self.capture_h2).strip()
            if title and self.panel_stack and title not in self.panel_stack[-1]["titles"]:
                self.panel_stack[-1]["titles"].append(title)
            self.capture_h2 = None

        if tag == "button" and self.capture_button_label is not None:
            label = "".join(self.capture_button_label).strip()
            if label:
                if self.element_stack and "button" in self.element_stack:
                    if label in {"Workbench", "Project Lab"} and label not in self.view_buttons:
                        self.view_buttons.append(label)
                    elif label not in self.workflow_tabs and label in {
                        "Overview",
                        "Setup",
                        "Relax",
                        "SCF",
                        "DOS",
                        "Converge",
                        "Band",
                        "Elastic",
                        "Charge",
                        "Phonon",
                    }:
                        self.workflow_tabs.append(label)
            self.capture_button_label = None

        if tag == "option" and self.capture_button_label is not None:
            label = "".join(self.capture_button_label).strip()
            if label:
                if self.current_select_id == "material-class":
                    self.material_classes.append(label)
                elif self.current_select_id == "material-electronic-type":
                    self.electronic_types.append(label)
            self.capture_button_label = None

        if tag == "p" and self.capture_setup_note is not None:
            note = " ".join("".join(self.capture_setup_note).split())
            if note and "Hybrid `band`" in note:
                self.setup_note = note
            self.capture_setup_note = None

        if self.panel_stack:
            panel = self.panel_stack[-1]
            if panel["tag"] == tag and panel["depth"] == len(self.element_stack):
                finished = self.panel_stack.pop()
                titles = [str(item).strip() for item in finished.get("titles") or [] if str(item).strip()]
                for title in titles:
                    self.panels.append(
                        PanelRecord(
                            title=title,
                            view=finished["view"],
                            sidebar=bool(finished["sidebar"]),
                            flow_panels=list(finished["flow_panels"]),
                            complexity=finished["complexity"],
                        )
                    )

        if tag == "select":
            self.current_select_id = None

        if tag == "section" and self.section_stack:
            view_name = self.section_stack.pop()
            if view_name and self.current_views and self.current_views[-1] == view_name:
                self.current_views.pop()

        if tag == "aside" and self.current_sidebar_depth:
            self.current_sidebar_depth -= 1

        if tag not in VOID_TAGS and self.element_stack:
            self.element_stack.pop()

    def handle_data(self, data: str) -> None:
        if self.capture_h2 is not None:
            self.capture_h2.append(data)
        if self.capture_button_label is not None:
            self.capture_button_label.append(data)
        if self.capture_setup_note is not None:
            self.capture_setup_note.append(data)


def fetch_json(base_url: str, path: str, *, payload: dict[str, Any] | None = None) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    body: bytes | None = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"

    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore").strip()
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def summarize_panel(panel: PanelRecord) -> dict[str, Any]:
    return {
        "title": panel.title,
        "view": panel.view,
        "sidebar": panel.sidebar,
        "flow_panels": panel.flow_panels,
        "complexity": panel.complexity,
    }


def parse_template_structure() -> dict[str, Any]:
    parser = TemplateStructureParser()
    parser.feed(TEMPLATE_PATH.read_text(encoding="utf-8"))
    workbench_panels = [summarize_panel(panel) for panel in parser.panels if panel.view == "workbench"]
    project_lab_panels = [summarize_panel(panel) for panel in parser.panels if panel.view == "project-lab"]
    sidebar_panels = [summarize_panel(panel) for panel in parser.panels if panel.sidebar]
    return {
        "views": parser.views,
        "view_buttons": parser.view_buttons,
        "workflow_tabs": parser.workflow_tabs,
        "sidebar_panels": sidebar_panels,
        "workbench_panels": workbench_panels,
        "project_lab_panels": project_lab_panels,
        "material_classes": parser.material_classes,
        "electronic_types": parser.electronic_types,
        "setup_note": parser.setup_note,
    }


def choose_representative_systems(systems: list[dict[str, Any]]) -> dict[str, str]:
    available = {str(item.get("name") or "") for item in systems}
    selected: dict[str, str] = {}
    for role, name in PREFERRED_SAMPLE_SYSTEMS.items():
        if name in available:
            selected[role] = name
    if selected:
        return selected
    return {}


def summarize_detail_payload(detail: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "metadata": {
            "material_class": detail.get("metadata", {}).get("material_class"),
            "electronic_type": detail.get("metadata", {}).get("electronic_type"),
            "spin_polarized": detail.get("metadata", {}).get("spin_polarized"),
            "xc_electronic": detail.get("metadata", {}).get("xc_electronic"),
        },
        "steps": detail.get("steps"),
        "preview_files_count": len(detail.get("preview_files") or []),
    }
    for key in (
        "band_visualization",
        "dos_visualization",
        "phonon_visualization",
        "phonon_dos_visualization",
    ):
        payload = detail.get(key)
        if isinstance(payload, dict):
            summary[key] = {
                "mode": payload.get("mode"),
                "status": payload.get("status"),
                "title": payload.get("title"),
                "note": payload.get("note"),
                "artifacts": payload.get("artifacts"),
                "projected_elements": payload.get("projected_elements"),
                "panel_count": len(payload.get("panels") or []),
                "mesh": payload.get("mesh"),
            }
    dos_artifacts = detail.get("dos_artifacts")
    if isinstance(dos_artifacts, dict):
        summary["dos_artifacts"] = {
            "status": dos_artifacts.get("status"),
            "elements": dos_artifacts.get("elements"),
            "pdos_files": len(dos_artifacts.get("pdos_files") or []),
            "ipdos_files": len(dos_artifacts.get("ipdos_files") or []),
        }
    system_audit = detail.get("system_audit")
    if isinstance(system_audit, dict):
        summary["system_audit"] = {
            "overall": system_audit.get("overall"),
            "counts": system_audit.get("counts"),
        }
    report_snapshot = detail.get("report_snapshot")
    if isinstance(report_snapshot, dict):
        summary["report_snapshot"] = {
            "workflow_completed": report_snapshot.get("workflow_completed"),
            "workflow_total": report_snapshot.get("workflow_total"),
            "metrics": report_snapshot.get("metrics"),
        }
    return summary


def build_validation_cases(selected: dict[str, str]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    primary = selected.get("primary_full_feature")
    fallback = selected.get("fallback_dos_unstable_phonon")
    risk = selected.get("high_risk_submission")
    if primary:
        cases.append({"system": primary, "step": "band", "target": "local", "mpi_np": 1})
        cases.append({"system": primary, "step": "phonon", "target": "local", "mpi_np": 1})
    if fallback:
        cases.append({"system": fallback, "step": "dos", "target": "local", "mpi_np": 1})
    if risk:
        cases.append({"system": risk, "step": "band", "target": "local", "mpi_np": 1})
    return cases


def collect_snapshot(base_url: str) -> dict[str, Any]:
    overview = fetch_json(base_url, "/api/overview")
    systems = fetch_json(base_url, "/api/systems")
    if not isinstance(systems, list):
        raise RuntimeError("/api/systems did not return a list payload")

    template_structure = parse_template_structure()
    selected = choose_representative_systems(systems)

    system_payloads: dict[str, Any] = {}
    for _, system_name in selected.items():
        detail = fetch_json(base_url, f"/api/systems/{system_name}")
        system_payloads[system_name] = summarize_detail_payload(detail)

    validation_cases = build_validation_cases(selected)
    validation_results = []
    for case in validation_cases:
        result = fetch_json(base_url, "/api/jobs/validate-submit", payload=case)
        validation_results.append(
            {
                **case,
                "readiness": result.get("readiness"),
                "risk_level": result.get("risk_level"),
                "warnings": result.get("warnings"),
                "blocking_errors": result.get("blocking_errors"),
            }
        )

    version = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else None
    return {
        "capture_date": date.today().isoformat(),
        "app_version_file": version,
        "overview": overview,
        "template_structure": template_structure,
        "systems_index": systems,
        "selected_samples": selected,
        "sample_payloads": system_payloads,
        "validation_results": validation_results,
    }


def format_flow_scope(flow_panels: list[str]) -> str:
    if not flow_panels:
        return "global"
    return ", ".join(flow_panels)


def coverage_gap(snapshot: dict[str, Any]) -> list[str]:
    present = {
        str((item.get("metadata") or {}).get("material_class") or "").lower()
        for item in snapshot["sample_payloads"].values()
    }
    all_known = {"bulk", "2d", "slab", "molecule"}
    return sorted(item for item in all_known if item not in present)


def render_markdown(snapshot: dict[str, Any], base_url: str) -> str:
    overview = snapshot["overview"]
    template = snapshot["template_structure"]
    selected = snapshot["selected_samples"]
    sample_payloads = snapshot["sample_payloads"]
    validation_results = snapshot["validation_results"]

    lines: list[str] = []
    lines.append(f"# UI Baseline Snapshot ({snapshot['capture_date']})")
    lines.append("")
    lines.append("This file is generated by `scripts/refresh_ui_baseline.py` from the live API and current template files.")
    lines.append("")
    lines.append("## Capture Scope")
    lines.append("")
    lines.append(f"- Capture date: `{snapshot['capture_date']}`")
    lines.append(f"- Base URL: `{base_url}`")
    lines.append(f"- App version (API): `{overview.get('app_version')}`")
    lines.append(f"- App version (VERSION file): `{snapshot.get('app_version_file')}`")
    lines.append(f"- Workspace root: `{overview.get('workspace_root')}`")
    lines.append(f"- Systems count: `{overview.get('systems_count')}`")
    lines.append(f"- Backend provider: `{overview.get('backend_provider')}`")
    lines.append(f"- AiiDA available: `{overview.get('aiida_available')}`")
    lines.append(f"- AiiDA profile: `{overview.get('aiida_profile')}`")
    lines.append("- Feature flags:")
    for key, value in sorted((overview.get("features") or {}).items()):
        lines.append(f"  - `{key} = {value}`")

    lines.append("")
    lines.append("## Template Structure")
    lines.append("")
    lines.append("### Main Views")
    lines.append("")
    for view in template["views"]:
        lines.append(f"- `{view}`")

    lines.append("")
    lines.append("### Sidebar Panels")
    lines.append("")
    for panel in template["sidebar_panels"]:
        complexity = f" [{panel['complexity']}]" if panel["complexity"] else ""
        lines.append(f"- `{panel['title']}`{complexity}")

    lines.append("")
    lines.append("### Workflow Tabs")
    lines.append("")
    for index, label in enumerate(template["workflow_tabs"], start=1):
        lines.append(f"{index}. `{label}`")

    lines.append("")
    lines.append("### Workbench Panels")
    lines.append("")
    for panel in template["workbench_panels"]:
        complexity = f" | complexity={panel['complexity']}" if panel["complexity"] else ""
        lines.append(f"- `{panel['title']}` | flows={format_flow_scope(panel['flow_panels'])}{complexity}")

    lines.append("")
    lines.append("### Project Lab Panels")
    lines.append("")
    for panel in template["project_lab_panels"]:
        complexity = f" | complexity={panel['complexity']}" if panel["complexity"] else ""
        lines.append(f"- `{panel['title']}`{complexity}")

    lines.append("")
    lines.append("### Setup Baseline")
    lines.append("")
    lines.append("- Material classes from template:")
    for item in template["material_classes"]:
        lines.append(f"  - `{item}`")
    lines.append("- Electronic type options from template:")
    for item in template["electronic_types"]:
        lines.append(f"  - `{item}`")
    sample_name = next(iter(sample_payloads.keys()), None)
    if sample_name:
        detail = fetch_json(base_url, f"/api/systems/{sample_name}")
        material_settings = detail.get("material_settings") or {}
        lines.append("- Geometry XC options from live payload:")
        for item in material_settings.get("xc_geometry_options") or []:
            lines.append(f"  - `{item.get('value')}`")
        lines.append("- Electronic XC options from live payload:")
        for item in material_settings.get("xc_electronic_options") or []:
            lines.append(f"  - `{item.get('value')}`")
    if template.get("setup_note"):
        lines.append(f"- Setup note: {template['setup_note']}")

    lines.append("")
    lines.append("## Representative Regression Samples")
    lines.append("")
    if not selected:
        lines.append("- No preferred sample systems were found in the current workspace.")
    else:
        for role, system_name in selected.items():
            summary = next((item for item in snapshot["systems_index"] if item.get("name") == system_name), {})
            lines.append(f"- `{role}` -> `{system_name}`")
            lines.append(f"  - steps: `{stable_json(summary.get('steps'))}`")
            lines.append(f"  - summary: `{stable_json(summary.get('summary'))}`")
    gap = coverage_gap(snapshot)
    if gap:
        lines.append("- Coverage gaps in selected samples:")
        for item in gap:
            lines.append(f"  - `{item}`")

    lines.append("")
    lines.append("## Sample Payload Baseline")
    lines.append("")
    for system_name, payload in sample_payloads.items():
        lines.append(f"### `{system_name}`")
        lines.append("")
        lines.append(f"- Metadata: `{stable_json(payload.get('metadata'))}`")
        lines.append(f"- Steps: `{stable_json(payload.get('steps'))}`")
        lines.append(f"- Preview files count: `{payload.get('preview_files_count')}`")
        for key in ("band_visualization", "dos_visualization", "phonon_visualization", "phonon_dos_visualization"):
            if key in payload:
                lines.append(f"- {key}: `{stable_json(payload[key])}`")
        if "dos_artifacts" in payload:
            lines.append(f"- dos_artifacts: `{stable_json(payload['dos_artifacts'])}`")
        if "system_audit" in payload:
            lines.append(f"- system_audit: `{stable_json(payload['system_audit'])}`")
        if "report_snapshot" in payload:
            lines.append(f"- report_snapshot: `{stable_json(payload['report_snapshot'])}`")
        lines.append("")

    lines.append("## Submission Validation Baseline")
    lines.append("")
    for case in validation_results:
        lines.append(
            f"- `{case['system']}` -> `{case['step']}` -> `{case['target']}` -> `mpi_np={case['mpi_np']}`: "
            f"`readiness={case['readiness']}` | `risk_level={case['risk_level']}`"
        )
        lines.append(f"  - warnings: `{stable_json(case.get('warnings'))}`")
        lines.append(f"  - blocking_errors: `{stable_json(case.get('blocking_errors'))}`")

    lines.append("")
    lines.append("## Regression Checklist")
    lines.append("")
    lines.append("1. Confirm `GET /api/overview` and `GET /api/systems` still return valid JSON.")
    lines.append("2. Open the primary full-feature sample and verify Band, DOS, PDOS, phonon dispersion, and phonon DOS panels still render.")
    lines.append("3. Re-run the submission validation cases and diff warning/blocking text against this file.")
    lines.append("4. Re-check any setup note or workflow wording that intentionally changed.")
    lines.append("5. If behavior changed intentionally, regenerate this file and review the diff before accepting it as the new baseline.")
    lines.append("")
    return "\n".join(lines)


def write_outputs(snapshot: dict[str, Any], markdown: str, output_dir: Path, write_dated_copy: bool) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    latest_json = output_dir / "ui-baseline-latest.json"
    latest_md = output_dir / "ui-baseline-latest.md"
    latest_json.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_md.write_text(markdown, encoding="utf-8")
    written.extend([latest_json, latest_md])

    if write_dated_copy:
        stamp = snapshot["capture_date"]
        dated_json = output_dir / f"ui-baseline-{stamp}.json"
        dated_md = output_dir / f"ui-baseline-{stamp}.md"
        dated_json.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        dated_md.write_text(markdown, encoding="utf-8")
        written.extend([dated_json, dated_md])

    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh the UI/API regression baseline snapshot.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"UI API base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for generated baseline files")
    parser.add_argument("--dated-copy", action="store_true", help="Also write ui-baseline-YYYY-MM-DD.{md,json}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_url = str(args.base_url).rstrip("/")
    output_dir = Path(args.output_dir).resolve()
    snapshot = collect_snapshot(base_url)
    markdown = render_markdown(snapshot, base_url)
    written = write_outputs(snapshot, markdown, output_dir, write_dated_copy=bool(args.dated_copy))
    print("Wrote baseline files:")
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
