#!/bin/bash
# Script to run Jest tests with a timeout to prevent hanging

# Set timeout in seconds
TIMEOUT=30

# Run the test command with timeout
timeout $TIMEOUT npx jest --coverage --collect-coverage-from=static/js/OrganizationDetailsPopup.tsx --watchAll=false static/js/OrganizationDetailsPopup.test.tsx || echo "Test command timed out after $TIMEOUT seconds"

# Display coverage report
npx jest --coverageReporters="text-summary" --coverage --collect-coverage-from=static/js/OrganizationDetailsPopup.tsx --watchAll=false static/js/OrganizationDetailsPopup.test.tsx 