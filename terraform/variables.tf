variable "project_name" {
  description = "Base prefix used to name AWS resources for this benchmark."
  type        = string
  default     = "fastapi-web-lambda"
}

variable "aws_region" {
  description = "AWS region where all benchmark resources are deployed."
  type        = string
  default     = "eu-west-1"
}

variable "aws_profile" {
  description = "AWS CLI named profile used for Terraform operations."
  type        = string
}

variable "terraform_s3_bucket" {
  description = "Existing S3 bucket where the packaged events Lambda zip is uploaded before plan/apply."
  type        = string
}

variable "events_s3_key" {
  description = "S3 object key for the classic events Lambda zip archive."
  type        = string
  default     = "fastapi-benchmark/events.zip"
}

variable "events_memory_mb" {
  description = "Memory allocation in MB for the classic Lambda running FastAPI + Lambda Web Adapter."
  type        = number
  default     = 512
}

variable "events_runtime" {
  description = "Managed Python runtime for the classic Lambda."
  type        = string
  default     = "python3.15"
}

variable "web_runtime" {
  description = "Managed Python runtime for the AWS Lambda Web Function."
  type        = string
  default     = "python3.15"
}

variable "lwa_layer_arn" {
  description = "Official AWS Lambda Web Adapter Arm64 layer ARN in eu-west-1."
  type        = string
  default     = "arn:aws:lambda:eu-west-1:753240598075:layer:LambdaAdapterLayerArm64:30"
}

variable "delay_seconds" {
  description = "Simulated async I/O delay in seconds inside GET /dummy."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention period in days."
  type        = number
  default     = 14
}
