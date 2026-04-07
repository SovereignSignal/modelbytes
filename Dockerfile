FROM python:3.11-slim

# Cache bust
ARG CACHE_BUST=20260407_modelbytes

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Create state directory (for Railway volume or local)
RUN mkdir -p /app/state

# Run the monitor
CMD ["python", "monitor.py", "--post"]