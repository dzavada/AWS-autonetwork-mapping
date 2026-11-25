"""Official AWS Architecture Icons catalog, resolver, and base64 SVG embedder.

All icons served and embedded by this module come strictly from the official
Amazon Web Services Architecture Icon Package (assets/aws-icons/).
"""

from __future__ import annotations

import base64
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICONS_ROOT = PROJECT_ROOT / "assets" / "aws-icons"
MANIFEST_PATH = ICONS_ROOT / "manifest.json"


# Official AWS Architecture Group Icons
GROUP_ICONS: Dict[str, str] = {
    "aws_cloud": "AWS-Cloud-logo_32",
    "aws_cloud_alt": "AWS-Cloud_32",
    "account": "AWS-Account_32",
    "region": "Region_32",
    "vpc": "Virtual-private-cloud-VPC_32",
    "public_subnet": "Public-subnet_32",
    "private_subnet": "Private-subnet_32",
    "asg": "Auto-Scaling-group_32",
    "spot_fleet": "Spot-Fleet_32",
    "corporate_dc": "Corporate-data-center_32",
    "server_contents": "Server-contents_32",
}


# Official AWS Category Icons
CATEGORY_META: Dict[str, Dict[str, str]] = {
    "Networking": {
        "label": "Networking & Edge",
        "icon_key": "Arch-Category_Networking-Content-Delivery_64",
        "color": "#8C4FFF",
    },
    "Compute": {
        "label": "Compute",
        "icon_key": "Arch-Category_Compute_64",
        "color": "#ED7100",
    },
    "Containers": {
        "label": "Containers",
        "icon_key": "Arch-Category_Containers_64",
        "color": "#ED7100",
    },
    "Serverless": {
        "label": "Serverless",
        "icon_key": "Arch-Category_Serverless_64",
        "color": "#ED7100",
    },
    "Databases": {
        "label": "Databases",
        "icon_key": "Arch-Category_Databases_64",
        "color": "#C925D1",
    },
    "Storage": {
        "label": "Storage",
        "icon_key": "Arch-Category_Storage_64",
        "color": "#7AA116",
    },
    "Integration": {
        "label": "App Integration",
        "icon_key": "Arch-Category_Application-Integration_64",
        "color": "#E7157B",
    },
    "Analytics": {
        "label": "Analytics & Streaming",
        "icon_key": "Arch-Category_Analytics_64",
        "color": "#8C4FFF",
    },
    "Security": {
        "label": "Security & Identity",
        "icon_key": "Arch-Category_Security-Identity_64",
        "color": "#DD344C",
    },
    "Management": {
        "label": "Management & Ops",
        "icon_key": "Arch-Category_Management-Tools_64",
        "color": "#E7157B",
    },
    "AI/ML": {
        "label": "AI & Machine Learning",
        "icon_key": "Arch-Category_Artificial-Intelligence_64",
        "color": "#01A88D",
    },
}


