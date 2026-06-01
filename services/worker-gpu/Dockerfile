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

# Start Jupyter Lab
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root", "--NotebookApp.token=''", "--NotebookApp.password=''"]
