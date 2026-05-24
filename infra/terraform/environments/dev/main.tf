# ═══════════════════════════════════════════════════════════════
# FORGE - Development Environment
# Cost-optimized configuration with single NAT gateway,
# smaller instance sizes, and relaxed retention policies.
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

  # Remote state backend - update bucket name before applying
  backend "s3" {
    bucket         = "forge-terraform-state-dev"
    key            = "environments/dev/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "forge-terraform-locks-dev"
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
      Environment = "dev"
      ManagedBy   = "terraform"
      Repository  = "github.com/forge-dev/forge"
      Owner       = "platform-team"
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
  description = "SNS topic ARN for CloudWatch alarms"
  type        = string
  default     = ""
}

# ─────────────────────────────────────────────
# Local configuration
# ─────────────────────────────────────────────
locals {
  name        = "forge"
  environment = "dev"

  common_tags = {
    Environment = local.environment
    Project     = "FORGE"
    ManagedBy   = "terraform"
  }
}

# ─────────────────────────────────────────────
# VPC Module
# ─────────────────────────────────────────────
module "vpc" {
  source = "../../modules/vpc"

  name        = local.name
  environment = local.environment

  vpc_cidr           = "10.10.0.0/16"
  az_count           = 2             # 2 AZs in dev (cost saving)
  single_nat_gateway = true          # Single NAT in dev (cost saving)
  enable_flow_logs   = false         # Disable flow logs in dev (cost saving)

  tags = local.common_tags
}

# ─────────────────────────────────────────────
# RDS Module
# ─────────────────────────────────────────────
module "rds" {
  source = "../../modules/rds"

  name        = local.name
  environment = local.environment

  subnet_ids         = module.vpc.database_subnet_ids
  security_group_ids = [module.vpc.database_security_group_id]

  # Small instance for dev
  instance_class        = "db.t4g.small"
  allocated_storage     = 20
  max_allocated_storage = 100

  # Database config
  db_name     = "forge"
  db_username = "forge"
  db_password = ""  # Auto-generate

  # No multi-AZ in dev
  multi_az              = false
  backup_retention_days = 3
  max_connections       = 100
  create_read_replica   = false

  alarm_sns_topic_arn = var.alarm_sns_topic_arn
  tags                = local.common_tags
}

# ─────────────────────────────────────────────
# Redis Module
# ─────────────────────────────────────────────
module "redis" {
  source = "../../modules/redis"

  name        = local.name
  environment = local.environment

  subnet_ids         = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.cache_security_group_id]

  # Small node for dev
  node_type       = "cache.t4g.small"
  num_cache_nodes = 1  # Single node in dev

  # Persistence settings
  enable_aof              = false  # Disable AOF in dev
  snapshot_retention_days = 1

  alarm_sns_topic_arn = var.alarm_sns_topic_arn
  tags                = local.common_tags
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
  description = "RDS endpoint"
  value       = module.rds.db_endpoint
}

output "db_address" {
  description = "RDS hostname"
  value       = module.rds.db_address
}

output "db_port" {
  description = "RDS port"
  value       = module.rds.db_port
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

output "redis_port" {
  description = "Redis port"
  value       = module.redis.port
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

output "application_security_group_id" {
  description = "Application security group ID"
  value       = module.vpc.application_security_group_id
}

output "nat_gateway_public_ips" {
  description = "NAT gateway public IPs (for whitelist)"
  value       = module.vpc.nat_gateway_public_ips
}
