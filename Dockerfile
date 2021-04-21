FROM dolfinx/dev-env

ARG DOLFINY_BUILD_TYPE=Release

ARG UFL_GIT_COMMIT=d60cd09
ARG BASIX_GIT_COMMIT=5d4430f
ARG FFCX_GIT_COMMIT=7db53a7
ARG DOLFINX_GIT_COMMIT=755bdfe

ENV PYTHONDONTWRITEBYTECODE=1

RUN git clone --branch main https://github.com/FEniCS/basix.git \
        && cd basix \
        && git checkout $BASIX_GIT_COMMIT \
        && cmake -G Ninja -DCMAKE_BUILD_TYPE=$DOLFINY_BUILD_TYPE -B build -S . \
        && cmake --build build \
        && cmake --install build \
        && python3 -m pip install ./python \
    && \
    pip3 install git+https://github.com/FEniCS/ufl.git@$UFL_GIT_COMMIT \
    && \
    pip3 install git+https://github.com/FEniCS/ffcx.git@$FFCX_GIT_COMMIT

RUN git clone --branch main https://github.com/FEniCS/dolfinx.git \
        && cd dolfinx \
        && git checkout $DOLFINX_GIT_COMMIT \
        && export PETSC_ARCH=linux-gnu-real-32 \
        && cmake -G Ninja -DCMAKE_BUILD_TYPE=$DOLFINY_BUILD_TYPE -B build -S ./cpp/ \
        && cmake --build build \
        && cmake --install build \
        && python3 -m pip install ./python

RUN pip3 install matplotlib
