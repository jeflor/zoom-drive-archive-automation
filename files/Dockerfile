FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY zoom_backup_cloud.py .

CMD ["python", "zoom_backup_cloud.py"]
