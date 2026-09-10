FROM python:3.11-slim

WORKDIR /app

# Deliberately no OpenMPI/anuga build step (unlike worker-cpu/worker-gpu) —
# this worker never touches ANUGA, so it stays small and starts fast, which
# matters since it should scale in/out quickly on its own queue.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
CMD ["python", "src/worker.py"]
