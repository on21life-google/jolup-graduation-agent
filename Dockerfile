FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 jolup

# The lock is generated and validated by the project maintainer before sharing.
COPY requirements.txt requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY --chown=jolup:jolup app.py ./app.py
COPY --chown=jolup:jolup .streamlit/config.toml ./.streamlit/config.toml
COPY --chown=jolup:jolup jolup ./jolup
COPY --chown=jolup:jolup data ./data
COPY --chown=jolup:jolup scripts ./scripts
COPY --chown=jolup:jolup docs ./docs
RUN mkdir -p /app/data/index /app/data/models \
    && chown -R jolup:jolup /app/data/index /app/data/models

USER jolup
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.maxUploadSize=2", "--browser.gatherUsageStats=false"]
