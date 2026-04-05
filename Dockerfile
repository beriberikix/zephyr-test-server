FROM debian:bookworm-slim

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        qemu-system-arm \
        qemu-system-x86 \
        qemu-system-misc \
        qemu-kvm \
        libc6 \
        libc6:i386 \
        libstdc++6 \
        libstdc++6:i386 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/build
