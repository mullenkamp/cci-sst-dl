FROM debian:13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates cdo python3 python3-pip python3-pendulum rclone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY cci_sst_dl/cci_sst_download.py /app/cci_sst_download.py

# Output directory for [remote] type='local' runs. chmod 777 so any uid (set
# at runtime via docker-compose's `user:`) can write here. The actual owner
# of the files on the host is controlled by the container's runtime uid/gid.
RUN mkdir -p /data/output && chmod 777 /data/output

ENV PYTHONUNBUFFERED=1

# --failures-file pins the post-run failures list to a known mounted path
# (docker-compose.yml mounts the host project dir at /work). Without this,
# the default `<parameters.toml dir>/failures.txt` would land at
# `/failures.txt` inside the container, which is NOT a mounted volume — so
# the file would vanish on container exit. Users can still override by
# passing --failures-file <other path> on the command line (argparse takes
# the last occurrence).
ENTRYPOINT ["python3", "/app/cci_sst_download.py", "/parameters.toml", "--failures-file", "/work/failures.txt"]