# Curated mapping from discovered resource_type -> (official_icon_key, category)
RESOURCE_TYPE_MAP: Dict[str, Tuple[str, str]] = {
    # External / Edge entrypoints (official AWS light resource icons)
    "external:internet": ("Res_Internet_48_Light", "Networking"),
    "external:users": ("Res_Users_48_Light", "Networking"),
    # Global & Edge Networking
    "route53:hosted-zone": ("Arch_Amazon-Route-53_64", "Networking"),
    "cloudfront:distribution": ("Arch_Amazon-CloudFront_64", "Networking"),
    "globalaccelerator:accelerator": ("Arch_AWS-Global-Accelerator_64", "Networking"),
    "wafv2:web-acl": ("Arch_AWS-WAF_64", "Security"),
    "shield:protection": ("Arch_AWS-Shield_64", "Security"),
    "apigateway:rest-api": ("Arch_Amazon-API-Gateway_64", "Networking"),
    "apigatewayv2:api": ("Arch_Amazon-API-Gateway_64", "Networking"),
    # VPC Gateways & Connectivity
    "ec2:internet-gateway": ("Res_Amazon-VPC_Internet-Gateway_48", "Networking"),
    "ec2:nat-gateway": ("Res_Amazon-VPC_NAT-Gateway_48", "Networking"),
    "ec2:vpc-endpoint": ("Res_Amazon-VPC_Endpoints_48", "Networking"),
    "ec2:vpc-peering-connection": ("Res_Amazon-VPC_Peering-Connection_48", "Networking"),
    "ec2:vpn-gateway": ("Res_Amazon-VPC_VPN-Gateway_48", "Networking"),
    "ec2:vpn-connection": ("Res_Amazon-VPC_VPN-Connection_48", "Networking"),
    "ec2:transit-gateway": ("Arch_AWS-Transit-Gateway_64", "Networking"),
    "directconnect:connection": ("Arch_AWS-Direct-Connect_64", "Networking"),
    # Load Balancers
    "elbv2:alb": ("Res_Elastic-Load-Balancing_Application-Load-Balancer_48", "Networking"),
    "elbv2:nlb": ("Res_Elastic-Load-Balancing_Network-Load-Balancer_48", "Networking"),
    "elbv2:gwlb": ("Res_Elastic-Load-Balancing_Gateway-Load-Balancer_48", "Networking"),
    "elb:clb": ("Res_Elastic-Load-Balancing_Classic-Load-Balancer_48", "Networking"),
    "elbv2:loadbalancer": ("Arch_Elastic-Load-Balancing_64", "Networking"),
    # Compute & Containers
    "ec2:instance": ("Arch_Amazon-EC2_64", "Compute"),
    "autoscaling:group": ("Arch_Amazon-EC2-Auto-Scaling_64", "Compute"),
    "lambda:function": ("Arch_AWS-Lambda_64", "Serverless"),
    "ecs:cluster": ("Arch_Amazon-Elastic-Container-Service_64", "Containers"),
    "ecs:service": ("Arch_Amazon-Elastic-Container-Service_64", "Containers"),
    "ecs:fargate-service": ("Arch_AWS-Fargate_64", "Containers"),
    "eks:cluster": ("Arch_Amazon-Elastic-Kubernetes-Service_64", "Containers"),
    "ecr:repository": ("Arch_Amazon-Elastic-Container-Registry_64", "Containers"),
    "apprunner:service": ("Arch_AWS-App-Runner_64", "Compute"),
    "elasticbeanstalk:environment": ("Arch_AWS-Elastic-Beanstalk_64", "Compute"),
    "lightsail:instance": ("Arch_Amazon-Lightsail_64", "Compute"),
    "batch:compute-environment": ("Arch_AWS-Batch_64", "Compute"),
    # Databases & Caching
    "rds:instance": ("Arch_Amazon-RDS_64", "Databases"),
    "rds:aurora-cluster": ("Arch_Amazon-Aurora_64", "Databases"),
    "dynamodb:table": ("Arch_Amazon-DynamoDB_64", "Databases"),
    "elasticache:cluster": ("Arch_Amazon-ElastiCache_64", "Databases"),
    "docdb:cluster": ("Arch_Amazon-DocumentDB_64", "Databases"),
    "neptune:cluster": ("Arch_Amazon-Neptune_64", "Databases"),
    "memorydb:cluster": ("Arch_Amazon-MemoryDB_64", "Databases"),
    "redshift:cluster": ("Arch_Amazon-Redshift_64", "Analytics"),
    "opensearch:domain": ("Arch_Amazon-OpenSearch-Service_64", "Analytics"),
    # Storage
    "s3:bucket": ("Arch_Amazon-Simple-Storage-Service_64", "Storage"),
    "efs:file-system": ("Arch_Amazon-EFS_64", "Storage"),
    "ec2:volume": ("Arch_Amazon-Elastic-Block-Store_64", "Storage"),
    "backup:vault": ("Arch_AWS-Backup_64", "Storage"),
    # Application Integration & Messaging
    "sqs:queue": ("Arch_Amazon-Simple-Queue-Service_64", "Integration"),
    "sns:topic": ("Arch_Amazon-Simple-Notification-Service_64", "Integration"),
    "events:event-bus": ("Arch_Amazon-EventBridge_64", "Integration"),
    "events:rule": ("Arch_Amazon-EventBridge_64", "Integration"),
    "states:state-machine": ("Arch_AWS-Step-Functions_64", "Integration"),
    # Analytics & Streaming
    "kinesis:stream": ("Arch_Amazon-Kinesis-Data-Streams_64", "Analytics"),
    "firehose:delivery-stream": ("Arch_Amazon-Kinesis_64", "Analytics"),
    "kafka:cluster": ("Arch_Amazon-Managed-Streaming-for-Apache-Kafka_64", "Analytics"),
    "glue:job": ("Arch_AWS-Glue_64", "Analytics"),
    "athena:workgroup": ("Arch_Amazon-Athena_64", "Analytics"),
    # Security, Identity & Compliance
    "secretsmanager:secret": ("Arch_AWS-Secrets-Manager_64", "Security"),
    "kms:key": ("Arch_AWS-Key-Management-Service_64", "Security"),
    "acm:certificate": ("Arch_AWS-Certificate-Manager_64", "Security"),
    "cognito-idp:userpool": ("Arch_Amazon-Cognito_64", "Security"),
    "guardduty:detector": ("Arch_Amazon-GuardDuty_64", "Security"),
    "iam:role": ("Arch_AWS-Identity-and-Access-Management_64", "Security"),
    "securityhub:hub": ("Arch_AWS-Security-Hub_64", "Security"),
    # Management & Governance
    "cloudwatch:alarm": ("Arch_Amazon-CloudWatch_64", "Management"),
    "logs:log-group": ("Arch_Amazon-CloudWatch_64", "Management"),
    "cloudtrail:trail": ("Arch_AWS-CloudTrail_64", "Management"),
    "config:recorder": ("Arch_AWS-Config_64", "Management"),
    "cloudformation:stack": ("Arch_AWS-CloudFormation_64", "Management"),
    "ssm:parameter": ("Arch_AWS-Systems-Manager_64", "Management"),
    # AI & ML / Frontend
    "bedrock:model": ("Arch_Amazon-Bedrock_64", "AI/ML"),
    "sagemaker:endpoint": ("Arch_Amazon-SageMaker_64", "AI/ML"),
    "amplify:app": ("Arch_AWS-Amplify_64", "Networking"),
    "codepipeline:pipeline": ("Arch_AWS-CodePipeline_64", "Management"),
}


