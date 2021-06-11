FROM python:3.9.5-slim

RUN apt-get -qq update && \
    apt-get install -qq -y git && \
    rm -rf /var/lib/apt/lists/*

COPY . /unfurl

RUN pip install git+file:/unfurl#egg=unfurl[full]
