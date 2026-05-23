# ═══════════════════════════════════════════════════════════════
# FORGE - AWS RDS PostgreSQL 16 Module
# Production-grade RDS with pgvector, multi-AZ, automated
# backups, enhanced monitoring, and parameter group tuning.
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
  identifier = "${var.name}-${var.environment}-postgres"
  common_tags = merge(var.tags, {
    "terraform"         = "true"
    "terraform-module"  = "rds"
    "forge-environment" = var.environment
    "engine"            = "postgres"
    "engine-version"    = "16"
  })
}

# ─────────────────────────────────────────────
# Random password for RDS (if not provided)
# ─────────────────────────────────────────────
resource "random_password" "db_password" {
  count   = var.db_password == "" ? 1 : 0
  length  = 32
  special = true
  # Avoid characters that PostgreSQL connection strings don't handle well
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

locals {
  db_password = var.db_password != "" ? var.db_password : random_password.db_password[0].result
}

# ─────────────────────────────────────────────
# AWS Secrets Manager - Store DB credentials
# ─────────────────────────────────────────────
resource "aws_secretsmanager_secret" "db_credentials" {
  name                    = "${var.name}/${var.environment}/rds/credentials"
  description             = "RDS PostgreSQL credentials for FORGE ${var.environment}"
  recovery_window_in_days = var.environment == "prod" ? 30 : 7

  tags = merge(local.common_tags, {
    Name = "${local.identifier}-credentials"
  })
}

resource "aws_secretsmanager_secret_version" "db_credentials" {
  secret_id = aws_secretsmanager_secret.db_credentials.id
  secret_string = jsonencode({
    username             = var.db_username
    password             = local.db_password
    engine               = "postgres"
    host                 = aws_db_instance.main.address
    port                 = aws_db_instance.main.port
    dbname               = var.db_name
    dbInstanceIdentifier = aws_db_instance.main.id
  })

  depends_on = [aws_db_instance.main]
}

# ─────────────────────────────────────────────
# RDS Parameter Group (PostgreSQL 16 tuning)
# ─────────────────────────────────────────────
resource "aws_db_parameter_group" "main" {
  name        = "${local.identifier}-params"
  family      = "postgres16"
  description = "Custom parameter group for FORGE PostgreSQL 16 (${var.environment})"

  # ─────────────────────────────────────────────
  # pgvector extension support
  # ─────────────────────────────────────────────
  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements,auto_explain,pgvector"
  }

  # ─────────────────────────────────────────────
  # Memory settings
  # ─────────────────────────────────────────────
  parameter {
    name  = "work_mem"
    value = "65536" # 64MB per sort operation
  }

  parameter {
    name  = "maintenance_work_mem"
    value = "524288" # 512MB for VACUUM, index builds
  }

  parameter {
    name  = "effective_cache_size"
    value = "12582912" # 12GB assumed for query planner
  }

  # ─────────────────────────────────────────────
  # Connection & query settings
  # ─────────────────────────────────────────────
  parameter {
    name  = "max_connections"
    value = tostring(var.max_connections)
  }

  parameter {
    name  = "statement_timeout"
    value = "300000" # 5 minutes
  }

  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "60000" # 1 minute
  }

  parameter {
    name  = "lock_timeout"
    value = "30000" # 30 seconds
  }

  # ─────────────────────────────────────────────
  # WAL & replication
  # ─────────────────────────────────────────────
  parameter {
    name  = "wal_buffers"
    value = "2048" # 16MB (2048 * 8KB)
  }

  parameter {
    name  = "checkpoint_completion_target"
    value = "0.9"
  }

  parameter {
    name  = "wal_compression"
    value = "on"
  }

  # ─────────────────────────────────────────────
  # Query performance
  # ─────────────────────────────────────────────
  parameter {
    name  = "default_statistics_target"
    value = "100"
  }

  parameter {
    name  = "random_page_cost"
    value = "1.1" # Optimized for SSD storage
  }

  parameter {
    name  = "effective_io_concurrency"
    value = "200" # Higher for SSD
  }

  # ─────────────────────────────────────────────
  # Logging for performance analysis
  # ─────────────────────────────────────────────
  parameter {
    name  = "log_min_duration_statement"
    value = "1000" # Log queries > 1 second
  }

  parameter {
    name  = "log_checkpoints"
    value = "on"
  }

  parameter {
    name  = "log_connections"
    value = "on"
  }

  parameter {
    name  = "log_disconnections"
    value = "on"
  }

  parameter {
    name  = "log_lock_waits"
    value = "on"
  }

  parameter {
    name  = "log_temp_files"
    value = "0" # Log all temp file usage
  }

  # ─────────────────────────────────────────────
  # Auto-explain for slow query analysis
  # ─────────────────────────────────────────────
  parameter {
    name  = "auto_explain.log_min_duration"
    value = "5000" # Auto-explain for queries > 5 seconds
  }

  parameter {
    name  = "auto_explain.log_analyze"
    value = "on"
  }

  parameter {
    name  = "auto_explain.log_buffers"
    value = "on"
  }

  parameter {
    name  = "auto_explain.log_nested_statements"
    value = "on"
  }

  # ─────────────────────────────────────────────
  # pg_stat_statements
  # ─────────────────────────────────────────────
  parameter {
    name  = "pg_stat_statements.track"
    value = "all"
  }

  parameter {
    name  = "pg_stat_statements.max"
    value = "10000"
  }

  tags = merge(local.common_tags, {
    Name = "${local.identifier}-params"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# ─────────────────────────────────────────────
# RDS Option Group
# ─────────────────────────────────────────────
resource "aws_db_option_group" "main" {
  name                     = "${local.identifier}-options"
  option_group_description = "Option group for FORGE PostgreSQL 16 (${var.environment})"
  engine_name              = "postgres"
  major_engine_version     = "16"

  tags = merge(local.common_tags, {
    Name = "${local.identifier}-options"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# ─────────────────────────────────────────────
# IAM Role for Enhanced Monitoring
# ─────────────────────────────────────────────
resource "aws_iam_role" "rds_monitoring" {
  name = "${local.identifier}-monitoring-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "monitoring.rds.amazonaws.com"
        }
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# ─────────────────────────────────────────────
# KMS Key for RDS encryption
# ─────────────────────────────────────────────
resource "aws_kms_key" "rds" {
  description             = "KMS key for FORGE RDS encryption (${var.environment})"
  deletion_window_in_days = var.environment == "prod" ? 30 : 7
  enable_key_rotation     = true
  multi_region            = false

  tags = merge(local.common_tags, {
    Name = "${local.identifier}-kms"
  })
}

resource "aws_kms_alias" "rds" {
  name          = "alias/forge/${var.environment}/rds"
  target_key_id = aws_kms_key.rds.key_id
}

# ─────────────────────────────────────────────
# RDS Subnet Group
# ─────────────────────────────────────────────
resource "aws_db_subnet_group" "main" {
  name        = "${local.identifier}-subnet-group"
  description = "Subnet group for FORGE RDS (${var.environment})"
  subnet_ids  = var.subnet_ids

  tags = merge(local.common_tags, {
    Name = "${local.identifier}-subnet-group"
  })
}

# ─────────────────────────────────────────────
# RDS Instance
# ─────────────────────────────────────────────
resource "aws_db_instance" "main" {
  identifier = local.identifier

  # Engine
  engine               = "postgres"
  engine_version       = "16.4"
  instance_class       = var.instance_class
  storage_type         = "gp3"
  allocated_storage    = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_encrypted    = true
  kms_key_id           = aws_kms_key.rds.arn

  # Database
  db_name  = var.db_name
  username = var.db_username
  password = local.db_password
  port     = 5432

  # Network
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = var.security_group_ids
  publicly_accessible    = false
  multi_az               = var.multi_az

  # Parameter and option groups
  parameter_group_name = aws_db_parameter_group.main.name
  option_group_name    = aws_db_option_group.main.name

  # Backups
  backup_retention_period   = var.backup_retention_days
  backup_window             = "03:00-04:00"
  copy_tags_to_snapshot     = true
  delete_automated_backups  = var.environment != "prod"
  skip_final_snapshot       = var.environment != "prod"
  final_snapshot_identifier = var.environment == "prod" ? "${local.identifier}-final-snapshot" : null

  # Maintenance
  maintenance_window         = "Mon:04:00-Mon:05:00"
  auto_minor_version_upgrade = true
  allow_major_version_upgrade = false

  # Monitoring
  monitoring_interval             = 60
  monitoring_role_arn             = aws_iam_role.rds_monitoring.arn
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.rds.arn
  performance_insights_retention_period = var.environment == "prod" ? 731 : 7

  # Deletion protection (always on for prod)
  deletion_protection = var.environment == "prod"

  # Apply changes immediately in non-prod
  apply_immediately = var.environment != "prod"

  tags = merge(local.common_tags, {
    Name = local.identifier
  })

  depends_on = [
    aws_db_parameter_group.main,
    aws_db_subnet_group.main,
    aws_iam_role_policy_attachment.rds_monitoring,
  ]

  lifecycle {
    ignore_changes = [
      password, # Managed externally after creation
    ]
    prevent_destroy = false
  }
}

# ─────────────────────────────────────────────
# Read Replica (prod only)
# ─────────────────────────────────────────────
resource "aws_db_instance" "read_replica" {
  count = var.create_read_replica ? 1 : 0

  identifier              = "${local.identifier}-replica"
  replicate_source_db     = aws_db_instance.main.identifier
  instance_class          = var.replica_instance_class != "" ? var.replica_instance_class : var.instance_class
  storage_type            = "gp3"
  storage_encrypted       = true
  kms_key_id              = aws_kms_key.rds.arn
  parameter_group_name    = aws_db_parameter_group.main.name
  vpc_security_group_ids  = var.security_group_ids
  publicly_accessible     = false
  skip_final_snapshot     = true
  auto_minor_version_upgrade = true

  monitoring_interval = 60
  monitoring_role_arn = aws_iam_role.rds_monitoring.arn

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.rds.arn
  performance_insights_retention_period = 7

  apply_immediately = var.environment != "prod"

  tags = merge(local.common_tags, {
    Name = "${local.identifier}-replica"
    role = "read-replica"
  })
}

# ─────────────────────────────────────────────
# CloudWatch Alarms
# ─────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  alarm_name          = "${local.identifier}-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "RDS CPU utilization is too high"
  alarm_actions       = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []
  ok_actions          = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.id
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "storage_low" {
  alarm_name          = "${local.identifier}-storage-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FreeStorageSpace"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 10737418240 # 10GB in bytes
  alarm_description   = "RDS free storage space is critically low"
  alarm_actions       = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.id
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "connections_high" {
  alarm_name          = "${local.identifier}-connections-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "DatabaseConnections"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = var.max_connections * 0.8
  alarm_description   = "RDS connection count is too high (>80% of max)"
  alarm_actions       = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.id
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
  description = "List of subnet IDs for the DB subnet group"
  type        = list(string)
}

variable "security_group_ids" {
  description = "List of security group IDs for the RDS instance"
  type        = list(string)
}

variable "instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t4g.medium"
}

variable "replica_instance_class" {
  description = "RDS read replica instance class (empty = same as primary)"
  type        = string
  default     = ""
}

variable "allocated_storage" {
  description = "Initial allocated storage in GB"
  type        = number
  default     = 100
}

variable "max_allocated_storage" {
  description = "Maximum storage autoscaling limit in GB"
  type        = number
  default     = 1000
}

variable "db_name" {
  description = "Name of the default database"
  type        = string
  default     = "forge"
}

variable "db_username" {
  description = "Master username for the database"
  type        = string
  default     = "forge"
}

variable "db_password" {
  description = "Master password (leave empty to auto-generate)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "multi_az" {
  description = "Enable Multi-AZ deployment"
  type        = bool
  default     = true
}

variable "backup_retention_days" {
  description = "Number of days to retain automated backups"
  type        = number
  default     = 7
}

variable "max_connections" {
  description = "Maximum number of database connections"
  type        = number
  default     = 200
}

variable "create_read_replica" {
  description = "Create a read replica"
  type        = bool
  default     = false
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
output "db_instance_id" {
  description = "The RDS instance ID"
  value       = aws_db_instance.main.id
}

output "db_instance_arn" {
  description = "The ARN of the RDS instance"
  value       = aws_db_instance.main.arn
}

output "db_endpoint" {
  description = "The connection endpoint"
  value       = aws_db_instance.main.endpoint
}

output "db_address" {
  description = "The hostname of the RDS instance"
  value       = aws_db_instance.main.address
}

output "db_port" {
  description = "The database port"
  value       = aws_db_instance.main.port
}

output "db_name" {
  description = "The database name"
  value       = aws_db_instance.main.db_name
}

output "db_username" {
  description = "The master username"
  value       = aws_db_instance.main.username
  sensitive   = true
}

output "db_password" {
  description = "The master password"
  value       = local.db_password
  sensitive   = true
}

output "db_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret containing DB credentials"
  value       = aws_secretsmanager_secret.db_credentials.arn
}

output "db_read_replica_endpoint" {
  description = "The read replica endpoint (if created)"
  value       = var.create_read_replica ? aws_db_instance.read_replica[0].endpoint : null
}

output "kms_key_arn" {
  description = "ARN of the KMS key used for RDS encryption"
  value       = aws_kms_key.rds.arn
}

output "db_connection_string" {
  description = "PostgreSQL connection string (asyncpg format)"
  value       = "postgresql+asyncpg://${var.db_username}:${local.db_password}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${var.db_name}"
  sensitive   = true
}
