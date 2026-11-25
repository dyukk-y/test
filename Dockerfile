FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLBACKEND=Agg
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dps_bot ./dps_bot
COPY docs ./docs
VOLUME ["/app/data"]
CMD ["python", "-m", "dps_bot"]
