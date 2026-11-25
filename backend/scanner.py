"""Comprehensive read-only AWS environment scanner and relationship inference engine."""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound

from .icon_catalog import GROUP_ICONS, catalog
from .models import AwsContainer, AwsEdge, AwsResource, EnvironmentGraph

BOTO_CONFIG = Config(
    connect_timeout=5,
    read_timeout=12,
    retries={"max_attempts": 2, "mode": "standard"},
)

# Sub-resource types skipped in the universal tagging sweep because they are either
# already modeled as containers/edges or represent low-level plumbing noise.
TAG_SWEEP_SKIP_PREFIXES = (
    "ec2:subnet",
    "ec2:vpc/",
    "ec2:route-table",
    "ec2:network-interface",
    "ec2:security-group",
    "ec2:network-acl",
    "ec2:dhcp-options",
    "ec2:Snapshot",
    "ec2:snapshot",
    "ec2:image",
    "ec2:elastic-ip",
    "ec2:volume",  # Optional low-level block attachment; EC2 metadata captures volume count
    "elasticloadbalancing:targetgroup",
    "elasticloadbalancing:listener",
    "rds:subgrp",
    "rds:pg",
    "rds:cluster-pg",
    "rds:snapshot",
    "rds:cluster-snapshot",
    "ecs:task/",
    "ecs:task-definition",
    "logs:log-group",
)


def _tags_to_dict(tag_list: Optional[List[Dict[str, Any]]]) -> Dict[str, str]:
    if not tag_list:
        return {}
    out: Dict[str, str] = {}
    for item in tag_list:
        k = item.get("Key") or item.get("key")
        v = item.get("Value") or item.get("value") or ""
        if k:
            out[str(k)] = str(v)
    return out


def _name_from_tags(tags: Dict[str, str], fallback: str) -> str:
    return tags.get("Name") or tags.get("name") or fallback


def list_aws_profiles() -> List[Dict[str, Any]]:
    """Return available local AWS CLI profiles (~/.aws/config & ~/.aws/credentials) plus active env."""
    profiles: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    try:
        session = boto3.Session()
        avail = session.available_profiles
    except Exception:
        avail = []

    # Check if environment credentials exist
    import os
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE"):
        profiles.append({
            "name": "__env__",
            "label": "Environment Variables (Active Shell)",
            "default_region": os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1",
        })
        seen.add("__env__")

    for p in avail:
        if p in seen:
            continue
        try:
            s = boto3.Session(profile_name=p)
            reg = s.region_name or "us-east-1"
        except Exception:
            reg = "us-east-1"
        profiles.append({
            "name": p,
            "label": f"{p} ({reg})",
            "default_region": reg,
        })
        seen.add(p)

    if not profiles:
        profiles.append({
            "name": "default",
            "label": "default (~/.aws)",
            "default_region": "us-east-1",
        })
    return profiles


def _make_session(profile: Optional[str]) -> boto3.Session:
    if not profile or profile == "__env__":
        return boto3.Session()
    return boto3.Session(profile_name=profile)


def list_enabled_regions(profile: Optional[str] = None) -> Tuple[List[str], Optional[str]]:
    """List enabled AWS regions for the given profile, falling back to standard commercial regions."""
    fallback_regions = [
        "us-east-1", "us-east-2", "us-west-1", "us-west-2",
        "ca-central-1", "sa-east-1",
        "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-north-1",
        "ap-northeast-1", "ap-northeast-2", "ap-southeast-1", "ap-southeast-2", "ap-south-1",
    ]
    try:
        session = _make_session(profile)
        base_region = session.region_name or "us-east-1"
        ec2 = session.client("ec2", region_name=base_region, config=BOTO_CONFIG)
        resp = ec2.describe_regions(AllRegions=False)
        regions = sorted(r["RegionName"] for r in resp.get("Regions", []))
        return (regions or fallback_regions), None
    except Exception as exc:
        return fallback_regions, str(exc)


ProgressCallback = Callable[[str, int, int], None]


