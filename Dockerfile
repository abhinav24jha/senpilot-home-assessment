FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md requirements.lock ./
COPY uarb_agent ./uarb_agent
RUN pip install --no-cache-dir -c requirements.lock . && useradd --create-home agent && mkdir -p /app/data && chown agent:agent /app/data
USER agent
CMD ["uarb-agent", "watch", "--send"]
