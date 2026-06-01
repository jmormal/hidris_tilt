FROM nvcr.io/nvidia/nvhpc:24.7-devel-cuda_multi-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV MAX_JOBS=1
ENV MAKEFLAGS="-j1"

RUN apt-get update && apt-get install -y --no-install-recommends \
  python3 python3-dev python3-pip python3-venv \
  git wget curl build-essential gfortran \
  libnetcdf-dev libhdf5-dev gdal-bin libgdal-dev \
  swig pkg-config ninja-build \
  openmpi-bin libopenmpi-dev \
  && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/python python /usr/bin/python3 1 && \
  update-alternatives --install /usr/bin/pip pip /usr/bin/pip3 1

RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
  pip install --no-cache-dir \
  "numpy>=2.0" scipy matplotlib netCDF4 \
  cython meshpy pytest pybind11 \
  meson meson-python ninja \
  mpi4py

RUN git clone --branch develop --depth 1 \
  https://github.com/anuga-community/anuga_core.git /tmp/anuga-src

WORKDIR /app

RUN cd /tmp/anuga-src && \
  CC=nvc CXX=nvc++ pip install --no-cache-dir --no-build-isolation -e . \
  -Csetup-args=-Dgpu_offload=true \
  -Csetup-args=-Dgpu_arch=cc89

RUN python -c "from anuga.shallow_water import sw_domain_gpu_ext; print('GPU ext OK')"

ENV OMP_NUM_THREADS=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
