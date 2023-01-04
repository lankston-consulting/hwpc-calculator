#########################################################
# The first image built is a sandbox build environment
# for the hwpccalc Python package.
ARG PY_VERSION=3.10.4
FROM python:${PY_VERSION} AS builder
ENV PYTHONBUFFERED 1

RUN pip install --upgrade build pip 

# Set app directory
ENV PKG_HOME /hwpccalc
WORKDIR $PKG_HOME

COPY src/ src/
COPY pyproject.toml .
COPY setup.py .

RUN python -m build

#########################################################
# Create a base image to be used by production containers,
# but start with a clean Python image. Get the built wheel
# from the builder sandbox.
ARG PY_VERSION=3.10.4
FROM python:${PY_VERSION} AS base
ENV PYTHONBUFFERED 1

RUN pip install --upgrade pip wheel

COPY ./requirements.txt .
RUN pip install -r requirements.txt

ENV PKG_HOME /hwpccalc
COPY --from=builder $PKG_HOME/dist/hwpccalc-*.whl .
RUN pip install --no-cache-dir hwpccalc-*.whl

ENV TINI_VERSION v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /tini
RUN chmod +x /tini

COPY .env .env

#########################################################
# The production worker image. This should be tagged as 
# hwpc-calc:worker* when pushed to ECR
FROM base AS worker
ENV PYTHONBUFFERED 1

EXPOSE 8786
EXPOSE 8787
ENTRYPOINT ["/tini", "-g", "--"]

#########################################################
# The production client (hwpc-calc) image. Almost identical to
# the worker, but this executes the hwpc-calc loop and 
# collects results from SaaI launched tasks
FROM base AS client
ENV PYTHONBUFFERED 1

# Set app directory
COPY ./src/hwpccalc/main.py main.py
EXPOSE 8786
EXPOSE 8787
EXPOSE 8080
# ENTRYPOINT ["python", "main.py"]
ENTRYPOINT ["/tini", "-g", "--", "python", "main.py"]
