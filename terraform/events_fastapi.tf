# Verify the pre-uploaded zip archive produced by scripts/package_lambdas.py.
data "aws_s3_object" "events_zip" {
  bucket = var.terraform_s3_bucket
  key    = var.events_s3_key
}

resource "aws_cloudwatch_log_group" "events_fastapi" {
  name              = "/aws/lambda/${var.project_name}-events"
  retention_in_days = var.log_retention_days
}

# Classic event-driven Lambda running FastAPI + Uvicorn via the AWS Lambda Web Adapter layer.
resource "aws_lambda_function" "events_fastapi" {
  function_name    = "${var.project_name}-events"
  description      = "Classic Lambda with FastAPI, Uvicorn, and AWS Lambda Web Adapter Layer."
  role             = aws_iam_role.events_fastapi.arn
  s3_bucket        = data.aws_s3_object.events_zip.bucket
  s3_key           = data.aws_s3_object.events_zip.key
  source_code_hash = filebase64sha256("${path.module}/../build/events.zip")
  handler          = "run.sh"
  runtime          = var.events_runtime
  architectures    = ["arm64"]
  memory_size      = var.events_memory_mb
  timeout          = 30
  layers           = [var.lwa_layer_arn]

  environment {
    variables = {
      AWS_LAMBDA_EXEC_WRAPPER = "/opt/bootstrap"
      PORT                    = "8080"
      DELAY_SECONDS           = tostring(var.delay_seconds)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.events_fastapi,
    aws_iam_role_policy_attachment.events_fastapi_logs,
  ]
}

# Public Function URL for the classic Lambda benchmark.
resource "aws_lambda_function_url" "events_fastapi" {
  function_name      = aws_lambda_function.events_fastapi.function_name
  authorization_type = "NONE"
}
