FROM python:3.12-slim

# Install fonts
RUN apt-get update && \
    apt-get install -y --no-install-recommends fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create data dir
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "main.py"]
