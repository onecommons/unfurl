FROM python:3.13-slim

RUN apt-get -qq update && \
    apt-get -qq install -y --no-install-recommends git curl unzip wget expect build-essential && \
    apt-get -qq clean -y && rm -rf /var/lib/apt/lists/*

ARG TERRAFORM_VERSION=1.1.4
ARG HELM_VERSION=3.7.1
ARG GCLOUD_VERSION=499.0.0
ARG KOMPOSE_VERSION=v1.27.0
# `TARGETARCH` and `TARGETOS` are supplied automatically by Docker
# Buildx for multi-platform builds (e.g. `--platform linux/amd64,linux/arm64`).
# A plain `docker build` without buildx leaves them empty, so we
# default to amd64 below so single-arch builds still work locally.
ARG TARGETARCH=amd64
ARG TARGETOS=linux
ENV PATH="${PATH}:/google-cloud-sdk/bin:$HOME/.cargo/bin" \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1

# Map TARGETARCH to the per-tool naming each upstream uses.  GCloud
# publishes its arm64 archive as `linux-arm` (no `64` suffix); the
# others all use `linux-arm64` / `linux-amd64` directly.
RUN GCLOUD_ARCH="$(case "$TARGETARCH" in amd64) echo x86 ;; arm64) echo arm ;; *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 1 ;; esac)" && \
    \
    echo "Installing Terraform (${TARGETOS}/${TARGETARCH})" && \
    wget -qO /terraform.zip \
    "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_${TARGETOS}_${TARGETARCH}.zip" && \
    unzip /terraform.zip -d /usr/local/bin && \
    rm -f /terraform.zip && \
    \
    echo "Installing Helm (${TARGETOS}/${TARGETARCH})" && \
    wget -qO /helm.tar.gz \
    "https://get.helm.sh/helm-v${HELM_VERSION}-${TARGETOS}-${TARGETARCH}.tar.gz" && \
    tar -zxf /helm.tar.gz && \
    mv "/${TARGETOS}-${TARGETARCH}/helm" /usr/local/bin/helm && \
    rm -rf /helm.tar.gz "/${TARGETOS}-${TARGETARCH}" \
    \
    echo "Installing GCloud SDK (${TARGETOS}/${GCLOUD_ARCH})" && \
    wget -qO /google-cloud-sdk.tar.gz \
    "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-sdk-${GCLOUD_VERSION}-${TARGETOS}-${GCLOUD_ARCH}.tar.gz" && \
    tar -zxf /google-cloud-sdk.tar.gz && \
    rm -f /google-cloud-sdk.tar.gz && \
    \
    echo "Installing Kompose (${TARGETOS}/${TARGETARCH})" && \
    wget -qO /usr/local/bin/kompose \
    "https://github.com/kubernetes/kompose/releases/download/${KOMPOSE_VERSION}/kompose-${TARGETOS}-${TARGETARCH}" && \
    chmod +x /usr/local/bin/kompose && \
    \
    echo "Installing Kubectl and GKE auth plugin" && \
    gcloud components install --quiet kubectl gke-gcloud-auth-plugin && \
    \
    echo "Installing Rust" && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

COPY . /unfurl

RUN . "$HOME/.cargo/env" && \
    pip install --no-cache-dir --upgrade pip pipenv==2024.0.0 setuptools-rust && \
    cd /unfurl/unfurl/templates/python3.13 && \
    pipenv install --system --deploy && \
    pip install --no-deps /unfurl && \
    ansible-galaxy collection install ansible.utils community.docker kubernetes.core && \
    unfurl version && rm -rf /unfurl
