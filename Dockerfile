FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --gecos "" mcpuser

# schema.py was missing here until now: it is imported unconditionally by
# server.py, so the image built green and the container died on ImportError. The
# CI docker job proves the image *builds*, not that it starts.
COPY entrypoint.sh vault.py schema.py documents.py changes.py api.py server.py ./
RUN chmod +x entrypoint.sh

# The change journal lives here, outside the vault -- inside it, Syncthing would
# replicate it and git would track a file that changes on every poll.
ENV STATE_DIR=/state
RUN mkdir -p /state

EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
