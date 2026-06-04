FROM python:3.11-slim AS builder

RUN sed -i 's/ main$/ main contrib non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends build-essential ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Cache mount speeds up repeated builds dramatically
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install -r requirements.txt


FROM python:3.11-slim

RUN sed -i 's/ main$/ main contrib non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app


# 1. Copy the library files (You already have this)
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# 2. ADD THIS LINE: Copy the executables (uvicorn, etc.)
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
