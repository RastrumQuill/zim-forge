FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# wget is used by "File mirror" jobs, which run from this same image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates templates
COPY static static

EXPOSE 5000

CMD ["python", "app.py"]
