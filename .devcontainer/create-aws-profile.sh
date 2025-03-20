#!/usr/bin/env sh
set -ex

mkdir -p ~/.aws

if [ "$GITHUB_ACTIONS" = "true" ]; then
  LOCALSTACK_ENDPOINT_URL="http://localhost:4566"
else
  LOCALSTACK_ENDPOINT_URL="http://localstack:4566"
fi

cat >> ~/.aws/config <<EOF
[profile development]
sso_session = org
sso_account_id = 789893201852
sso_role_name = LowRiskAccountAdminAccess
region = us-east-1

[sso-session org]
sso_start_url = https://d-9067c20053.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access

[profile localstack]
region=us-east-1
output=json
endpoint_url = $LOCALSTACK_ENDPOINT_URL


EOF
cat >> ~/.aws/credentials <<EOF
[localstack]
aws_access_key_id=test
aws_secret_access_key=test
EOF
