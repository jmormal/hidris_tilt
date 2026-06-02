FROM python:3.11-slim

WORKDIR /app

# Install OS-level dependencies (OpenMPI)
RUN apt-get update && apt-get install -y \
  openmpi-bin \
  libopenmpi-dev \
  && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# COPY notebooks/ /app/notebooks/
COPY src/ /app/src/
CMD ["python", "src/worker.py"]
