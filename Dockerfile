FROM python:3.12-alpine

LABEL org.opencontainers.image.title="Sensmos LoRa Docker"
LABEL org.opencontainers.image.description="Passive Sensmos LoRa gateway listeners for Semtech GWMP/UDP and ChirpStack Gateway Bridge MQTT"
LABEL org.opencontainers.image.source="https://github.com/C-Man-The-Man/sensmos-lora"

RUN pip install --no-cache-dir paho-mqtt chirpstack-api

WORKDIR /app

COPY sensmos-gw-udp.py /app/sensmos-gw-udp.py
COPY sensmos-gw-mqtt.py /app/sensmos-gw-mqtt.py
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod 755 /app/entrypoint.sh /app/sensmos-gw-udp.py /app/sensmos-gw-mqtt.py

ENTRYPOINT ["/app/entrypoint.sh"]
