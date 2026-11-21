# Same base image the tests were labeled in (its Python and SQLite decide which sqlglot tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# selector.py reads the precomputed map (map.bin) and a snapshot of the pinned sqlglot
# sources (src.bin) to apply patches; nothing is downloaded.
COPY selector.py map.bin src.bin fixb.py fixb_build.py fixb_tpcds.json /app/
# Fix B: extract pinned sqlglot sources and fingerprint the TPC-DS pipeline with this image's
# Python at build time, so the runtime check only has to fingerprint the patched side.
RUN PYTHONDONTWRITEBYTECODE=1 python /app/fixb_build.py /app /app/pinned /app/pinned_tpcds.json \
    && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
