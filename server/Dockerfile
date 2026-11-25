FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
ENV DATABASE_URL=sqlite:////data/cameradvr.db \
    LICENSE_PRIVATE_KEY_FILE=/keys/license_private.pem
VOLUME ["/data", "/keys"]
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
