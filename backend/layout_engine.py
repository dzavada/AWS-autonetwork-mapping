"""Deterministic Hierarchical AWS Architecture Layout Engine & Standalone SVG Renderer.

Uses 100% official AWS Architecture Icons (Service, Resource, Category, and Group SVGs)
and follows official AWS Architecture Diagramming Guidelines for container hierarchy & colors.
"""

from __future__ import annotations

import html
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from PIL import Image, ImageDraw, ImageFont

from .icon_catalog import CATEGORY_META, GROUP_ICONS, catalog

# Official AWS Architecture Group Visual Styles
CONTAINER_STYLES: Dict[str, Dict[str, Any]] = {
    "aws_cloud": {
        "stroke": "#232F3E",
        "fill": "#F8FAFC",
        "dash": "",
        "stroke_width": 2.2,
        "radius": 10,
        "title_color": "#232F3E",
        "icon_size": 28,
    },
    "account": {
        "stroke": "#CD2264",
        "fill": "#FFFFFF",
        "dash": "",
        "stroke_width": 1.6,
        "radius": 8,
        "title_color": "#A3164D",
        "icon_size": 24,
    },
    "region": {
        "stroke": "#00A4A6",
        "fill": "#FCFEFF",
        "dash": "7,4",
        "stroke_width": 1.8,
        "radius": 8,
        "title_color": "#007E80",
        "icon_size": 24,
    },
    "vpc": {
        "stroke": "#8C4FFF",
        "fill": "#FAF8FF",
        "dash": "",
        "stroke_width": 2.0,
        "radius": 8,
        "title_color": "#6933CC",
        "icon_size": 26,
    },
    "public_subnet": {
        "stroke": "#7AA116",
        "fill": "#F5FAF0",
        "dash": "",
        "stroke_width": 1.5,
        "radius": 6,
        "title_color": "#4D6B08",
        "icon_size": 22,
    },
    "private_subnet": {
        "stroke": "#00A4A6",
        "fill": "#F1F9FB",
        "dash": "",
        "stroke_width": 1.5,
        "radius": 6,
        "title_color": "#00687A",
        "icon_size": 22,
    },
    "category_box": {
        "stroke": "#D5DBDB",
        "fill": "#FFFFFF",
        "dash": "4,3",
        "stroke_width": 1.2,
        "radius": 6,
        "title_color": "#232F3E",
        "icon_size": 20,
    },
}

EDGE_STYLES: Dict[str, Dict[str, str]] = {
    "routing": {"color": "#8C4FFF", "dash": "", "label": "DNS / Edge Routing"},
    "traffic": {"color": "#147EBA", "dash": "", "label": "Network / HTTP Traffic"},
    "event": {"color": "#E7157B", "dash": "5,3", "label": "Async Event / Trigger"},
    "storage": {"color": "#7AA116", "dash": "4,3", "label": "Data / Storage Access"},
    "security_group": {"color": "#ED7100", "dash": "3,3", "label": "Security Group / Policy Flow"},
}

CATEGORY_ORDER = [
    "Networking",
    "Serverless",
    "Integration",
    "Containers",
    "Compute",
    "Databases",
    "Storage",
    "AI/ML",
    "Analytics",
    "Security",
    "Management",
]

NODE_W = 138
NODE_H = 104
NODE_GAP_X = 34
NODE_GAP_Y = 28


def _grid_dimensions(count: int, max_cols: int) -> Tuple[int, int, int, int]:
    """Return (cols, rows, width, height) for a grid of `count` resource cards."""
    if count <= 0:
        return 0, 0, 0, 0
    cols = min(count, max_cols)
    rows = (count + cols - 1) // cols
    w = cols * NODE_W + (cols - 1) * NODE_GAP_X
    h = rows * NODE_H + (rows - 1) * NODE_GAP_Y
    return cols, rows, w, h


def _place_grid(
    items: List[Dict[str, Any]],
    start_x: float,
    start_y: float,
    max_cols: int,
    placed_nodes: List[Dict[str, Any]],
) -> None:
    for idx, item in enumerate(items):
        col = idx % max_cols
        row = idx // max_cols
        nx = start_x + col * (NODE_W + NODE_GAP_X)
        ny = start_y + row * (NODE_H + NODE_GAP_Y)
        placed_nodes.append({
            **item,
            "x": round(nx, 1),
            "y": round(ny, 1),
            "w": NODE_W,
            "h": NODE_H,
        })


def _subnet_sort_key(sub: Dict[str, Any], sub_resources: List[Dict[str, Any]]) -> Tuple[int, str, str]:
    """Sort subnets so Compute/App subnets sit above Database/Storage subnets, then by AZ & Name."""
    cats = {r["category"] for r in sub_resources}
    if "Databases" in cats and "Compute" not in cats and "Containers" not in cats:
        tier = 2
    elif sub_resources:
        tier = 0
    else:
        tier = 3
    return (tier, sub.get("az") or "", sub.get("name") or "")


