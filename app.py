#!/usr/bin/env python3
"""AWS Architecture Mapper — Interactive Web Application & CLI Entry Point.

Scans an entire AWS environment via read-only boto3 APIs across local ~/.aws profiles
and builds hierarchical architecture diagrams using 100% official AWS Architecture Icons.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.icon_catalog import CATEGORY_META, GROUP_ICONS, ICONS_ROOT, catalog
from backend.layout_engine import compute_architecture_layout, render_standalone_png, render_standalone_svg
from backend.sample_environment import build_sample_environment
from backend.scanner import AwsEnvironmentScanner, list_aws_profiles, list_enabled_regions

PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
SNAPSHOTS_DIR = PROJECT_ROOT / "snapshots"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# In-memory scan job state & active topology store
# -----------------------------------------------------------------------------
SCAN_JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
ACTIVE_GRAPH: Dict[str, Any] = build_sample_environment().to_dict()


class ScanStartRequest(BaseModel):
    profile: str = "default"
    regions: Optional[List[str]] = None
    include_global: bool = True
    include_tag_sweep: bool = True


class LayoutRequest(BaseModel):
    hide_empty_default_vpcs: bool = True
    hide_empty_subnets: bool = False
    filter_regions: Optional[List[str]] = None
    filter_categories: Optional[List[str]] = None
    search_query: str = ""
    graph: Optional[Dict[str, Any]] = None


# -----------------------------------------------------------------------------
# FastAPI Web Server Definition
# -----------------------------------------------------------------------------
app = FastAPI(
    title="AWS Architecture Mapper",
    description="Automated AWS Environment Topology Discovery & Official AWS Icon Diagrammer",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _list_saved_snapshots() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for p in sorted(SNAPSHOTS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            stat = p.stat()
            items.append({
                "filename": p.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            })
        except Exception:
            pass
    return items[:30]


@app.get("/api/config")
def get_config() -> Dict[str, Any]:
    """Return AWS CLI profiles, official AWS category & group icon data URIs, and saved snapshots."""
    categories_out = {}
    for k, meta in CATEGORY_META.items():
        categories_out[k] = {
            **meta,
            "data_uri": catalog.get_data_uri(meta["icon_key"]),
        }
    group_icons_out = {
        k: catalog.get_data_uri(v)
        for k, v in GROUP_ICONS.items()
    }
    # Also include dark cloud logo for dark top studio header
    group_icons_out["aws_cloud_dark"] = catalog.get_data_uri("AWS-Cloud-logo_32_Dark")
    return {
        "profiles": list_aws_profiles(),
        "categories": categories_out,
        "group_icons": group_icons_out,
        "snapshots": _list_saved_snapshots(),
    }


@app.get("/api/regions")
def get_regions(profile: str = Query("default")) -> Dict[str, Any]:
    """Fetch enabled AWS regions for the selected AWS CLI profile."""
    regions, warning = list_enabled_regions(profile)
    return {
        "profile": profile,
        "regions": regions,
        "warning": warning,
    }


@app.get("/api/sample")
def get_sample_environment() -> Dict[str, Any]:
    """Load the built-in multi-region sample AWS architecture graph and layout."""
    global ACTIVE_GRAPH
    graph = build_sample_environment().to_dict()
    ACTIVE_GRAPH = graph
    layout = compute_architecture_layout(graph, hide_empty_default_vpcs=True)
    return {"graph": graph, "layout": layout}


@app.post("/api/layout")
def recompute_layout(req: LayoutRequest) -> Dict[str, Any]:
    """Recompute deterministic hierarchical layout with updated filter/display settings."""
    target_graph = req.graph if req.graph is not None else ACTIVE_GRAPH
    layout = compute_architecture_layout(
        target_graph,
        hide_empty_default_vpcs=req.hide_empty_default_vpcs,
        hide_empty_subnets=req.hide_empty_subnets,
        filter_regions=req.filter_regions,
        filter_categories=req.filter_categories,
        search_query=req.search_query,
    )
    return {"layout": layout}


@app.post("/api/scan/start")
def start_scan(req: ScanStartRequest) -> Dict[str, Any]:
    """Start a background multi-region read-only AWS environment scan."""
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        SCAN_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "message": f"Initializing AWS session for profile '{req.profile}'...",
            "completed": 0,
            "total": 10,
            "started_at": time.time(),
            "error": None,
            "snapshot_file": None,
            "graph": None,
            "layout": None,
        }

    def _worker() -> None:
        global ACTIVE_GRAPH

        def _on_progress(msg: str, done: int, total: int) -> None:
            with JOBS_LOCK:
                if job_id in SCAN_JOBS:
                    SCAN_JOBS[job_id]["message"] = msg
                    SCAN_JOBS[job_id]["completed"] = done
                    SCAN_JOBS[job_id]["total"] = total

        try:
            scanner = AwsEnvironmentScanner(
                profile=req.profile,
                regions=req.regions if req.regions else None,
                include_global=req.include_global,
                include_tag_sweep=req.include_tag_sweep,
                progress_cb=_on_progress,
            )
            env_graph = scanner.scan()
            graph_dict = env_graph.to_dict()
            ACTIVE_GRAPH = graph_dict

            # Persist snapshot to local disk
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_prof = "".join(c if c.isalnum() or c in "-_" else "_" for c in req.profile)
            snap_name = f"aws_{env_graph.account_id}_{safe_prof}_{ts}.json"
            snap_path = SNAPSHOTS_DIR / snap_name
            snap_path.write_text(json.dumps(graph_dict, indent=2), encoding="utf-8")

            layout = compute_architecture_layout(graph_dict, hide_empty_default_vpcs=True)

            with JOBS_LOCK:
                SCAN_JOBS[job_id].update({
                    "status": "completed",
                    "message": f"Scan complete — discovered {len(graph_dict['resources'])} resources across {len(graph_dict['regions_scanned'])} regions.",
                    "completed": SCAN_JOBS[job_id]["total"],
                    "snapshot_file": snap_name,
                    "graph": graph_dict,
                    "layout": layout,
                })
        except ProfileNotFound as exc:
            with JOBS_LOCK:
                SCAN_JOBS[job_id].update({
                    "status": "failed",
                    "error": f"AWS Profile not found: {exc}. Verify ~/.aws/config or ~/.aws/credentials.",
                })
        except NoCredentialsError:
            with JOBS_LOCK:
                SCAN_JOBS[job_id].update({
                    "status": "failed",
                    "error": (
                        f"No AWS credentials found for profile '{req.profile}'. "
                        f"Configure `aws configure --profile {req.profile}` or run `aws sso login --profile {req.profile}`."
                    ),
                })
        except (ClientError, BotoCoreError) as exc:
            err_str = str(exc)
            hint = ""
            if "SSO" in err_str or "Token" in err_str or "ExpiredToken" in err_str:
                hint = f" Tip: Run `aws sso login --profile {req.profile}` in your terminal and retry."
            with JOBS_LOCK:
                SCAN_JOBS[job_id].update({
                    "status": "failed",
                    "error": f"AWS Authentication / API Error: {err_str}.{hint}",
                })
        except Exception as exc:
            with JOBS_LOCK:
                SCAN_JOBS[job_id].update({
                    "status": "failed",
                    "error": f"Unexpected scan error: {exc}",
                })

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"job_id": job_id}


@app.get("/api/scan/status/{job_id}")
def get_scan_status(job_id: str) -> Dict[str, Any]:
    with JOBS_LOCK:
        job = SCAN_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Scan job not found")
        resp = {
            "job_id": job["job_id"],
            "status": job["status"],
            "message": job["message"],
            "completed": job["completed"],
            "total": job["total"],
            "error": job["error"],
            "snapshot_file": job["snapshot_file"],
        }
        if job["status"] == "completed":
            resp["graph"] = job["graph"]
            resp["layout"] = job["layout"]
            resp["snapshots"] = _list_saved_snapshots()
        return resp


@app.get("/api/snapshots")
def get_snapshots() -> Dict[str, Any]:
    return {"snapshots": _list_saved_snapshots()}


@app.get("/api/snapshots/{filename}")
def load_snapshot(filename: str) -> Dict[str, Any]:
    global ACTIVE_GRAPH
    safe_name = Path(filename).name
    path = SNAPSHOTS_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Snapshot file not found")
    graph_dict = json.loads(path.read_text(encoding="utf-8"))
    ACTIVE_GRAPH = graph_dict
    layout = compute_architecture_layout(graph_dict, hide_empty_default_vpcs=True)
    return {"graph": graph_dict, "layout": layout, "filename": safe_name}


# Mount official AWS SVG assets and frontend web app
app.mount("/assets/aws-icons", StaticFiles(directory=str(ICONS_ROOT)), name="aws-icons")


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "index.html"))


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


# -----------------------------------------------------------------------------
# Headless CLI Mode (`python3 app.py --cli ...`)
# -----------------------------------------------------------------------------
def _export_png_from_svg_macos(svg_path: Path, png_path: Path) -> bool:
    """Attempt high-res PNG conversion on macOS via qlmanage or Chrome headless."""
    if shutil.which("qlmanage"):
        with tempfile.TemporaryDirectory() as tmpdir:
            res = subprocess.run(
                ["qlmanage", "-t", "-s", "3200", "-o", tmpdir, str(svg_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            candidate = Path(tmpdir) / f"{svg_path.name}.png"
            if res.returncode == 0 and candidate.exists():
                shutil.copyfile(candidate, png_path)
                return True
    return False


def run_cli(args: argparse.Namespace) -> int:
    if args.sample:
        print("[*] Generating diagram from sample multi-region AWS environment...")
        graph_dict = build_sample_environment().to_dict()
    else:
        regions = [r.strip() for r in args.regions.split(",") if r.strip()] if args.regions else None
        print(f"[*] Scanning AWS environment using profile '{args.profile}'...")

        def _cli_progress(msg: str, done: int, total: int) -> None:
            pct = int((done / max(total, 1)) * 100)
            print(f"    [{pct:3d}%] {msg}")

        try:
            scanner = AwsEnvironmentScanner(
                profile=args.profile,
                regions=regions,
                include_global=not args.no_global,
                include_tag_sweep=not args.no_tag_sweep,
                progress_cb=_cli_progress,
            )
            graph_dict = scanner.scan().to_dict()
        except Exception as exc:
            print(f"[!] AWS Scan failed: {exc}", file=sys.stderr)
            return 1

    print(
        f"[+] Topology ready: {len(graph_dict['resources'])} resources, "
        f"{len(graph_dict['containers'])} containers, {len(graph_dict['edges'])} edges."
    )

    if args.output_json:
        jpath = Path(args.output_json).expanduser().resolve()
        jpath.parent.mkdir(parents=True, exist_ok=True)
        jpath.write_text(json.dumps(graph_dict, indent=2), encoding="utf-8")
        print(f"[+] Saved topology JSON -> {jpath}")

    layout = compute_architecture_layout(
        graph_dict,
        hide_empty_default_vpcs=not args.show_empty_default_vpcs,
    )
    svg_str = render_standalone_svg(layout, show_edges=not args.hide_edges)

    svg_out = Path(args.output_svg or "aws-architecture.svg").expanduser().resolve()
    svg_out.parent.mkdir(parents=True, exist_ok=True)
    svg_out.write_text(svg_str, encoding="utf-8")
    print(f"[+] Saved standalone Official AWS Icon SVG -> {svg_out}")

    if args.output_png:
        png_out = Path(args.output_png).expanduser().resolve()
        png_out.parent.mkdir(parents=True, exist_ok=True)
        render_standalone_png(layout, png_out, scale=2.0, show_edges=not args.hide_edges)
        print(f"[+] Saved high-resolution 2x Retina Official AWS Icon PNG -> {png_out}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AWS Architecture Mapper — Interactive Web App & CLI Scanner (100% Official AWS Icons)"
    )
    parser.add_argument("--cli", action="store_true", help="Run in headless CLI mode instead of starting the Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Web server bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8420, help="Web server port (default: 8420)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser on startup")

    # CLI flags
    parser.add_argument("--profile", default="default", help="AWS CLI profile name from ~/.aws (or __env__)")
    parser.add_argument("--regions", default="", help="Comma-separated AWS regions (omit for all enabled regions)")
    parser.add_argument("--sample", action="store_true", help="Use built-in sample AWS environment in CLI mode")
    parser.add_argument("--output-svg", default="aws-architecture.svg", help="Output SVG file path (CLI mode)")
    parser.add_argument("--output-png", default="", help="Optional output PNG file path (CLI mode)")
    parser.add_argument("--output-json", default="", help="Optional output JSON snapshot path (CLI mode)")
    parser.add_argument("--no-global", action="store_true", help="Skip global Route53/CloudFront/S3 scan")
    parser.add_argument("--no-tag-sweep", action="store_true", help="Skip Resource Groups Tagging API sweep")
    parser.add_argument("--show-empty-default-vpcs", action="store_true", help="Include empty default VPCs")
    parser.add_argument("--hide-edges", action="store_true", help="Omit connection arrows in CLI SVG")

    args = parser.parse_args()

    if args.cli:
        sys.exit(run_cli(args))

    url = f"http://{args.host}:{args.port}"
    print(f"\n  AWS Architecture Mapper running at: {url}")
    print("  Using 100% Official AWS Architecture Icons (assets/aws-icons/)\n")

    if not args.no_browser:
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
