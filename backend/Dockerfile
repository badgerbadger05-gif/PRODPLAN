FROM python:3.11-slim

WORKDIR /app

ARG INSTALL_DEV=false
ENV TZ=Europe/Moscow

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir -r requirements-dev.txt; fi

COPY . .

EXPOSE 8000

# --timeout-worker-healthcheck: the multiprocess supervisor kills a worker that
# misses its liveness ping (default 5s). A single slow/blocking request must not
# be able to take a worker down mid-transaction, as the auto-sync tick once did.
# Requires uvicorn >= 0.37 (see requirements.txt).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--timeout-worker-healthcheck", "120"]
