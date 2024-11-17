# Use an official Python runtime as a parent image
FROM python:3.13.0-alpine3.20

# Set the working directory in the container to /app
WORKDIR /app

# Add current directory code to /app in container
ADD . /app

# Fixing busybox vulnerabilities identified by Snyk
RUN apk add --no-cache --upgrade busybox
RUN apk add --no-cache busybox-extras

# Install build dependencies
RUN apk add --no-cache --virtual build-deps gcc musl-dev libffi-dev pkgconf mariadb-dev

# Install runtime dependencies
RUN apk add --no-cache mariadb-connector-c-dev

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Remove build dependencies
RUN apk del build-deps

# Install Node.js and npm
RUN apk update && apk add --no-cache nodejs npm

# Install npm dependencies
RUN npm install