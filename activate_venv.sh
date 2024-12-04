#!/bin/bash

# Activate the virtual environment
source venv/bin/activate

# Ensure the virtual environment is activated correctly
echo "Using Python interpreter: $(which python)"



# Check if pip install was successful
if [ $? -ne 0 ]; then
    echo "Failed to install required packages."
    exit 1
fi

# Run the setup.py script within the activated virtual environment



if [ $? -eq 0 ]; then
    echo "Setup completed successfully."
else
    echo "Setup failed."
    exit 1
fi

# Keep the shell alive to prevent it from closing
echo "The environment is active. The virtual environment is ready."

# Install required packages
echo "Installing required packages..."
pip install -r requirements.txt

echo "Running setup.py script..."
python setup.py