def compute_architecture_layout(
    graph_dict: Dict[str, Any],
    hide_empty_default_vpcs: bool = True,
    hide_empty_subnets: bool = False,
    filter_regions: Optional[List[str]] = None,
    filter_categories: Optional[List[str]] = None,
    search_query: str = "",
) -> Dict[str, Any]:
    """Compute hierarchical bounding boxes, node positions, routed edges, and embedded official SVG bundle."""
    raw_containers: List[Dict[str, Any]] = graph_dict.get("containers", [])
    raw_resources: List[Dict[str, Any]] = graph_dict.get("resources", [])
    raw_edges: List[Dict[str, Any]] = graph_dict.get("edges", [])

    reg_allow: Optional[Set[str]] = set(filter_regions) if filter_regions else None
    cat_allow: Optional[Set[str]] = set(filter_categories) if filter_categories else None
    q = search_query.strip().lower()

    # Identify empty default VPCs (default VPCs whose only resources, if any, are an idle default IGW)
    default_vpc_ids: Set[str] = {
        c["metadata"]["vpc_id"]
        for c in raw_containers
        if c["kind"] == "vpc" and c.get("is_default") and c.get("metadata", {}).get("vpc_id")
    }
    empty_default_vpcs: Set[str] = set()
    if hide_empty_default_vpcs:
        for dvid in default_vpc_ids:
            vpc_res = [r for r in raw_resources if r.get("vpc_id") == dvid]
            non_igw = [r for r in vpc_res if r["resource_type"] != "ec2:internet-gateway"]
            if not non_igw:
                empty_default_vpcs.add(dvid)

    # Filter resources
    resources: List[Dict[str, Any]] = []
    for r in raw_resources:
        if r.get("vpc_id") in empty_default_vpcs:
            continue
        if reg_allow and r["region"] not in reg_allow:
            continue
        if cat_allow and r["category"] not in cat_allow:
            continue
        if q:
            haystack = f"{r['name']} {r['sublabel']} {r['service']} {r['resource_type']} {r['region']} {r.get('arn', '')}".lower()
            tag_str = " ".join(f"{k}={v}" for k, v in (r.get("tags") or {}).items()).lower()
            if q not in haystack and q not in tag_str:
                continue
        resources.append(r)

    containers_by_id: Dict[str, Dict[str, Any]] = {c["id"]: c for c in raw_containers}

    placed_boxes: List[Dict[str, Any]] = []
    placed_nodes: List[Dict[str, Any]] = []

    # Partition regions: "global" first, then alphabetical regional containers
    region_containers = [
        c for c in raw_containers
        if c["kind"] == "region"
        and (not reg_allow or c.get("region") in reg_allow)
    ]
    region_containers.sort(key=lambda c: (0 if c.get("region") == "global" else 1, c.get("region") or ""))

    outer_pad = 28
    cloud_header_h = 54
    acct_pad = 24
    acct_header_h = 46

    content_x = outer_pad + acct_pad + 24
    cursor_y = outer_pad + cloud_header_h + acct_pad + acct_header_h
    max_region_w = 780

    # First pass: measure & lay out each Region block vertically
    region_blocks: List[Tuple[Dict[str, Any], float, float, List[Dict[str, Any]], List[Dict[str, Any]]]] = []

    for reg_c in region_containers:
        reg_name = reg_c.get("region") or ""
        reg_res = [r for r in resources if r["region"] == reg_name]
        reg_vpcs = [
            c for c in raw_containers
            if c["kind"] == "vpc"
            and c.get("region") == reg_name
            and c.get("metadata", {}).get("vpc_id") not in empty_default_vpcs
        ]

        # Skip region if completely empty after filters
        if not reg_res and not reg_vpcs:
            continue

        local_boxes: List[Dict[str, Any]] = []
        local_nodes: List[Dict[str, Any]] = []

        reg_pad_x = 24
        reg_header_h = 46
        reg_pad_bottom = 24

        if reg_name == "global":
            # Global services laid out cleanly in a horizontal row inside Global Edge box
            reg_res.sort(key=lambda r: (CATEGORY_ORDER.index(r["category"]) if r["category"] in CATEGORY_ORDER else 99, r["name"]))
            _, _, gw, gh = _grid_dimensions(len(reg_res), max_cols=7)
            box_w = max(gw + reg_pad_x * 2, 520)
            box_h = reg_header_h + gh + reg_pad_bottom
            _place_grid(reg_res, reg_pad_x, reg_header_h, max_cols=7, placed_nodes=local_nodes)
            region_blocks.append((reg_c, box_w, box_h, local_boxes, local_nodes))
            max_region_w = max(max_region_w, box_w)
            continue

        # Regional layout: Left column = VPCs; Right column = Regional Managed/Serverless Services
        vpc_col_x = reg_pad_x
        vpc_cursor_y = reg_header_h
        vpc_col_w = 0.0

        placed_vpc_ids: Set[str] = set()
        for vpc_c in reg_vpcs:
            vid = vpc_c.get("metadata", {}).get("vpc_id", "")
            placed_vpc_ids.add(vid)
            vpc_pad = 20
            vpc_header_h = 44

            vpc_all_res = [r for r in reg_res if r.get("vpc_id") == vid]
            subnets = [
                c for c in raw_containers
                if c["kind"] in ("public_subnet", "private_subnet")
                and c.get("parent_id") == vpc_c["id"]
            ]
            valid_subnet_ids = {s.get("metadata", {}).get("subnet_id") for s in subnets}

            # VPC-level resources (IGW, Gateway VPC Endpoints, VPC Peering, or resources whose subnet wasn't listed)
            vpc_level_res = [
                r for r in vpc_all_res
                if not r.get("subnet_id") or r.get("subnet_id") not in valid_subnet_ids
            ]

            inner_y = vpc_header_h
            inner_max_w = 340.0

            if vpc_level_res:
                vpc_level_res.sort(key=lambda r: r["name"])
                _, _, vgw_w, vgw_h = _grid_dimensions(len(vpc_level_res), max_cols=5)
                _place_grid(
                    vpc_level_res,
                    vpc_col_x + vpc_pad,
                    vpc_cursor_y + inner_y,
                    max_cols=5,
                    placed_nodes=local_nodes,
                )
                inner_max_w = max(inner_max_w, vgw_w)
                inner_y += vgw_h + 18

            # Group subnets into Public Row(s) on top, Private Row(s) below
            pub_subs = [s for s in subnets if s["kind"] == "public_subnet"]
            priv_subs = [s for s in subnets if s["kind"] == "private_subnet"]

            subnet_res_map: Dict[str, List[Dict[str, Any]]] = {}
            for s in subnets:
                sid = s.get("metadata", {}).get("subnet_id", "")
                s_items = [r for r in vpc_all_res if r.get("subnet_id") == sid]
                s_items.sort(key=lambda r: (CATEGORY_ORDER.index(r["category"]) if r["category"] in CATEGORY_ORDER else 99, r["name"]))
                subnet_res_map[s["id"]] = s_items

            if hide_empty_subnets or cat_allow or q:
                pub_subs = [s for s in pub_subs if subnet_res_map.get(s["id"])]
                priv_subs = [s for s in priv_subs if subnet_res_map.get(s["id"])]

            pub_subs.sort(key=lambda s: _subnet_sort_key(s, subnet_res_map.get(s["id"], [])))
            priv_subs.sort(key=lambda s: _subnet_sort_key(s, subnet_res_map.get(s["id"], [])))

            for tier_subs in (pub_subs, priv_subs):
                if not tier_subs:
                    continue
                # Lay out up to 2 subnets side-by-side per row inside VPC
                subs_per_row = 2
                for r_idx in range(0, len(tier_subs), subs_per_row):
                    row_subs = tier_subs[r_idx:r_idx + subs_per_row]
                    sub_x = vpc_col_x + vpc_pad
                    row_max_h = 0.0
                    row_total_w = 0.0

                    for s_idx, sub_c in enumerate(row_subs):
                        s_items = subnet_res_map.get(sub_c["id"], [])
                        sub_pad = 16
                        sub_header_h = 40
                        if s_items:
                            _, _, sw, sh = _grid_dimensions(len(s_items), max_cols=3)
                            sub_box_w = max(sw + sub_pad * 2, 230)
                            sub_box_h = sub_header_h + sh + sub_pad
                            _place_grid(
                                s_items,
                                sub_x + sub_pad,
                                vpc_cursor_y + inner_y + sub_header_h,
                                max_cols=3,
                                placed_nodes=local_nodes,
                            )
                        else:
                            sub_box_w = 230
                            sub_box_h = 74

                        local_boxes.append({
                            "id": sub_c["id"],
                            "kind": sub_c["kind"],
                            "name": sub_c["name"],
                            "subtitle": sub_c.get("cidr") or "",
                            "icon_key": sub_c["icon_key"],
                            "x": round(sub_x, 1),
                            "y": round(vpc_cursor_y + inner_y, 1),
                            "w": round(sub_box_w, 1),
                            "h": round(sub_box_h, 1),
                            "z": 4,
                            "metadata": sub_c.get("metadata", {}),
                        })
                        sub_x += sub_box_w + 18
                        row_total_w += sub_box_w + (18 if s_idx > 0 else 0)
                        row_max_h = max(row_max_h, sub_box_h)

                    inner_max_w = max(inner_max_w, row_total_w)
                    inner_y += row_max_h + 16

            if inner_y == vpc_header_h:
                inner_y += 48  # Empty VPC minimum height

            vpc_w = inner_max_w + vpc_pad * 2
            vpc_h = inner_y + 10
            local_boxes.append({
                "id": vpc_c["id"],
                "kind": "vpc",
                "name": vpc_c["name"],
                "subtitle": "",
                "icon_key": vpc_c["icon_key"],
                "x": round(vpc_col_x, 1),
                "y": round(vpc_cursor_y, 1),
                "w": round(vpc_w, 1),
                "h": round(vpc_h, 1),
                "z": 3,
                "metadata": vpc_c.get("metadata", {}),
            })
            vpc_col_w = max(vpc_col_w, vpc_w)
            vpc_cursor_y += vpc_h + 22

        vpc_total_h = (vpc_cursor_y - 22 - reg_header_h) if reg_vpcs else 0.0

        # Regional Managed / Serverless Resources (not placed inside a shown VPC)
        regional_managed = [
            r for r in reg_res
            if not r.get("vpc_id") or r.get("vpc_id") not in placed_vpc_ids
        ]

        mgd_col_x = vpc_col_x + (vpc_col_w + 26 if vpc_col_w > 0 else 0)
        mgd_cursor_y = reg_header_h
        mgd_col_w = 0.0

        if regional_managed:
            by_cat: Dict[str, List[Dict[str, Any]]] = {}
            for r in regional_managed:
                by_cat.setdefault(r["category"], []).append(r)

            ordered_cats = sorted(
                by_cat.keys(),
                key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99,
            )

            # Arrange category cards in up to 2 columns on the right side for compact aspect ratio
            cat_cards: List[Tuple[str, List[Dict[str, Any]], float, float]] = []
            for cat_name in ordered_cats:
                c_items = sorted(by_cat[cat_name], key=lambda r: r["name"])
                _, _, cw, ch = _grid_dimensions(len(c_items), max_cols=3)
                card_w = max(cw + 28, 200)
                card_h = 36 + ch + 14
                cat_cards.append((cat_name, c_items, card_w, card_h))

            cat_cols = 2 if len(cat_cards) >= 3 else 1
            for idx in range(0, len(cat_cards), cat_cols):
                row_cards = cat_cards[idx:idx + cat_cols]
                cx = mgd_col_x
                row_h = 0.0
                row_w = 0.0
                for c_idx, (cat_name, c_items, card_w, card_h) in enumerate(row_cards):
                    cat_meta = CATEGORY_META.get(cat_name, CATEGORY_META["Management"])
                    _place_grid(c_items, cx + 14, mgd_cursor_y + 36, max_cols=3, placed_nodes=local_nodes)
                    local_boxes.append({
                        "id": f"cat:{reg_name}:{cat_name}",
                        "kind": "category_box",
                        "name": cat_meta["label"],
                        "subtitle": f"{len(c_items)}",
                        "icon_key": cat_meta["icon_key"],
                        "x": round(cx, 1),
                        "y": round(mgd_cursor_y, 1),
                        "w": round(card_w, 1),
                        "h": round(card_h, 1),
                        "z": 3,
                        "metadata": {"category": cat_name, "region": reg_name},
                    })
                    cx += card_w + 18
                    row_w += card_w + (18 if c_idx > 0 else 0)
                    row_h = max(row_h, card_h)
                mgd_col_w = max(mgd_col_w, row_w)
                mgd_cursor_y += row_h + 16

        mgd_total_h = (mgd_cursor_y - 16 - reg_header_h) if regional_managed else 0.0
        inner_region_h = max(vpc_total_h, mgd_total_h, 80.0)
        inner_region_w = vpc_col_w + (26 if vpc_col_w > 0 and mgd_col_w > 0 else 0) + mgd_col_w

        reg_box_w = max(inner_region_w + reg_pad_x * 2, 520)
        reg_box_h = reg_header_h + inner_region_h + reg_pad_bottom
        region_blocks.append((reg_c, reg_box_w, reg_box_h, local_boxes, local_nodes))
        max_region_w = max(max_region_w, reg_box_w)

    # Second pass: translate local coordinates of each Region block into global canvas coordinates
    reg_x = content_x
    for reg_c, r_w, r_h, l_boxes, l_nodes in region_blocks:
        # Stretch region width uniformly to max_region_w for clean architectural alignment
        final_r_w = max_region_w
        placed_boxes.append({
            "id": reg_c["id"],
            "kind": "region",
            "name": reg_c["name"],
            "subtitle": f"{len(l_nodes)} resources",
            "icon_key": reg_c["icon_key"],
            "x": round(reg_x, 1),
            "y": round(cursor_y, 1),
            "w": round(final_r_w, 1),
            "h": round(r_h, 1),
            "z": 2,
            "metadata": reg_c.get("metadata", {}),
        })
        for b in l_boxes:
            b["x"] = round(b["x"] + reg_x, 1)
            b["y"] = round(b["y"] + cursor_y, 1)
            placed_boxes.append(b)
        for n in l_nodes:
            n["x"] = round(n["x"] + reg_x, 1)
            n["y"] = round(n["y"] + cursor_y, 1)
            placed_nodes.append(n)
        cursor_y += r_h + 26

    if not region_blocks:
        cursor_y += 140

    # Account & AWS Cloud outer frames
    acct_x = outer_pad + acct_pad
    acct_y = outer_pad + cloud_header_h
    acct_w = max_region_w + 48
    acct_h = (cursor_y - acct_y) + 6

    acct_c = next((c for c in raw_containers if c["kind"] == "account"), None)
    placed_boxes.append({
        "id": acct_c["id"] if acct_c else "account:default",
        "kind": "account",
        "name": acct_c["name"] if acct_c else f"AWS Account: {graph_dict.get('account_alias', '')}",
        "subtitle": f"Profile: {graph_dict.get('profile', 'default')}",
        "icon_key": GROUP_ICONS["account"],
        "x": round(acct_x, 1),
        "y": round(acct_y, 1),
        "w": round(acct_w, 1),
        "h": round(acct_h, 1),
        "z": 1,
        "metadata": acct_c.get("metadata", {}) if acct_c else {},
    })

    cloud_w = acct_w + acct_pad * 2
    cloud_h = cloud_header_h + acct_h + acct_pad
    placed_boxes.append({
        "id": "cloud:aws",
        "kind": "aws_cloud",
        "name": "Amazon Web Services Cloud Architecture",
        "subtitle": f"{len(placed_nodes)} Official Resources Mapped",
        "icon_key": GROUP_ICONS["aws_cloud"],
        "x": round(outer_pad, 1),
        "y": round(outer_pad, 1),
        "w": round(cloud_w, 1),
        "h": round(cloud_h, 1),
        "z": 0,
        "metadata": {},
    })

    # Sort boxes back-to-front by z-order
    placed_boxes.sort(key=lambda b: b["z"])

    # Filter edges to visible nodes
    node_lookup = {n["id"]: n for n in placed_nodes}
    visible_edges: List[Dict[str, Any]] = []
    for e in raw_edges:
        if e["source"] in node_lookup and e["target"] in node_lookup:
            visible_edges.append(e)

    # Collect all official AWS SVG data URIs required for standalone rendering
    used_icons = [n["icon_key"] for n in placed_nodes] + [b["icon_key"] for b in placed_boxes]
    icons_bundle = catalog.build_data_uri_bundle(used_icons)

    canvas_w = int(cloud_w + outer_pad * 2)
    canvas_h = int(cloud_h + outer_pad * 2)

    return {
        "canvas_width": canvas_w,
        "canvas_height": canvas_h,
        "boxes": placed_boxes,
        "nodes": placed_nodes,
        "edges": visible_edges,
        "icons": icons_bundle,
        "container_styles": CONTAINER_STYLES,
        "edge_styles": EDGE_STYLES,
    }


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _edge_anchor_points(src: Dict[str, Any], tgt: Dict[str, Any]) -> Tuple[float, float, float, float, str]:
    """Compute clean card-perimeter attachment points and cubic bezier SVG path between two nodes."""
    sx_c = src["x"] + src["w"] / 2
    sy_c = src["y"] + src["h"] / 2
    tx_c = tgt["x"] + tgt["w"] / 2
    ty_c = tgt["y"] + tgt["h"] / 2

    dx = tx_c - sx_c
    dy = ty_c - sy_c

    if abs(dy) >= abs(dx) * 0.62:
        # Primarily vertical connection: attach to bottom/top of card
        if dy >= 0:
            x1, y1 = sx_c, src["y"] + src["h"] - 3
            x2, y2 = tx_c, tgt["y"] + 2
        else:
            x1, y1 = sx_c, src["y"] + 2
            x2, y2 = tx_c, tgt["y"] + tgt["h"] - 3
        cy1 = y1 + (y2 - y1) * 0.45
        cy2 = y2 - (y2 - y1) * 0.45
        path = f"M {x1:.1f} {y1:.1f} C {x1:.1f} {cy1:.1f}, {x2:.1f} {cy2:.1f}, {x2:.1f} {y2:.1f}"
    else:
        # Primarily horizontal connection: attach to right/left perimeter of card
        if dx >= 0:
            x1, y1 = src["x"] + src["w"] - 6, src["y"] + 44
            x2, y2 = tgt["x"] + 6, tgt["y"] + 44
        else:
            x1, y1 = src["x"] + 6, src["y"] + 44
            x2, y2 = tgt["x"] + tgt["w"] - 6, tgt["y"] + 44
        cx1 = x1 + (x2 - x1) * 0.45
        cx2 = x2 - (x2 - x1) * 0.45
        path = f"M {x1:.1f} {y1:.1f} C {cx1:.1f} {y1:.1f}, {cx2:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}"

    return x1, y1, x2, y2, path


