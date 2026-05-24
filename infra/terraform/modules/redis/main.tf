# ═══════════════════════════════════════════════════════════════
# FORGE - AWS ElastiCache Redis 7 Module
# Production-grade Redis cluster with encryption, automatic
# failover, and CloudWatch monitoring.
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
}

# ─────────────────────────────────────────────
# Local values
# ─────────────────────────────────────────────
locals {
  cluster_id = "${var.name}-${var.environment}-redis"
  common_tags = merge(var.tags, {
    "terraform"         = "true"
    "terraform-module"  = "redis"
    "forge-environment" = var.environment
    "engine"            = "redis"
    "engine-version"    = "7"
  })
}

# ─────────────────────────────────────────────
# Random auth token for Redis AUTH
# ─────────────────────────────────────────────
resource "random_password" "auth_token" {
  count   = var.auth_token == "" ? 1 : 0
  length  = 32
  special = false # Redis AUTH tokens must be alphanumeric only
}

locals {
  auth_token = var.auth_token != "" ? var.auth_token : random_password.auth_token[0].result
}

# ─────────────────────────────────────────────
# AWS Secrets Manager - Store Redis credentials
# ─────────────────────────────────────────────
resource "aws_secretsmanager_secret" "redis_credentials" {
  name                    = "${var.name}/${var.environment}/redis/credentials"
  description             = "ElastiCache Redis credentials for FORGE ${var.environment}"
  recovery_window_in_days = var.environment == "prod" ? 30 : 7

  tags = merge(local.common_tags, {
    Name = "${local.cluster_id}-credentials"
  })
}

resource "aws_secretsmanager_secret_version" "redis_credentials" {
  secret_id = aws_secretsmanager_secret.redis_credentials.id
  secret_string = jsonencode({
    auth_token  = local.auth_token
    primary_endpoint = "${aws_elasticache_replication_group.main.primary_endpoint_address}:${var.port}"
    reader_endpoint  = "${aws_elasticache_replication_group.main.reader_endpoint_address}:${var.port}"
    redis_url   = "rediss://:${local.auth_token}@${aws_elasticache_replication_group.main.primary_endpoint_address}:${var.port}/0"
  })

  depends_on = [aws_elasticache_replication_group.main]
}

# ─────────────────────────────────────────────
# KMS Key for ElastiCache encryption
# ─────────────────────────────────────────────
resource "aws_kms_key" "redis" {
  description             = "KMS key for FORGE ElastiCache encryption (${var.environment})"
  deletion_window_in_days = var.environment == "prod" ? 30 : 7
  enable_key_rotation     = true

  tags = merge(local.common_tags, {
    Name = "${local.cluster_id}-kms"
  })
}

resource "aws_kms_alias" "redis" {
  name          = "alias/forge/${var.environment}/redis"
  target_key_id = aws_kms_key.redis.key_id
}

# ─────────────────────────────────────────────
# ElastiCache Subnet Group
# ─────────────────────────────────────────────
resource "aws_elasticache_subnet_group" "main" {
  name        = "${local.cluster_id}-subnet-group"
  description = "Subnet group for FORGE ElastiCache (${var.environment})"
  subnet_ids  = var.subnet_ids

  tags = merge(local.common_tags, {
    Name = "${local.cluster_id}-subnet-group"
  })
}

