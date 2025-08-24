FROM python:3.11


WORKDIR /app


RUN apt-get update && apt-get install -y --no-install-recommends \
gcc libpq-dev && rm -rf /var/lib/apt/lists/*


COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt


COPY backend/ /app/backend


ENV PYTHONUNBUFFERED=1


CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]