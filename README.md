# DM → N2N-DL Bridge

AWS serverless bridge that receives [Digital Matter](https://www.digitalmatter.com/) device webhooks and forwards them to the [N2N-DL](https://www.nnnco.io/) platform ingestion API.

---

## Architecture

```
Digital Matter          AWS (SAM stack)                            N2N-DL
Device Manager  ──POST──▶  API Gateway
                             │
                         IngestFunction          DynamoDB
                         (auth + validate)  ──▶  RawEventsTable (30-day TTL)
                             │
                         SQS: IngestQueue (FIFO)
                             │
                         TransformFunction
                         (DM → N2N-DL format)
                             │
                         SQS: DeliverQueue (FIFO)
                             │
                         DeliverFunction  ──POST──▶  nnnco.io ingest API
                             │ (on failure)
                         SQS: DeliverDLQ
                             │
                         RetryFunction   ──POST──▶  nnnco.io ingest API
                         (exponential backoff,
                          SNS alert on exhaustion)
```

### Lambda functions

| Function | Trigger | Role |
|---|---|---|
| `ingest` | API Gateway POST `/webhook` | Validates DM shared secret, saves raw event to DynamoDB, enqueues to SQS |
| `transform` | SQS IngestQueue | Converts DM payload to N2N-DL format; reads enterprise ID and device type from SSM |
| `deliver` | SQS DeliverQueue | POSTs to N2N-DL API; raises on failure so SQS retries |
| `retry` | SQS DeliverDLQ | Exponential-backoff retry (up to `MaxRetryAttempts`); publishes SNS alert on exhaustion |

### Shared library

`n2n_common.py` provides token sanitisation, payload building, auth header construction, and structured logging. It is bundled directly into each Lambda package (no Lambda Layer dependency at runtime) to keep cold starts simple.

> **Note:** The `layers/` directory contains two versions of the shared library (`common` and `common-fixed`) from an earlier Layer-based approach. They are retained for reference but are not used by the current SAM deployment — the functions embed their own copy.

---

## Deployment

For full step-by-step instructions — AWS credentials, SSM setup, SAM build/deploy, DM Device Manager configuration, and verification — see **[DEPLOYMENT.md](./DEPLOYMENT.md)**.

---

## Prerequisites

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python 3.11+
- AWS credentials with permissions to deploy SAM stacks (CloudFormation, Lambda, SQS, DynamoDB, SNS, API Gateway, SSM, IAM, S3)

---

## SSM Parameter Store setup

Before deploying, create the following parameters in AWS SSM Parameter Store in your target region:

| Parameter path | Type | Description |
|---|---|---|
| `/dm-n2n-bridge/n2n-api-key` | `SecureString` | N2N-DL API key |
| `/dm-n2n-bridge/n2n-enterprise-id` | `String` | Your N2N enterprise ID |
| `/dm-n2n-bridge/n2n-device-type` | `String` | N2N device type token (e.g. `digital-matter-oyster3`) |
| `/dm-n2n-bridge/dm-webhook-secret` | `SecureString` | Shared secret configured in DM Device Manager |

```bash
aws ssm put-parameter --name "/dm-n2n-bridge/n2n-api-key"        --type SecureString --value "YOUR_KEY"
aws ssm put-parameter --name "/dm-n2n-bridge/n2n-enterprise-id"  --type String       --value "YOUR_ENTERPRISE_ID"
aws ssm put-parameter --name "/dm-n2n-bridge/n2n-device-type"    --type String       --value "YOUR_DEVICE_TYPE"
aws ssm put-parameter --name "/dm-n2n-bridge/dm-webhook-secret"  --type SecureString --value "YOUR_SECRET"
```

Parameter paths can be overridden via SAM parameters if you use a different naming convention.

---

## Deploy

```bash
# 1. Copy and edit the SAM config
cp samconfig.toml.example samconfig.toml
# Edit samconfig.toml: set your region, alert email, and SSM paths

# 2. Build
sam build

# 3. Deploy (guided first time)
sam deploy --guided
# Subsequent deploys:
sam deploy
```

After deployment, the stack Outputs include the **WebhookUrl** — configure this as the webhook endpoint in DM Device Manager, along with the shared secret.

---

## Digital Matter webhook configuration

In DM Device Manager, set:

- **URL:** the `WebhookUrl` from the stack Outputs
- **Auth method:** `X-DM-Secret` header (preferred) or Basic Auth
- **Secret:** the value stored at your `DmWebhookSecretParam` SSM path

The Ingest Lambda accepts both auth methods for compatibility.

---

## Running tests

```bash
pip install pytest python-dateutil
cd tests
pytest test_transform.py -v
```

Tests cover token sanitisation, device ID generation, Basic Auth header construction, and N2N payload building.

---

## Repository structure

```
dm-n2n-bridge/
├── infra/
│   └── template.yaml          # SAM template (API GW, SQS, DynamoDB, SNS, Lambdas)
├── lambdas/
│   ├── ingest/                # Webhook receiver
│   │   ├── handler.py
│   │   ├── n2n_common.py
│   │   └── requirements.txt
│   ├── transform/             # DM → N2N-DL transformer
│   │   ├── handler.py
│   │   ├── n2n_common.py
│   │   └── requirements.txt
│   ├── deliver/               # N2N-DL HTTP delivery
│   │   ├── handler.py
│   │   ├── n2n_common.py
│   │   └── requirements.txt
│   └── retry/                 # DLQ retry + alerting
│       ├── handler.py
│       ├── n2n_common.py
│       └── requirements.txt
├── layers/
│   ├── common/                # Lambda Layer approach (reference only)
│   └── common-fixed/          # Lambda Layer approach (reference only)
├── tests/
│   └── test_transform.py
├── samconfig.toml.example     # Copy to samconfig.toml (gitignored)
└── .gitignore
```

---

## Environment variables (set by SAM template)

| Variable | Source | Description |
|---|---|---|
| `ENVIRONMENT` | SAM parameter | `dev` / `staging` / `prod` |
| `N2N_ENTERPRISE_ID_PARAM` | SAM parameter | SSM path for enterprise ID |
| `N2N_DEVICE_TYPE_PARAM` | SAM parameter | SSM path for device type |
| `N2N_API_KEY_PARAM` | SAM parameter | SSM path for API key |
| `INGEST_QUEUE_URL` | CloudFormation ref | SQS ingest queue URL |
| `DELIVER_QUEUE_URL` | CloudFormation ref | SQS delivery queue URL |
| `RAW_TABLE` | CloudFormation ref | DynamoDB raw events table |
| `DM_SECRET_PARAM` | SAM parameter | SSM path for DM webhook secret |
| `ALERT_TOPIC_ARN` | CloudFormation ref | SNS topic for delivery failure alerts |
| `MAX_RETRY_ATTEMPTS` | SAM parameter | Max retry attempts before alerting (default: 5) |
| `DEBUG` | Globals | Set to `"true"` to enable debug logging |

---

## Security notes

- All secrets (API keys, webhook shared secret) are stored in SSM Parameter Store and fetched at runtime — **never** stored in environment variables or code.
- Webhook auth uses `hmac.compare_digest` (constant-time) to prevent timing attacks.
- IAM policies follow least-privilege: each Lambda has only the SSM/SQS/DynamoDB/SNS permissions it needs.

---

## License

MIT
