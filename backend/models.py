"""Data models for AWS Architecture Mapper."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AwsContainer:
    """Represents an official AWS architectural boundary group (Cloud, Account, Region, VPC, Subnet, ASG)."""

    id: str
    kind: str  # aws_cloud | account | region | vpc | public_subnet | private_subnet | asg
    name: str
    icon_key: str
    parent_id: Optional[str] = None
    region: Optional[str] = None
    cidr: Optional[str] = None
    az: Optional[str] = None
    is_default: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AwsResource:
    """Represents an individual discovered AWS service resource node."""

    id: str
    arn: str
    resource_type: str  # e.g. ec2:instance, elbv2:alb, rds:instance, s3:bucket, lambda:function
    service: str        # e.g. ec2, elbv2, rds, s3, lambda
    category: str       # Compute, Containers, Databases, Networking, Storage, Serverless, Integration, Security, Analytics, Management, AI/ML
    name: str
    sublabel: str       # e.g. t3.large | PostgreSQL 16.2 | Python 3.12 | Application LB
    icon_key: str
    region: str         # us-east-1, global, etc.
    vpc_id: Optional[str] = None
    subnet_id: Optional[str] = None
    subnet_ids: List[str] = field(default_factory=list)
    az: Optional[str] = None
    asg_id: Optional[str] = None
    security_group_ids: List[str] = field(default_factory=list)
    state: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AwsEdge:
    """Represents a directed architectural connection or dependency between two AWS resources."""

    id: str
    source: str
    target: str
    kind: str  # traffic | routing | event | storage | security_group | peering
    label: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EnvironmentGraph:
    """Complete scanned AWS environment topology."""

    account_id: str
    account_alias: str
    caller_arn: str
    profile: str
    scanned_at: str
    regions_scanned: List[str]
    is_sample: bool = False
    containers: List[AwsContainer] = field(default_factory=list)
    resources: List[AwsResource] = field(default_factory=list)
    edges: List[AwsEdge] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "account_alias": self.account_alias,
            "caller_arn": self.caller_arn,
            "profile": self.profile,
            "scanned_at": self.scanned_at,
            "regions_scanned": self.regions_scanned,
            "is_sample": self.is_sample,
            "containers": [c.to_dict() for c in self.containers],
            "resources": [r.to_dict() for r in self.resources],
            "edges": [e.to_dict() for e in self.edges],
            "warnings": self.warnings,
            "stats": self.stats,
        }

