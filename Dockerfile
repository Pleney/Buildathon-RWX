# Keep the exact base image digest used by the scorer (Python 3.13 slim, linux/amd64).
# Runtime selector uses Python stdlib only (zero third-party packages, zero network fetches
# during build; BLAST_VERIFIER_MODE=off by default so verifier.py is never imported).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

WORKDIR /app
COPY selector.py patch_parser.py packer.py expr_profiler.py mapping_profiler.py verifier.py map.pkl.gz profiles.pkl.gz /app/

# Precompile bytecode for /app inside the target Python 3.13 image
# and ensure world-readable permissions for user `nobody` (65534:65534) on read-only FS.
RUN python -m compileall -q /app \
    && chmod -R a+rX /app

CMD ["python", "-S", "-E", "/app/selector.py"]
