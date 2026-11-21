# Keep the exact base image digest used by the scorer (Python 3.13 slim, linux/amd64).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

WORKDIR /app

COPY selector.py patch_parser.py packer.py map.pkl.gz /app/

# Precompile bytecode inside the target Python 3.13 image and ensure world-readable
# permissions for user `nobody` (65534:65534) on a read-only root filesystem.
RUN python -m compileall -q /app && chmod -R a+rX /app

CMD ["python", "-S", "-E", "/app/selector.py"]
