FROM python:3.11-slim

WORKDIR /app

# Copy all files
COPY . .

# Create data directory
RUN mkdir -p data

# Expose port
EXPOSE 8080

# Set environment
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Run the app
CMD ["python3", "app.py"]
