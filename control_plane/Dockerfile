FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY init_db.py .

EXPOSE 8001

CMD ["python", "main.py"]