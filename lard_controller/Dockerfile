ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.22
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LARD_HEALTH_PORT=8099

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/lard \
    && /opt/lard/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/lard/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/
COPY ha_packages/ /app/ha_packages/
COPY rootfs/ /

RUN chmod a+x /etc/s6-overlay/s6-rc.d/lard-controller/run \
    && chmod a+x /etc/s6-overlay/s6-rc.d/lard-controller/finish \
    && chmod a+x /run.sh

# s6-overlay in the HA base image is PID 1 (/init). The controller is a
# foreground longrun child. Do not override ENTRYPOINT.
LABEL \
    io.hass.name="LARD Controller" \
    io.hass.description="Board-priority Braiins actuator supervised by HA Supervisor" \
    io.hass.type="addon" \
    io.hass.version="0.1.14" \
    io.hass.arch="amd64|aarch64|armv7"
