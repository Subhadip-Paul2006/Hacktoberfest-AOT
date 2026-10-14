# Base image for SŪTRA runtime
FROM python:3.11-slim AS base

# Prevent Python from writing pyc files and keep stdout unbuffered
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTBRIDGE_HOME=/data \
    # Required for some subprocess interactions in adapters
    DEBIAN_FRONTEND=noninteractive

# Create non-root user
RUN useradd -m -s /bin/bash agentbridge

# Install minimal OS dependencies required for adapters or git operations
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /app

# Switch to non-root user
RUN mkdir -p /data && chown -R agentbridge:agentbridge /app /data
USER agentbridge

# Copy Python requirements first to leverage Docker cache
COPY --chown=agentbridge:agentbridge pyproject.toml ./

# Install Python dependencies
RUN pip install --no-cache-dir --user -e .

# Copy the rest of the application
COPY --chown=agentbridge:agentbridge . .

# Expose control plane port
EXPOSE 8765 8766

# Define persistent volume for SQLite state and config
VOLUME ["/data"]

# Add path for local pip installs
ENV PATH="/home/sutra/.local/bin:${PATH}"

# Default entrypoint using the bootstrap script
ENTRYPOINT ["python", "scripts/bootstrap.py"]
CMD ["agentbridge", "control-plane", "--host", "0.0.0.0", "--port", "8766"]
