data "archive_file" "web_fastapi" {
  type        = "zip"
  source_dir  = "${path.module}/../build/web"
  output_path = "${path.module}/web_fastapi.zip"
}

# Dedicated versioned S3 bucket required by Lambda Web Function revisions.
resource "aws_s3_bucket" "web_artifacts" {
  bucket        = "${var.project_name}-artifacts-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "web_artifacts" {
  bucket                  = aws_s3_bucket.web_artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "web_artifacts" {
  bucket = aws_s3_bucket.web_artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

data "aws_iam_policy_document" "web_artifacts_bucket_policy" {
  statement {
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]

    resources = ["${aws_s3_bucket.web_artifacts.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_s3_bucket_policy" "web_artifacts" {
  bucket = aws_s3_bucket.web_artifacts.id
  policy = data.aws_iam_policy_document.web_artifacts_bucket_policy.json

  depends_on = [aws_s3_bucket_public_access_block.web_artifacts]
}

resource "aws_s3_object" "web_fastapi_zip" {
  bucket = aws_s3_bucket.web_artifacts.id
  key    = "web_fastapi/${data.archive_file.web_fastapi.output_md5}.zip"
  source = data.archive_file.web_fastapi.output_path
  etag   = data.archive_file.web_fastapi.output_md5

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [aws_s3_bucket_versioning.web_artifacts]
}

resource "aws_cloudwatch_log_group" "web_fastapi" {
  name              = "/aws/lambda/web/${var.project_name}-web"
  retention_in_days = var.log_retention_days
}

resource "aws_lambdaweb_function" "web_fastapi" {
  function_name = "${var.project_name}-web"
}

resource "aws_lambdaweb_function_revision" "web_fastapi" {
  function_name = aws_lambdaweb_function.web_fastapi.function_name
  description   = "Native Python 3.15 ASGI FastAPI app running on AWS Lambda Web Functions."

  build_config {
    code_config {
      s3_object {
        bucket     = aws_s3_bucket.web_artifacts.id
        key        = aws_s3_object.web_fastapi_zip.key
        version_id = aws_s3_object.web_fastapi_zip.version_id
      }
    }

    runtime_config {
      runtime = var.web_runtime
    }
  }

  service_config {
    execution_role_arn              = aws_iam_role.web_fastapi.arn
    timeout_seconds                 = 30
    max_concurrency_per_environment = 64

    environment_variables = {
      DELAY_SECONDS = tostring(var.delay_seconds)
    }

    telemetry_config {
      logging_config {
        application_log_level = "INFO"
        system_log_level      = "INFO"
        log_group             = aws_cloudwatch_log_group.web_fastapi.name
      }
    }
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_cloudwatch_log_group.web_fastapi,
    aws_iam_role_policy_attachment.web_fastapi_logs,
    aws_s3_bucket_policy.web_artifacts,
  ]
}

# Public HTTPS endpoint in eu-west-1 for the Lambda Web Function benchmark.
resource "aws_lambdaweb_function_endpoint" "web_fastapi" {
  function_name        = aws_lambdaweb_function.web_fastapi.function_name
  endpoint_name        = "default"
  endpoint_type        = "HomeRegion"
  auth_type            = "ApplicationManaged"
  auto_deployment_mode = "Disabled"
  description          = "Public HTTPS endpoint for the FastAPI Lambda Web Function benchmark."

  revision_weight {
    revision_id = aws_lambdaweb_function_revision.web_fastapi.revision_id
    weight      = 100
  }
}
