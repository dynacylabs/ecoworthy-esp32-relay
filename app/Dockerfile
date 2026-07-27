FROM python:3.12-slim
WORKDIR /app
# git: needed by the `esphome` CLI to fetch the victron_ble external
# component (external_components: source: github://... in
# victron-mppt.yaml) when the Firmware tab runs a compile.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