# Service namespace aliases for ARN catch-all discovery (arn:aws:<service>:...)
SERVICE_NAMESPACE_ALIASES: Dict[str, Tuple[str, str]] = {
    "ec2": ("Arch_Amazon-EC2_64", "Compute"),
    "lambda": ("Arch_AWS-Lambda_64", "Serverless"),
    "s3": ("Arch_Amazon-Simple-Storage-Service_64", "Storage"),
    "rds": ("Arch_Amazon-RDS_64", "Databases"),
    "dynamodb": ("Arch_Amazon-DynamoDB_64", "Databases"),
    "elasticloadbalancing": ("Arch_Elastic-Load-Balancing_64", "Networking"),
    "ecs": ("Arch_Amazon-Elastic-Container-Service_64", "Containers"),
    "eks": ("Arch_Amazon-Elastic-Kubernetes-Service_64", "Containers"),
    "ecr": ("Arch_Amazon-Elastic-Container-Registry_64", "Containers"),
    "sqs": ("Arch_Amazon-Simple-Queue-Service_64", "Integration"),
    "sns": ("Arch_Amazon-Simple-Notification-Service_64", "Integration"),
    "events": ("Arch_Amazon-EventBridge_64", "Integration"),
    "states": ("Arch_AWS-Step-Functions_64", "Integration"),
    "apigateway": ("Arch_Amazon-API-Gateway_64", "Networking"),
    "execute-api": ("Arch_Amazon-API-Gateway_64", "Networking"),
    "cloudfront": ("Arch_Amazon-CloudFront_64", "Networking"),
    "route53": ("Arch_Amazon-Route-53_64", "Networking"),
    "elasticache": ("Arch_Amazon-ElastiCache_64", "Databases"),
    "es": ("Arch_Amazon-OpenSearch-Service_64", "Analytics"),
    "opensearch": ("Arch_Amazon-OpenSearch-Service_64", "Analytics"),
    "kinesis": ("Arch_Amazon-Kinesis-Data-Streams_64", "Analytics"),
    "firehose": ("Arch_Amazon-Kinesis_64", "Analytics"),
    "kafka": ("Arch_Amazon-Managed-Streaming-for-Apache-Kafka_64", "Analytics"),
    "kms": ("Arch_AWS-Key-Management-Service_64", "Security"),
    "secretsmanager": ("Arch_AWS-Secrets-Manager_64", "Security"),
    "acm": ("Arch_AWS-Certificate-Manager_64", "Security"),
    "wafv2": ("Arch_AWS-WAF_64", "Security"),
    "waf": ("Arch_AWS-WAF_64", "Security"),
    "cognito-idp": ("Arch_Amazon-Cognito_64", "Security"),
    "cognito-identity": ("Arch_Amazon-Cognito_64", "Security"),
    "elasticfilesystem": ("Arch_Amazon-EFS_64", "Storage"),
    "fsx": ("Arch_Amazon-FSx-for-Lustre_64", "Storage"),
    "backup": ("Arch_AWS-Backup_64", "Storage"),
    "cloudwatch": ("Arch_Amazon-CloudWatch_64", "Management"),
    "logs": ("Arch_Amazon-CloudWatch_64", "Management"),
    "cloudtrail": ("Arch_AWS-CloudTrail_64", "Management"),
    "cloudformation": ("Arch_AWS-CloudFormation_64", "Management"),
    "ssm": ("Arch_AWS-Systems-Manager_64", "Management"),
    "glue": ("Arch_AWS-Glue_64", "Analytics"),
    "athena": ("Arch_Amazon-Athena_64", "Analytics"),
    "redshift": ("Arch_Amazon-Redshift_64", "Analytics"),
    "sagemaker": ("Arch_Amazon-SageMaker_64", "AI/ML"),
    "bedrock": ("Arch_Amazon-Bedrock_64", "AI/ML"),
    "apprunner": ("Arch_AWS-App-Runner_64", "Compute"),
    "amplify": ("Arch_AWS-Amplify_64", "Networking"),
    "codepipeline": ("Arch_AWS-CodePipeline_64", "Management"),
    "guardduty": ("Arch_Amazon-GuardDuty_64", "Security"),
    "timestream": ("Arch_Amazon-Timestream_64", "Databases"),
    "neptune-db": ("Arch_Amazon-Neptune_64", "Databases"),
    "memorydb": ("Arch_Amazon-MemoryDB_64", "Databases"),
    "transfer": ("Arch_AWS-Transfer-Family_64", "Storage"),
}


