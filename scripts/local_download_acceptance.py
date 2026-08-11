"""Exercise every customer download family against completed local job data.

Requires a short-lived local API key plus explicit fixture IDs. The caller owns
credential creation/revocation so the raw key never needs to be printed.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import httpx

from kanopy import Kanopy


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def members_with_suffix(archive: zipfile.ZipFile, suffix: str) -> list[str]:
    return [name for name in archive.namelist() if name.endswith(suffix)]


def ply_vertex_properties(archive: zipfile.ZipFile, member: str) -> set[str]:
    properties: set[str] = set()
    with archive.open(member) as source:
        for raw_line in source:
            line = raw_line.decode("ascii", errors="strict").strip()
            if line == "end_header":
                return properties
            if line.startswith("property "):
                properties.add(line.rsplit(" ", 1)[-1])
    raise AssertionError(f"{member} has no PLY end_header")


def assert_csv(path: Path, minimum_rows: int = 1) -> None:
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    if len(rows) < minimum_rows:
        raise AssertionError(f"{path.name} contains {len(rows)} data rows")


api_key = required_env("ACCEPTANCE_API_KEY")
base_url = required_env("ACCEPTANCE_BASE_URL")
project_id = required_env("ACCEPTANCE_PROJECT_ID")
job_id = required_env("ACCEPTANCE_JOB_ID")
georeferenced_job_id = required_env("ACCEPTANCE_GEO_JOB_ID")
target_epsg = int(os.environ.get("ACCEPTANCE_EPSG", "26919"))

timeout = httpx.Timeout(30 * 60, connect=30)
with tempfile.TemporaryDirectory(prefix="kanopy-download-acceptance-") as tmp:
    output = Path(tmp)
    with Kanopy(api_key, base_url=base_url, timeout=timeout) as kanopy:
        identity = kanopy.get_identity()
        project = kanopy.get_project(project_id)
        job = kanopy.get_job(job_id)
        geo_job = kanopy.get_job(georeferenced_job_id)
        assert identity.get("id")
        assert str(project.get("id")) == project_id
        assert str(job.get("status", "")).lower() == "completed"
        assert str(geo_job.get("status", "")).lower() == "completed"

        complete_zip = kanopy.download_job_folder(job_id, output / "job.zip")
        with zipfile.ZipFile(complete_zip) as archive:
            assert members_with_suffix(archive, "/summary.txt")
            assert members_with_suffix(archive, "/analytics/trees.csv")
            assert members_with_suffix(archive, "/analytics/poles.csv")
            assert members_with_suffix(archive, "/analytics/spans.csv")
            assert any("/camera/" in name for name in archive.namelist())
            merged = [
                name
                for name in archive.namelist()
                if "/point_clouds/segmented/merged_point_cloud" in name
                and name.endswith(".ply")
            ]
            assert merged, "complete job ZIP has no merged segmented point cloud"
            properties = ply_vertex_properties(archive, merged[0])
            coordinate_and_color = {"x", "y", "z", "red", "green", "blue"}
            embedded_fields = properties - coordinate_and_color
            assert embedded_fields, "merged PLY has no embedded vertex fields"

        geo_zip = kanopy.download_job_folder(
            georeferenced_job_id,
            output / "point-clouds.zip",
            include="point_cloud",
            point_cloud_epsg=target_epsg,
        )
        with zipfile.ZipFile(geo_zip) as archive:
            names = archive.namelist()
            assert any(name.endswith(".las") for name in names)
            assert any(name.endswith(f"_epsg{target_epsg}.ply") for name in names)
            reports = members_with_suffix(archive, "/registration_report.txt")
            assert reports
            report = archive.read(reports[0]).decode("utf-8")
            assert f"EPSG:{target_epsg}" in report

        for table in ("trees", "poles", "spans"):
            table_path = kanopy.download_job_table(
                job_id, table, output / f"job-{table}.csv"
            )
            assert_csv(table_path)

        project_exports = (
            ("trees", "csv"),
            ("trees", "json"),
            ("trees", "kml"),
            ("trees", "geojson"),
            ("poles", "csv"),
            ("poles", "json"),
            ("spans", "csv"),
            ("spans", "json"),
        )
        for table, format_name in project_exports:
            destination = output / f"project-{table}.{format_name}"
            kanopy.download_project_table(
                project_id, table, destination, format=format_name
            )
            assert destination.stat().st_size > 0
        assert json.loads((output / "project-trees.geojson").read_text())["features"]
        assert "<Placemark>" in (output / "project-trees.kml").read_text()

        tree_payload = kanopy.list_project_trees(project_id, job_id=job_id, limit=1)
        pole_payload = kanopy.list_project_poles(project_id, job_id=job_id, limit=1)
        tree_id = str(tree_payload["trees"][0]["tree_id"])
        pole_id = str(pole_payload["poles"][0]["pole_id"])
        tree_pdf = kanopy.download_tree_report(
            project_id, tree_id, output / "tree-analysis.pdf"
        )
        pole_pdf = kanopy.download_pole_report(
            project_id, pole_id, output / "pole-analysis.pdf"
        )
        assert tree_pdf.read_bytes().startswith(b"%PDF-")
        assert pole_pdf.read_bytes().startswith(b"%PDF-")

        counts = kanopy.get_project_object_counts(project_id, job_ids=[job_id])
        assert counts["trees"][job_id] > 0
        assert counts["poles"][job_id] > 0

        audit_csv = kanopy.download_audit_events(output / "audit-events.csv")
        staff_csv = kanopy.download_audit_events(
            output / "staff-access.csv", staff_only=True
        )
        assert audit_csv.read_text().startswith("created_at,action,")
        assert staff_csv.read_text().startswith("created_at,action,")

        project_zip = kanopy.download_project_export(
            project_id,
            output / "project.zip",
            timeout=30 * 60,
            poll_interval=2,
        )
        with zipfile.ZipFile(project_zip) as archive:
            summaries = members_with_suffix(archive, "/summary.csv")
            assert len(summaries) == 1
            summary = list(
                csv.DictReader(io.StringIO(archive.read(summaries[0]).decode("utf-8")))
            )
            assert summary

print(
    "PASS: completed-job archives, embedded PLY fields, requested-CRS point clouds, "
    "job/project analytics, portable formats, PDFs, counts, audit exports, and async "
    "project export"
)
