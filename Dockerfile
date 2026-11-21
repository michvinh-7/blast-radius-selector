# Same base image the tests were labeled in (its Python and SQLite decide which sqlglot tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# selector.py reads the precomputed map (map.bin) and a snapshot of the pinned sqlglot
# sources (src.bin) to apply patches; nothing is downloaded and no tests are run at runtime.
COPY selector.py map.bin src.bin /app/
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
