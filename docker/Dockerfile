FROM python:3.9.9-slim

RUN apt-get -qq update && \
    apt-get -qq install -y git curl unzip wget expect && \
    apt-get -qq clean -y && rm -rf /var/lib/apt/lists/*

ARG TERRAFORM_VERSION=1.1.4
ARG HELM_VERSION=3.7.1
ARG GCLOUD_VERSION=398.0.0
ARG KOMPOSE_VERSION=v1.28.0
ENV PATH="${PATH}:/google-cloud-sdk/bin" \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1

RUN echo "Installing Terraform" && \
    wget -qO /terraform.zip \
    "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" && \
    unzip /terraform.zip -d /usr/local/bin && \
    rm -f /terraform.zip && \
    \
    echo "Installing Helm" && \
    wget -qO /helm.tar.gz \
    "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" && \
    tar -zxf /helm.tar.gz && \
    mv /linux-amd64/helm /usr/local/bin/helm && \
    rm -rf /helm.tar.gz /linux-amd64 \
    \
    echo "Installing GCloud SDK" && \
    wget -qO /google-cloud-sdk.tar.gz \
    "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-sdk-${GCLOUD_VERSION}-linux-x86.tar.gz" && \
    tar -zxf /google-cloud-sdk.tar.gz && \
    rm -f /google-cloud-sdk.tar.gz && \
    \
    echo "Installing Kompose" && \
    wget -qO /usr/local/bin/kompose \
    "https://github.com/kubernetes/kompose/releases/download/${KOMPOSE_VERSION}/kompose-linux-amd64" && \
    chmod +x /usr/local/bin/kompose && \
    \
    echo "Installing Kubectl and GKE auth plugin" && \
    gcloud components install --quiet kubectl gke-gcloud-auth-plugin

COPY . /unfurl

RUN pip install --no-cache-dir --upgrade pip pipenv && \
    cd /unfurl/unfurl/templates/python3.9 && \
    pipenv install --system --deploy && \
    pip install --no-deps /unfurl && \
    unfurl version && rm -rf /unfurl
