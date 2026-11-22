Build tooling for `cmpmap.bin` (not used at runtime).

    cp -r <sqlglot@eb11aec> /tmp/s && cd /tmp/s
    CMPMAP_ROOT=/tmp/s CMPMAP_DIR=/out/seed0 PYTHONPATH=<this dir> PYTHONHASHSEED=0 \
        python -m pytest tests -n 4 -p no:cacheprovider     # must pass all 1620 tests
    (repeat with PYTHONHASHSEED=1 -> /out/seed1)
    python3 merge.py /out/seed0 /out/seed1 -o cmpmap.bin
