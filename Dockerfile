# Keep the exact base image digest used by the scorer (Python 3.13 slim, linux/amd64).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Install exact dependencies from PyPI and fetch sqlglot at the pinned commit
# (eb11aec4be8cb2122afe713a4816662c3f0fb2fa) using Python stdlib only.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock \
    && python -c "\
import tarfile, urllib.request, pathlib, shutil; \
url = 'https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz'; \
urllib.request.urlretrieve(url, '/tmp/sg.tar.gz'); \
tarfile.open('/tmp/sg.tar.gz').extractall('/tmp'); \
shutil.move('/tmp/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa', '/sqlglot'); \
pathlib.Path('/tmp/sg.tar.gz').unlink()" \
    && rm -f /tmp/requirements.lock

WORKDIR /app
COPY selector.py patch_parser.py packer.py verifier.py map.pkl.gz /app/

# Precompile bytecode for both /app and /sqlglot inside the target Python 3.13 image
# and ensure world-readable permissions for user `nobody` (65534:65534) on read-only FS.
RUN python -m compileall -q /app /sqlglot/sqlglot /sqlglot/tests \
    && chmod -R a+rX /app /sqlglot

ENV PYTHONPATH=/sqlglot
ENV SKIP_INTEGRATION=0

CMD ["python", "-E", "/app/selector.py"]
