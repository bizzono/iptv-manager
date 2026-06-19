FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY iptv_manager.py .
# channels.csv and iptv.db are mounted as a volume at runtime

EXPOSE 8765

ENV SERVER_HOST=localhost

CMD ["python", "iptv_manager.py"]
