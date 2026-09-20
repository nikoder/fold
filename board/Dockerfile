FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 incus-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# The repo is bind-mounted at /app (see compose.yaml) so the container
# always runs the checked-out code; nothing is baked in.
WORKDIR /app/board
CMD ["python3", "server.py"]
