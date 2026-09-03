FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Madrid \
    WOFFU_HOST=0.0.0.0 \
    WOFFU_PORT=5000

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin woffu \
    && mkdir -p /app/data \
    && chown -R woffu:woffu /app

USER woffu

EXPOSE 5000

VOLUME ["/app/data"]

CMD ["python", "app.py"]
