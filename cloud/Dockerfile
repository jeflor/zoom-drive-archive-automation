FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY backup_worklist.json .

# Streams Zoom -> Drive in chunks; no disk buffering, so memory stays flat
# regardless of recording size.
CMD ["python", "-u", "main.py"]
