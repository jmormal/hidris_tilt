FROM nvidia/cuda:12.4.1-devel-ubuntu22.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# THROTTLE SETTINGS: Prevents CPU spikes that crash Hyprland
ENV MAX_JOBS=1
ENV MAKEFLAGS="-j1"

# Install System Dependencies
RUN apt-get update && apt-get install -y \
  python3-pip \
  python3-dev \
  python3-venv \
  git \
  libgdal-dev \
  libopenmpi-dev \
  libnetcdf-dev \
  libhdf5-dev \
  pkg-config \
  gcc \
  g++ \
  gfortran \
  make \
  meson \
  ninja-build \
  && rm -rf /var/lib/apt/lists/*

# Fix GDAL paths
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

WORKDIR /code

# Pre-install Python build tools (Added setuptools & wheel just in case)
RUN pip3 install --no-cache-dir --upgrade pip setuptools wheel

RUN pip3 install -r requirements.txt

# After the pip3 install numpy scipy ... block, add:
RUN pip3 install --no-cache-dir --no-binary mpi4py mpi4py
# THE FIX: Clone and install using --no-build-isolation
RUN git clone --depth 1 https://github.com/anuga-community/anuga_core.git anuga-src && \
  cd anuga-src && \
  MAX_JOBS=1 pip3 install --no-cache-dir --no-build-isolation -e .

COPY . .

ENTRYPOINT ["/bin/bash"]
