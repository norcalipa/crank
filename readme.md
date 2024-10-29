<!-- Copyright (c) 2024 Isaac Adams -->
<!-- Licensed under the MIT License. See LICENSE file in the project root for full license information. -->
# Crank.fyi

[![codecov](https://codecov.io/gh/norcalipa/crank/graph/badge.svg?token=5CR414ORFK)](https://codecov.io/gh/norcalipa/crank)
[![Known Vulnerabilities](https://snyk.io/test/github/norcalipa/crank/badge.svg)](https://snyk.io/test/github/norcalipa/crank)
[![Build Image](https://github.com/norcalipa/crank/actions/workflows/build-image.yml/badge.svg)](https://github.com/norcalipa/crank/actions/workflows/build-image.yml)
[![Run Tests](https://github.com/norcalipa/crank/actions/workflows/run-tests.yml/badge.svg)](https://github.com/norcalipa/crank/actions/workflows/run-tests.yml)
[![Deploy to Kubernetes (prod)](https://github.com/norcalipa/crank/actions/workflows/deploy-home.yml/badge.svg)](https://github.com/norcalipa/crank/actions/workflows/deploy-home.yml)
[![Change Tracking Marker](https://github.com/norcalipa/crank/actions/workflows/new-relic-change-tracking.yml/badge.svg)](https://github.com/norcalipa/crank/actions/workflows/new-relic-change-tracking.yml)


## Overview

This project is a web application built with Python using the Django framework, React, and TypeScript. It includes a backend API, frontend UI, and various automated tests.

## Technologies Used

- **Python**: Backend API
- **Django**: Web framework
- **pip**: Package manager for Python
- **MySQL**: Database
- **JavaScript**: Frontend logic
- **React**: Frontend framework
- **TypeScript**: Type safety for JavaScript
- **Jest**: JavaScript testing framework
- **npm**: Package manager for JavaScript
- **Docker**: Containerization
- **GitHub Actions**: CI/CD
- **Kubernetes**: Container orchestration

## Setup

### Prerequisites

- Node.js (version 18)
- Python (version 3.8)
- npm
- pip

### Installation

1. **Clone the repository**:
    ```sh
    git clone https://github.com/your-repo/crank.git
    cd crank
    ```

2. **Install Python dependencies**:
    ```sh
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    pip install -r requirements-dev.txt
    ```

3. **Install Node.js dependencies**:
    ```sh
    npm install
    ```

## Running the Application

### Development

1. **Start the backend server**:
    ```sh
    python manage.py runserver
    ```

2. **Start the frontend development server**:
    ```sh
    npm start
    ```

### Production

1. **Build the frontend**:
    ```sh
    npx webpack
    ```

2. **Run the backend server**:
    ```sh
    python manage.py runserver
    ```

## Testing

### Running Tests

1. **Run Python tests**:
    ```sh
    pytest
    ```

2. **Run JavaScript tests**:
    ```sh
    npx jest
    ```

### Continuous Integration

This project uses GitHub Actions for CI/CD. The workflows are defined in the `.github/workflows` directory.

- **Run Tests**: Executes tests on every push.
- **Build Image**: Builds and pushes Docker images on pull request merges.

## Contributing

1. **Fork the repository**.
2. **Create a new branch** (`git checkout -b feature-branch`).
3. **Commit your changes** (`git commit -m 'Add new feature'`).
4. **Push to the branch** (`git push origin feature-branch`).
5. **Create a Pull Request**.

## License

This project is licensed under the MIT License.