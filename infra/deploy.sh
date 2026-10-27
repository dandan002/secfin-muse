#!/usr/bin/env bash
# Build both images, push them to ECR and roll every service onto them.
#
#   cd infra/terraform/market && terraform init -backend-config=backend.hcl && terraform workspace select -or-create demo
#   ../../deploy.sh -var-file=demo.tfvars
#
# Extra arguments go to `terraform apply` (use the -var-file=... form). Needs terraform, docker
# and the AWS CLI.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
mocks="$here/../mocks"
cd "$here/terraform/market"

tag="${TAG:-$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- "$mocks" || echo "-dirty-$(date +%s)")}"

# ECR first, so there is somewhere to push before the services exist.
terraform apply -input=false -target=aws_ecr_repository.mocks -target=aws_ecr_repository.desk_chat "$@"

# `console` only takes the -var/-var-file flags (not e.g. -auto-approve).
var_args=()
for a in "$@"; do [[ "$a" == -var=* || "$a" == -var-file=* ]] && var_args+=("$a"); done
tf_eval() { echo "$1" | terraform console "${var_args[@]}" | tr -d '"'; }
mocks_repo="$(tf_eval aws_ecr_repository.mocks.repository_url)"
chat_repo="$(tf_eval aws_ecr_repository.desk_chat.repository_url)"
ib_url="$(tf_eval 'local.public_urls["ib"]')"
region="${mocks_repo#*.dkr.ecr.}"; region="${region%%.*}"

aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "${mocks_repo%%/*}"

docker build --platform linux/amd64 -t "$mocks_repo:$tag" "$mocks"
docker push "$mocks_repo:$tag"

# The browser calls mock-ib directly, so its public URL is baked into the bundle.
docker build --platform linux/amd64 --build-arg "VITE_IB_URL=$ib_url" -t "$chat_repo:$tag" "$mocks/apps/desk-chat"
docker push "$chat_repo:$tag"

terraform apply -input=false -var "image_tag=$tag" "$@"
terraform output urls
