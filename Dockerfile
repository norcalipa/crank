# Use an official Python runtime as a parent image
FROM python:3.13.0b2-alpine3.19

# create a non-root user to run the app as
RUN addgroup -S appgroup -g 1000
RUN adduser -S appuser -u 1000 -G appgroup

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
RUN python manage.py collectstatic --noinput

RUN chown -R appuser:appgroup /app

USER appuser

# Make port 8080 available to the world outside this container
EXPOSE 8080

# Run the application when the container launches
CMD ["python", "manage.py", "runserver", "0.0.0.0:8080"]