# ─────────────────────────────────────────────
# ElastiCache Parameter Group
# ─────────────────────────────────────────────
resource "aws_elasticache_parameter_group" "main" {
  name        = "${local.cluster_id}-params"
  family      = "redis7"
  description = "Custom parameter group for FORGE Redis 7 (${var.environment})"

  # Memory management
  parameter {
    name  = "maxmemory-policy"
    value = var.maxmemory_policy
  }

  # Persistence
  parameter {
    name  = "appendonly"
    value = var.enable_aof ? "yes" : "no"
  }

  parameter {
    name  = "appendfsync"
    value = "everysec"
  }

  # Connection settings
  parameter {
    name  = "tcp-keepalive"
    value = "300"
  }

  parameter {
    name  = "timeout"
    value = "0"
  }

  # Slow log settings
  parameter {
    name  = "slowlog-log-slower-than"
    value = "10000" # 10ms
  }

  parameter {
    name  = "slowlog-max-len"
    value = "1000"
  }

  # Keyspace notifications for Celery
  parameter {
    name  = "notify-keyspace-events"
    value = "Ex" # Expired events (for task monitoring)
  }

  # Latency tracking
  parameter {
    name  = "latency-tracking"
    value = "yes"
  }

  parameter {
    name  = "latency-tracking-info-percentiles"
    value = "50 99 99.9"
  }

  tags = merge(local.common_tags, {
    Name = "${local.cluster_id}-params"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# ─────────────────────────────────────────────
# ElastiCache Replication Group (Redis Cluster)
# ─────────────────────────────────────────────
resource "aws_elasticache_replication_group" "main" {
  replication_group_id = local.cluster_id
  description          = "FORGE Redis cluster for ${var.environment} environment"

  # Engine
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.node_type
  parameter_group_name = aws_elasticache_parameter_group.main.name

  # Cluster configuration
  num_cache_clusters         = var.num_cache_nodes
  automatic_failover_enabled = var.num_cache_nodes > 1
  multi_az_enabled           = var.num_cache_nodes > 1

  # Network
  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = var.security_group_ids
  port               = var.port

  # Security
  at_rest_encryption_enabled  = true
  transit_encryption_enabled  = true
  kms_key_id                  = aws_kms_key.redis.arn
  auth_token                  = local.auth_token
  auth_token_update_strategy  = "ROTATE"

  # Maintenance & backups
  maintenance_window         = "mon:05:00-mon:06:00"
  snapshot_retention_limit   = var.snapshot_retention_days
  snapshot_window            = "04:00-05:00"
  final_snapshot_identifier  = var.environment == "prod" ? "${local.cluster_id}-final" : null

  # Auto minor version upgrade
  auto_minor_version_upgrade = true

  # Apply immediately in non-prod
  apply_immediately = var.environment != "prod"

  # Log delivery
  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis_slow.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis_engine.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "engine-log"
  }

  tags = merge(local.common_tags, {
    Name = local.cluster_id
  })
}

# ─────────────────────────────────────────────
# CloudWatch Log Groups
# ─────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "redis_slow" {
  name              = "/aws/elasticache/${local.cluster_id}/slow-logs"
  retention_in_days = 30

  tags = merge(local.common_tags, {
    Name = "${local.cluster_id}-slow-logs"
  })
}

resource "aws_cloudwatch_log_group" "redis_engine" {
  name              = "/aws/elasticache/${local.cluster_id}/engine-logs"
  retention_in_days = 7

  tags = merge(local.common_tags, {
    Name = "${local.cluster_id}-engine-logs"
  })
}

# ─────────────────────────────────────────────
# CloudWatch Alarms
# ─────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  alarm_name          = "${local.cluster_id}-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ElastiCache"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "Redis CPU utilization is too high"
  alarm_actions       = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []

  dimensions = {
    ReplicationGroupId = aws_elasticache_replication_group.main.id
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "memory_high" {
  alarm_name          = "${local.cluster_id}-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "DatabaseMemoryUsagePercentage"
  namespace           = "AWS/ElastiCache"
  period              = 300
  statistic           = "Average"
  threshold           = 85
  alarm_description   = "Redis memory usage is too high (>85%)"
  alarm_actions       = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []

  dimensions = {
    ReplicationGroupId = aws_elasticache_replication_group.main.id
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "connections_high" {
  alarm_name          = "${local.cluster_id}-connections-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CurrConnections"
  namespace           = "AWS/ElastiCache"
  period              = 300
  statistic           = "Average"
  threshold           = 1000
  alarm_description   = "Redis connection count is too high"
  alarm_actions       = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []

  dimensions = {
    ReplicationGroupId = aws_elasticache_replication_group.main.id
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "evictions_high" {
  alarm_name          = "${local.cluster_id}-evictions-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Evictions"
  namespace           = "AWS/ElastiCache"
  period              = 300
  statistic           = "Sum"
  threshold           = 100
  alarm_description   = "Redis eviction rate is high - consider scaling up"
  alarm_actions       = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []

  dimensions = {
    ReplicationGroupId = aws_elasticache_replication_group.main.id
  }

  tags = local.common_tags
}

# ─────────────────────────────────────────────
# Variables
# ─────────────────────────────────────────────
variable "name" {
  description = "Name prefix for all resources"
  type        = string
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
}

variable "subnet_ids" {
  description = "List of subnet IDs for ElastiCache"
  type        = list(string)
}

variable "security_group_ids" {
  description = "List of security group IDs"
  type        = list(string)
}

variable "node_type" {
  description = "ElastiCache node type"
  type        = string
  default     = "cache.t4g.medium"
}

variable "num_cache_nodes" {
  description = "Number of cache nodes (>1 enables clustering)"
  type        = number
  default     = 2

  validation {
    condition     = var.num_cache_nodes >= 1 && var.num_cache_nodes <= 6
    error_message = "num_cache_nodes must be between 1 and 6."
  }
}

variable "port" {
  description = "Redis port"
  type        = number
  default     = 6379
}

variable "auth_token" {
  description = "Redis AUTH token (leave empty to auto-generate)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "maxmemory_policy" {
  description = "Redis maxmemory eviction policy"
  type        = string
  default     = "allkeys-lru"

  validation {
    condition = contains([
      "noeviction", "allkeys-lru", "volatile-lru",
      "allkeys-random", "volatile-random", "volatile-ttl",
      "allkeys-lfu", "volatile-lfu"
    ], var.maxmemory_policy)
    error_message = "Invalid maxmemory_policy value."
  }
}

variable "enable_aof" {
  description = "Enable AOF persistence"
  type        = bool
  default     = true
}

variable "snapshot_retention_days" {
  description = "Number of days to retain Redis snapshots"
  type        = number
  default     = 7
}

variable "alarm_sns_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarms"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}

# ─────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────
output "replication_group_id" {
  description = "The ID of the replication group"
  value       = aws_elasticache_replication_group.main.id
}

output "primary_endpoint_address" {
  description = "The primary endpoint address"
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "reader_endpoint_address" {
  description = "The reader endpoint address"
  value       = aws_elasticache_replication_group.main.reader_endpoint_address
}

output "port" {
  description = "The Redis port"
  value       = var.port
}

output "auth_token" {
  description = "The Redis AUTH token"
  value       = local.auth_token
  sensitive   = true
}

output "redis_url" {
  description = "Redis URL (TLS enabled)"
  value       = "rediss://:${local.auth_token}@${aws_elasticache_replication_group.main.primary_endpoint_address}:${var.port}/0"
  sensitive   = true
}

output "redis_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret containing Redis credentials"
  value       = aws_secretsmanager_secret.redis_credentials.arn
}

output "kms_key_arn" {
  description = "ARN of the KMS key used for encryption"
  value       = aws_kms_key.redis.arn
}
