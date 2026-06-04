# Stage 1: Build the frontend chat UI
FROM node:20-slim AS frontend-builder

WORKDIR /app

# Install git to clone
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Clone the dev branch of WrenAI repository
RUN git clone -b dev https://github.com/CodeFindc/WrenAI.git /app

# Install dependencies and build the frontend chat UI
WORKDIR /app/sdk/wren-langchain/examples/wren-chat-ui
RUN npm ci && npm run build && rm -rf node_modules

# Stage 2: Final python image
FROM python:3.14

# Set working directory
WORKDIR /app

# Install basic build tools, git, and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-cloned and built app from stage 1
COPY --from=frontend-builder /app /app

# Install core/wren package to get the "wren" CLI command tool
RUN pip install -e /app/core/wren[all] 

# Change working directory to the sdk/wren-langchain folder
WORKDIR /app/sdk/wren-langchain

# Install the wren-langchain package in editable mode with all optional dependencies
RUN sed -i 's/wren-engine/wrenai/g' pyproject.toml && pip install -e .[all]

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
