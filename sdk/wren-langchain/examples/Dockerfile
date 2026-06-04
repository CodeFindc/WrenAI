# Use official Python 3.11 slim image
FROM python:3.14

# Set working directory
WORKDIR /app

# Install basic build tools, git, and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Clone the dev branch of WrenAI repository
RUN git clone -b dev https://github.com/CodeFindc/WrenAI.git /app

# Install core/wren package to get the "wren" CLI command tool
RUN pip install -e /app/core/wren[all]

# Change working directory to the sdk/wren-langchain folder
WORKDIR /app/sdk/wren-langchain

# Install the wren-langchain package in editable mode with all optional dependencies
RUN pip install -e .[all]

# Change working directory to examples
WORKDIR /app/sdk/wren-langchain/examples

# Install additional requirements for the example
RUN pip install --no-cache-dir -r requirements.txt

# Ensure entrypoint.sh is executable and has correct line endings
RUN chmod +x entrypoint.sh 

# Expose the API server port
EXPOSE 8201

# Use entrypoint.sh to initialize Wren profile/MDL/memory index
ENTRYPOINT ["./entrypoint.sh"]

# CMD command to start the stateful FastAPI server via python3 directly
CMD ["python3", "langgraph_fastapi_multi.py"]
