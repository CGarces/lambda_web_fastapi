# FastAPI on AWS Lambda: Events + LWA vs. Lambda Web Functions

## Objective

Minimal benchmark comparing the same **FastAPI** application (`GET /` health check and `GET /dummy` with a 1-second `await asyncio.sleep`) deployed on AWS Lambda (`arm64`, `python3.15`) using two approaches:

1. **Lambda Events + AWS Lambda Web Adapter (LWA)** (`lambdas/events_fastapi/`): 512 MB function running `uvicorn` behind the official LWA layer, exposed via Lambda Function URL.
2. **AWS Lambda Web Functions** (`lambdas/web_fastapi/`): native ASGI deployment without `uvicorn` or adapter layers, running on the fixed 2 GB / 2 vCPU environment.

---

## 1. Package the Lambdas

### Prerequisites

- Python 3 with `pip`
- AWS CLI with a configured profile
- Terraform `>= 1.10.0`
- An existing S3 bucket for the `events.zip` artifact and Terraform state

### Build and upload

Run [`scripts/package_lambdas.py`](scripts/package_lambdas.py) to install `cp315` `manylinux2014_aarch64` wheels, create `build/events.zip` (FastAPI + Uvicorn + `run.sh`) and upload it to S3, and prepare `build/web/` (FastAPI + `main.py`) for Terraform's `archive_file`:

```bash
python scripts/package_lambdas.py \
  --bucket <your-s3-bucket> \
  --profile <your-aws-profile> \
  --region eu-west-1
```

Use `--skip-upload` if you only want to build locally under `build/`.

---

## 2. Deploy with Terraform

Before running Terraform, update the `backend "s3"` block (`bucket`, `region`, `profile`) in `terraform/versions.tf` to match your AWS environment. If you deploy outside `eu-west-1`, also override `lwa_layer_arn` in `terraform/variables.tf` (or via `-var`).

```bash
terraform -chdir=terraform init

terraform -chdir=terraform plan -out=tfplan \
  -var="terraform_s3_bucket=<your-s3-bucket>" \
  -var="aws_profile=<your-aws-profile>" \
  -var="aws_region=eu-west-1"

terraform -chdir=terraform apply "tfplan"
```

Terraform outputs the two public endpoints (`events_url` and `web_url`), which you can test with:

```bash
curl "<events_url>/dummy"
curl "<web_url>/dummy"
```

To remove the deployed infrastructure:

```bash
terraform -chdir=terraform destroy \
  -var="terraform_s3_bucket=<your-s3-bucket>" \
  -var="aws_profile=<your-aws-profile>" \
  -var="aws_region=eu-west-1"
```
