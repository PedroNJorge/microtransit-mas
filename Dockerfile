# Microtransit MAS: run a scenario (and watch it in the viewer) without installing anything but Docker.
#   docker build -t microtransit-mas .
#   docker run --rm -p 8765:8765 microtransit-mas                                  # default scenario
#   docker run --rm -p 8765:8765 microtransit-mas --scenario config/scenarios/07_migrating_demand.json
# then open porto_graph_map.html in your browser (it connects to ws://127.0.0.1:8765).
#   docker run --rm -it -p 8765:8765 --entrypoint python microtransit-mas viz_demo.py config/scenarios/06_hub_failure.json   # demo with map overlays
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src MAS_WS_HOST=0.0.0.0

WORKDIR /app

# dependencies first: this layer is rebuilt only when pyproject.toml or uv.lock change
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY config ./config
COPY scripts ./scripts
COPY tools ./tools
COPY porto_graph_map.html viz_demo.py ./

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8765
# `docker run IMAGE <args>` passes the args to the launcher. Experiments: see below in the README.
ENTRYPOINT ["python", "-m", "harness.launcher"]
