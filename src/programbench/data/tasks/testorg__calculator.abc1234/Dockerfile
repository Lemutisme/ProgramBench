FROM ubuntu:22.04
RUN apt-get update && apt-get install -y git python3 python3-pip && pip3 install pytest pytest-xdist && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