class AwsEnvironmentScanner:
    """Thread-safe, read-only multi-region AWS topology scanner."""

    def __init__(
        self,
        profile: Optional[str] = None,
        regions: Optional[List[str]] = None,
        include_global: bool = True,
        include_tag_sweep: bool = True,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> None:
        self.profile = profile or "default"
        self.session = _make_session(profile)
        self.requested_regions = regions
        self.include_global = include_global
        self.include_tag_sweep = include_tag_sweep
        self.progress_cb = progress_cb

        self._lock = threading.Lock()
        self.containers: Dict[str, AwsContainer] = {}
        self.resources: Dict[str, AwsResource] = {}
        self.edges: Dict[str, AwsEdge] = {}
        self.warnings: List[str] = []

        # Cross-reference lookup tables populated during scan for relationship synthesis
        self._arn_to_id: Dict[str, str] = {}
        self._dns_to_id: Dict[str, str] = {}
        self._s3_bucket_to_id: Dict[str, str] = {}
        self._ddb_table_to_id: Dict[Tuple[str, str], str] = {}
        self._sqs_name_to_id: Dict[Tuple[str, str], str] = {}
        self._private_ip_to_id: Dict[Tuple[str, str], str] = {}  # (vpc_id, ip) -> resource_id
        self._sg_inbound_sources: Dict[str, List[Tuple[str, str]]] = {}  # sg_id -> [(source_sg_id, port_label)]
        self._route53_aliases: List[Tuple[str, str, str]] = []  # (zone_node_id, record_name, target_dns)
        self._cf_origins: List[Tuple[str, str]] = []  # (cf_node_id, origin_domain)
        self._lambda_env_values: List[Tuple[str, str, List[str]]] = []  # (lambda_node_id, region, candidate_strings)
        self._peering_pairs: List[Tuple[str, str, str]] = []  # (peering_node_id, req_vpc, acc_vpc)

        self._completed_steps = 0
        self._total_steps = 1

    def _emit_progress(self, message: str, advance: int = 1) -> None:
        with self._lock:
            self._completed_steps += advance
            done = min(self._completed_steps, self._total_steps)
            total = max(self._total_steps, 1)
        if self.progress_cb:
            self.progress_cb(message, done, total)

    def _warn(self, msg: str) -> None:
        with self._lock:
            if msg not in self.warnings:
                self.warnings.append(msg)

    def _add_container(self, container: AwsContainer) -> None:
        with self._lock:
            self.containers[container.id] = container

    def _add_resource(self, res: AwsResource, dns_names: Optional[List[str]] = None) -> None:
        with self._lock:
            self.resources[res.id] = res
            if res.arn:
                self._arn_to_id[res.arn] = res.id
            if dns_names:
                for d in dns_names:
                    if d:
                        clean = d.lower().rstrip(".")
                        self._dns_to_id[clean] = res.id

    def _add_edge(self, source: str, target: str, kind: str, label: str = "", metadata: Optional[Dict[str, Any]] = None) -> None:
        if not source or not target or source == target:
            return
        edge_id = f"edge:{source}->{target}:{kind}"
        with self._lock:
            if edge_id not in self.edges:
                self.edges[edge_id] = AwsEdge(
                    id=edge_id,
                    source=source,
                    target=target,
                    kind=kind,
                    label=label,
                    metadata=metadata or {},
                )

    def scan(self) -> EnvironmentGraph:
        """Execute full read-only AWS environment discovery and return EnvironmentGraph."""
        # 1. Authenticate & identify account
        sts = self.session.client("sts", config=BOTO_CONFIG)
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        caller_arn = identity["Arn"]

        account_alias = account_id
        try:
            iam = self.session.client("iam", config=BOTO_CONFIG)
            aliases = iam.list_account_aliases().get("AccountAliases", [])
            if aliases:
                account_alias = f"{aliases[0]} ({account_id})"
        except Exception:
            pass

        # 2. Resolve regions
        if self.requested_regions:
            regions = list(dict.fromkeys(self.requested_regions))
        else:
            regions, reg_err = list_enabled_regions(self.profile)
            if reg_err:
                self._warn(f"Region discovery fallback used: {reg_err}")

        self._total_steps = 2 + (3 if self.include_global else 0) + len(regions) * 5
        self._emit_progress(f"Authenticated as {account_alias}", 1)

        # Create root AWS Cloud + Account containers
        cloud_id = "cloud:aws"
        acct_container_id = f"account:{account_id}"
        self._add_container(
            AwsContainer(
                id=cloud_id,
                kind="aws_cloud",
                name="AWS Cloud",
                icon_key=GROUP_ICONS["aws_cloud"],
            )
        )
        self._add_container(
            AwsContainer(
                id=acct_container_id,
                kind="account",
                name=f"AWS Account: {account_alias}",
                icon_key=GROUP_ICONS["account"],
                parent_id=cloud_id,
                metadata={"account_id": account_id, "caller_arn": caller_arn},
            )
        )

        # Create Region containers upfront for scanned regions
        for reg in regions:
            self._add_container(
                AwsContainer(
                    id=f"region:{reg}",
                    kind="region",
                    name=f"Region: {reg}",
                    icon_key=GROUP_ICONS["region"],
                    parent_id=acct_container_id,
                    region=reg,
                )
            )

        # 3. Scan Global Services (Route 53, CloudFront, S3, Global WAF)
        if self.include_global:
            self._add_container(
                AwsContainer(
                    id="region:global",
                    kind="region",
                    name="Global Edge & Account Services",
                    icon_key=GROUP_ICONS["aws_cloud_alt"],
                    parent_id=acct_container_id,
                    region="global",
                )
            )
            self._scan_global_services(account_id, set(regions))

        # 4. Scan Regions concurrently
        max_region_workers = min(max(len(regions), 1), 6)
        with ThreadPoolExecutor(max_workers=max_region_workers) as pool:
            futures = {pool.submit(self._scan_single_region, account_id, reg): reg for reg in regions}
            for fut in as_completed(futures):
                reg = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    self._warn(f"[{reg}] Region scan error: {exc}")

        # 5. Prune completely empty Region containers (no VPCs and no resources) so multi-region scans stay clean
        self._prune_empty_regions()

        # 6. Synthesize cross-service architectural relationships
        self._emit_progress("Synthesizing network, DNS, event, and security group relationships...", 1)
        self._synthesize_relationships()

        # Filter edges so both endpoints exist
        valid_ids = set(self.resources.keys())
        clean_edges = [
            e for e in self.edges.values()
            if e.source in valid_ids and e.target in valid_ids
        ]

        # Compute summary stats
        by_category: Dict[str, int] = {}
        by_region: Dict[str, int] = {}
        by_service: Dict[str, int] = {}
        for r in self.resources.values():
            by_category[r.category] = by_category.get(r.category, 0) + 1
            by_region[r.region] = by_region.get(r.region, 0) + 1
            by_service[r.service] = by_service.get(r.service, 0) + 1

        vpc_count = sum(1 for c in self.containers.values() if c.kind == "vpc")
        subnet_count = sum(1 for c in self.containers.values() if c.kind in ("public_subnet", "private_subnet"))

        stats = {
            "total_resources": len(self.resources),
            "total_containers": len(self.containers),
            "total_vpcs": vpc_count,
            "total_subnets": subnet_count,
            "total_edges": len(clean_edges),
            "by_category": by_category,
            "by_region": by_region,
            "by_service": by_service,
        }

        return EnvironmentGraph(
            account_id=account_id,
            account_alias=account_alias,
            caller_arn=caller_arn,
            profile=self.profile,
            scanned_at=datetime.now(timezone.utc).isoformat(),
            regions_scanned=regions,
            is_sample=False,
            containers=list(self.containers.values()),
            resources=list(self.resources.values()),
            edges=clean_edges,
            warnings=self.warnings,
            stats=stats,
        )

    # -------------------------------------------------------------------------
    # Global Service Collectors
    # -------------------------------------------------------------------------
    def _scan_global_services(self, account_id: str, target_regions: Set[str]) -> None:
        # Route 53
        self._emit_progress("Scanning global Route 53 hosted zones & DNS records...", 1)
        try:
            r53 = self.session.client("route53", config=BOTO_CONFIG)
            paginator = r53.get_paginator("list_hosted_zones")
            for page in paginator.paginate():
                for hz in page.get("HostedZones", []):
                    hz_id = hz["Id"].split("/")[-1]
                    zone_name = hz["Name"].rstrip(".")
                    is_private = hz.get("Config", {}).get("PrivateZone", False)
                    node_id = f"r53:{hz_id}"
                    icon_key, cat = catalog.resolve_resource_icon("route53:hosted-zone", "route53")
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=f"arn:aws:route53:::hostedzone/{hz_id}",
                            resource_type="route53:hosted-zone",
                            service="route53",
                            category=cat,
                            name=zone_name,
                            sublabel=f"{'Private' if is_private else 'Public'} Zone ({hz.get('ResourceRecordSetCount', 0)} records)",
                            icon_key=icon_key,
                            region="global",
                            metadata={
                                "hosted_zone_id": hz_id,
                                "private_zone": is_private,
                                "record_count": hz.get("ResourceRecordSetCount", 0),
                            },
                        )
                    )
                    # Inspect Alias / CNAME record sets for topology edges
                    try:
                        rr_pag = r53.get_paginator("list_resource_record_sets")
                        for rr_page in rr_pag.paginate(HostedZoneId=hz_id):
                            for rr in rr_page.get("ResourceRecordSets", []):
                                alias = rr.get("AliasTarget")
                                if alias and alias.get("DNSName"):
                                    target_dns = alias["DNSName"].lower().rstrip(".")
                                    # Strip dualstack. prefix common on ELB aliases
                                    if target_dns.startswith("dualstack."):
                                        target_dns = target_dns[len("dualstack."):]
                                    with self._lock:
                                        self._route53_aliases.append((node_id, rr.get("Name", "").rstrip("."), target_dns))
                                elif rr.get("Type") == "CNAME":
                                    for val in rr.get("ResourceRecords", []):
                                        v = val.get("Value", "").lower().rstrip(".")
                                        if "amazonaws.com" in v or "cloudfront.net" in v:
                                            with self._lock:
                                                self._route53_aliases.append((node_id, rr.get("Name", "").rstrip("."), v))
                    except Exception:
                        pass
        except (ClientError, BotoCoreError) as exc:
            self._warn(f"[global] Route 53 scan skipped: {exc.__class__.__name__}")

        # CloudFront + Global WAF
        self._emit_progress("Scanning CloudFront distributions & Global WAF...", 1)
        try:
            cf = self.session.client("cloudfront", config=BOTO_CONFIG)
            paginator = cf.get_paginator("list_distributions")
            for page in paginator.paginate():
                dist_list = page.get("DistributionList", {})
                for dist in dist_list.get("Items", []) or []:
                    dist_id = dist["Id"]
                    domain = dist["DomainName"]
                    aliases = dist.get("Aliases", {}).get("Items", []) or []
                    label = aliases[0] if aliases else domain
                    node_id = f"cf:{dist_id}"
                    icon_key, cat = catalog.resolve_resource_icon("cloudfront:distribution", "cloudfront")
                    origins = [o.get("DomainName", "") for o in dist.get("Origins", {}).get("Items", [])]
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=dist.get("ARN", f"arn:aws:cloudfront::{account_id}:distribution/{dist_id}"),
                            resource_type="cloudfront:distribution",
                            service="cloudfront",
                            category=cat,
                            name=label,
                            sublabel=f"CloudFront ({dist.get('Status', 'Deployed')})",
                            icon_key=icon_key,
                            region="global",
                            state=dist.get("Status"),
                            metadata={
                                "distribution_id": dist_id,
                                "domain_name": domain,
                                "aliases": aliases,
                                "origins": origins,
                                "web_acl_id": dist.get("WebACLId", ""),
                            },
                        ),
                        dns_names=[domain] + aliases,
                    )
                    for orig in origins:
                        if orig:
                            with self._lock:
                                self._cf_origins.append((node_id, orig.lower().rstrip(".")))
        except (ClientError, BotoCoreError) as exc:
            self._warn(f"[global] CloudFront scan skipped: {exc.__class__.__name__}")

        # Global WAFv2 (CloudFront scope lives in us-east-1)
        try:
            waf = self.session.client("wafv2", region_name="us-east-1", config=BOTO_CONFIG)
            acls = waf.list_web_acls(Scope="CLOUDFRONT").get("WebACLs", [])
            for acl in acls:
                node_id = f"waf-global:{acl['Id']}"
                icon_key, cat = catalog.resolve_resource_icon("wafv2:web-acl", "wafv2")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=acl.get("ARN", ""),
                        resource_type="wafv2:web-acl",
                        service="wafv2",
                        category=cat,
                        name=acl["Name"],
                        sublabel="Global Web ACL",
                        icon_key=icon_key,
                        region="global",
                        metadata={"id": acl["Id"], "scope": "CLOUDFRONT"},
                    )
                )
        except Exception:
            pass

        # S3 Buckets (mapped to their home region!)
        self._emit_progress("Scanning S3 buckets and resolving bucket regions...", 1)
        try:
            s3 = self.session.client("s3", config=BOTO_CONFIG)
            buckets = s3.list_buckets().get("Buckets", [])

            def _inspect_bucket(b: Dict[str, Any]) -> None:
                bname = b["Name"]
                b_region = b.get("BucketRegion")
                if not b_region:
                    try:
                        loc = s3.get_bucket_location(Bucket=bname).get("LocationConstraint")
                        if loc is None:
                            b_region = "us-east-1"
                        elif loc == "EU":
                            b_region = "eu-west-1"
                        else:
                            b_region = loc
                    except Exception:
                        b_region = "global"

                # If user explicitly filtered regions, only include buckets in those regions (or global fallback)
                if self.requested_regions and b_region not in target_regions and b_region != "global":
                    return

                # Ensure region container exists if bucket is in a region
                if b_region != "global" and f"region:{b_region}" not in self.containers:
                    self._add_container(
                        AwsContainer(
                            id=f"region:{b_region}",
                            kind="region",
                            name=f"Region: {b_region}",
                            icon_key=GROUP_ICONS["region"],
                            parent_id=f"account:{account_id}",
                            region=b_region,
                        )
                    )

                node_id = f"s3:{bname}"
                icon_key, cat = catalog.resolve_resource_icon("s3:bucket", "s3")
                dns_candidates = [
                    f"{bname}.s3.amazonaws.com",
                    f"{bname}.s3.{b_region}.amazonaws.com",
                    f"{bname}.s3-{b_region}.amazonaws.com",
                    f"{bname}.s3-website-{b_region}.amazonaws.com",
                    f"{bname}.s3-website.{b_region}.amazonaws.com",
                ]
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=f"arn:aws:s3:::{bname}",
                        resource_type="s3:bucket",
                        service="s3",
                        category=cat,
                        name=bname,
                        sublabel=f"S3 Bucket ({b_region})",
                        icon_key=icon_key,
                        region=b_region,
                        metadata={
                            "bucket_name": bname,
                            "created": b.get("CreationDate").isoformat() if b.get("CreationDate") else "",
                        },
                    ),
                    dns_names=dns_candidates,
                )
                with self._lock:
                    self._s3_bucket_to_id[bname] = node_id

            with ThreadPoolExecutor(max_workers=10) as bpool:
                list(bpool.map(_inspect_bucket, buckets))
        except (ClientError, BotoCoreError) as exc:
            self._warn(f"[global] S3 scan skipped: {exc.__class__.__name__}")

    # -------------------------------------------------------------------------
    # Per-Region Collectors
    # -------------------------------------------------------------------------
    def _scan_single_region(self, account_id: str, region: str) -> None:
        # Step A: VPC & Networking Topology (must run first in region so subnet/VPC containers exist)
        self._emit_progress(f"[{region}] Scanning VPCs, Subnets, Gateways & Security Groups...", 1)
        self._scan_region_networking(account_id, region)

        # Step B-E: Run compute/containers, databases, integration/API, and security/ops/tag-sweep concurrently!
        tasks = [
            (f"[{region}] Scanning Compute, Containers & Load Balancers...", self._scan_region_compute_and_lb),
            (f"[{region}] Scanning Databases, Caching & Analytics...", self._scan_region_databases_and_analytics),
            (f"[{region}] Scanning Serverless, APIs & Messaging...", self._scan_region_serverless_and_integration),
            (f"[{region}] Scanning Storage, Security & Universal Tag Sweep...", self._scan_region_storage_security_and_sweep),
        ]
        with ThreadPoolExecutor(max_workers=4) as rpool:
            futs = {}
            for msg, fn in tasks:
                self._emit_progress(msg, 1)
                futs[rpool.submit(fn, account_id, region)] = msg
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:
                    self._warn(f"[{region}] Collector error: {exc}")

    def _scan_region_networking(self, account_id: str, region: str) -> None:
        ec2 = self.session.client("ec2", region_name=region, config=BOTO_CONFIG)
        reg_container_id = f"region:{region}"

        # 1. VPCs
        vpc_map: Dict[str, Dict[str, Any]] = {}
        try:
            for vpc in ec2.describe_vpcs().get("Vpcs", []):
                vid = vpc["VpcId"]
                tags = _tags_to_dict(vpc.get("Tags"))
                is_def = bool(vpc.get("IsDefault", False))
                cidr = vpc.get("CidrBlock", "")
                vname = _name_from_tags(tags, f"{vid} (Default)" if is_def else vid)
                vpc_map[vid] = vpc
                self._add_container(
                    AwsContainer(
                        id=f"vpc:{vid}",
                        kind="vpc",
                        name=f"{vname} ({cidr})" if cidr else vname,
                        icon_key=GROUP_ICONS["vpc"],
                        parent_id=reg_container_id,
                        region=region,
                        cidr=cidr,
                        is_default=is_def,
                        metadata={"vpc_id": vid, "cidr": cidr, "is_default": is_def, "tags": tags},
                    )
                )
        except (ClientError, BotoCoreError) as exc:
            self._warn(f"[{region}] EC2/VPC access error: {exc.__class__.__name__}")
            return

        if not vpc_map:
            return

        # 2. Route Tables -> Determine Public vs Private Subnets + NAT/IGW routes
        public_subnets: Set[str] = set()
        main_rt_is_public: Dict[str, bool] = {}
        explicit_subnet_rt_public: Dict[str, bool] = {}
        subnet_nat_targets: Dict[str, List[str]] = {}

        try:
            for rt in ec2.describe_route_tables().get("RouteTables", []):
                vid = rt.get("VpcId", "")
                routes = rt.get("Routes", [])
                has_igw = any(
                    str(r.get("GatewayId", "")).startswith("igw-") and r.get("State") != "blackhole"
                    for r in routes
                )
                nat_ids = [
                    r["NatGatewayId"]
                    for r in routes
                    if r.get("NatGatewayId")
                ]
                for assoc in rt.get("Associations", []):
                    if assoc.get("Main"):
                        main_rt_is_public[vid] = has_igw
                    sub_id = assoc.get("SubnetId")
                    if sub_id:
                        explicit_subnet_rt_public[sub_id] = has_igw
                        if nat_ids:
                            subnet_nat_targets[sub_id] = nat_ids
        except Exception:
            pass

        # 3. Subnets
        try:
            for sub in ec2.describe_subnets().get("Subnets", []):
                sid = sub["SubnetId"]
                vid = sub["VpcId"]
                tags = _tags_to_dict(sub.get("Tags"))
                az = sub.get("AvailabilityZone", "")
                cidr = sub.get("CidrBlock", "")
                is_pub = explicit_subnet_rt_public.get(
                    sid,
                    main_rt_is_public.get(vid, bool(sub.get("MapPublicIpOnLaunch", False))),
                )
                if is_pub:
                    public_subnets.add(sid)
                kind = "public_subnet" if is_pub else "private_subnet"
                sname = _name_from_tags(tags, sid)
                az_short = az.split("-")[-1] if az else ""
                display = f"{sname} [{az_short}]" if az_short and az_short not in sname else sname
                self._add_container(
                    AwsContainer(
                        id=f"subnet:{sid}",
                        kind=kind,
                        name=display,
                        icon_key=GROUP_ICONS[kind],
                        parent_id=f"vpc:{vid}",
                        region=region,
                        cidr=cidr,
                        az=az,
                        is_default=vpc_map.get(vid, {}).get("IsDefault", False),
                        metadata={
                            "subnet_id": sid,
                            "vpc_id": vid,
                            "cidr": cidr,
                            "az": az,
                            "public": is_pub,
                            "available_ips": sub.get("AvailableIpAddressCount"),
                            "tags": tags,
                        },
                    )
                )
        except Exception:
            pass

        # 4. Internet Gateways
        vpc_igw_node: Dict[str, str] = {}
        try:
            for igw in ec2.describe_internet_gateways().get("InternetGateways", []):
                igw_id = igw["InternetGatewayId"]
                tags = _tags_to_dict(igw.get("Tags"))
                attachments = igw.get("Attachments", [])
                vid = attachments[0].get("VpcId") if attachments else None
                # Skip default VPC's idle IGW unless default VPC actually has resources (handled in UI filter too)
                node_id = f"igw:{igw_id}"
                icon_key, cat = catalog.resolve_resource_icon("ec2:internet-gateway", "ec2")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=f"arn:aws:ec2:{region}:{account_id}:internet-gateway/{igw_id}",
                        resource_type="ec2:internet-gateway",
                        service="vpc",
                        category=cat,
                        name=_name_from_tags(tags, igw_id),
                        sublabel="Internet Gateway",
                        icon_key=icon_key,
                        region=region,
                        vpc_id=vid,
                        tags=tags,
                        metadata={"igw_id": igw_id, "vpc_id": vid},
                    )
                )
                if vid:
                    vpc_igw_node[vid] = node_id
        except Exception:
            pass

        # 5. NAT Gateways
        try:
            for nat in ec2.describe_nat_gateways().get("NatGateways", []):
                if nat.get("State") in ("deleted", "deleting"):
                    continue
                nat_id = nat["NatGatewayId"]
                vid = nat.get("VpcId")
                sid = nat.get("SubnetId")
                tags = _tags_to_dict(nat.get("Tags"))
                node_id = f"nat:{nat_id}"
                icon_key, cat = catalog.resolve_resource_icon("ec2:nat-gateway", "ec2")
                pub_ips = [a.get("PublicIp") for a in nat.get("NatGatewayAddresses", []) if a.get("PublicIp")]
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=f"arn:aws:ec2:{region}:{account_id}:natgateway/{nat_id}",
                        resource_type="ec2:nat-gateway",
                        service="vpc",
                        category=cat,
                        name=_name_from_tags(tags, nat_id),
                        sublabel=f"NAT ({pub_ips[0]})" if pub_ips else "NAT Gateway",
                        icon_key=icon_key,
                        region=region,
                        vpc_id=vid,
                        subnet_id=sid,
                        state=nat.get("State"),
                        tags=tags,
                        metadata={"nat_gateway_id": nat_id, "public_ips": pub_ips},
                    )
                )
                if vid and vid in vpc_igw_node:
                    self._add_edge(node_id, vpc_igw_node[vid], "routing", "Egress")
        except Exception:
            pass

        # 6. VPC Endpoints
        try:
            for vpce in ec2.describe_vpc_endpoints().get("VpcEndpoints", []):
                if vpce.get("State") in ("deleted", "deleting"):
                    continue
                ep_id = vpce["VpcEndpointId"]
                vid = vpce.get("VpcId")
                sids = vpce.get("SubnetIds", []) or []
                svc_name = vpce.get("ServiceName", "")
                short_svc = svc_name.split(".")[-1] if svc_name else "endpoint"
                tags = _tags_to_dict(vpce.get("Tags"))
                node_id = f"vpce:{ep_id}"
                icon_key, cat = catalog.resolve_resource_icon("ec2:vpc-endpoint", "ec2")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=f"arn:aws:ec2:{region}:{account_id}:vpc-endpoint/{ep_id}",
                        resource_type="ec2:vpc-endpoint",
                        service="vpc",
                        category=cat,
                        name=_name_from_tags(tags, f"{short_svc} endpoint"),
                        sublabel=f"{vpce.get('VpcEndpointType', 'Gateway')} Endpoint",
                        icon_key=icon_key,
                        region=region,
                        vpc_id=vid,
                        subnet_id=sids[0] if sids else None,
                        subnet_ids=sids,
                        security_group_ids=[g["GroupId"] for g in vpce.get("Groups", []) if g.get("GroupId")],
                        state=vpce.get("State"),
                        tags=tags,
                        metadata={"vpc_endpoint_id": ep_id, "service_name": svc_name},
                    )
                )
        except Exception:
            pass

        # 7. Transit Gateways & VPC Peering
        try:
            for tgw in ec2.describe_transit_gateways().get("TransitGateways", []):
                if tgw.get("State") in ("deleted", "deleting"):
                    continue
                tgw_id = tgw["TransitGatewayId"]
                tags = _tags_to_dict(tgw.get("Tags"))
                node_id = f"tgw:{tgw_id}"
                icon_key, cat = catalog.resolve_resource_icon("ec2:transit-gateway", "ec2")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=tgw.get("TransitGatewayArn", f"arn:aws:ec2:{region}:{account_id}:transit-gateway/{tgw_id}"),
                        resource_type="ec2:transit-gateway",
                        service="vpc",
                        category=cat,
                        name=_name_from_tags(tags, tgw_id),
                        sublabel="Transit Gateway",
                        icon_key=icon_key,
                        region=region,
                        state=tgw.get("State"),
                        tags=tags,
                    )
                )
        except Exception:
            pass

        try:
            for pcx in ec2.describe_vpc_peering_connections().get("VpcPeeringConnections", []):
                st = pcx.get("Status", {}).get("Code", "")
                if st in ("deleted", "deleting", "rejected", "failed", "expired"):
                    continue
                pcx_id = pcx["VpcPeeringConnectionId"]
                req_vpc = pcx.get("RequesterVpcInfo", {}).get("VpcId", "")
                acc_vpc = pcx.get("AccepterVpcInfo", {}).get("VpcId", "")
                tags = _tags_to_dict(pcx.get("Tags"))
                node_id = f"pcx:{pcx_id}"
                icon_key, cat = catalog.resolve_resource_icon("ec2:vpc-peering-connection", "ec2")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=f"arn:aws:ec2:{region}:{account_id}:vpc-peering-connection/{pcx_id}",
                        resource_type="ec2:vpc-peering-connection",
                        service="vpc",
                        category=cat,
                        name=_name_from_tags(tags, pcx_id),
                        sublabel=f"{req_vpc} <-> {acc_vpc}",
                        icon_key=icon_key,
                        region=region,
                        vpc_id=req_vpc or None,
                        state=st,
                        tags=tags,
                        metadata={"requester_vpc": req_vpc, "accepter_vpc": acc_vpc},
                    )
                )
                with self._lock:
                    self._peering_pairs.append((node_id, req_vpc, acc_vpc))
        except Exception:
            pass

        # 8. Security Groups (capture SG -> SG rules for automatic tier traffic edges)
        try:
            for sg in ec2.describe_security_groups().get("SecurityGroups", []):
                sg_id = sg["GroupId"]
                sources: List[Tuple[str, str]] = []
                for perm in sg.get("IpPermissions", []):
                    proto = perm.get("IpProtocol", "-1")
                    fport = perm.get("FromPort")
                    tport = perm.get("ToPort")
                    if proto == "-1":
                        port_lbl = "All Traffic"
                    elif fport == tport and fport is not None:
                        port_lbl = f"{proto.upper()} {fport}"
                    elif fport is not None and tport is not None:
                        port_lbl = f"{proto.upper()} {fport}-{tport}"
                    else:
                        port_lbl = proto.upper()
                    for pair in perm.get("UserIdGroupPairs", []):
                        src_sg = pair.get("GroupId")
                        if src_sg and src_sg != sg_id:
                            sources.append((src_sg, port_lbl))
                if sources:
                    with self._lock:
                        self._sg_inbound_sources[sg_id] = sources
        except Exception:
            pass

    def _scan_region_compute_and_lb(self, account_id: str, region: str) -> None:
        ec2 = self.session.client("ec2", region_name=region, config=BOTO_CONFIG)

        # Auto Scaling Groups
        instance_to_asg: Dict[str, str] = {}
        try:
            asg_client = self.session.client("autoscaling", region_name=region, config=BOTO_CONFIG)
            for page in asg_client.get_paginator("describe_auto_scaling_groups").paginate():
                for asg in page.get("AutoScalingGroups", []):
                    asg_name = asg["AutoScalingGroupName"]
                    asg_id = f"asg:{region}:{asg_name}"
                    sub_csv = asg.get("VPCZoneIdentifier", "")
                    sids = [s.strip() for s in sub_csv.split(",") if s.strip()]
                    for inst in asg.get("Instances", []):
                        if inst.get("InstanceId"):
                            instance_to_asg[inst["InstanceId"]] = asg_id
                    # Also register ASG metadata container if it has instances or desired > 0
                    if sids:
                        sub_container = self.containers.get(f"subnet:{sids[0]}")
                        vid = sub_container.metadata.get("vpc_id") if sub_container else None
                        if vid:
                            self._add_container(
                                AwsContainer(
                                    id=asg_id,
                                    kind="asg",
                                    name=f"ASG: {asg_name} ({asg.get('DesiredCapacity', 0)})",
                                    icon_key=GROUP_ICONS["asg"],
                                    parent_id=f"vpc:{vid}",
                                    region=region,
                                    metadata={
                                        "min": asg.get("MinSize"),
                                        "max": asg.get("MaxSize"),
                                        "desired": asg.get("DesiredCapacity"),
                                        "subnets": sids,
                                    },
                                )
                            )
        except Exception:
            pass

        # EC2 Instances
        try:
            paginator = ec2.get_paginator("describe_instances")
            for page in paginator.paginate():
                for resv in page.get("Reservations", []):
                    for inst in resv.get("Instances", []):
                        state = inst.get("State", {}).get("Name", "")
                        if state == "terminated":
                            continue
                        iid = inst["InstanceId"]
                        vid = inst.get("VpcId")
                        sid = inst.get("SubnetId")
                        itype = inst.get("InstanceType", "ec2")
                        tags = _tags_to_dict(inst.get("Tags"))
                        node_id = f"ec2:{iid}"
                        icon_key, cat = catalog.resolve_resource_icon("ec2:instance", "ec2")
                        sgs = [g["GroupId"] for g in inst.get("SecurityGroups", []) if g.get("GroupId")]
                        priv_ip = inst.get("PrivateIpAddress")
                        pub_ip = inst.get("PublicIpAddress")
                        self._add_resource(
                            AwsResource(
                                id=node_id,
                                arn=f"arn:aws:ec2:{region}:{account_id}:instance/{iid}",
                                resource_type="ec2:instance",
                                service="ec2",
                                category=cat,
                                name=_name_from_tags(tags, iid),
                                sublabel=f"{itype} ({state})",
                                icon_key=icon_key,
                                region=region,
                                vpc_id=vid,
                                subnet_id=sid,
                                az=inst.get("Placement", {}).get("AvailabilityZone"),
                                asg_id=instance_to_asg.get(iid),
                                security_group_ids=sgs,
                                state=state,
                                tags=tags,
                                metadata={
                                    "instance_id": iid,
                                    "instance_type": itype,
                                    "private_ip": priv_ip,
                                    "public_ip": pub_ip,
                                    "platform": inst.get("PlatformDetails", "Linux/UNIX"),
                                },
                            ),
                            dns_names=[inst.get("PublicDnsName", ""), inst.get("PrivateDnsName", "")],
                        )
                        if vid and priv_ip:
                            with self._lock:
                                self._private_ip_to_id[(vid, priv_ip)] = node_id
        except Exception:
            pass

        # ECS Clusters & Services (run before ELBv2 target matching so TargetGroup ARNs link cleanly)
        tg_arn_to_ecs_service: Dict[str, List[str]] = {}
        try:
            ecs = self.session.client("ecs", region_name=region, config=BOTO_CONFIG)
            cluster_arns = []
            for page in ecs.get_paginator("list_clusters").paginate():
                cluster_arns.extend(page.get("clusterArns", []))
            if cluster_arns:
                clusters = ecs.describe_clusters(clusters=cluster_arns, include=["TAGS"]).get("clusters", [])
                for cl in clusters:
                    cl_name = cl["clusterName"]
                    cl_arn = cl["clusterArn"]
                    svc_arns = []
                    for spage in ecs.get_paginator("list_services").paginate(cluster=cl_arn):
                        svc_arns.extend(spage.get("serviceArns", []))

                    if not svc_arns:
                        # Standalone cluster node
                        node_id = f"ecs-cluster:{region}:{cl_name}"
                        icon_key, cat = catalog.resolve_resource_icon("ecs:cluster", "ecs")
                        self._add_resource(
                            AwsResource(
                                id=node_id,
                                arn=cl_arn,
                                resource_type="ecs:cluster",
                                service="ecs",
                                category=cat,
                                name=cl_name,
                                sublabel=f"ECS Cluster ({cl.get('runningTasksCount', 0)} tasks)",
                                icon_key=icon_key,
                                region=region,
                                state=cl.get("status"),
                                tags=_tags_to_dict(cl.get("tags")),
                            )
                        )
                    else:
                        # Describe services in batches of 10
                        for idx in range(0, len(svc_arns), 10):
                            batch = svc_arns[idx:idx + 10]
                            svcs = ecs.describe_services(cluster=cl_arn, services=batch, include=["TAGS"]).get("services", [])
                            for svc in svcs:
                                sname = svc["serviceName"]
                                launch = svc.get("launchType", "EC2")
                                rtype = "ecs:fargate-service" if launch == "FARGATE" else "ecs:service"
                                icon_key, cat = catalog.resolve_resource_icon(rtype, "ecs")
                                awsvpc = svc.get("networkConfiguration", {}).get("awsvpcConfiguration", {})
                                sids = awsvpc.get("subnets", []) or []
                                sgs = awsvpc.get("securityGroups", []) or []
                                vid = None
                                if sids and f"subnet:{sids[0]}" in self.containers:
                                    vid = self.containers[f"subnet:{sids[0]}"].metadata.get("vpc_id")
                                node_id = f"ecs-svc:{region}:{cl_name}:{sname}"
                                self._add_resource(
                                    AwsResource(
                                        id=node_id,
                                        arn=svc["serviceArn"],
                                        resource_type=rtype,
                                        service="ecs",
                                        category=cat,
                                        name=sname,
                                        sublabel=f"{launch} ({svc.get('runningCount', 0)}/{svc.get('desiredCount', 0)} tasks)",
                                        icon_key=icon_key,
                                        region=region,
                                        vpc_id=vid,
                                        subnet_id=sids[0] if sids else None,
                                        subnet_ids=sids,
                                        security_group_ids=sgs,
                                        state=svc.get("status"),
                                        tags=_tags_to_dict(svc.get("tags")),
                                        metadata={"cluster": cl_name, "launch_type": launch},
                                    )
                                )
                                for lb_ref in svc.get("loadBalancers", []):
                                    tg_arn = lb_ref.get("targetGroupArn")
                                    if tg_arn:
                                        tg_arn_to_ecs_service.setdefault(tg_arn, []).append(node_id)
        except Exception:
            pass

        # EKS Clusters
        try:
            eks = self.session.client("eks", region_name=region, config=BOTO_CONFIG)
            for cl_name in eks.list_clusters().get("clusters", []):
                cl = eks.describe_cluster(name=cl_name).get("cluster", {})
                vpc_cfg = cl.get("resourcesVpcConfig", {})
                vid = vpc_cfg.get("vpcId")
                sids = vpc_cfg.get("subnetIds", []) or []
                sgs = list(set((vpc_cfg.get("securityGroupIds") or []) + (
                    [vpc_cfg["clusterSecurityGroupId"]] if vpc_cfg.get("clusterSecurityGroupId") else []
                )))
                node_id = f"eks:{region}:{cl_name}"
                icon_key, cat = catalog.resolve_resource_icon("eks:cluster", "eks")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=cl.get("arn", ""),
                        resource_type="eks:cluster",
                        service="eks",
                        category=cat,
                        name=cl_name,
                        sublabel=f"EKS v{cl.get('version', '')} ({cl.get('status', '')})",
                        icon_key=icon_key,
                        region=region,
                        vpc_id=vid,
                        subnet_id=sids[0] if sids else None,
                        subnet_ids=sids,
                        security_group_ids=sgs,
                        state=cl.get("status"),
                        tags=cl.get("tags") or {},
                    )
                )
        except Exception:
            pass

        # Elastic Load Balancers v2 (ALB / NLB / GWLB) + Target Groups
        try:
            elbv2 = self.session.client("elbv2", region_name=region, config=BOTO_CONFIG)
            lbs = []
            for page in elbv2.get_paginator("describe_load_balancers").paginate():
                lbs.extend(page.get("LoadBalancers", []))

            lb_arn_to_node: Dict[str, Tuple[str, Optional[str]]] = {}
            for lb in lbs:
                lb_arn = lb["LoadBalancerArn"]
                lb_name = lb["LoadBalancerName"]
                lb_type = lb.get("Type", "application")
                scheme = lb.get("Scheme", "internet-facing")
                vid = lb.get("VpcId")
                azs = lb.get("AvailabilityZones", [])
                sids = [z["SubnetId"] for z in azs if z.get("SubnetId")]
                rtype_map = {
                    "application": "elbv2:alb",
                    "network": "elbv2:nlb",
                    "gateway": "elbv2:gwlb",
                }
                rtype = rtype_map.get(lb_type, "elbv2:loadbalancer")
                icon_key, cat = catalog.resolve_resource_icon(rtype, "elbv2")
                node_id = f"elbv2:{region}:{lb_name}"
                dns_name = lb.get("DNSName", "")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=lb_arn,
                        resource_type=rtype,
                        service="elbv2",
                        category=cat,
                        name=lb_name,
                        sublabel=f"{lb_type.upper()} ({scheme})",
                        icon_key=icon_key,
                        region=region,
                        vpc_id=vid,
                        subnet_id=sids[0] if sids else None,
                        subnet_ids=sids,
                        security_group_ids=lb.get("SecurityGroups", []) or [],
                        state=lb.get("State", {}).get("Code"),
                        metadata={"dns_name": dns_name, "scheme": scheme, "type": lb_type},
                    ),
                    dns_names=[dns_name],
                )
                lb_arn_to_node[lb_arn] = (node_id, vid)

                # If internet-facing in a VPC with an Internet Gateway, connect IGW -> LB
                if scheme == "internet-facing" and vid:
                    for r in list(self.resources.values()):
                        if r.resource_type == "ec2:internet-gateway" and r.vpc_id == vid:
                            self._add_edge(r.id, node_id, "routing", "HTTPS/HTTP")

            # Target Groups -> Connect LB -> EC2 / ECS / Lambda / IP targets
            if lb_arn_to_node:
                for tg_page in elbv2.get_paginator("describe_target_groups").paginate():
                    for tg in tg_page.get("TargetGroups", []):
                        tg_arn = tg["TargetGroupArn"]
                        parent_lbs = tg.get("LoadBalancerArns", []) or []
                        if not parent_lbs:
                            continue
                        port_lbl = f"{tg.get('Protocol', 'TCP')} {tg.get('Port', '')}".strip()
                        # Link ECS services registered to this target group
                        for lb_arn in parent_lbs:
                            lb_entry = lb_arn_to_node.get(lb_arn)
                            if not lb_entry:
                                continue
                            lb_node_id, lb_vpc_id = lb_entry
                            for ecs_node_id in tg_arn_to_ecs_service.get(tg_arn, []):
                                self._add_edge(lb_node_id, ecs_node_id, "traffic", port_lbl)

                            # Also inspect target health for EC2 instances, Lambdas, or private IPs
                            try:
                                ths = elbv2.describe_target_health(TargetGroupArn=tg_arn).get("TargetHealthDescriptions", [])
                                for th in ths:
                                    tgt_id = th.get("Target", {}).get("Id", "")
                                    ttype = tg.get("TargetType", "instance")
                                    if ttype == "instance" and tgt_id.startswith("i-"):
                                        self._add_edge(lb_node_id, f"ec2:{tgt_id}", "traffic", port_lbl)
                                    elif ttype == "lambda" and tgt_id:
                                        with self._lock:
                                            lam_id = self._arn_to_id.get(tgt_id)
                                        if lam_id:
                                            self._add_edge(lb_node_id, lam_id, "traffic", "Invoke")
                                    elif ttype == "ip" and lb_vpc_id and tgt_id:
                                        with self._lock:
                                            ip_target = self._private_ip_to_id.get((lb_vpc_id, tgt_id))
                                        if ip_target:
                                            self._add_edge(lb_node_id, ip_target, "traffic", port_lbl)
                            except Exception:
                                pass
        except Exception:
            pass

    def _scan_region_databases_and_analytics(self, account_id: str, region: str) -> None:
        # RDS & Aurora Clusters + Instances
        try:
            rds = self.session.client("rds", region_name=region, config=BOTO_CONFIG)
            subnet_groups: Dict[str, Tuple[Optional[str], List[str]]] = {}
            try:
                for page in rds.get_paginator("describe_db_subnet_groups").paginate():
                    for sg in page.get("DBSubnetGroups", []):
                        vid = sg.get("VpcId")
                        sids = [s["SubnetIdentifier"] for s in sg.get("Subnets", []) if s.get("SubnetIdentifier")]
                        subnet_groups[sg["DBSubnetGroupName"]] = (vid, sids)
            except Exception:
                pass

            clustered_member_ids: Set[str] = set()
            for page in rds.get_paginator("describe_db_clusters").paginate():
                for cl in page.get("DBClusters", []):
                    cid = cl["DBClusterIdentifier"]
                    engine = cl.get("Engine", "aurora")
                    sub_grp = cl.get("DBSubnetGroup", "")
                    vid, sids = subnet_groups.get(sub_grp, (None, []))
                    sgs = [g["VpcSecurityGroupId"] for g in cl.get("VpcSecurityGroups", []) if g.get("VpcSecurityGroupId")]
                    members = [m["DBInstanceIdentifier"] for m in cl.get("DBClusterMembers", []) if m.get("DBInstanceIdentifier")]
                    clustered_member_ids.update(members)

                    if "docdb" in engine:
                        rtype, svc = "docdb:cluster", "docdb"
                    elif "neptune" in engine:
                        rtype, svc = "neptune:cluster", "neptune"
                    else:
                        rtype, svc = "rds:aurora-cluster", "rds"

                    icon_key, cat = catalog.resolve_resource_icon(rtype, svc)
                    node_id = f"rds-cluster:{region}:{cid}"
                    ep = cl.get("Endpoint", "")
                    reader_ep = cl.get("ReaderEndpoint", "")
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=cl.get("DBClusterArn", ""),
                            resource_type=rtype,
                            service=svc,
                            category=cat,
                            name=cid,
                            sublabel=f"{engine} {cl.get('EngineVersion', '')} ({len(members)} nodes)".strip(),
                            icon_key=icon_key,
                            region=region,
                            vpc_id=vid,
                            subnet_id=sids[0] if sids else None,
                            subnet_ids=sids,
                            security_group_ids=sgs,
                            state=cl.get("Status"),
                            tags=_tags_to_dict(cl.get("TagList")),
                            metadata={
                                "endpoint": ep,
                                "reader_endpoint": reader_ep,
                                "members": members,
                                "multi_az": cl.get("MultiAZ"),
                            },
                        ),
                        dns_names=[ep, reader_ep],
                    )

            for page in rds.get_paginator("describe_db_instances").paginate():
                for db in page.get("DBInstances", []):
                    dbid = db["DBInstanceIdentifier"]
                    if dbid in clustered_member_ids:
                        continue  # Already represented cleanly by its Aurora/DocDB/Neptune cluster
                    engine = db.get("Engine", "rds")
                    sub_grp_obj = db.get("DBSubnetGroup") or {}
                    vid = sub_grp_obj.get("VpcId")
                    sids = [s["SubnetIdentifier"] for s in sub_grp_obj.get("Subnets", []) if s.get("SubnetIdentifier")]
                    sgs = [g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", []) if g.get("VpcSecurityGroupId")]
                    ep = (db.get("Endpoint") or {}).get("Address", "")
                    node_id = f"rds:{region}:{dbid}"
                    icon_key, cat = catalog.resolve_resource_icon("rds:instance", "rds")
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=db.get("DBInstanceArn", ""),
                            resource_type="rds:instance",
                            service="rds",
                            category=cat,
                            name=dbid,
                            sublabel=f"{engine} ({db.get('DBInstanceClass', '')})",
                            icon_key=icon_key,
                            region=region,
                            vpc_id=vid,
                            subnet_id=sids[0] if sids else None,
                            subnet_ids=sids,
                            az=db.get("AvailabilityZone"),
                            security_group_ids=sgs,
                            state=db.get("DBInstanceStatus"),
                            tags=_tags_to_dict(db.get("TagList")),
                            metadata={
                                "endpoint": ep,
                                "multi_az": db.get("MultiAZ"),
                                "storage_gb": db.get("AllocatedStorage"),
                            },
                        ),
                        dns_names=[ep],
                    )
        except Exception:
            pass

        # DynamoDB Tables
        try:
            ddb = self.session.client("dynamodb", region_name=region, config=BOTO_CONFIG)
            tnames = []
            for page in ddb.get_paginator("list_tables").paginate():
                tnames.extend(page.get("TableNames", []))
            for tname in tnames:
                try:
                    t = ddb.describe_table(TableName=tname).get("Table", {})
                    t_arn = t.get("TableArn", f"arn:aws:dynamodb:{region}:{account_id}:table/{tname}")
                    stream_arn = t.get("LatestStreamArn", "")
                    billing = (t.get("BillingModeSummary") or {}).get("BillingMode", "PROVISIONED")
                    node_id = f"ddb:{region}:{tname}"
                    icon_key, cat = catalog.resolve_resource_icon("dynamodb:table", "dynamodb")
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=t_arn,
                            resource_type="dynamodb:table",
                            service="dynamodb",
                            category=cat,
                            name=tname,
                            sublabel=f"DynamoDB ({t.get('ItemCount', 0)} items, {billing.lower()})",
                            icon_key=icon_key,
                            region=region,
                            state=t.get("TableStatus"),
                            metadata={"item_count": t.get("ItemCount", 0), "size_bytes": t.get("TableSizeBytes", 0)},
                        )
                    )
                    with self._lock:
                        self._ddb_table_to_id[(region, tname)] = node_id
                        if stream_arn:
                            self._arn_to_id[stream_arn] = node_id
                except Exception:
                    pass
        except Exception:
            pass

        # ElastiCache Clusters
        try:
            ecache = self.session.client("elasticache", region_name=region, config=BOTO_CONFIG)
            csub_map: Dict[str, Tuple[Optional[str], List[str]]] = {}
            try:
                for sg in ecache.describe_cache_subnet_groups().get("CacheSubnetGroups", []):
                    csub_map[sg["CacheSubnetGroupName"]] = (
                        sg.get("VpcId"),
                        [s["SubnetIdentifier"] for s in sg.get("Subnets", []) if s.get("SubnetIdentifier")],
                    )
            except Exception:
                pass

            for cc in ecache.describe_cache_clusters(ShowCacheNodeInfo=True).get("CacheClusters", []):
                cid = cc["CacheClusterId"]
                vid, sids = csub_map.get(cc.get("CacheSubnetGroupName", ""), (None, []))
                sgs = [g["SecurityGroupId"] for g in cc.get("SecurityGroups", []) if g.get("SecurityGroupId")]
                ep = (cc.get("ConfigurationEndpoint") or {}).get("Address", "")
                if not ep and cc.get("CacheNodes"):
                    ep = (cc["CacheNodes"][0].get("Endpoint") or {}).get("Address", "")
                node_id = f"elasticache:{region}:{cid}"
                icon_key, cat = catalog.resolve_resource_icon("elasticache:cluster", "elasticache")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=cc.get("ARN", ""),
                        resource_type="elasticache:cluster",
                        service="elasticache",
                        category=cat,
                        name=cid,
                        sublabel=f"{cc.get('Engine', 'redis')} ({cc.get('CacheNodeType', '')})",
                        icon_key=icon_key,
                        region=region,
                        vpc_id=vid,
                        subnet_id=sids[0] if sids else None,
                        subnet_ids=sids,
                        security_group_ids=sgs,
                        state=cc.get("CacheClusterStatus"),
                        metadata={"endpoint": ep, "engine_version": cc.get("EngineVersion")},
                    ),
                    dns_names=[ep],
                )
        except Exception:
            pass

        # OpenSearch Domains
        try:
            os_client = self.session.client("opensearch", region_name=region, config=BOTO_CONFIG)
            dnames = [d["DomainName"] for d in os_client.list_domain_names().get("DomainNames", [])]
            if dnames:
                for dom in os_client.describe_domains(DomainNames=dnames[:50]).get("DomainStatusList", []):
                    dname = dom["DomainName"]
                    vpc_opts = dom.get("VPCOptions") or {}
                    vid = vpc_opts.get("VPCId")
                    sids = vpc_opts.get("SubnetIds", []) or []
                    sgs = vpc_opts.get("SecurityGroupIds", []) or []
                    node_id = f"opensearch:{region}:{dname}"
                    icon_key, cat = catalog.resolve_resource_icon("opensearch:domain", "opensearch")
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=dom.get("ARN", ""),
                            resource_type="opensearch:domain",
                            service="opensearch",
                            category=cat,
                            name=dname,
                            sublabel=f"OpenSearch {dom.get('EngineVersion', '')}",
                            icon_key=icon_key,
                            region=region,
                            vpc_id=vid,
                            subnet_id=sids[0] if sids else None,
                            subnet_ids=sids,
                            security_group_ids=sgs,
                        )
                    )
        except Exception:
            pass

        # Kinesis Streams
        try:
            kin = self.session.client("kinesis", region_name=region, config=BOTO_CONFIG)
            for sname in kin.list_streams().get("StreamNames", []):
                s_arn = f"arn:aws:kinesis:{region}:{account_id}:stream/{sname}"
                node_id = f"kinesis:{region}:{sname}"
                icon_key, cat = catalog.resolve_resource_icon("kinesis:stream", "kinesis")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=s_arn,
                        resource_type="kinesis:stream",
                        service="kinesis",
                        category=cat,
                        name=sname,
                        sublabel="Kinesis Data Stream",
                        icon_key=icon_key,
                        region=region,
                    )
                )
        except Exception:
            pass

    def _scan_region_serverless_and_integration(self, account_id: str, region: str) -> None:
        # SQS Queues
        try:
            sqs = self.session.client("sqs", region_name=region, config=BOTO_CONFIG)
            q_urls = sqs.list_queues().get("QueueUrls", []) or []
            for qurl in q_urls:
                qname = qurl.rstrip("/").split("/")[-1]
                q_arn = f"arn:aws:sqs:{region}:{account_id}:{qname}"
                node_id = f"sqs:{region}:{qname}"
                icon_key, cat = catalog.resolve_resource_icon("sqs:queue", "sqs")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=q_arn,
                        resource_type="sqs:queue",
                        service="sqs",
                        category=cat,
                        name=qname,
                        sublabel="FIFO Queue" if qname.endswith(".fifo") else "Standard Queue",
                        icon_key=icon_key,
                        region=region,
                        metadata={"queue_url": qurl},
                    )
                )
                with self._lock:
                    self._sqs_name_to_id[(region, qname)] = node_id
        except Exception:
            pass

        # SNS Topics & Subscriptions
        try:
            sns = self.session.client("sns", region_name=region, config=BOTO_CONFIG)
            for page in sns.get_paginator("list_topics").paginate():
                for top in page.get("Topics", []):
                    t_arn = top["TopicArn"]
                    tname = t_arn.split(":")[-1]
                    node_id = f"sns:{region}:{tname}"
                    icon_key, cat = catalog.resolve_resource_icon("sns:topic", "sns")
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=t_arn,
                            resource_type="sns:topic",
                            service="sns",
                            category=cat,
                            name=tname,
                            sublabel="SNS Topic",
                            icon_key=icon_key,
                            region=region,
                        )
                    )
            for spage in sns.get_paginator("list_subscriptions").paginate():
                for sub in spage.get("Subscriptions", []):
                    t_arn = sub.get("TopicArn", "")
                    ep_arn = sub.get("Endpoint", "")
                    proto = sub.get("Protocol", "")
                    if proto in ("sqs", "lambda") and t_arn and ep_arn.startswith("arn:aws:"):
                        with self._lock:
                            src_id = self._arn_to_id.get(t_arn)
                        # Defer ARN target lookup via _arn_to_id in synthesis if lambda isn't scanned yet
                        if src_id:
                            self._add_edge(src_id, f"__arn__:{ep_arn}", "event", f"SNS->{proto.upper()}")
        except Exception:
            pass

        # AWS Lambda Functions + Event Source Mappings
        try:
            lam = self.session.client("lambda", region_name=region, config=BOTO_CONFIG)
            for page in lam.get_paginator("list_functions").paginate():
                for fn in page.get("Functions", []):
                    fname = fn["FunctionName"]
                    f_arn = fn["FunctionArn"]
                    vpc_cfg = fn.get("VpcConfig") or {}
                    vid = vpc_cfg.get("VpcId") or None
                    sids = vpc_cfg.get("SubnetIds") or []
                    sgs = vpc_cfg.get("SecurityGroupIds") or []
                    if not vid:
                        sids, sgs = [], []
                    node_id = f"lambda:{region}:{fname}"
                    icon_key, cat = catalog.resolve_resource_icon("lambda:function", "lambda")
                    runtime = fn.get("Runtime") or fn.get("PackageType", "Lambda")
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=f_arn,
                            resource_type="lambda:function",
                            service="lambda",
                            category=cat,
                            name=fname,
                            sublabel=f"{runtime} ({fn.get('MemorySize', 128)} MB)",
                            icon_key=icon_key,
                            region=region,
                            vpc_id=vid,
                            subnet_id=sids[0] if sids else None,
                            subnet_ids=sids,
                            security_group_ids=sgs,
                            metadata={
                                "runtime": runtime,
                                "memory_mb": fn.get("MemorySize"),
                                "timeout_s": fn.get("Timeout"),
                                "architectures": fn.get("Architectures", []),
                            },
                        )
                    )
                    # Extract candidate strings from env vars (ONLY used in-memory to match resource names/ARNs; never persisted!)
                    env_vars = (fn.get("Environment") or {}).get("Variables") or {}
                    if env_vars:
                        candidates = [str(v) for v in env_vars.values() if isinstance(v, str) and 2 < len(v) < 256]
                        if candidates:
                            with self._lock:
                                self._lambda_env_values.append((node_id, region, candidates))

            for esm_page in lam.get_paginator("list_event_source_mappings").paginate():
                for esm in esm_page.get("EventSourceMappings", []):
                    src_arn = esm.get("EventSourceArn", "")
                    fn_arn = esm.get("FunctionArn", "")
                    if src_arn and fn_arn:
                        # Strip :$LATEST or alias suffix on function ARN
                        base_fn_arn = ":".join(fn_arn.split(":")[:7])
                        self._add_edge(f"__arn__:{src_arn}", f"__arn__:{base_fn_arn}", "event", "Event Trigger")
        except Exception:
            pass

        # API Gateway REST (v1) & HTTP/WebSocket (v2)
        try:
            apigw = self.session.client("apigateway", region_name=region, config=BOTO_CONFIG)
            for item in apigw.get_rest_apis(limit=100).get("items", []):
                api_id = item["id"]
                api_name = item.get("name", api_id)
                node_id = f"apigw:{region}:{api_id}"
                icon_key, cat = catalog.resolve_resource_icon("apigateway:rest-api", "apigateway")
                dns = f"{api_id}.execute-api.{region}.amazonaws.com"
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=f"arn:aws:apigateway:{region}::/restapis/{api_id}",
                        resource_type="apigateway:rest-api",
                        service="apigateway",
                        category=cat,
                        name=api_name,
                        sublabel="REST API",
                        icon_key=icon_key,
                        region=region,
                        metadata={"api_id": api_id, "endpoint": dns},
                    ),
                    dns_names=[dns],
                )
                # Inspect REST resources for Lambda integrations
                try:
                    for res_item in apigw.get_resources(restApiId=api_id, embed=["methods"]).get("items", []):
                        for method_cfg in (res_item.get("resourceMethods") or {}).values():
                            uri = (method_cfg.get("methodIntegration") or {}).get("uri", "")
                            m = re.search(r"arn:aws:lambda:[^/]+:function:[^/:]+", uri)
                            if m:
                                self._add_edge(node_id, f"__arn__:{m.group(0)}", "traffic", "Invoke")
                except Exception:
                    pass
        except Exception:
            pass

        try:
            apigw2 = self.session.client("apigatewayv2", region_name=region, config=BOTO_CONFIG)
            for item in apigw2.get_apis().get("Items", []):
                api_id = item["ApiId"]
                proto = item.get("ProtocolType", "HTTP")
                node_id = f"apigwv2:{region}:{api_id}"
                icon_key, cat = catalog.resolve_resource_icon("apigatewayv2:api", "apigateway")
                ep = item.get("ApiEndpoint", "").replace("https://", "").replace("wss://", "")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=f"arn:aws:apigateway:{region}::/apis/{api_id}",
                        resource_type="apigatewayv2:api",
                        service="apigateway",
                        category=cat,
                        name=item.get("Name", api_id),
                        sublabel=f"{proto} API",
                        icon_key=icon_key,
                        region=region,
                        metadata={"api_id": api_id, "protocol": proto, "endpoint": ep},
                    ),
                    dns_names=[ep] if ep else None,
                )
                try:
                    for integ in apigw2.get_integrations(ApiId=api_id).get("Items", []):
                        uri = integ.get("IntegrationUri", "")
                        if uri.startswith("arn:aws:lambda:"):
                            base_arn = ":".join(uri.split(":")[:7])
                            self._add_edge(node_id, f"__arn__:{base_arn}", "traffic", "Route")
                        elif uri.startswith("arn:aws:elasticloadbalancing:"):
                            self._add_edge(node_id, f"__arn__:{uri}", "traffic", "VPC Link")
                except Exception:
                    pass
        except Exception:
            pass

        # EventBridge & Step Functions
        try:
            events = self.session.client("events", region_name=region, config=BOTO_CONFIG)
            for bus in events.list_event_buses().get("EventBuses", []):
                bname = bus["Name"]
                rules = events.list_rules(EventBusName=bname, Limit=50).get("Rules", [])
                # Only show default bus if it actually has user-created rules (skip AWS managed internal rules if empty)
                user_rules = [r for r in rules if not r.get("ManagedBy")]
                if bname == "default" and not user_rules:
                    continue
                node_id = f"eventbus:{region}:{bname}"
                icon_key, cat = catalog.resolve_resource_icon("events:event-bus", "events")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=bus.get("Arn", ""),
                        resource_type="events:event-bus",
                        service="events",
                        category=cat,
                        name=f"EventBridge ({bname})",
                        sublabel=f"{len(user_rules)} active rules",
                        icon_key=icon_key,
                        region=region,
                    )
                )
                for r in user_rules[:25]:
                    try:
                        targets = events.list_targets_by_rule(Rule=r["Name"], EventBusName=bname).get("Targets", [])
                        for t in targets:
                            t_arn = t.get("Arn", "")
                            if t_arn:
                                base_arn = ":".join(t_arn.split(":")[:7]) if ":lambda:" in t_arn else t_arn
                                self._add_edge(node_id, f"__arn__:{base_arn}", "event", r["Name"][:24])
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            sfn = self.session.client("stepfunctions", region_name=region, config=BOTO_CONFIG)
            for sm in sfn.list_state_machines().get("stateMachines", []):
                sm_name = sm["name"]
                node_id = f"sfn:{region}:{sm_name}"
                icon_key, cat = catalog.resolve_resource_icon("states:state-machine", "states")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=sm["stateMachineArn"],
                        resource_type="states:state-machine",
                        service="states",
                        category=cat,
                        name=sm_name,
                        sublabel=f"Step Functions ({sm.get('type', 'STANDARD')})",
                        icon_key=icon_key,
                        region=region,
                    )
                )
        except Exception:
            pass

    def _scan_region_storage_security_and_sweep(self, account_id: str, region: str) -> None:
        # EFS File Systems
        try:
            efs = self.session.client("efs", region_name=region, config=BOTO_CONFIG)
            for fs in efs.describe_file_systems().get("FileSystems", []):
                fs_id = fs["FileSystemId"]
                sids: List[str] = []
                sgs: List[str] = []
                vid: Optional[str] = None
                try:
                    mts = efs.describe_mount_targets(FileSystemId=fs_id).get("MountTargets", [])
                    for mt in mts:
                        if mt.get("SubnetId"):
                            sids.append(mt["SubnetId"])
                        if mt.get("VpcId"):
                            vid = mt["VpcId"]
                        try:
                            mt_sgs = efs.describe_mount_target_security_groups(MountTargetId=mt["MountTargetId"]).get("SecurityGroups", [])
                            sgs.extend(mt_sgs)
                        except Exception:
                            pass
                except Exception:
                    pass
                node_id = f"efs:{region}:{fs_id}"
                icon_key, cat = catalog.resolve_resource_icon("efs:file-system", "efs")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=fs.get("FileSystemArn", ""),
                        resource_type="efs:file-system",
                        service="efs",
                        category=cat,
                        name=fs.get("Name") or fs_id,
                        sublabel="EFS File System",
                        icon_key=icon_key,
                        region=region,
                        vpc_id=vid,
                        subnet_id=sids[0] if sids else None,
                        subnet_ids=sids,
                        security_group_ids=list(set(sgs)),
                        state=fs.get("LifeCycleState"),
                        tags=_tags_to_dict(fs.get("Tags")),
                    )
                )
        except Exception:
            pass

        # ECR Repositories
        try:
            ecr = self.session.client("ecr", region_name=region, config=BOTO_CONFIG)
            for page in ecr.get_paginator("describe_repositories").paginate():
                for repo in page.get("repositories", []):
                    rname = repo["repositoryName"]
                    node_id = f"ecr:{region}:{rname}"
                    icon_key, cat = catalog.resolve_resource_icon("ecr:repository", "ecr")
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=repo["repositoryArn"],
                            resource_type="ecr:repository",
                            service="ecr",
                            category=cat,
                            name=rname,
                            sublabel="ECR Repository",
                            icon_key=icon_key,
                            region=region,
                            metadata={"uri": repo.get("repositoryUri", "")},
                        )
                    )
        except Exception:
            pass

        # Secrets Manager & Customer-Managed KMS Keys & Regional WAFv2
        try:
            sm = self.session.client("secretsmanager", region_name=region, config=BOTO_CONFIG)
            for page in sm.get_paginator("list_secrets").paginate():
                for sec in page.get("SecretList", []):
                    sname = sec["Name"]
                    node_id = f"secret:{region}:{sname}"
                    icon_key, cat = catalog.resolve_resource_icon("secretsmanager:secret", "secretsmanager")
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=sec["ARN"],
                            resource_type="secretsmanager:secret",
                            service="secretsmanager",
                            category=cat,
                            name=sname,
                            sublabel="Secrets Manager",
                            icon_key=icon_key,
                            region=region,
                            tags=_tags_to_dict(sec.get("Tags")),
                        )
                    )
        except Exception:
            pass

        try:
            kms = self.session.client("kms", region_name=region, config=BOTO_CONFIG)
            for page in kms.get_paginator("list_aliases").paginate():
                for al in page.get("Aliases", []):
                    aname = al.get("AliasName", "")
                    key_id = al.get("TargetKeyId")
                    if not key_id or aname.startswith("alias/aws/"):
                        continue
                    k_arn = f"arn:aws:kms:{region}:{account_id}:key/{key_id}"
                    node_id = f"kms:{region}:{key_id}"
                    icon_key, cat = catalog.resolve_resource_icon("kms:key", "kms")
                    self._add_resource(
                        AwsResource(
                            id=node_id,
                            arn=k_arn,
                            resource_type="kms:key",
                            service="kms",
                            category=cat,
                            name=aname.replace("alias/", ""),
                            sublabel="Customer KMS Key",
                            icon_key=icon_key,
                            region=region,
                        )
                    )
        except Exception:
            pass

        try:
            waf = self.session.client("wafv2", region_name=region, config=BOTO_CONFIG)
            for acl in waf.list_web_acls(Scope="REGIONAL").get("WebACLs", []):
                node_id = f"waf:{region}:{acl['Id']}"
                acl_arn = acl.get("ARN", "")
                icon_key, cat = catalog.resolve_resource_icon("wafv2:web-acl", "wafv2")
                self._add_resource(
                    AwsResource(
                        id=node_id,
                        arn=acl_arn,
                        resource_type="wafv2:web-acl",
                        service="wafv2",
                        category=cat,
                        name=acl["Name"],
                        sublabel="Regional Web ACL",
                        icon_key=icon_key,
                        region=region,
                    )
                )
                if acl_arn:
                    try:
                        assoc_arns = waf.list_resources_for_web_acl(WebACLArn=acl_arn).get("ResourceArns", [])
                        for tgt_arn in assoc_arns:
                            self._add_edge(node_id, f"__arn__:{tgt_arn}", "security_group", "Protects")
                    except Exception:
                        pass
        except Exception:
            pass

        # Universal Resource Groups Tagging API Sweep (discovers tagged resources across all 200+ AWS services)
        if self.include_tag_sweep:
            try:
                tag_api = self.session.client("resourcegroupstaggingapi", region_name=region, config=BOTO_CONFIG)
                for page in tag_api.get_paginator("get_resources").paginate(ResourcesPerPage=100):
                    for item in page.get("ResourceTagMappingList", []):
                        arn = item.get("ResourceARN", "")
                        if not arn:
                            continue
                        with self._lock:
                            if arn in self._arn_to_id:
                                continue
                        parts = arn.split(":", 5)
                        if len(parts) < 6:
                            continue
                        svc = parts[2]
                        res_part = parts[5]
                        combo = f"{svc}:{res_part}"
                        if any(combo.startswith(pref) for pref in TAG_SWEEP_SKIP_PREFIXES):
                            continue
                        # Also check if resource ID suffix is already tracked (e.g. i-xxx, nat-xxx, igw-xxx, vpce-xxx)
                        short_id = res_part.split("/")[-1].split(":")[-1]
                        already_present = False
                        with self._lock:
                            for existing in self.resources.values():
                                if existing.region == region and (existing.name == short_id or existing.id.endswith(f":{short_id}")):
                                    already_present = True
                                    break
                        if already_present:
                            continue

                        tags = _tags_to_dict(item.get("Tags"))
                        rtype_sub = res_part.split("/")[0].split(":")[0] if ("/" in res_part or ":" in res_part) else "resource"
                        rtype = f"{svc}:{rtype_sub}"
                        icon_key, cat = catalog.resolve_resource_icon(rtype, svc)
                        node_id = f"tag:{region}:{svc}:{short_id}"
                        self._add_resource(
                            AwsResource(
                                id=node_id,
                                arn=arn,
                                resource_type=rtype,
                                service=svc,
                                category=cat,
                                name=_name_from_tags(tags, short_id),
                                sublabel=f"{svc.upper()} ({rtype_sub})",
                                icon_key=icon_key,
                                region=region,
                                tags=tags,
                            )
                        )
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Post-Scan Pruning & Relationship Synthesis
    # -------------------------------------------------------------------------
    def _prune_empty_regions(self) -> None:
        """Remove Region containers that have zero resources and zero non-default VPCs."""
        active_regions = {r.region for r in self.resources.values()}
        non_default_vpc_regions = {
            c.region for c in self.containers.values()
            if c.kind == "vpc" and not c.is_default and c.region
        }
        keep_regions = active_regions | non_default_vpc_regions
        # If everything was empty, keep at least the first region so diagram isn't blank
        if not keep_regions and self.requested_regions:
            keep_regions.add(self.requested_regions[0])

        to_delete: List[str] = []
        for cid, cont in self.containers.items():
            if cont.kind in ("region", "vpc", "public_subnet", "private_subnet", "asg") and cont.region:
                if cont.region not in keep_regions:
                    to_delete.append(cid)
        for cid in to_delete:
            self.containers.pop(cid, None)

    def _synthesize_relationships(self) -> None:
        """Resolve deferred ARN edges, DNS/Origin edges, VPC Endpoint edges, Lambda env refs, and SG tier flows."""
        # 1. Resolve deferred __arn__:<arn> placeholders
        deferred = [e for e in list(self.edges.values()) if e.source.startswith("__arn__:") or e.target.startswith("__arn__:")]
        for e in deferred:
            self.edges.pop(e.id, None)
            src = self._arn_to_id.get(e.source[len("__arn__:"):], "") if e.source.startswith("__arn__:") else e.source
            tgt = self._arn_to_id.get(e.target[len("__arn__:"):], "") if e.target.startswith("__arn__:") else e.target
            if src and tgt:
                self._add_edge(src, tgt, e.kind, e.label, e.metadata)

        # 2. Route 53 Alias / CNAME -> CloudFront / ALB / NLB / API Gateway / S3
        for zone_id, rec_name, target_dns in self._route53_aliases:
            tgt_id = self._dns_to_id.get(target_dns)
            if tgt_id:
                self._add_edge(zone_id, tgt_id, "routing", rec_name)

        # 3. CloudFront Origins -> S3 / ALB / API Gateway
        for cf_id, orig_domain in self._cf_origins:
            tgt_id = self._dns_to_id.get(orig_domain)
            if not tgt_id and ".s3" in orig_domain:
                bname = orig_domain.split(".s3")[0]
                tgt_id = self._s3_bucket_to_id.get(bname)
            if tgt_id:
                self._add_edge(cf_id, tgt_id, "traffic", "Origin")

        # 4. VPC Endpoints -> S3 / DynamoDB in same region
        for r in list(self.resources.values()):
            if r.resource_type == "ec2:vpc-endpoint":
                svc_name = str(r.metadata.get("service_name", ""))
                if svc_name.endswith(".s3"):
                    for b in self.resources.values():
                        if b.resource_type == "s3:bucket" and b.region == r.region:
                            self._add_edge(r.id, b.id, "storage", "S3 Endpoint")
                elif svc_name.endswith(".dynamodb"):
                    for d in self.resources.values():
                        if d.resource_type == "dynamodb:table" and d.region == r.region:
                            self._add_edge(r.id, d.id, "storage", "DDB Endpoint")

        # 5. Lambda Environment Variable References -> DynamoDB, S3, SQS, SNS, RDS/ElastiCache
        for lam_id, reg, candidates in self._lambda_env_values:
            for val in candidates:
                clean = val.strip()
                if clean in self._arn_to_id:
                    self._add_edge(lam_id, self._arn_to_id[clean], "event", "Env Ref")
                    continue
                if clean in self._s3_bucket_to_id:
                    self._add_edge(lam_id, self._s3_bucket_to_id[clean], "storage", "Read/Write")
                if (reg, clean) in self._ddb_table_to_id:
                    self._add_edge(lam_id, self._ddb_table_to_id[(reg, clean)], "storage", "Query/Put")
                if (reg, clean) in self._sqs_name_to_id:
                    self._add_edge(lam_id, self._sqs_name_to_id[(reg, clean)], "event", "SendMessage")
                if "amazonaws.com" in clean:
                    host = clean.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0].lower()
                    if host in self._dns_to_id:
                        self._add_edge(lam_id, self._dns_to_id[host], "traffic", "Connect")
                    elif ".sqs." in clean:
                        qname = clean.rstrip("/").split("/")[-1]
                        if (reg, qname) in self._sqs_name_to_id:
                            self._add_edge(lam_id, self._sqs_name_to_id[(reg, qname)], "event", "SQS")

        # 6. Security Group -> Security Group Tier Traffic Inference
        sg_holders: Dict[str, List[str]] = {}
        for r in self.resources.values():
            for sg_id in r.security_group_ids:
                sg_holders.setdefault(sg_id, []).append(r.id)

        for dst_sg, src_list in self._sg_inbound_sources.items():
            dst_nodes = sg_holders.get(dst_sg, [])
            if not dst_nodes:
                continue
            for src_sg, port_lbl in src_list:
                src_nodes = sg_holders.get(src_sg, [])
                # Cap fan-out to prevent mesh explosion on shared SGs
                if 0 < len(src_nodes) * len(dst_nodes) <= 24:
                    for s_node in src_nodes:
                        for d_node in dst_nodes:
                            # Skip if a direct traffic edge already exists
                            direct_key = f"edge:{s_node}->{d_node}:traffic"
                            if direct_key not in self.edges:
                                self._add_edge(s_node, d_node, "security_group", port_lbl)

