output "aws_region" {
  description = "AWS region where the benchmark infrastructure is deployed."
  value       = var.aws_region
}

output "events_url" {
  description = "Public Function URL of the classic Lambda + Lambda Web Adapter."
  value       = aws_lambda_function_url.events_fastapi.function_url
}

output "events_function_name" {
  description = "Function name of the classic Lambda."
  value       = aws_lambda_function.events_fastapi.function_name
}

output "events_log_group" {
  description = "CloudWatch Log Group name for the classic Lambda."
  value       = aws_cloudwatch_log_group.events_fastapi.name
}

output "web_url" {
  description = "Public HTTPS endpoint URL of the AWS Lambda Web Function."
  value       = "https://${aws_lambdaweb_function_endpoint.web_fastapi.domain_name}"
}

output "web_function_name" {
  description = "Function name of the AWS Lambda Web Function."
  value       = aws_lambdaweb_function.web_fastapi.function_name
}

output "web_log_group" {
  description = "CloudWatch Log Group name for the AWS Lambda Web Function."
  value       = aws_cloudwatch_log_group.web_fastapi.name
}

output "web_revision_id" {
  description = "Active revision ID deployed to the Lambda Web Function endpoint."
  value       = aws_lambdaweb_function_revision.web_fastapi.revision_id
}
