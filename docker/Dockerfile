FROM python:3.9.4-slim-buster

RUN apt -qq update && \
    apt install -qq -y git && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p unfurl 
WORKDIR /unfurl 

COPY . .

RUN pip3 install git+file:$PWD#egg=unfurl[full]

RUN cd ../ && rm -rf unfurl
