# Multi-stage build for SDR-MCP Server
FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    git \
    libusb-1.0-0-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Build RTL-SDR drivers
WORKDIR /tmp
RUN git clone https://github.com/osmocom/rtl-sdr.git && \
    cd rtl-sdr && \
    mkdir build && cd build && \
    cmake .. -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON && \
    make && make install && \
    ldconfig

# Build HackRF drivers (optional)
RUN git clone https://github.com/greatscottgadgets/hackrf.git && \
    cd hackrf/host && \
    mkdir build && cd build && \
    cmake .. && \
    make && make install && \
    ldconfig

# Final stage
FROM python:3.11-slim

# Copy libraries from builder
COPY --from=builder /usr/local/lib /usr/local/lib
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /usr/local/include /usr/local/include

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/* && \
    ldconfig

# Create non-root user
RUN useradd -m -s /bin/bash sdr

# Set up working directory
WORKDIR /app

# Copy application files
COPY --chown=sdr:sdr . .

# Install Python dependencies
RUN pip install --no-cache-dir -e .

# Switch to non-root user
USER sdr

# Create directories for recordings and config
RUN mkdir -p /app/recordings /app/config

# Expose MCP stdio
EXPOSE 8080

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV SDR_RECORDINGS_PATH=/app/recordings

# Add healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sdr_mcp; print('OK')" || exit 1

# Default command
CMD ["python", "-m", "sdr_mcp.server"]

# Alternative command for development
# CMD ["python", "-m", "sdr_mcp.server", "--debug"]