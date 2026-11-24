data "aws_caller_identity" "current" {}

# Shared trust policy allowing the AWS Lambda service to assume execution roles.
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    actions = ["sts:AssumeRole"]
  }
}

# Execution role for the classic Lambda + Lambda Web Adapter.
resource "aws_iam_role" "events_fastapi" {
  name               = "${var.project_name}-events-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "events_fastapi_logs" {
  role       = aws_iam_role.events_fastapi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Execution role for the AWS Lambda Web Function.
resource "aws_iam_role" "web_fastapi" {
  name               = "${var.project_name}-web-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "web_fastapi_logs" {
  role       = aws_iam_role.web_fastapi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
