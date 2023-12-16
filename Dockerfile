# Use an official Python runtime as a parent image
FROM python:3.9-slim-buster

# Set the working directory in the container to /app
WORKDIR /app
COPY .env-prod .env

# Add current directory code to /app in container
ADD . /app

RUN apt-get update -y
RUN apt-get install -y pkg-config
RUN apt-get install -y wget
RUN apt-get install -y lsb-release
RUN wget https://dev.mysql.com/get/mysql-apt-config_0.8.17-1_all.deb
RUN dpkg -i mysql-apt-config_0.8.17-1_all.deb
RUN apt-get update
RUN apt-get install -y mysql-client
# Upgrade pip
RUN pip install --upgrade pip

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 8080 available to the world outside this container
EXPOSE 8080


# Run the application when the container launches
CMD ["python", "manage.py", "runserver", "0.0.0.0:8080"]