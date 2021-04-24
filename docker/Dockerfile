FROM python:3.9.4-slim-buster

RUN apt -qq update && \
    apt install -qq -y git && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p unfurl 
WORKDIR /unfurl 
COPY . .

RUN python setup.py sdist bdist_wheel

RUN pip install ./dist/unfurl-0.2.2.dev3-py2.py3-none-any.whl

RUN cd ../ && rm -rf unfurl