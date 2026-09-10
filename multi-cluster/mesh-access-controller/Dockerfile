FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY controller.py /app/controller.py
USER 10001:10001
ENTRYPOINT ["python3", "/app/controller.py"]
