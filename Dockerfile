# Универсальный образ сайта ClarifAI (Render, Hugging Face Spaces, Railway, любой сервер с Docker)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# кэш ответов модели — во временной папке контейнера
ENV LLM_CACHE_DIR=/tmp/llm-cache PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn webapp:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 75"]
