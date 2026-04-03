FROM python:3.12-slim

WORKDIR /app

COPY banking-sync/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY banking-sync/ .

EXPOSE 8080

CMD ["python", "app.py"]
