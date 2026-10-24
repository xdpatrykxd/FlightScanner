FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium
COPY app ./app
COPY static ./static
COPY config/airports.csv ./config/airports.csv
COPY README.md ./README.md
ENV DATABASE_PATH=/app/data/listings.sqlite3
EXPOSE 8080
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8080"]
