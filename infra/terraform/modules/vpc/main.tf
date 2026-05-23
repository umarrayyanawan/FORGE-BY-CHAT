# ═══════════════════════════════════════════════════════════════
# FORGE - AWS VPC Module
# Creates a production-grade VPC with public/private subnets,
# NAT gateways, and all necessary networking components.
# ═══════════════════════════════════════════════════════════════

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# ─────────────────────────────────────────────
# Data Sources
# ─────────────────────────────────────────────
data "aws_availability_zones" "available" {
  state = "available"
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

# ─────────────────────────────────────────────
# Local values
# ─────────────────────────────────────────────
locals {
  az_count = min(var.az_count, length(data.aws_availability_zones.available.names))
  azs      = slice(data.aws_availability_zones.available.names, 0, local.az_count)

  # Calculate subnet CIDRs automatically
  public_subnet_cidrs = [
    for i in range(local.az_count) :
    cidrsubnet(var.vpc_cidr, 8, i)
  ]
  private_subnet_cidrs = [
    for i in range(local.az_count) :
    cidrsubnet(var.vpc_cidr, 8, i + 10)
  ]
  database_subnet_cidrs = [
    for i in range(local.az_count) :
    cidrsubnet(var.vpc_cidr, 8, i + 20)
  ]

  common_tags = merge(var.tags, {
    "terraform"         = "true"
    "terraform-module"  = "vpc"
    "forge-environment" = var.environment
  })
}

# ─────────────────────────────────────────────
# VPC
# ─────────────────────────────────────────────
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  instance_tenancy     = "default"

  tags = merge(local.common_tags, {
    Name = "${var.name}-vpc"
  })
}

# ─────────────────────────────────────────────
# Internet Gateway
# ─────────────────────────────────────────────
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(local.common_tags, {
    Name = "${var.name}-igw"
  })
}

# ─────────────────────────────────────────────
# Public Subnets
# ─────────────────────────────────────────────
resource "aws_subnet" "public" {
  count = local.az_count

  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, {
    Name                     = "${var.name}-public-${local.azs[count.index]}"
    "subnet-type"            = "public"
    # Required for EKS
    "kubernetes.io/role/elb" = "1"
  })
}

# ─────────────────────────────────────────────
# Private Subnets (for application workloads)
# ─────────────────────────────────────────────
resource "aws_subnet" "private" {
  count = local.az_count

  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.private_subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = merge(local.common_tags, {
    Name                              = "${var.name}-private-${local.azs[count.index]}"
    "subnet-type"                     = "private"
    # Required for EKS internal load balancers
    "kubernetes.io/role/internal-elb" = "1"
  })
}

# ─────────────────────────────────────────────
# Database Subnets (isolated, no internet access)
# ─────────────────────────────────────────────
resource "aws_subnet" "database" {
  count = local.az_count

  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.database_subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = merge(local.common_tags, {
    Name          = "${var.name}-database-${local.azs[count.index]}"
    "subnet-type" = "database"
  })
}

# ─────────────────────────────────────────────
# Elastic IPs for NAT Gateways
# ─────────────────────────────────────────────
resource "aws_eip" "nat" {
  # One NAT per AZ for HA (unless single NAT is requested for cost savings)
  count = var.single_nat_gateway ? 1 : local.az_count

  domain = "vpc"

  tags = merge(local.common_tags, {
    Name = var.single_nat_gateway ? "${var.name}-nat-eip" : "${var.name}-nat-eip-${local.azs[count.index]}"
  })

  depends_on = [aws_internet_gateway.main]
}

# ─────────────────────────────────────────────
# NAT Gateways
# ─────────────────────────────────────────────
resource "aws_nat_gateway" "main" {
  count = var.single_nat_gateway ? 1 : local.az_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(local.common_tags, {
    Name = var.single_nat_gateway ? "${var.name}-nat" : "${var.name}-nat-${local.azs[count.index]}"
  })

  depends_on = [aws_internet_gateway.main]
}

# ─────────────────────────────────────────────
# Route Tables - Public
# ─────────────────────────────────────────────
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(local.common_tags, {
    Name = "${var.name}-public-rt"
  })
}

resource "aws_route_table_association" "public" {
  count = local.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ─────────────────────────────────────────────
# Route Tables - Private
# ─────────────────────────────────────────────
resource "aws_route_table" "private" {
  count = var.single_nat_gateway ? 1 : local.az_count

  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = var.single_nat_gateway ? aws_nat_gateway.main[0].id : aws_nat_gateway.main[count.index].id
  }

  tags = merge(local.common_tags, {
    Name = var.single_nat_gateway ? "${var.name}-private-rt" : "${var.name}-private-rt-${local.azs[count.index]}"
  })
}

resource "aws_route_table_association" "private" {
  count = local.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = var.single_nat_gateway ? aws_route_table.private[0].id : aws_route_table.private[count.index].id
}

# ─────────────────────────────────────────────
# Route Tables - Database (no internet, only VPC-internal)
# ─────────────────────────────────────────────
resource "aws_route_table" "database" {
  vpc_id = aws_vpc.main.id

  # No default route - database subnets are fully isolated

  tags = merge(local.common_tags, {
    Name = "${var.name}-database-rt"
  })
}

