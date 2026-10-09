# MCP server image for AgentCore Runtime (linux/arm64), serving streamable
# HTTP on 0.0.0.0:8000/mcp.
FROM python:3.12-slim
# Debian security fixes released after the base image was built.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
RUN useradd --uid 10001 --no-create-home mcp
USER mcp
EXPOSE 8000
CMD ["python", "server.py", "--http"]
