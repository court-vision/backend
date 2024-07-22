FROM python:3.12

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ./app /app

EXPOSE 80

ENV JWT_SECRET_KEY=REDACTED

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]

# Build command: docker build -t fbball-server .
# Run command: docker run -p 8080:8000 fbball-server