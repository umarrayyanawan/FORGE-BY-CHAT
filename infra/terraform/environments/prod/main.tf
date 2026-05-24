# ═══════════════════════════════════════════════════════════════
# FORGE - Production Environment
# High-availability configuration with multi-AZ deployment,
# larger instance sizes, longer retention, and full monitoring.
# ═══════════════════════════════════════════════════════════════

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state backend - separate from dev
  backend "s3" {
    bucket         = "forge-terraform-state-prod"
    key            = "environments/prod/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "forge-terraform-locks-prod"
  }
}

# ─────────────────────────────────────────────
# Provider configuration
# ─────────────────────────────────────────────
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "FORGE"
      Environment = "prod"
      ManagedBy   = "terraform"
      Repository  = "github.com/forge-dev/forge"
      Owner       = "platform-team"
      Criticality = "high"
      CostCenter  = "engineering"
    }
  }
}

# ─────────────────────────────────────────────
# Variables
# ─────────────────────────────────────────────
variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "alarm_sns_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarms (required in prod)"
  type        = string

  validation {
    condition     = length(var.alarm_sns_topic_arn) > 0
    error_message = "alarm_sns_topic_arn must be set in production."
  }
}

variable "db_password" {
  description = "RDS master password (set via TF_VAR_db_password or AWS Secrets Manager)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "redis_auth_token" {
  description = "Redis AUTH token (set via TF_VAR_redis_auth_token)"
  type        = string
  sensitive   = true
  default     = ""
}

# ─────────────────────────────────────────────
# Local configuration
# ─────────────────────────────────────────────
locals {
  name        = "forge"
  environment = "prod"

  common_tags = {
    Environment = local.environment
    Project     = "FORGE"
    ManagedBy   = "terraform"
    Criticality = "high"
  }
}

# ─────────────────────────────────────────────
# SNS Topic for Alerts (if not provided externally)
# ─────────────────────────────────────────────
resource "aws_sns_topic" "alerts" {
  name              = "${local.name}-${local.environment}-alerts"
  kms_master_key_id = "alias/aws/sns"

  tags = merge(local.common_tags, {
    Name = "${local.name}-${local.environment}-alerts"
  })
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCloudWatchAlarms"
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.alerts.arn
      }
    ]
  })
}

# ─────────────────────────────────────────────
# VPC Module - Full HA configuration
# ─────────────────────────────────────────────
module "vpc" {
  source = "../../modules/vpc"

  name        = local.name
  environment = local.environment

  vpc_cidr           = "10.20.0.0/16"  # Separate CIDR from dev
  az_count           = 3               # 3 AZs for production HA
  single_nat_gateway = false           # One NAT per AZ for HA
  enable_flow_logs   = true            # Always enable in prod

  tags = local.common_tags
}

# ─────────────────────────────────────────────
# RDS Module - Production configuration
# ─────────────────────────────────────────────
module "rds" {
  source = "../../modules/rds"

  name        = local.name
  environment = local.environment

  subnet_ids         = module.vpc.database_subnet_ids
  security_group_ids = [module.vpc.database_security_group_id]

  # Production-grade instance
  instance_class        = "db.r8g.xlarge"   # Memory-optimized for pgvector
  allocated_storage     = 500               # 500GB initial
  max_allocated_storage = 5000              # Auto-scale up to 5TB

  # Database config
  db_name     = "forge"
  db_username = "forge"
  db_password = var.db_password  # Supplied via TF_VAR or SOPS

  # Production HA settings
  multi_az              = true
  backup_retention_days = 30   # 30-day retention for prod
  max_connections       = 500  # Support 500 concurrent connections
  create_read_replica   = true
  replica_instance_class = "db.r8g.large"

  alarm_sns_topic_arn = aws_sns_topic.alerts.arn
  tags                = local.common_tags
}

# ─────────────────────────────────────────────
# Redis Module - Production configuration
# ─────────────────────────────────────────────
module "redis" {
  source = "../../modules/redis"

  name        = local.name
  environment = local.environment

  subnet_ids         = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.cache_security_group_id]

  # Production-grade node
  node_type       = "cache.r7g.large"   # Memory-optimized
  num_cache_nodes = 3                   # Primary + 2 replicas for HA

  # Production auth token
  auth_token = var.redis_auth_token  # Supplied via TF_VAR or SOPS

  # Persistence settings
  enable_aof              = true
  snapshot_retention_days = 7

  alarm_sns_topic_arn = aws_sns_topic.alerts.arn
  tags                = local.common_tags
}

