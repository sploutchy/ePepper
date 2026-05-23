FROM python:3.12-slim

# Install fonts. fonts-dejavu (not -core) ships the italic variants we need
# for the recipe-page renderer: DejaVuSerif-BoldItalic for the hung folio /
# page indicator and DejaVuSerif-Italic for sub-headings. The -core package
# only ships upright weights and would force a less-editorial fallback.
RUN apt-get update && \
    apt-get install -y --no-install-recommends fonts-dejavu && \
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