def render_standalone_svg(layout: Dict[str, Any], show_edges: bool = True, show_edge_labels: bool = True) -> str:
    """Render a 100% self-contained standalone SVG using only embedded official AWS SVG icons."""
    w = layout["canvas_width"]
    h = layout["canvas_height"]
    icons: Dict[str, str] = layout["icons"]
    nodes_by_id = {n["id"]: n for n in layout["nodes"]}

    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="background:#F1F5F9;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">',
        "<defs>",
        '  <filter id="cardShadow" x="-8%" y="-8%" width="116%" height="120%">',
        '    <feDropShadow dx="0" dy="2" stdDeviation="2.5" flood-color="#0F172A" flood-opacity="0.08"/>',
        "  </filter>",
    ]

    # Arrowhead markers per edge kind
    for kind, estyle in EDGE_STYLES.items():
        col = estyle["color"]
        parts.append(
            f'  <marker id="arrow-{kind}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">'
            f'<path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="{col}"/></marker>'
        )
    parts.append("</defs>")

    # Background rect
    parts.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="#F1F5F9"/>')

    # 1. Containers
    for box in layout["boxes"]:
        st = CONTAINER_STYLES.get(box["kind"], CONTAINER_STYLES["category_box"])
        bx, by, bw, bh = box["x"], box["y"], box["w"], box["h"]
        dash_attr = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        parts.append(
            f'<g class="aws-container" data-id="{html.escape(box["id"])}">'
            f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="{st["radius"]}" '
            f'fill="{st["fill"]}" stroke="{st["stroke"]}" stroke-width="{st["stroke_width"]}"{dash_attr}/>'
        )
        icon_uri = icons.get(box["icon_key"], "")
        isz = st["icon_size"]
        if icon_uri:
            parts.append(
                f'<image x="{bx + 10}" y="{by + 8}" width="{isz}" height="{isz}" href="{icon_uri}"/>'
            )
        title_x = bx + 10 + isz + 8
        title_y = by + 8 + isz * 0.70
        fsize = 14 if box["kind"] in ("aws_cloud", "account", "region") else 12.5
        parts.append(
            f'<text x="{title_x:.1f}" y="{title_y:.1f}" fill="{st["title_color"]}" font-size="{fsize}" font-weight="700">'
            f'{html.escape(box["name"])}</text>'
        )
        if box.get("subtitle"):
            parts.append(
                f'<text x="{bx + bw - 12:.1f}" y="{title_y:.1f}" text-anchor="end" fill="#64748B" font-size="11" font-weight="500">'
                f'{html.escape(str(box["subtitle"]))}</text>'
            )
        parts.append("</g>")

    # 2. Edge Paths (underneath node cards) + collect Edge Labels (rendered above node cards)
    edge_label_fragments: List[str] = []
    if show_edges:
        parts.append('<g class="aws-edges">')
        for edge in layout["edges"]:
            src = nodes_by_id.get(edge["source"])
            tgt = nodes_by_id.get(edge["target"])
            if not src or not tgt:
                continue
            est = EDGE_STYLES.get(edge["kind"], EDGE_STYLES["traffic"])
            x1, y1, x2, y2, path_d = _edge_anchor_points(src, tgt)
            dash_attr = f' stroke-dasharray="{est["dash"]}"' if est["dash"] else ""
            parts.append(
                f'<path d="{path_d}" fill="none" stroke="{est["color"]}" stroke-width="1.65" '
                f'stroke-opacity="0.78"{dash_attr} marker-end="url(#arrow-{edge["kind"]})"/>'
            )
            if show_edge_labels and edge.get("label"):
                mx = (x1 + x2) / 2
                my = (y1 + y2) / 2
                lbl = _truncate(edge["label"], 18)
                lw = len(lbl) * 5.5 + 10
                edge_label_fragments.append(
                    f'<rect x="{mx - lw / 2:.1f}" y="{my - 7.5:.1f}" width="{lw:.1f}" height="15" rx="4" '
                    f'fill="#FFFFFF" fill-opacity="0.95" stroke="{est["color"]}" stroke-width="0.9"/>'
                    f'<text x="{mx:.1f}" y="{my + 3:.1f}" text-anchor="middle" fill="#334155" font-size="9" font-weight="600">'
                    f'{html.escape(lbl)}</text>'
                )
        parts.append("</g>")

    # 3. Official AWS Resource Nodes
    parts.append('<g class="aws-nodes">')
    for node in layout["nodes"]:
        nx, ny, nw, nh = node["x"], node["y"], node["w"], node["h"]
        icon_uri = icons.get(node["icon_key"], "")
        icon_x = nx + (nw - 48) / 2
        icon_y = ny + 6
        name_line = _truncate(node["name"], 20)
        sub_line = _truncate(node.get("sublabel", ""), 22)
        parts.append(f'<g class="aws-node" data-id="{html.escape(node["id"])}">')
        parts.append(
            f'<rect x="{nx + 6:.1f}" y="{ny + 2:.1f}" width="{nw - 12:.1f}" height="{nh - 4:.1f}" rx="8" '
            f'fill="#FFFFFF" fill-opacity="0.98" stroke="#DCE3EC" stroke-width="1.1" filter="url(#cardShadow)"/>'
        )
        if icon_uri:
            parts.append(
                f'<image x="{icon_x:.1f}" y="{icon_y:.1f}" width="48" height="48" href="{icon_uri}"/>'
            )
        parts.append(
            f'<text x="{nx + nw / 2:.1f}" y="{ny + 69:.1f}" text-anchor="middle" fill="#0F172A" font-size="11" font-weight="600">'
            f'{html.escape(name_line)}</text>'
        )
        if sub_line:
            parts.append(
                f'<text x="{nx + nw / 2:.1f}" y="{ny + 84:.1f}" text-anchor="middle" fill="#64748B" font-size="9.5">'
                f'{html.escape(sub_line)}</text>'
            )
        parts.append("</g>")
    parts.append("</g>")

    # 4. Edge Labels rendered cleanly above cards
    if edge_label_fragments:
        parts.append('<g class="aws-edge-labels">')
        parts.extend(edge_label_fragments)
        parts.append("</g>")

    parts.append("</svg>")
    return "\n".join(parts)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for fp in candidates:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def _draw_dashed_rounded_rect(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    w: float,
    h: float,
    radius: float,
    fill: str,
    stroke: str,
    stroke_width: int,
    dash_str: str,
) -> None:
    if not dash_str:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=fill, outline=stroke, width=stroke_width)
        return
    # Fill first, then draw dashed border segments around perimeter
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=fill, outline=None)
    parts = [int(float(v)) for v in dash_str.split(",") if v.strip()]
    dash_len = max(4, parts[0] * max(1, stroke_width // 2))
    gap_len = max(3, (parts[1] if len(parts) > 1 else parts[0]) * max(1, stroke_width // 2))

    segments = [
        ((x + radius, y), (x + w - radius, y)),
        ((x + w, y + radius), (x + w, y + h - radius)),
        ((x + w - radius, y + h), (x + radius, y + h)),
        ((x, y + h - radius), (x, y + radius)),
    ]
    for (sx, sy), (ex, ey) in segments:
        dist = math.hypot(ex - sx, ey - sy)
        if dist <= 0:
            continue
        ux, uy = (ex - sx) / dist, (ey - sy) / dist
        pos = 0.0
        while pos < dist:
            end_pos = min(pos + dash_len, dist)
            draw.line(
                [(sx + ux * pos, sy + uy * pos), (sx + ux * end_pos, sy + uy * end_pos)],
                fill=stroke,
                width=stroke_width,
            )
            pos += dash_len + gap_len


def render_standalone_png(
    layout: Dict[str, Any],
    output_path: Path,
    scale: float = 2.0,
    show_edges: bool = True,
    show_edge_labels: bool = True,
) -> None:
    """Render a crisp high-DPI PNG diagram directly in Python using official AWS PNG icons."""
    w = int(round(layout["canvas_width"] * scale))
    h = int(round(layout["canvas_height"] * scale))
    canvas = Image.new("RGBA", (w, h), "#EEF2F6")
    draw = ImageDraw.Draw(canvas, "RGBA")

    font_title_lg = _load_font(int(round(14 * scale)), bold=True)
    font_title_sm = _load_font(int(round(12.5 * scale)), bold=True)
    font_sub = _load_font(int(round(11 * scale)), bold=False)
    font_node = _load_font(int(round(11 * scale)), bold=True)
    font_node_sub = _load_font(int(round(9.5 * scale)), bold=False)
    font_edge = _load_font(int(round(9.5 * scale)), bold=True)

    icon_cache: Dict[Tuple[str, int], Image.Image] = {}

    def _get_scaled_icon(icon_key: str, px: int) -> Optional[Image.Image]:
        ck = (icon_key, px)
        if ck in icon_cache:
            return icon_cache[ck]
        p = catalog.get_png_path(icon_key)
        if not p:
            return None
        img = Image.open(p).convert("RGBA").resize((px, px), Image.Resampling.LANCZOS)
        icon_cache[ck] = img
        return img

    # 1. Containers
    for box in layout["boxes"]:
        st = CONTAINER_STYLES.get(box["kind"], CONTAINER_STYLES["category_box"])
        bx, by, bw, bh = box["x"] * scale, box["y"] * scale, box["w"] * scale, box["h"] * scale
        sw = max(1, int(round(st["stroke_width"] * scale)))
        _draw_dashed_rounded_rect(
            draw, bx, by, bw, bh, st["radius"] * scale, st["fill"], st["stroke"], sw, st["dash"]
        )
        isz = int(round(st["icon_size"] * scale))
        ic = _get_scaled_icon(box["icon_key"], isz)
        if ic:
            canvas.alpha_composite(ic, (int(round(bx + 10 * scale)), int(round(by + 8 * scale))))

        tx = bx + (10 + st["icon_size"] + 8) * scale
        ty = by + 9 * scale
        tfont = font_title_lg if box["kind"] in ("aws_cloud", "account", "region") else font_title_sm
        draw.text((tx, ty), box["name"], fill=st["title_color"], font=tfont)
        if box.get("subtitle"):
            sub_txt = str(box["subtitle"])
            sb = draw.textbbox((0, 0), sub_txt, font=font_sub)
            sw_txt = sb[2] - sb[0]
            draw.text((bx + bw - 12 * scale - sw_txt, ty + 2 * scale), sub_txt, fill="#64748B", font=font_sub)

    # 2. Edges (lines drawn below cards, labels collected to draw above cards)
    nodes_by_id = {n["id"]: n for n in layout["nodes"]}
    deferred_edge_labels: List[Tuple[float, float, str, str]] = []
    if show_edges:
        for edge in layout["edges"]:
            src = nodes_by_id.get(edge["source"])
            tgt = nodes_by_id.get(edge["target"])
            if not src or not tgt:
                continue
            est = EDGE_STYLES.get(edge["kind"], EDGE_STYLES["traffic"])
            sx_c = src["x"] + src["w"] / 2
            sy_c = src["y"] + src["h"] / 2
            tx_c = tgt["x"] + tgt["w"] / 2
            ty_c = tgt["y"] + tgt["h"] / 2
            dx, dy = tx_c - sx_c, ty_c - sy_c

            if abs(dy) >= abs(dx) * 0.62:
                p0 = (sx_c, src["y"] + (src["h"] - 3 if dy >= 0 else 2))
                p3 = (tx_c, tgt["y"] + (2 if dy >= 0 else tgt["h"] - 3))
                p1 = (p0[0], p0[1] + (p3[1] - p0[1]) * 0.45)
                p2 = (p3[0], p3[1] - (p3[1] - p0[1]) * 0.45)
            else:
                p0 = (src["x"] + (src["w"] - 6 if dx >= 0 else 6), src["y"] + 44)
                p3 = (tgt["x"] + (6 if dx >= 0 else tgt["w"] - 6), tgt["y"] + 44)
                p1 = (p0[0] + (p3[0] - p0[0]) * 0.45, p0[1])
                p2 = (p3[0] - (p3[0] - p0[0]) * 0.45, p3[1])

            chord_len = math.hypot(p3[0] - p0[0], p3[1] - p0[1])
            steps = max(24, int(chord_len / 5))
            pts = []
            for i in range(steps + 1):
                t = i / steps
                mt = 1 - t
                px = (mt**3) * p0[0] + 3 * (mt**2) * t * p1[0] + 3 * mt * (t**2) * p2[0] + (t**3) * p3[0]
                py = (mt**3) * p0[1] + 3 * (mt**2) * t * p1[1] + 3 * mt * (t**2) * p2[1] + (t**3) * p3[1]
                pts.append((px * scale, py * scale))

            lw = max(2, int(round(1.65 * scale)))
            if est["dash"]:
                # Uniform pixel-based dash segments
                acc = 0.0
                drawing_on = True
                dash_px = 6.0 * scale
                gap_px = 4.0 * scale
                for idx in range(len(pts) - 1):
                    seg = math.hypot(pts[idx + 1][0] - pts[idx][0], pts[idx + 1][1] - pts[idx][1])
                    if drawing_on:
                        draw.line([pts[idx], pts[idx + 1]], fill=est["color"], width=lw)
                    acc += seg
                    limit = dash_px if drawing_on else gap_px
                    if acc >= limit:
                        acc = 0.0
                        drawing_on = not drawing_on
            else:
                draw.line(pts, fill=est["color"], width=lw)

            # Arrowhead at p3
            prev_x, prev_y = pts[-3]
            end_x, end_y = pts[-1]
            ang = math.atan2(end_y - prev_y, end_x - prev_x)
            alen = 7.5 * scale
            a1 = (end_x - alen * math.cos(ang - 0.48), end_y - alen * math.sin(ang - 0.48))
            a2 = (end_x - alen * math.cos(ang + 0.48), end_y - alen * math.sin(ang + 0.48))
            draw.polygon([(end_x, end_y), a1, a2], fill=est["color"])

            if show_edge_labels and edge.get("label"):
                mx, my = pts[len(pts) // 2]
                deferred_edge_labels.append((mx, my, _truncate(edge["label"], 18), est["color"]))

    # 3. Official AWS Resource Node Cards
    for node in layout["nodes"]:
        nx, ny, nw, nh = node["x"] * scale, node["y"] * scale, node["w"] * scale, node["h"] * scale
        # Subtle clean card shadow + crisp white card
        draw.rounded_rectangle(
            [nx + 7 * scale, ny + 4 * scale, nx + nw - 5 * scale, ny + nh - 2 * scale],
            radius=8 * scale,
            fill="#DCE4EE",
        )
        draw.rounded_rectangle(
            [nx + 6 * scale, ny + 2 * scale, nx + nw - 6 * scale, ny + nh - 4 * scale],
            radius=8 * scale,
            fill="#FFFFFF",
            outline="#CBD5E1",
            width=max(1, int(round(1.1 * scale))),
        )
        isz = int(round(48 * scale))
        ic = _get_scaled_icon(node["icon_key"], isz)
        if ic:
            canvas.alpha_composite(ic, (int(round(nx + (nw - isz) / 2)), int(round(ny + 6 * scale))))

        name_line = _truncate(node["name"], 20)
        nb = draw.textbbox((0, 0), name_line, font=font_node)
        nw_txt = nb[2] - nb[0]
        draw.text((nx + (nw - nw_txt) / 2, ny + 58 * scale), name_line, fill="#0F172A", font=font_node)

        if node.get("sublabel"):
            sub_line = _truncate(node["sublabel"], 22)
            sb = draw.textbbox((0, 0), sub_line, font=font_node_sub)
            sw_txt = sb[2] - sb[0]
            draw.text((nx + (nw - sw_txt) / 2, ny + 74 * scale), sub_line, fill="#64748B", font=font_node_sub)

    # 4. Edge Labels rendered cleanly on top
    for mx, my, lbl, color in deferred_edge_labels:
        bb = draw.textbbox((0, 0), lbl, font=font_edge)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        pad_x, pad_y = 5 * scale, 2.5 * scale
        draw.rounded_rectangle(
            [mx - tw / 2 - pad_x, my - th / 2 - pad_y, mx + tw / 2 + pad_x, my + th / 2 + pad_y],
            radius=4 * scale,
            fill="#FFFFFF",
            outline=color,
            width=max(1, int(round(1.0 * scale))),
        )
        draw.text((mx - tw / 2, my - th / 2 - 1 * scale), lbl, fill="#334155", font=font_edge)

    with open(output_path, "wb") as out_f:
        canvas.convert("RGB").save(out_f, format="PNG", optimize=True)

