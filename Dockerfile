FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source code
COPY . .

# Create persistent data directory
RUN mkdir -p /app/.data

# Run the bot
CMD ["python", "bot.py"]
