FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install ".[azure]"

COPY strata_platform ./strata_platform
COPY alembic.ini ./

EXPOSE 8000
# default command runs the API; the worker overrides CMD (see compose / container app)
CMD ["uvicorn", "strata_platform.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
