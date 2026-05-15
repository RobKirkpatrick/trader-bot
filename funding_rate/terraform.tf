# NEW: Terraform configuration for funding_rate module resources
# Defines DynamoDB tables, Lambda functions, EventBridge rules, and IAM permissions

terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "lambda_timeout" {
  type    = number
  default = 60
}

variable "lambda_memory" {
  type    = number
  default = 512
}

variable "sentinel_sns_topic_arn" {
  type = string
}

# ============================================================================
# DynamoDB Tables
# ============================================================================

resource "aws_dynamodb_table" "funding_rate_opportunities" {
  name           = "funding-rate-opportunities"
  billing_mode   = "PAY_PER_REQUEST"
  hash_key       = "perp_ticker"
  range_key      = "scanned_at"

  attribute {
    name = "perp_ticker"
    type = "S"
  }

  attribute {
    name = "scanned_at"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = {
    Name    = "funding-rate-opportunities"
    Module  = "funding_rate"
  }
}

resource "aws_dynamodb_table" "funding_rate_positions" {
  name           = "funding-rate-positions"
  billing_mode   = "PAY_PER_REQUEST"
  hash_key       = "position_id"

  attribute {
    name = "position_id"
    type = "S"
  }

  tags = {
    Name    = "funding-rate-positions"
    Module  = "funding_rate"
  }
}

# ============================================================================
# IAM Role for Lambda
# ============================================================================

resource "aws_iam_role" "funding_rate_lambda_role" {
  name = "funding-rate-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Module = "funding_rate"
  }
}

# CloudWatch Logs
resource "aws_iam_role_policy" "funding_rate_logs" {
  name = "funding-rate-logs"
  role = aws_iam_role.funding_rate_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

# DynamoDB
resource "aws_iam_role_policy" "funding_rate_dynamodb" {
  name = "funding-rate-dynamodb"
  role = aws_iam_role.funding_rate_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:DeleteItem"
        ]
        Resource = [
          aws_dynamodb_table.funding_rate_opportunities.arn,
          aws_dynamodb_table.funding_rate_positions.arn,
          "${aws_dynamodb_table.funding_rate_opportunities.arn}/index/*",
          "${aws_dynamodb_table.funding_rate_positions.arn}/index/*"
        ]
      }
    ]
  })
}

# SNS (publish alerts)
resource "aws_iam_role_policy" "funding_rate_sns" {
  name = "funding-rate-sns"
  role = aws_iam_role.funding_rate_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = [
          var.sentinel_sns_topic_arn
        ]
      }
    ]
  })
}

# Secrets Manager (for Coinbase credentials)
resource "aws_iam_role_policy" "funding_rate_secrets" {
  name = "funding-rate-secrets"
  role = aws_iam_role.funding_rate_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          "arn:aws:secretsmanager:*:*:secret:coinbase-api-*"
        ]
      }
    ]
  })
}

# ============================================================================
# EventBridge Rules and Lambda Targets
# ============================================================================

# Scanner rule: every 4 hours
resource "aws_cloudwatch_event_rule" "funding_rate_scanner" {
  name                = "funding-rate-scanner"
  schedule_expression = "rate(4 hours)"
  is_enabled          = true

  tags = {
    Module = "funding_rate"
  }
}

# Monitor rule: every 1 hour
resource "aws_cloudwatch_event_rule" "funding_rate_monitor" {
  name                = "funding-rate-monitor"
  schedule_expression = "rate(1 hour)"
  is_enabled          = true

  tags = {
    Module = "funding_rate"
  }
}

# ============================================================================
# Lambda Functions (stubs — fill in with actual code)
# ============================================================================

resource "aws_lambda_function" "funding_rate_scanner" {
  filename         = "lambda_scanner.zip"
  function_name    = "funding-rate-scanner"
  role             = aws_iam_role.funding_rate_lambda_role.arn
  handler          = "index.handler"
  runtime          = "python3.11"
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory

  environment {
    variables = {
      SENTINEL_SNS_ARN = var.sentinel_sns_topic_arn
    }
  }

  tags = {
    Module = "funding_rate"
  }
}

resource "aws_lambda_function" "funding_rate_monitor" {
  filename         = "lambda_monitor.zip"
  function_name    = "funding-rate-monitor"
  role             = aws_iam_role.funding_rate_lambda_role.arn
  handler          = "index.handler"
  runtime          = "python3.11"
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory

  environment {
    variables = {
      SENTINEL_SNS_ARN = var.sentinel_sns_topic_arn
    }
  }

  tags = {
    Module = "funding_rate"
  }
}

# Scanner EventBridge target
resource "aws_cloudwatch_event_target" "funding_rate_scanner_target" {
  rule      = aws_cloudwatch_event_rule.funding_rate_scanner.name
  target_id = "FundingRateScannerTarget"
  arn       = aws_lambda_function.funding_rate_scanner.arn

  retry_policy {
    maximum_event_age       = 3600
    maximum_retry_attempts  = 2
  }

  dead_letter_config {
    arn = aws_sqs_queue.funding_rate_dlq.arn
  }
}

# Monitor EventBridge target
resource "aws_cloudwatch_event_target" "funding_rate_monitor_target" {
  rule      = aws_cloudwatch_event_rule.funding_rate_monitor.name
  target_id = "FundingRateMonitorTarget"
  arn       = aws_lambda_function.funding_rate_monitor.arn

  retry_policy {
    maximum_event_age       = 3600
    maximum_retry_attempts  = 2
  }

  dead_letter_config {
    arn = aws_sqs_queue.funding_rate_dlq.arn
  }
}

# Lambda permissions for EventBridge invocation
resource "aws_lambda_permission" "funding_rate_scanner_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.funding_rate_scanner.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.funding_rate_scanner.arn
}

resource "aws_lambda_permission" "funding_rate_monitor_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.funding_rate_monitor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.funding_rate_monitor.arn
}

# ============================================================================
# Dead Letter Queue for failed executions
# ============================================================================

resource "aws_sqs_queue" "funding_rate_dlq" {
  name                       = "funding-rate-dlq"
  message_retention_seconds  = 86400  # 24 hours

  tags = {
    Module = "funding_rate"
  }
}

# ============================================================================
# CloudWatch Alarms
# ============================================================================

resource "aws_cloudwatch_metric_alarm" "scanner_errors" {
  alarm_name          = "funding-rate-scanner-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = "1"
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = "300"
  statistic           = "Sum"
  threshold           = "1"
  alarm_description   = "Alert when scanner Lambda has errors"

  dimensions = {
    FunctionName = aws_lambda_function.funding_rate_scanner.function_name
  }

  alarm_actions = [var.sentinel_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "monitor_errors" {
  alarm_name          = "funding-rate-monitor-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = "1"
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = "300"
  statistic           = "Sum"
  threshold           = "1"
  alarm_description   = "Alert when monitor Lambda has errors"

  dimensions = {
    FunctionName = aws_lambda_function.funding_rate_monitor.function_name
  }

  alarm_actions = [var.sentinel_sns_topic_arn]
}

# ============================================================================
# Outputs
# ============================================================================

output "scanner_lambda_function_name" {
  value = aws_lambda_function.funding_rate_scanner.function_name
}

output "monitor_lambda_function_name" {
  value = aws_lambda_function.funding_rate_monitor.function_name
}

output "opportunities_table_name" {
  value = aws_dynamodb_table.funding_rate_opportunities.name
}

output "positions_table_name" {
  value = aws_dynamodb_table.funding_rate_positions.name
}
