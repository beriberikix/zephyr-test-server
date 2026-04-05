FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        qemu-system-arm \
        qemu-system-x86 \
        qemu-system-misc \
        qemu-kvm \
        libc6 \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/build
