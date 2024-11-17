# Use an official Python runtime as a parent image
FROM ghcr.io/norcalipa/crank/crank-base:latest

# create a non-root user to run the app as
RUN addgroup -S appgroup -g 10000
RUN adduser -S appuser -u 10000 -G appgroup

# Set the working directory in the container to /app
WORKDIR /app

# Add current directory code to /app in container
ADD . /app

# Run Webpack to build the assets
RUN npx webpack

RUN python manage.py collectstatic --noinput

RUN chown -R appuser:appgroup /app

USER appuser

# Make port 8080 available to the world outside this container
EXPOSE 8080

# Run the application when the container launches
CMD ["python", "manage.py", "runserver", "0.0.0.0:8080"]