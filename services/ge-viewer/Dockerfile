FROM python:3.12-slim

WORKDIR /app

COPY docker/ge-viewer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/ge-viewer/app.py .

EXPOSE 8080

CMD ["python", "app.py"]