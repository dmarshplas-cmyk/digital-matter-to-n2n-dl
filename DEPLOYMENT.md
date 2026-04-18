# Deployment Guide

Step-by-step instructions for deploying the DM → N2N-DL bridge to AWS from scratch.

---

## Prerequisites

### Tools

| Tool | Version | Install |
|---|---|---|
| AWS CLI | v2 | [docs.aws.amazon.com/cli](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) |
| AWS SAM CLI | latest | [docs.aws.amazon.com/serverless-application-model](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) |
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) |
| Git | any | [git-scm.com](https://git-scm.com/) |

### AWS permissions

The IAM user or role you deploy with needs permissions to create and manage:
- CloudFormation stacks
- Lambda functions and layers
- API Gateway REST APIs
- SQS queues
- DynamoDB tables
- SNS topics
- SSM Parameter Store parameters
- CloudWatch Log Groups and Alarms
- IAM roles and policies (for Lambda execution roles)
- S3 (SAM uses a deployment bucket)

The simplest approach for a first deploy is to use an IAM user with `AdministratorAccess`. For production, scope it down to least-privilege once you know what the stack needs.

---

## Step 1 — Clone the repository

```bash
git clone https://github.com/dmarshplas-cmyk/digital-matter-to-n2n-dl.git
cd digital-matter-to-n2n-dl
```

---

## Step 2 — Configure AWS credentials

If you haven't already:

```bash
aws configure
```

You'll be prompted for:
- **AWS Access Key ID** — from your IAM user
- **AWS Secret Access Key** — from your IAM user
- **Default region** — use `ap-southeast-2` (Sydney) for Australia, or your preferred region
- **Default output format** — `json`

Verify it's working:

```bash
aws sts get-caller-identity
```

You should see your account ID and IAM user/role ARN.

---

## Step 3 — Store secrets in SSM Parameter Store

The bridge never stores secrets in code or environment variables — everything is pulled from SSM at runtime. You need to create four parameters before deploying.

Replace the placeholder values with your real credentials:

```bash
# N2N-DL API key (encrypted)
aws ssm put-parameter \
  --name "/dm-n2n-bridge/n2n-api-key" \
  --type SecureString \
  --value "YOUR_N2N_API_KEY"

# N2N enterprise ID (plain string — not secret, but kept in SSM for consistency)
aws ssm put-parameter \
  --name "/dm-n2n-bridge/n2n-enterprise-id" \
  --type String \
  --value "YOUR_N2N_ENTERPRISE_ID"

# N2N device type token — must be lowercase, hyphens only, e.g. "oyster3-edge"
aws ssm put-parameter \
  --name "/dm-n2n-bridge/n2n-device-type" \
  --type String \
  --value "YOUR_DEVICE_TYPE"

# DM webhook shared secret (encrypted) — you'll set the same value in DM Device Manager
aws ssm put-parameter \
  --name "/dm-n2n-bridge/dm-webhook-secret" \
  --type SecureString \
  --value "YOUR_DM_WEBHOOK_SECRET"
```

> **Where do I find these values?**
> - **N2N API key** — N2N-DL platform dashboard under API settings
> - **N2N enterprise ID** — N2N-DL platform dashboard
> - **N2N device type** — a token you define in the N2N-DL platform for your DM device model
> - **DM webhook secret** — make up a strong random string; you'll paste it into DM Device Manager in Step 6

To verify the parameters were created:

```bash
aws ssm get-parameters-by-path --path "/dm-n2n-bridge" --with-decryption
```

---

## Step 4 — Configure the SAM deployment

Copy the example config and edit it:

```bash
cp samconfig.toml.example samconfig.toml
```

Open `samconfig.toml` and update these values:

```toml
[default.deploy.parameters]
stack_name     = "dm-n2n-bridge-prod"   # or -dev, -staging
region         = "ap-southeast-2"       # must match where you created SSM params
confirm_changeset = true
capabilities   = "CAPABILITY_IAM"
resolve_s3     = true

parameter_overrides = [
  "Environment=prod",                   # dev | staging | prod
  "AlertEmail=ops@your-domain.com",     # where delivery failure alerts go

  # Leave these as-is unless you used different SSM paths in Step 3
  "N2NApiKeyParam=/dm-n2n-bridge/n2n-api-key",
  "N2NEnterpriseIdParam=/dm-n2n-bridge/n2n-enterprise-id",
  "N2NDeviceTypeParam=/dm-n2n-bridge/n2n-device-type",
  "DmWebhookSecretParam=/dm-n2n-bridge/dm-webhook-secret",

  "MaxRetryAttempts=5",
]
```

> **`samconfig.toml` is gitignored** — it will never be accidentally committed.

---

## Step 5 — Build and deploy

```bash
# Build the Lambda packages
sam build

# Deploy to AWS (first time — walks you through any remaining prompts)
sam deploy --guided
```

On first run `--guided` will ask a few questions — your answers from `samconfig.toml` will pre-fill most of them. After the first deploy, subsequent updates are just:

```bash
sam build && sam deploy
```

The deployment takes 2–3 minutes. When it completes, CloudFormation prints the stack Outputs:

```
Key                 Value
-------------       -------------------------------------------------------
WebhookUrl          https://abc123.execute-api.ap-southeast-2.amazonaws.com/prod/webhook
RawEventsTableName  dm-n2n-raw-events-prod
IngestQueueUrl      https://sqs.ap-southeast-2.amazonaws.com/...
DeliverDLQUrl       https://sqs.ap-southeast-2.amazonaws.com/...
```

**Copy the `WebhookUrl`** — you'll need it in the next step.

---

## Step 6 — Configure Digital Matter Device Manager

1. Log into [DM Device Manager](https://devicemanager.digitalmatter.com/)
2. Navigate to **Server** → **Webhooks** (or your specific server's webhook settings)
3. Set the **Webhook URL** to the `WebhookUrl` from Step 5
4. Set the **Authentication** to one of:
   - **Custom header:** header name `X-DM-Secret`, value = your DM webhook secret from Step 3
   - **Basic Auth:** username = anything, password = your DM webhook secret from Step 3
5. Save and send a test transmission from a device

---

## Step 7 — Confirm it's working

**Check the webhook received a transmission:**

```bash
aws logs tail /aws/lambda/dm-n2n-ingest-prod --follow
```

You should see JSON log lines like:
```json
{"level": "INFO", "msg": "Valid DM webhook received", "device_sn": "1234567", "timestamp": "2026-04-01T..."}
{"level": "INFO", "msg": "Message enqueued", "message_id": "...", "device_sn": "1234567"}
```

**Check the transform and delivery:**

```bash
aws logs tail /aws/lambda/dm-n2n-transform-prod --follow
aws logs tail /aws/lambda/dm-n2n-deliver-prod --follow
```

A successful delivery looks like:
```json
{"level": "INFO", "msg": "Successfully delivered to N2N-DL", "device_sn": "1234567", "status": 200, "attempt": 1}
```

**Check the DLQ is empty** (nothing stuck):

```bash
aws sqs get-queue-attributes \
  --queue-url $(aws cloudformation describe-stacks \
    --stack-name dm-n2n-bridge-prod \
    --query "Stacks[0].Outputs[?OutputKey=='DeliverDLQUrl'].OutputValue" \
    --output text) \
  --attribute-names ApproximateNumberOfMessages
```

Should return `"ApproximateNumberOfMessages": "0"`.

---

## Updating the stack

After making code changes:

```bash
sam build && sam deploy
```

SAM only replaces what changed — CloudFormation handles the diff.

---

## Tearing down

To remove the entire stack from AWS:

```bash
aws cloudformation delete-stack --stack-name dm-n2n-bridge-prod
```

> **Note:** The DynamoDB table and SQS queues will be deleted with the stack. If you want to keep the raw events table, add a `DeletionPolicy: Retain` to `RawEventsTable` in `template.yaml` before deleting.

---

## Deploying multiple environments

The stack is fully parameterised — run separate deploys with different `samconfig.toml` files:

```bash
# Dev
sam build && sam deploy --config-env dev

# Prod  
sam build && sam deploy --config-env prod
```

With a `samconfig.toml` structured like:

```toml
[dev.deploy.parameters]
stack_name = "dm-n2n-bridge-dev"
...

[prod.deploy.parameters]
stack_name = "dm-n2n-bridge-prod"
...
```

Each environment gets its own independent set of SQS queues, DynamoDB table, Lambda functions, and API Gateway endpoint.
