# Use an official Python runtime as a parent image
FROM python:3.13.0-alpine3.20

# Set the working directory in the container to /app
WORKDIR /app

# Add current directory code to /app in container
ADD . /app

COPY .env-prod .env

# fixing busybox vulnerabilities identified by synk
RUN apk add --no-cache --upgrade busybox
RUN apk add --no-cache busybox-extras

RUN apk add --no-cache --virtual build-deps gcc musl-dev libffi-dev pkgconf mariadb-dev
RUN apk add --no-cache mariadb-connector-c-dev
RUN pip install --no-cache-dir -r requirements.txt
RUN apk del build-deps

# Install Node.js and npm
RUN apk update
RUN apk add nodejs npm

# Install npm dependencies
RUN npm install