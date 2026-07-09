FROM python:3.11-slim

WORKDIR /workspace

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code files
COPY app/ ./app/

# Expose FastAPI port
EXPOSE 8000

# Run Uvicorn dev server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
