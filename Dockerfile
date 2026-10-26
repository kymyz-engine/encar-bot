FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py encar.py names.py ./

# run as non-root; /data holds subscriptions and the name cache
RUN useradd --create-home bot && mkdir /data && chown bot /data
USER bot
VOLUME /data

CMD ["python", "bot.py"]
