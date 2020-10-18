FROM ubuntu:focal

RUN apt-get update
RUN apt-get install -y git python3-pip && \
 rm -rf /var/lib/apt/lists/*
RUN pip3 install tox
