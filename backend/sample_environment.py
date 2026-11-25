"""Realistic sample AWS multi-region production environment for instant preview and testing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

from .icon_catalog import GROUP_ICONS, catalog
from .models import AwsContainer, AwsEdge, AwsResource, EnvironmentGraph


def build_sample_environment() -> EnvironmentGraph:
    """Construct a rich, realistic AWS architecture graph using 100% official AWS icon mappings."""
    account_id = "492081746310"
    account_alias = "acme-commerce-prod (492081746310)"
    caller_arn = f"arn:aws:iam::{account_id}:role/AWSReadOnlyArchitectRole"

    containers: List[AwsContainer] = [
        AwsContainer(id="cloud:aws", kind="aws_cloud", name="AWS Cloud", icon_key=GROUP_ICONS["aws_cloud"]),
        AwsContainer(
            id=f"account:{account_id}",
            kind="account",
            name=f"AWS Account: {account_alias}",
            icon_key=GROUP_ICONS["account"],
            parent_id="cloud:aws",
            metadata={"account_id": account_id},
        ),
        AwsContainer(
            id="region:global",
            kind="region",
            name="Global Edge & Account Services",
            icon_key=GROUP_ICONS["aws_cloud_alt"],
            parent_id=f"account:{account_id}",
            region="global",
        ),
        AwsContainer(
            id="region:us-east-1",
            kind="region",
            name="Region: us-east-1 (N. Virginia)",
            icon_key=GROUP_ICONS["region"],
            parent_id=f"account:{account_id}",
            region="us-east-1",
        ),
        AwsContainer(
            id="region:us-west-2",
            kind="region",
            name="Region: us-west-2 (Oregon - Analytics & DR)",
            icon_key=GROUP_ICONS["region"],
            parent_id=f"account:{account_id}",
            region="us-west-2",
        ),
        # Primary Production VPC in us-east-1
        AwsContainer(
            id="vpc:vpc-0prod100",
            kind="vpc",
            name="prod-core-vpc (10.10.0.0/16)",
            icon_key=GROUP_ICONS["vpc"],
            parent_id="region:us-east-1",
            region="us-east-1",
            cidr="10.10.0.0/16",
            is_default=False,
            metadata={"vpc_id": "vpc-0prod100", "cidr": "10.10.0.0/16"},
        ),
        AwsContainer(
            id="subnet:subnet-pub1a",
            kind="public_subnet",
            name="prod-public-1a [1a]",
            icon_key=GROUP_ICONS["public_subnet"],
            parent_id="vpc:vpc-0prod100",
            region="us-east-1",
            cidr="10.10.1.0/24",
            az="us-east-1a",
            metadata={"subnet_id": "subnet-pub1a", "vpc_id": "vpc-0prod100", "public": True},
        ),
        AwsContainer(
            id="subnet:subnet-pub1b",
            kind="public_subnet",
            name="prod-public-1b [1b]",
            icon_key=GROUP_ICONS["public_subnet"],
            parent_id="vpc:vpc-0prod100",
            region="us-east-1",
            cidr="10.10.2.0/24",
            az="us-east-1b",
            metadata={"subnet_id": "subnet-pub1b", "vpc_id": "vpc-0prod100", "public": True},
        ),
        AwsContainer(
            id="subnet:subnet-app1a",
            kind="private_subnet",
            name="prod-app-private-1a [1a]",
            icon_key=GROUP_ICONS["private_subnet"],
            parent_id="vpc:vpc-0prod100",
            region="us-east-1",
            cidr="10.10.11.0/24",
            az="us-east-1a",
            metadata={"subnet_id": "subnet-app1a", "vpc_id": "vpc-0prod100", "public": False},
        ),
        AwsContainer(
            id="subnet:subnet-app1b",
            kind="private_subnet",
            name="prod-app-private-1b [1b]",
            icon_key=GROUP_ICONS["private_subnet"],
            parent_id="vpc:vpc-0prod100",
            region="us-east-1",
            cidr="10.10.12.0/24",
            az="us-east-1b",
            metadata={"subnet_id": "subnet-app1b", "vpc_id": "vpc-0prod100", "public": False},
        ),
        AwsContainer(
            id="subnet:subnet-data1a",
            kind="private_subnet",
            name="prod-data-isolated-1a [1a]",
            icon_key=GROUP_ICONS["private_subnet"],
            parent_id="vpc:vpc-0prod100",
            region="us-east-1",
            cidr="10.10.21.0/24",
            az="us-east-1a",
            metadata={"subnet_id": "subnet-data1a", "vpc_id": "vpc-0prod100", "public": False},
        ),
        # Empty Default VPC in us-east-1 (filtered out by default via Hide Empty Default VPCs)
        AwsContainer(
            id="vpc:vpc-0default99",
            kind="vpc",
            name="vpc-0default99 (Default) (172.31.0.0/16)",
            icon_key=GROUP_ICONS["vpc"],
            parent_id="region:us-east-1",
            region="us-east-1",
            cidr="172.31.0.0/16",
            is_default=True,
            metadata={"vpc_id": "vpc-0default99", "cidr": "172.31.0.0/16", "is_default": True},
        ),
        # Analytics VPC in us-west-2
        AwsContainer(
            id="vpc:vpc-0west200",
            kind="vpc",
            name="analytics-dr-vpc (10.20.0.0/16)",
            icon_key=GROUP_ICONS["vpc"],
            parent_id="region:us-west-2",
            region="us-west-2",
            cidr="10.20.0.0/16",
            is_default=False,
            metadata={"vpc_id": "vpc-0west200", "cidr": "10.20.0.0/16"},
        ),
        AwsContainer(
            id="subnet:subnet-west-priv2a",
            kind="private_subnet",
            name="analytics-private-2a [2a]",
            icon_key=GROUP_ICONS["private_subnet"],
            parent_id="vpc:vpc-0west200",
            region="us-west-2",
            cidr="10.20.10.0/24",
            az="us-west-2a",
            metadata={"subnet_id": "subnet-west-priv2a", "vpc_id": "vpc-0west200", "public": False},
        ),
    ]

    def mk_res(
        rid: str,
        rtype: str,
        svc: str,
        name: str,
        sublabel: str,
        region: str,
        vpc_id: str | None = None,
        subnet_id: str | None = None,
        meta: Dict | None = None,
        tags: Dict[str, str] | None = None,
    ) -> AwsResource:
        icon_key, cat = catalog.resolve_resource_icon(rtype, svc)
        return AwsResource(
            id=rid,
            arn=f"arn:aws:{svc}:{'' if region == 'global' else region}:{account_id}:{rid.split(':')[-1]}",
            resource_type=rtype,
            service=svc,
            category=cat,
            name=name,
            sublabel=sublabel,
            icon_key=icon_key,
            region=region,
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            state="active",
            tags=tags or {"Environment": "production", "ManagedBy": "Terraform"},
            metadata=meta or {},
        )

    resources: List[AwsResource] = [
        # Global Edge
        mk_res("r53:Z0918273", "route53:hosted-zone", "route53", "acmecloud.io", "Public Zone (42 records)", "global"),
        mk_res("waf-global:edge-acl", "wafv2:web-acl", "wafv2", "prod-edge-waf", "Global Web ACL", "global"),
        mk_res("cf:E283910ABCDEF", "cloudfront:distribution", "cloudfront", "cdn.acmecloud.io", "CloudFront (Deployed)", "global", meta={"domain_name": "d192837.cloudfront.net"}),

        # us-east-1 VPC Gateways & Public Subnets
        mk_res("igw:igw-0prod1", "ec2:internet-gateway", "vpc", "prod-igw", "Internet Gateway", "us-east-1", vpc_id="vpc-0prod100"),
        mk_res("vpce:vpce-0s3gw", "ec2:vpc-endpoint", "vpc", "s3-gateway-ep", "Gateway Endpoint", "us-east-1", vpc_id="vpc-0prod100", meta={"service_name": "com.amazonaws.us-east-1.s3"}),
        mk_res("elbv2:us-east-1:prod-ingress-alb", "elbv2:alb", "elbv2", "prod-ingress-alb", "APPLICATION (internet-facing)", "us-east-1", vpc_id="vpc-0prod100", subnet_id="subnet-pub1a", meta={"dns_name": "prod-ingress-alb-1928.us-east-1.elb.amazonaws.com"}),
        mk_res("nat:nat-01a", "ec2:nat-gateway", "vpc", "prod-nat-1a", "NAT (54.210.18.42)", "us-east-1", vpc_id="vpc-0prod100", subnet_id="subnet-pub1a"),
        mk_res("nat:nat-01b", "ec2:nat-gateway", "vpc", "prod-nat-1b", "NAT (52.44.91.10)", "us-east-1", vpc_id="vpc-0prod100", subnet_id="subnet-pub1b"),

        # us-east-1 Private App Subnets
        mk_res("ecs-svc:us-east-1:prod:checkout-svc", "ecs:fargate-service", "ecs", "checkout-service", "FARGATE (6/6 tasks)", "us-east-1", vpc_id="vpc-0prod100", subnet_id="subnet-app1a"),
        mk_res("ecs-svc:us-east-1:prod:catalog-svc", "ecs:fargate-service", "ecs", "catalog-graphql", "FARGATE (4/4 tasks)", "us-east-1", vpc_id="vpc-0prod100", subnet_id="subnet-app1b"),
        mk_res("eks:us-east-1:platform-eks", "eks:cluster", "eks", "platform-eks", "EKS v1.31 (ACTIVE)", "us-east-1", vpc_id="vpc-0prod100", subnet_id="subnet-app1b"),
        mk_res("ec2:i-08a91bc11", "ec2:instance", "ec2", "media-transcoder-01", "c7g.2xlarge (running)", "us-east-1", vpc_id="vpc-0prod100", subnet_id="subnet-app1a"),

        # us-east-1 Private Data Subnet
        mk_res("rds-cluster:us-east-1:orders-aurora-pg", "rds:aurora-cluster", "rds", "orders-aurora-pg", "aurora-postgresql 16.4 (3 nodes)", "us-east-1", vpc_id="vpc-0prod100", subnet_id="subnet-data1a"),
        mk_res("elasticache:us-east-1:session-redis", "elasticache:cluster", "elasticache", "session-redis", "redis (cache.r7g.large)", "us-east-1", vpc_id="vpc-0prod100", subnet_id="subnet-data1a"),
        mk_res("efs:us-east-1:fs-09ab12", "efs:file-system", "efs", "shared-assets-efs", "EFS File System", "us-east-1", vpc_id="vpc-0prod100", subnet_id="subnet-data1a"),

        # us-east-1 Regional Managed & Serverless Services
        mk_res("apigwv2:us-east-1:mobile-bff", "apigatewayv2:api", "apigateway", "mobile-bff-api", "HTTP API", "us-east-1"),
        mk_res("cognito:us-east-1:customer-pool", "cognito-idp:userpool", "cognito-idp", "acme-customer-auth", "Cognito User Pool", "us-east-1"),
        mk_res("lambda:us-east-1:order-processor", "lambda:function", "lambda", "order-processor", "python3.12 (1024 MB)", "us-east-1"),
        mk_res("lambda:us-east-1:invoice-renderer", "lambda:function", "lambda", "invoice-renderer", "nodejs22.x (512 MB)", "us-east-1"),
        mk_res("sqs:us-east-1:orders-fulfillment.fifo", "sqs:queue", "sqs", "orders-fulfillment.fifo", "FIFO Queue", "us-east-1"),
        mk_res("sns:us-east-1:order-domain-events", "sns:topic", "sns", "order-domain-events", "SNS Topic", "us-east-1"),
        mk_res("eventbus:us-east-1:commerce-bus", "events:event-bus", "events", "EventBridge (commerce)", "8 active rules", "us-east-1"),
        mk_res("sfn:us-east-1:fulfillment-saga", "states:state-machine", "states", "fulfillment-saga", "Step Functions (STANDARD)", "us-east-1"),
        mk_res("ddb:us-east-1:product-inventory", "dynamodb:table", "dynamodb", "product-inventory", "DynamoDB (1.4M items, pay_per_request)", "us-east-1"),
        mk_res("s3:acme-web-static-prod", "s3:bucket", "s3", "acme-web-static-prod", "S3 Bucket (us-east-1)", "us-east-1"),
        mk_res("s3:acme-invoices-vault", "s3:bucket", "s3", "acme-invoices-vault", "S3 Bucket (us-east-1)", "us-east-1"),
        mk_res("bedrock:us-east-1:shopping-assistant", "bedrock:model", "bedrock", "shopping-assistant-kb", "Amazon Bedrock", "us-east-1"),
        mk_res("ecr:us-east-1:acme-services", "ecr:repository", "ecr", "acme/microservices", "ECR Repository", "us-east-1"),
        mk_res("secret:us-east-1:prod-db-creds", "secretsmanager:secret", "secretsmanager", "prod/aurora/credentials", "Secrets Manager", "us-east-1"),
        mk_res("kms:us-east-1:prod-cmk", "kms:key", "kms", "prod-encryption-cmk", "Customer KMS Key", "us-east-1"),
        mk_res("cw:us-east-1:slo-alarms", "cloudwatch:alarm", "cloudwatch", "prod-slo-monitors", "CloudWatch Alarms (14)", "us-east-1"),

        # us-west-2 Analytics & DR
        mk_res("pcx:pcx-0eastwest", "ec2:vpc-peering-connection", "vpc", "east-west-peering", "vpc-0prod100 <-> vpc-0west200", "us-west-2", vpc_id="vpc-0west200"),
        mk_res("opensearch:us-west-2:catalog-search", "opensearch:domain", "opensearch", "catalog-search-cluster", "OpenSearch 2.17", "us-west-2", vpc_id="vpc-0west200", subnet_id="subnet-west-priv2a"),
        mk_res("redshift:us-west-2:enterprise-dwh", "redshift:cluster", "redshift", "enterprise-dwh", "Redshift RA3 (4 nodes)", "us-west-2", vpc_id="vpc-0west200", subnet_id="subnet-west-priv2a"),
        mk_res("kinesis:us-west-2:clickstream-ingest", "kinesis:stream", "kinesis", "clickstream-ingest", "Kinesis Data Stream", "us-west-2"),
        mk_res("glue:us-west-2:lakehouse-etl", "glue:job", "glue", "lakehouse-spark-etl", "AWS Glue PySpark", "us-west-2"),
        mk_res("s3:acme-datalake-usw2", "s3:bucket", "s3", "acme-datalake-usw2", "S3 Bucket (us-west-2)", "us-west-2"),
        mk_res("ddb:us-west-2:product-inventory-dr", "dynamodb:table", "dynamodb", "product-inventory-dr", "DynamoDB Global Replica", "us-west-2"),
    ]

    def mk_edge(src: str, tgt: str, kind: str, label: str = "") -> AwsEdge:
        return AwsEdge(id=f"edge:{src}->{tgt}:{kind}", source=src, target=tgt, kind=kind, label=label)

    edges: List[AwsEdge] = [
        mk_edge("r53:Z0918273", "cf:E283910ABCDEF", "routing", "cdn / www"),
        mk_edge("r53:Z0918273", "apigwv2:us-east-1:mobile-bff", "routing", "api.acmecloud.io"),
        mk_edge("waf-global:edge-acl", "cf:E283910ABCDEF", "security_group", "Protects"),
        mk_edge("cf:E283910ABCDEF", "s3:acme-web-static-prod", "traffic", "Static Origin"),
        mk_edge("cf:E283910ABCDEF", "elbv2:us-east-1:prod-ingress-alb", "traffic", "Dynamic Origin"),
        mk_edge("igw:igw-0prod1", "elbv2:us-east-1:prod-ingress-alb", "routing", "HTTPS 443"),
        mk_edge("nat:nat-01a", "igw:igw-0prod1", "routing", "Egress"),
        mk_edge("nat:nat-01b", "igw:igw-0prod1", "routing", "Egress"),
        mk_edge("vpce:vpce-0s3gw", "s3:acme-web-static-prod", "storage", "S3 Endpoint"),
        mk_edge("elbv2:us-east-1:prod-ingress-alb", "ecs-svc:us-east-1:prod:checkout-svc", "traffic", "HTTPS 8443"),
        mk_edge("elbv2:us-east-1:prod-ingress-alb", "ecs-svc:us-east-1:prod:catalog-svc", "traffic", "HTTPS 8080"),
        mk_edge("ecs-svc:us-east-1:prod:checkout-svc", "rds-cluster:us-east-1:orders-aurora-pg", "security_group", "TCP 5432"),
        mk_edge("ecs-svc:us-east-1:prod:checkout-svc", "elasticache:us-east-1:session-redis", "security_group", "TCP 6379"),
        mk_edge("ecs-svc:us-east-1:prod:checkout-svc", "sns:us-east-1:order-domain-events", "event", "Publish"),
        mk_edge("ecs-svc:us-east-1:prod:catalog-svc", "ddb:us-east-1:product-inventory", "storage", "Query"),
        mk_edge("ecs-svc:us-east-1:prod:catalog-svc", "bedrock:us-east-1:shopping-assistant", "traffic", "InvokeAgent"),
        mk_edge("ec2:i-08a91bc11", "efs:us-east-1:fs-09ab12", "security_group", "NFS 2049"),
        mk_edge("cognito:us-east-1:customer-pool", "apigwv2:us-east-1:mobile-bff", "security_group", "JWT Auth"),
        mk_edge("apigwv2:us-east-1:mobile-bff", "lambda:us-east-1:order-processor", "traffic", "Route"),
        mk_edge("sns:us-east-1:order-domain-events", "sqs:us-east-1:orders-fulfillment.fifo", "event", "Fanout"),
        mk_edge("sqs:us-east-1:orders-fulfillment.fifo", "lambda:us-east-1:order-processor", "event", "Event Trigger"),
        mk_edge("lambda:us-east-1:order-processor", "sfn:us-east-1:fulfillment-saga", "event", "StartExecution"),
        mk_edge("eventbus:us-east-1:commerce-bus", "lambda:us-east-1:invoice-renderer", "event", "OrderCompleted"),
        mk_edge("lambda:us-east-1:invoice-renderer", "s3:acme-invoices-vault", "storage", "PutObject"),
        mk_edge("ddb:us-east-1:product-inventory", "ddb:us-west-2:product-inventory-dr", "storage", "Global Table"),
        mk_edge("kinesis:us-west-2:clickstream-ingest", "glue:us-west-2:lakehouse-etl", "event", "Stream ETL"),
        mk_edge("glue:us-west-2:lakehouse-etl", "s3:acme-datalake-usw2", "storage", "Parquet Write"),
        mk_edge("s3:acme-datalake-usw2", "redshift:us-west-2:enterprise-dwh", "storage", "COPY / Spectrum"),
        mk_edge("ecs-svc:us-east-1:prod:catalog-svc", "opensearch:us-west-2:catalog-search", "traffic", "Cross-VPC Search"),
    ]

    by_category: Dict[str, int] = {}
    by_region: Dict[str, int] = {}
    by_service: Dict[str, int] = {}
    for r in resources:
        by_category[r.category] = by_category.get(r.category, 0) + 1
        by_region[r.region] = by_region.get(r.region, 0) + 1
        by_service[r.service] = by_service.get(r.service, 0) + 1

    return EnvironmentGraph(
        account_id=account_id,
        account_alias=account_alias,
        caller_arn=caller_arn,
        profile="sample-demo",
        scanned_at=datetime.now(timezone.utc).isoformat(),
        regions_scanned=["us-east-1", "us-west-2"],
        is_sample=True,
        containers=containers,
        resources=resources,
        edges=edges,
        warnings=[],
        stats={
            "total_resources": len(resources),
            "total_containers": len(containers),
            "total_vpcs": sum(1 for c in containers if c.kind == "vpc"),
            "total_subnets": sum(1 for c in containers if c.kind in ("public_subnet", "private_subnet")),
            "total_edges": len(edges),
            "by_category": by_category,
            "by_region": by_region,
            "by_service": by_service,
        },
    )

