FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY spending_bot.py .
CMD ["python", "spending_bot.py"]
