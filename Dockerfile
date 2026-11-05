# Lexaior API. Document processing runs in the separate consumers repository (Kafka).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /srv
COPY pymicroservices/requirements.txt pymicroservices/requirements.txt
# CPU-only torch keeps the image small; retrieval models load lazily from EMBEDDING_CACHE_DIR.
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r pymicroservices/requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 lexaior && chown -R lexaior /srv
USER lexaior

EXPOSE 8000
# Apply migrations, then serve. Render sets $PORT.
CMD ["sh", "scripts/start-api.sh"]
