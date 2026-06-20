FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .

RUN pip install --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

RUN addgroup --system tuya && adduser --system --ingroup tuya tuya

WORKDIR /app
COPY --from=builder /install /usr/local
COPY poller.py .

USER tuya

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["python", "poller.py"]