resource "aws_route_table_association" "database" {
  count = local.az_count

  subnet_id      = aws_subnet.database[count.index].id
  route_table_id = aws_route_table.database.id
}

# ─────────────────────────────────────────────
# RDS Subnet Group
# ─────────────────────────────────────────────
resource "aws_db_subnet_group" "main" {
  name        = "${var.name}-db-subnet-group"
  description = "Database subnet group for ${var.name}"
  subnet_ids  = aws_subnet.database[*].id

  tags = merge(local.common_tags, {
    Name = "${var.name}-db-subnet-group"
  })
}

# ─────────────────────────────────────────────
# ElastiCache Subnet Group
# ─────────────────────────────────────────────
resource "aws_elasticache_subnet_group" "main" {
  name        = "${var.name}-cache-subnet-group"
  description = "Cache subnet group for ${var.name}"
  subnet_ids  = aws_subnet.private[*].id

  tags = merge(local.common_tags, {
    Name = "${var.name}-cache-subnet-group"
  })
}

# ─────────────────────────────────────────────
# Security Groups
# ─────────────────────────────────────────────

# Default security group (lock it down)
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  # Intentionally empty - no default ingress/egress

  tags = merge(local.common_tags, {
    Name = "${var.name}-default-sg-LOCKED"
  })
}

# Application security group
resource "aws_security_group" "application" {
  name        = "${var.name}-app-sg"
  description = "Security group for application instances in ${var.name}"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP from internet (redirect to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "API from within VPC"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.name}-app-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# Database security group
resource "aws_security_group" "database" {
  name        = "${var.name}-db-sg"
  description = "Security group for RDS instances in ${var.name}"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL from application"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.application.id]
  }

  egress {
    description = "No outbound from database"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = merge(local.common_tags, {
    Name = "${var.name}-db-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# Cache security group
resource "aws_security_group" "cache" {
  name        = "${var.name}-cache-sg"
  description = "Security group for ElastiCache instances in ${var.name}"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Redis from application"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.application.id]
  }

  egress {
    description = "No outbound from cache"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = merge(local.common_tags, {
    Name = "${var.name}-cache-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# ─────────────────────────────────────────────
# VPC Flow Logs
# ─────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  name              = "/aws/vpc/${var.name}/flow-logs"
  retention_in_days = 30

  tags = merge(local.common_tags, {
    Name = "${var.name}-vpc-flow-logs"
  })
}

resource "aws_iam_role" "vpc_flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  name = "${var.name}-vpc-flow-logs-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "vpc-flow-logs.amazonaws.com"
        }
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "vpc_flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  name = "${var.name}-vpc-flow-logs-policy"
  role = aws_iam_role.vpc_flow_logs[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams"
        ]
        Effect   = "Allow"
        Resource = "*"
      }
    ]
  })
}

resource "aws_flow_log" "main" {
  count = var.enable_flow_logs ? 1 : 0

  vpc_id          = aws_vpc.main.id
  iam_role_arn    = aws_iam_role.vpc_flow_logs[0].arn
  log_destination = aws_cloudwatch_log_group.vpc_flow_logs[0].arn
  traffic_type    = "ALL"

  tags = merge(local.common_tags, {
    Name = "${var.name}-flow-log"
  })
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

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid CIDR block."
  }
}

variable "az_count" {
  description = "Number of availability zones to use"
  type        = number
  default     = 3

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 6
    error_message = "az_count must be between 2 and 6."
  }
}

variable "single_nat_gateway" {
  description = "Use a single NAT gateway (cost-saving for dev, not recommended for prod)"
  type        = bool
  default     = false
}

variable "enable_flow_logs" {
  description = "Enable VPC flow logs to CloudWatch"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}

# ─────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────
output "vpc_id" {
  description = "The ID of the VPC"
  value       = aws_vpc.main.id
}

output "vpc_cidr" {
  description = "The CIDR block of the VPC"
  value       = aws_vpc.main.cidr_block
}

output "public_subnet_ids" {
  description = "List of public subnet IDs"
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "List of private subnet IDs"
  value       = aws_subnet.private[*].id
}

output "database_subnet_ids" {
  description = "List of database subnet IDs"
  value       = aws_subnet.database[*].id
}

output "db_subnet_group_name" {
  description = "Name of the RDS subnet group"
  value       = aws_db_subnet_group.main.name
}

output "cache_subnet_group_name" {
  description = "Name of the ElastiCache subnet group"
  value       = aws_elasticache_subnet_group.main.name
}

output "internet_gateway_id" {
  description = "The ID of the Internet Gateway"
  value       = aws_internet_gateway.main.id
}

output "nat_gateway_ids" {
  description = "List of NAT Gateway IDs"
  value       = aws_nat_gateway.main[*].id
}

output "nat_gateway_public_ips" {
  description = "List of public IPs of NAT Gateways"
  value       = aws_eip.nat[*].public_ip
}

output "application_security_group_id" {
  description = "ID of the application security group"
  value       = aws_security_group.application.id
}

output "database_security_group_id" {
  description = "ID of the database security group"
  value       = aws_security_group.database.id
}

output "cache_security_group_id" {
  description = "ID of the cache security group"
  value       = aws_security_group.cache.id
}

output "availability_zones" {
  description = "List of availability zones used"
  value       = local.azs
}
