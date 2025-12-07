FROM python:3.12-slim-bookworm

# 1. Set environment variables
# PYTHONDONTWRITEBYTECODE: Prevents Python from writing pyc files to disc
# PYTHONUNBUFFERED: Ensures logs are flushed immediately to the stream
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 2. Install system dependencies required for building Python packages
# - gcc & build-essential: Required to compile psycopg2 and other C-extensions
# - libpq-dev: Required header files for PostgreSQL (psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 3. Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy application code
COPY . /app

# 5. Create a non-root user for security
# Running as root is a security risk. We create a user 'appuser' and switch to it.
RUN useradd -m -u 1000 appuser
USER appuser

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]