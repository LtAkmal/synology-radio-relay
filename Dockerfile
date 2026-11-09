FROM python:3.12-alpine
RUN apk add --no-cache ffmpeg
WORKDIR /app
COPY radio_relay.py /app/radio_relay.py
USER nobody
ENV PYTHONUNBUFFERED=1
CMD ["python3", "-I", "/app/radio_relay.py"]
