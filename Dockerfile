# popn-server: the pop'n Taisen Puzzle-dama Online server (gate, battle relay, rankings).
#
# Which service runs is picked by the compose `command:` (python -u -m popn.<module>).
#
# Stdlib only: no requirements file, nothing to pip install. If a module ever gains a
# third-party import, add a requirements.txt here and install it before the COPY.
#
# All mutable state lives under /data (accounts.json = accounts/characters/rooms) and logs
# under /logs; compose mounts both from the state folder. The image itself is stateless.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POPN_DATA=/data \
    POPN_LOG_DIR=/logs \
    POPN_STATE_DB=/data/accounts.json

WORKDIR /app
COPY popn /app/popn

# Prove the package imports and the self-tests pass in the image's own Python, not just the
# CI runner's. Use a throwaway data/log dir so nothing is baked into the image.
RUN t=$(mktemp -d) \
    && POPN_LOG_DIR=$t/logs POPN_STATE_DB=$t/accounts.json \
       python -m unittest discover -s /app -p "test_*.py" -t /app \
    && rm -rf "$t" \
    && find /app -name __pycache__ -prune -exec rm -rf {} +

VOLUME ["/data", "/logs"]

# gate 5731 (the game/lobby TCP server); relay 5730 (battle relay for relay-patched
# installs); web 5732 (ps2pzdweb rankings; the stock client's port 80 is patched).
EXPOSE 5731 5730 5732

CMD ["python", "-u", "-m", "popn.gate"]
