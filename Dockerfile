FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

CMD ["uvicorn", "gateway_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
