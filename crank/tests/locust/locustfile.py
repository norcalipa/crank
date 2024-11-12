# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from locust import HttpUser, task

class PublicUser(HttpUser):
    @task
    def index(self):
        self.client.get("/") # get the home page
        self.client.get("/organization/22/") # get the organization page for Uber
        self.client.get("/organization/3/") # get the organization page for Google
        self.client.get("/organization/35/") # get the organization page for DoorDash
        self.client.get("/algo/4/") # get the home page with a different algorithm
