# Use an official Python runtime as a parent image
FROM python:3.9-slim-buster

# Set the working directory in the container to /app
WORKDIR /app
COPY .env-prod .env

# Add current directory code to /app in container
ADD . /app

# Upgrade pip
RUN pip install --upgrade pip

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 8080 available to the world outside this container
EXPOSE 8080


# Run the application when the container launches
CMD ["python", "manage.py", "runserver", "0.0.0.0:8080"]