class IconCatalog:
    """Indexes and serves official AWS Architecture Icons (SVG paths + base64 data URIs)."""

    def __init__(self) -> None:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            self.manifest: Dict[str, Dict[str, str]] = json.load(f)

        self.flat_index: Dict[str, str] = {}
        self.png_index: Dict[str, str] = self.manifest.get("png_lookup", {})
        self.normalized_service_index: List[Tuple[str, str]] = []

        for section, items in self.manifest.items():
            if section == "png_lookup":
                continue
            for key, rel_path in items.items():
                self.flat_index[key] = rel_path
                if section == "services":
                    norm = re.sub(r"[^a-z0-9]", "", key.lower().replace("arch_", "").replace("amazon", "").replace("aws", "").replace("64", ""))
                    if norm:
                        self.normalized_service_index.append((norm, key))

    def resolve_resource_icon(self, resource_type: str, service: str = "") -> Tuple[str, str]:
        """Resolve (official_icon_key, category) for any AWS resource type or ARN service."""
        if resource_type in RESOURCE_TYPE_MAP:
            icon_key, category = RESOURCE_TYPE_MAP[resource_type]
            if icon_key in self.flat_index:
                return icon_key, category

        svc = (service or resource_type.split(":")[0]).lower().strip()
        if svc in SERVICE_NAMESPACE_ALIASES:
            icon_key, category = SERVICE_NAMESPACE_ALIASES[svc]
            if icon_key in self.flat_index:
                return icon_key, category

        # Fuzzy match against all 303 official AWS Service icons
        norm_svc = re.sub(r"[^a-z0-9]", "", svc)
        if norm_svc:
            for norm_candidate, icon_key in self.normalized_service_index:
                if norm_svc == norm_candidate or norm_svc in norm_candidate or norm_candidate in norm_svc:
                    return icon_key, "Management"

        # Ultimate fallback: Official AWS Management Category icon (still 100% official AWS SVG)
        return "Arch-Category_Management-Tools_64", "Management"

    def get_rel_path(self, icon_key: str) -> str:
        """Return relative SVG icon path inside assets/aws-icons/."""
        if icon_key in self.flat_index:
            return self.flat_index[icon_key]
        return self.flat_index["AWS-Cloud_32"]

    def get_png_path(self, icon_key: str) -> Optional[Path]:
        """Return Path to official high-DPI AWS PNG icon if available."""
        rel = self.png_index.get(icon_key) or self.png_index.get("AWS-Cloud_32")
        if rel:
            p = ICONS_ROOT / rel
            if p.exists():
                return p
        return None

    @lru_cache(maxsize=1024)
    def get_data_uri(self, icon_key: str) -> str:
        """Return base64-encoded SVG data URI for crisp offline/standalone SVG & PNG export."""
        rel_path = self.get_rel_path(icon_key)
        svg_path = ICONS_ROOT / rel_path
        raw = svg_path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"

    def build_data_uri_bundle(self, icon_keys: List[str]) -> Dict[str, str]:
        """Build a dictionary of {icon_key: data_uri} for all icons used in a graph."""
        bundle: Dict[str, str] = {}
        # Always include group and category icons
        all_keys = set(icon_keys) | set(GROUP_ICONS.values()) | {
            m["icon_key"] for m in CATEGORY_META.values()
        }
        for key in sorted(all_keys):
            if key in self.flat_index:
                bundle[key] = self.get_data_uri(key)
        return bundle


catalog = IconCatalog()