# ─────────────────────────────────────────────
# WAF Web ACL for API protection
# ─────────────────────────────────────────────
resource "aws_wafv2_web_acl" "api" {
  name        = "${local.name}-${local.environment}-api-waf"
  description = "WAF for FORGE API (${local.environment})"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  # Rate limiting rule
  rule {
    name     = "RateLimitRule"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 2000
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  # AWS Managed Rules - Common Rule Set
  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-common-rules"
      sampled_requests_enabled   = true
    }
  }

  # SQL Injection protection
  rule {
    name     = "AWSManagedRulesSQLiRuleSet"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesSQLiRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-sqli-rules"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-waf"
    sampled_requests_enabled   = true
  }

  tags = merge(local.common_tags, {
    Name = "${local.name}-${local.environment}-api-waf"
  })
}

# ─────────────────────────────────────────────
# CloudWatch Dashboard
# ─────────────────────────────────────────────
resource "aws_cloudwatch_dashboard" "forge" {
  dashboard_name = "${local.name}-${local.environment}"
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 1
        properties = {
          markdown = "# FORGE Production Dashboard"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 1
        width  = 8
        height = 6
        properties = {
          title   = "RDS CPU Utilization"
          metrics = [["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", module.rds.db_instance_id]]
          period  = 300
          stat    = "Average"
          view    = "timeSeries"
          region  = var.aws_region
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 1
        width  = 8
        height = 6
        properties = {
          title   = "RDS Database Connections"
          metrics = [["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", module.rds.db_instance_id]]
          period  = 300
          stat    = "Average"
          view    = "timeSeries"
          region  = var.aws_region
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 1
        width  = 8
        height = 6
        properties = {
          title   = "Redis CPU Utilization"
          metrics = [["AWS/ElastiCache", "CPUUtilization", "ReplicationGroupId", module.redis.replication_group_id]]
          period  = 300
          stat    = "Average"
          view    = "timeSeries"
          region  = var.aws_region
        }
      }
    ]
  })
}

# ─────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────
output "vpc_id" {
  description = "VPC ID"
  value       = module.vpc.vpc_id
}

output "vpc_cidr" {
  description = "VPC CIDR block"
  value       = module.vpc.vpc_cidr
}

output "public_subnet_ids" {
  description = "Public subnet IDs"
  value       = module.vpc.public_subnet_ids
}

output "private_subnet_ids" {
  description = "Private subnet IDs"
  value       = module.vpc.private_subnet_ids
}

output "database_subnet_ids" {
  description = "Database subnet IDs"
  value       = module.vpc.database_subnet_ids
}

output "db_endpoint" {
  description = "RDS primary endpoint"
  value       = module.rds.db_endpoint
}

output "db_read_replica_endpoint" {
  description = "RDS read replica endpoint"
  value       = module.rds.db_read_replica_endpoint
}

output "db_address" {
  description = "RDS hostname"
  value       = module.rds.db_address
}

output "db_name" {
  description = "Database name"
  value       = module.rds.db_name
}

output "db_credentials_secret_arn" {
  description = "Secrets Manager ARN for DB credentials"
  value       = module.rds.db_credentials_secret_arn
}

output "db_connection_string" {
  description = "Database connection string"
  value       = module.rds.db_connection_string
  sensitive   = true
}

output "redis_primary_endpoint" {
  description = "Redis primary endpoint"
  value       = module.redis.primary_endpoint_address
}

output "redis_reader_endpoint" {
  description = "Redis reader endpoint"
  value       = module.redis.reader_endpoint_address
}

output "redis_url" {
  description = "Redis connection URL (TLS)"
  value       = module.redis.redis_url
  sensitive   = true
}

output "redis_credentials_secret_arn" {
  description = "Secrets Manager ARN for Redis credentials"
  value       = module.redis.redis_credentials_secret_arn
}

output "waf_web_acl_arn" {
  description = "WAF Web ACL ARN"
  value       = aws_wafv2_web_acl.api.arn
}

output "alerts_sns_topic_arn" {
  description = "SNS topic ARN for production alerts"
  value       = aws_sns_topic.alerts.arn
}

output "nat_gateway_public_ips" {
  description = "NAT gateway public IPs (for IP allowlisting)"
  value       = module.vpc.nat_gateway_public_ips
}

output "availability_zones" {
  description = "Availability zones in use"
  value       = module.vpc.availability_zones
}
