FROM python:3.12-slim
WORKDIR /app
COPY topology-exporter.py .
EXPOSE 9500
CMD ["python", "-u", "topology-exporter.py"]
